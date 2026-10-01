"""Verbose, structured logging for printer troubleshooting.

Two files are written side by side in the log directory, one pair per
application so separate processes never share (and fight over rotating) a file:

* ``printer-<app>.log``   - human-readable lines, ``key=value`` data appended.
* ``printer-<app>.jsonl`` - one JSON object per line, for grepping or loading later.

Every record carries the host name, process ID, thread name, and the current
print attempt ID (if any), so one failed label can be followed end to end.
"""

import contextlib
import contextvars
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator, Optional

LOGGER_NAME = "ibp_printing"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 20
HOSTNAME = socket.gethostname()
DEFAULT_APP = "ibp-printing"
_APP_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")

_ATTEMPT_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ibp_printing_attempt_id", default=None
)
_CONFIGURED_DIRS: set[tuple[Path, str]] = set()
_CONFIGURE_LOCK = threading.Lock()


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the package namespace."""
    if name == LOGGER_NAME or name.startswith(LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log_event(logger: logging.Logger, level: int, message: str, /, **data: Any) -> None:
    """Log ``message`` with structured ``data`` attached.

    The first three parameters are positional-only, so ``data`` may itself
    contain keys named ``logger``, ``level`` or ``message``.
    """
    logger.log(level, message, extra={"data": data}, stacklevel=2)


def current_attempt_id() -> Optional[str]:
    """Return the active print attempt ID, if one is set."""
    return _ATTEMPT_ID.get()


@contextlib.contextmanager
def attempt(label: str, **data: Any) -> Iterator[str]:
    """Tag every record logged inside the block with an attempt ID.

    A fresh ID is minted for the outermost block. A nested block (for example
    ``print_image`` called by the label watcher's per-file attempt) reuses the
    active ID, so one ID covers the whole story, including the spooler job name.
    """
    outer_id = _ATTEMPT_ID.get()
    attempt_id = outer_id or uuid.uuid4().hex[:10]
    token = _ATTEMPT_ID.set(attempt_id)
    logger = get_logger("attempt")
    started = time.monotonic()
    if outer_id is not None:
        data = {**data, "nested": True}
    log_event(logger, logging.INFO, f"BEGIN {label}", **data)
    try:
        yield attempt_id
    except BaseException as exc:
        log_event(
            logger,
            logging.ERROR,
            f"END {label} (raised {type(exc).__name__})",
            elapsed_s=round(time.monotonic() - started, 3),
        )
        raise
    else:
        log_event(
            logger,
            logging.INFO,
            f"END {label}",
            elapsed_s=round(time.monotonic() - started, 3),
        )
    finally:
        _ATTEMPT_ID.reset(token)


@contextlib.contextmanager
def timed_step(logger: logging.Logger, step: str, **data: Any) -> Iterator[None]:
    """Log how long a step took, and log loudly if it raised."""
    started = time.monotonic()
    log_event(logger, logging.DEBUG, f"step {step} ...", **data)
    try:
        yield
    except BaseException as exc:
        log_event(
            logger,
            logging.ERROR,
            f"step {step} FAILED",
            elapsed_s=round(time.monotonic() - started, 3),
            error=describe_exception(exc),
        )
        raise
    log_event(
        logger,
        logging.DEBUG,
        f"step {step} ok",
        elapsed_s=round(time.monotonic() - started, 3),
    )


_EXCEPTION_ATTRS = (
    # pywintypes.error and com_error.
    "winerror",
    "funcname",
    "strerror",
    "hresult",
    "excepinfo",
    # wmi.x_wmi (and its subclasses) wrap the underlying com_error.
    "info",
    "com_error",
)


def describe_exception(exc: BaseException) -> dict[str, Any]:
    """Extract the useful fields from an exception, including pywin32/WMI errors."""
    details: dict[str, Any] = {"type": type(exc).__name__, "repr": repr(exc)}
    try:
        text = str(exc)
    except Exception:  # pylint: disable=broad-exception-caught
        text = ""
    if text:
        details["str"] = text
    for attr in _EXCEPTION_ATTRS:
        try:
            value = getattr(exc, attr, None)
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if value is None or value == "":
            continue
        details[attr] = value if isinstance(value, (int, str)) else repr(value)
    return details


def exception_summary(exc: BaseException) -> str:
    """One line for an error list: ``Type: text (winerror=..., ...)``."""
    details = describe_exception(exc)
    head = f"{details['type']}: {details.get('str') or details['repr']}"
    extras = [
        f"{key}={details[key]}"
        for key in _EXCEPTION_ATTRS
        if key in details and str(details[key]) not in head
    ]
    return f"{head} ({', '.join(extras)})" if extras else head


def add_context(record: logging.LogRecord) -> bool:
    """Handler filter: attach host, attempt ID, and a data dict to every record."""
    record.host = HOSTNAME
    record.attempt_id = _ATTEMPT_ID.get() or "-"
    if not isinstance(getattr(record, "data", None), dict):
        record.data = {}
    return True


class HumanFormatter(logging.Formatter):
    """``time level [attempt] logger: message | key=value ...``"""

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s %(levelname)-7s [%(attempt_id)s] %(threadName)s "
            "%(name)s: %(message)s"
        )

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        data = getattr(record, "data", None)
        if data:
            pairs = " ".join(f"{key}={_compact(value)}" for key, value in data.items())
            text = f"{text} | {pairs}"
        return text


class JsonFormatter(logging.Formatter):
    """One JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "host": getattr(record, "host", HOSTNAME),
            "pid": record.process,
            "thread": record.threadName,
            "attempt_id": getattr(record, "attempt_id", "-"),
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=repr, ensure_ascii=False)


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value if value and " " not in value else json.dumps(value)
    return json.dumps(value, default=repr, ensure_ascii=False)


