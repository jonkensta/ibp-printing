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
from typing import Any, Callable, Iterator, Optional

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


class SafeRotatingFileHandler(RotatingFileHandler):
    """A size-rotating file handler whose rotation can fail without loss.

    Two processes of the same app (say, two shippy windows, or ``shippy
    diagnose-printer`` next to a running shippy) can still hold one log file
    open. On Windows the second process's open handle makes renaming the file
    fail with a sharing violation. The standard handler then drops the record
    being written, and because it shifts the backups *before* renaming the
    current file, every failed attempt pushes the history one slot further
    and deletes the oldest backup.

    This handler instead:

    * renames the current file out of the way first, so the usual failure (the
      file is open elsewhere) happens before any backup is touched;
    * undoes every rename it made if a later step fails, and only deletes the
      oldest backup once the whole rotation has succeeded;
    * on failure reopens the current file in append mode and keeps writing,
      notes the failure in the log itself, and does not try to rotate again
      for ``retry_interval_s`` (so a busy log is not renamed on every record).

    A custom ``namer``/``rotator`` is not supported; backups are plain renames.
    """

    retry_interval_s = 300.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._retry_after = 0.0
        self.clock = time.monotonic
        self.last_rotation_error: Optional[BaseException] = None

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if self.clock() < self._retry_after:
            return False
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        try:
            if self.backupCount > 0:
                self._rotate_files()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.last_rotation_error = exc
            self._retry_after = self.clock() + self.retry_interval_s
            self.stream = self._open()  # mode "a": keep appending
            self._note_rotation_failure(exc)
            return
        self.last_rotation_error = None
        self._retry_after = 0.0
        if not self.delay:
            self.stream = self._open()

    def _rotate_files(self) -> None:
        """Shift ``base`` -> ``.1`` -> ... -> ``.N``, all or nothing."""
        base = self.baseFilename
        backups = [f"{base}.{index}" for index in range(1, self.backupCount + 1)]
        stamp = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        holding = f"{base}.rotating-{stamp}"
        dropping = f"{backups[-1]}.dropping-{stamp}"
        done: list[tuple[str, str]] = []
        try:
            if os.path.exists(base):
                os.rename(base, holding)
                done.append((base, holding))
            if os.path.exists(backups[-1]):
                os.rename(backups[-1], dropping)
                done.append((backups[-1], dropping))
            for index in range(len(backups) - 1, 0, -1):
                source, target = backups[index - 1], backups[index]
                if os.path.exists(source):
                    os.rename(source, target)
                    done.append((source, target))
            if os.path.exists(holding):
                os.rename(holding, backups[0])
                done.append((holding, backups[0]))
        except OSError:
            for source, target in reversed(done):
                with contextlib.suppress(OSError):
                    os.rename(target, source)
            raise
        if os.path.exists(dropping):
            with contextlib.suppress(OSError):
                os.remove(dropping)

    def _note_rotation_failure(self, exc: BaseException) -> None:
        """Write a WARNING line about ``exc`` straight into this file."""
        record = logging.LogRecord(
            f"{LOGGER_NAME}.log",
            logging.WARNING,
            __file__,
            0,
            "log rotation failed (is another copy of this app running?); "
            "still appending to the current file, retrying in %d s",
            (int(self.retry_interval_s),),
            None,
        )
        record.data = {
            "file": self.baseFilename,
            "error": describe_exception(exc),
        }
        add_context(record)
        if self.stream is None:
            return
        with contextlib.suppress(Exception):
            self.stream.write(self.format(record) + self.terminator)
            self.flush()


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
    stops another from rotating it. Two copies of the *same* app can still
    share a file; ``SafeRotatingFileHandler`` then keeps appending and retries
    rotation later instead of losing records or backups.

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
            handler = SafeRotatingFileHandler(
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
    for hook in list(_CONFIGURE_HOOKS):
        try:
            hook(f"configure_logging(app={app})")
        except Exception:  # pylint: disable=broad-exception-caught
            package_logger.exception("configure_logging hook failed")
    return log_dir


# Called with a context string at the end of every configure_logging call,
# e.g. to record which printing mode (direct USB on/off) is active.
_CONFIGURE_HOOKS: list[Callable[[str], Any]] = []


def add_configure_hook(hook: Callable[[str], Any]) -> None:
    """Run ``hook(context)`` after each :func:`configure_logging` (once per hook)."""
    if hook not in _CONFIGURE_HOOKS:
        _CONFIGURE_HOOKS.append(hook)


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
