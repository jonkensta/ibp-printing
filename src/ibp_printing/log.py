"""Verbose, structured logging for printer troubleshooting.

Two files are written side by side in the log directory:

* ``printer.log``   - human-readable lines, ``key=value`` data appended.
* ``printer.jsonl`` - one JSON object per line, for grepping or loading later.

Every record carries the host name, process ID, thread name, and the current
print attempt ID (if any), so one failed label can be followed end to end.
"""

import contextlib
import contextvars
import json
import logging
import os
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

_ATTEMPT_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ibp_printing_attempt_id", default=None
)
_CONFIGURED_DIRS: set[Path] = set()
_CONFIGURE_LOCK = threading.Lock()


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the package namespace."""
    if name == LOGGER_NAME or name.startswith(LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log_event(logger: logging.Logger, level: int, message: str, **data: Any) -> None:
    """Log ``message`` with structured ``data`` attached."""
    logger.log(level, message, extra={"data": data}, stacklevel=2)


def current_attempt_id() -> Optional[str]:
    """Return the active print attempt ID, if one is set."""
    return _ATTEMPT_ID.get()


@contextlib.contextmanager
def attempt(label: str, **data: Any) -> Iterator[str]:
    """Tag every record logged inside the block with a fresh attempt ID."""
    attempt_id = uuid.uuid4().hex[:10]
    token = _ATTEMPT_ID.set(attempt_id)
    logger = get_logger("attempt")
    started = time.monotonic()
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


def describe_exception(exc: BaseException) -> dict[str, Any]:
    """Extract the useful fields from an exception, including pywin32 errors."""
    details: dict[str, Any] = {"type": type(exc).__name__, "repr": repr(exc)}
    # pywintypes.error and com_error expose winerror/funcname/strerror.
    for attr in ("winerror", "funcname", "strerror", "hresult", "excepinfo"):
        value = getattr(exc, attr, None)
        if value is not None:
            details[attr] = value if isinstance(value, (int, str)) else repr(value)
    return details


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


def configure_logging(
    log_dir: Optional[Path] = None, *, console: bool = True, level: int = logging.DEBUG
) -> Path:
    """Attach the text and JSON file handlers to the package logger.

    Safe to call more than once; handlers are only added the first time for a
    given directory. Records still propagate to the root logger, so a host
    application's own log file also receives them.

    Returns:
        The directory logs are written to.
    """
    log_dir = Path(log_dir) if log_dir else default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    package_logger = logging.getLogger(LOGGER_NAME)
    package_logger.setLevel(level)

    with _CONFIGURE_LOCK:
        if log_dir in _CONFIGURED_DIRS:
            return log_dir
        _CONFIGURED_DIRS.add(log_dir)

        for filename, formatter in (
            ("printer.log", HumanFormatter()),
            ("printer.jsonl", JsonFormatter()),
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
        if console and sys.stderr is not None:
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