def default_log_dir() -> Path:
    """Per-machine log directory (``%LOCALAPPDATA%\\ibp-printing\\logs`` on Windows)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / "ibp-printing" / "logs"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ibp-printing" / "logs"


def log_file_names(app: str = DEFAULT_APP) -> tuple[str, str]:
    """The ``(text, json)`` log file names used for ``app``."""
    safe = _APP_NAME_PATTERN.sub("-", app).strip("-.") or DEFAULT_APP
    return f"printer-{safe}.log", f"printer-{safe}.jsonl"


def configure_logging(
    log_dir: Optional[Path] = None,
    *,
    app: str = DEFAULT_APP,
    console: bool = True,
    level: int = logging.DEBUG,
) -> Path:
    """Attach the text and JSON file handlers to the package logger.

    Each application writes its own pair of files, ``printer-<app>.log`` and
    ``printer-<app>.jsonl`` (shippy uses ``app="shippy"``, the label watcher
    ``app="watcher"``, ``ibp-print-diag`` ``app="diag"``). Separate processes
    must never share a rotating file: on Windows one process's open handle
    stops another from rotating it.

    Safe to call more than once; handlers are only added the first time for a
    given directory and app. Records still propagate to the root logger, so a
    host application's own log file also receives them.

    Returns:
        The directory logs are written to.
    """
    log_dir = Path(log_dir) if log_dir else default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    text_name, json_name = log_file_names(app)

    package_logger = logging.getLogger(LOGGER_NAME)
    package_logger.setLevel(level)

    with _CONFIGURE_LOCK:
        if (log_dir, text_name) in _CONFIGURED_DIRS:
            return log_dir
        _CONFIGURED_DIRS.add((log_dir, text_name))

        for filename, formatter in (
            (text_name, HumanFormatter()),
            (json_name, JsonFormatter()),
        ):
            handler = RotatingFileHandler(
                log_dir / filename,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            handler.addFilter(add_context)
            package_logger.addHandler(handler)

        # pythonw.exe has no stderr; a StreamHandler would fail on every record.
        has_console = any(
            isinstance(existing, logging.StreamHandler)
            and not hasattr(existing, "baseFilename")
            for existing in package_logger.handlers
        )
        if console and sys.stderr is not None and not has_console:
            stream = logging.StreamHandler()
            stream.setLevel(logging.INFO)
            stream.setFormatter(HumanFormatter())
            stream.addFilter(add_context)
            package_logger.addHandler(stream)

    log_event(
        package_logger,
        logging.INFO,
        "logging configured",
        log_dir=str(log_dir),
        app=app,
        files=[text_name, json_name],
        python=sys.version.split()[0],
        executable=sys.executable,
        platform=sys.platform,
        pid=os.getpid(),
    )
    return log_dir


def install_exception_hooks() -> None:
    """Route uncaught exceptions (main and worker threads) into the log."""
    logger = get_logger("uncaught")

    def _sys_hook(exc_type, exc, tb):
        logger.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        thread_name = args.thread.name if args.thread else "?"
        if args.exc_value is None:
            logger.critical(
                "uncaught %s in thread %s", args.exc_type.__name__, thread_name
            )
            return
        logger.critical(
            "uncaught exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
