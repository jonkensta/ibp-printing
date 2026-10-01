"""Shared state and steps of the label watcher: read, submit, file, remember."""

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Union

from ibp_printing import api
from ibp_printing.backends import PrintError
from ibp_printing.log import describe_exception, get_logger, log_event
from ibp_printing.models import Discovery, PrintResult
from ibp_printing.paths import (
    CHECK_PRINTER_DIR,
    PRINTED_DIR,
    TO_PRINT_DIR,
)
from ibp_printing.watcher import messages
from ibp_printing.watcher.config import WatcherConfig
from ibp_printing.watcher.detect import (
    LoadedLabel,
    ShapeDecision,
    TransientReadError,
    UnsupportedFormat,
    classify_shape,
    load_label,
    read_file_bytes,
    sha256_bytes,
    wait_until_stable,
)
from ibp_printing.watcher.notify import Notifier
from ibp_printing.watcher.state import FolderState, StateFile, iso

logger = get_logger(__name__)

MOVE_RETRIES = 5
MOVE_RETRY_DELAY_S = 0.5
READ_ATTEMPTS = 3
READ_RETRY_DELAY_S = 0.5


class _RetryRequest:  # pylint: disable=too-few-public-methods
    """Queue item asking the worker to retry the to-print folder."""

    def __repr__(self) -> str:
        return "<retry to-print>"


RETRY = _RetryRequest()
QueueItem = Union[Path, _RetryRequest, None]


@dataclass
class FileOutcome:
    """What happened to one file. ``status`` is a short machine-readable word."""

    status: str
    path: Path
    detail: str = ""
    moved_to: Optional[Path] = None
    sha256: Optional[str] = None

    def to_log(self) -> dict[str, Any]:
        """Flatten for structured logging."""
        return {
            "status": self.status,
            "file": str(self.path),
            "detail": self.detail,
            "moved_to": str(self.moved_to) if self.moved_to else None,
            "sha256": self.sha256,
        }


@dataclass
class Submission:
    """The result of handing one image to the printer API."""

    kind: str  # "printed", "to_print" (definitely not printed) or "check_printer"
    result: Optional[PrintResult]
    error: str = ""


def file_signature(path: Path) -> Optional[tuple[int, int]]:
    """``(size, mtime_ns)``, or None if the file cannot be stat'ed."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def arrival_time(path: Path) -> Optional[float]:
    """When the file appeared or last changed, as wall-clock seconds.

    The newest of mtime and ctime/birth time: some browsers set mtime from the
    server's Last-Modified header, but creation (Windows) or inode change
    (Linux, includes renames) time is always the moment it landed here.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    times = [stat.st_mtime, stat.st_ctime]
    birth = getattr(stat, "st_birthtime", None)
    if birth is not None:
        times.append(birth)
    return max(times)


class WatcherCore:  # pylint: disable=too-many-instance-attributes
    """State and shared steps of the label watcher (see ``LabelWatcher``).

    Everything here is used by both the Downloads path and the to-print queue:
    bookkeeping, the state file, notifications, reading a file, submitting it
    to a printer and moving it into a result folder.
    """

    def __init__(
        self,
        config: WatcherConfig,
        watch_dir: Path,
        *,
        notifier: Optional[Notifier] = None,
        stop_event: Optional[threading.Event] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        state: Optional[StateFile] = None,
    ) -> None:
        self.config = config
        self.watch_dir = Path(watch_dir)
        self.notifier = notifier or Notifier(enabled=config.notify_on_failure)
        self.stop_event = stop_event or threading.Event()
        self._clock = clock
        self._sleep = sleep
        self._state_file = state

        self._queue: "queue.Queue[QueueItem]" = queue.Queue()
        # Downloads paths queued or being processed, and the one in progress.
        self._pending: set[Path] = set()
        self._current: Optional[Path] = None
        self._dirty: set[Path] = set()
        # Wall-clock time each not-yet-finished Downloads path was first seen;
        # the minimum holds back the saved high-water mark.
        self._first_seen: dict[Path, float] = {}
        self._deferred: dict[Path, int] = {}
        self._retry_queued = False
        self._retry_reason = ""
        self._accepting = True
        self._pending_lock = threading.Lock()
        # Serializes printer access between the worker and the heartbeat.
        self.printer_lock = threading.Lock()

        self._submitted_at: dict[str, float] = {}
        self._stuck_hashes: set[str] = set()
        self._startup_files: dict[Path, Optional[tuple[int, int]]] = {}
        self._decided: dict[Path, tuple[int, int]] = {}
        self._notified: set[tuple[str, str]] = set()
        # sha256 -> {"file", "since"}: submitted, outcome not yet recorded.
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._prior_in_flight: dict[str, dict[str, Any]] = {}
        self._printers_available: Optional[bool] = None
        self._stats: dict[str, int] = {}
        self._stats_lock = threading.Lock()

        self._worker: Optional[threading.Thread] = None
        self._retry_thread: Optional[threading.Thread] = None
        self._observer: Any = None
        self._observer_ok_since = time.time()

    @property
    def stats(self) -> dict[str, int]:
        """A copy of the per-status counters (safe to log from any thread)."""
        with self._stats_lock:
            return dict(self._stats)

    @property
    def to_print_dir(self) -> Path:
        """``<watch_dir>/to-print``."""
        return self.watch_dir / TO_PRINT_DIR

    def existing_files(self, folder: Optional[Path] = None) -> list[Path]:
        """Regular files directly inside ``folder`` (the watch folder), oldest first."""
        folder = folder or self.watch_dir
        files = []
        try:
            entries = list(folder.iterdir())
        except OSError as exc:
            log_event(
                logger,
                logging.ERROR,
                "could not list folder",
                folder=str(folder),
                error=describe_exception(exc),
            )
            return []
        for entry in entries:
            try:
                if entry.is_file():
                    files.append(entry)
            except OSError:
                continue
        return sorted(files, key=lambda p: (file_signature(p) or (0, 0))[1])

    def _count(self, status: str) -> None:
        with self._stats_lock:
            self._stats[status] = self._stats.get(status, 0) + 1

    def _forget(self, path: Path) -> None:
        with self._pending_lock:
            self._first_seen.pop(path, None)
            self._deferred.pop(path, None)

    def _save_state(self, *, clean: bool) -> None:
        if self._state_file is None:
            return
        now = time.time()
        with self._pending_lock:
            seen_until = min([now, *self._first_seen.values()])
            in_flight = {**self._prior_in_flight, **self._in_flight}
        self._state_file.save(
            self.watch_dir,
            FolderState(
                seen_until=seen_until,
                updated=now,
                clean_shutdown=clean,
                in_flight=in_flight,
            ),
        )

    def _notify_once(self, path: Path, kind: str, text: str) -> None:
        """Show a box for this file and kind of problem unless already shown."""
        key = (str(path), kind)
        if key in self._notified:
            log_event(
                logger,
                logging.DEBUG,
                "already notified about this file",
                file=str(path),
                kind=kind,
            )
            return
        self._notified.add(key)
        if not self.config.notify_on_failure:
            log_event(
                logger,
                logging.INFO,
                "failure notification disabled",
                kind=kind,
                text=text,
            )
            return
        log_event(
            logger, logging.INFO, "notifying volunteer", kind=kind, file=str(path)
        )
        self.notifier.notify(text)

    def _recent_submission(self, digest: str) -> Optional[float]:
        """Seconds since identical content was submitted, if within the window."""
        now = self._clock()
        self._submitted_at = {
            key: when
            for key, when in self._submitted_at.items()
            if now - when < self.config.dedupe_seconds
        }
        when = self._submitted_at.get(digest)
        return None if when is None else now - when

    def _read_stable(self, path: Path) -> Union[FileOutcome, tuple[bytes, str, int]]:
        """Wait for the file to settle, then read it; or say why not."""
        stability = wait_until_stable(
            path,
            stable_s=self.config.stable_seconds,
            timeout_s=self.config.stable_timeout_s,
            interval_s=max(0.02, min(0.25, self.config.stable_seconds / 4)),
            clock=self._clock,
            sleep=self._sleep,
            cancel=self.stop_event.is_set,
        )
        log_event(
            logger,
            logging.INFO if stability.ok else logging.WARNING,
            "stability check",
            file=path.name,
            ok=stability.ok,
            size_bytes=stability.size,
            waited_s=round(stability.waited_s, 2),
            checks=stability.checks,
            reason=stability.reason,
        )
        if not stability.ok:
            if stability.reason == "cancelled":
                return FileOutcome("shutdown", path, "shutting down")
            if stability.reason == "file disappeared":
                return FileOutcome("missing", path, stability.reason)
            return FileOutcome("unstable", path, stability.reason)
        try:
            data = read_file_bytes(
                path,
                attempts=READ_ATTEMPTS,
                delay_s=READ_RETRY_DELAY_S,
                sleep=self._sleep,
            )
        except FileNotFoundError:
            return FileOutcome("missing", path, "disappeared before it could be read")
        except TransientReadError as exc:
            return FileOutcome("unreadable", path, str(exc))
        return data, sha256_bytes(data), len(data)

    def _dry_run(
        self, path: Path, digest: str, image: Any, job_name: str
    ) -> FileOutcome:
        log_event(
            logger,
            logging.WARNING,
            "DRY RUN: would print (nothing printed, nothing moved)",
            file=str(path),
            job_name=job_name,
            image_size=list(image.size),
            track_timeout_s=self.config.track_timeout_s,
        )
        self._submitted_at[digest] = self._clock()
        return FileOutcome("dry_run", path, "would print", sha256=digest)

    def _file_interrupted(self, path: Path, digest: str) -> FileOutcome:
        """The previous run died while printing this content: never auto-reprint."""
        info = self._prior_in_flight.pop(digest)
        since = iso(info.get("since")) or "?"
        log_event(
            logger,
            logging.WARNING,
            "this label was being printed when the watcher last stopped; "
            "not printing it again",
            file=str(path),
            previous_file=info.get("file"),
            since=since,
        )
        moved_to = self.move_to(path, CHECK_PRINTER_DIR)
        self._notify_once(
            path, "interrupted", messages.interrupted(path, moved_to, since)
        )
        return FileOutcome(
            "check_printer",
            path,
            f"watcher stopped while this label was printing (at {since})",
            moved_to=moved_to,
            sha256=digest,
        )

    def _submit(self, path: Path, digest: str, image: Any, job_name: str) -> Submission:
        """Hand the image to the printer API and classify what happened.

        The content hash is reserved (in memory and in the state file) before
        submission, so neither a second copy nor a restart can print it again
        while its fate is unknown. Only a definite failure releases it.
        """
        self._submitted_at[digest] = self._clock()
        with self._pending_lock:
            self._in_flight[digest] = {"file": str(path), "since": time.time()}
        self._save_state(clean=False)

        result: Optional[PrintResult] = None
        with self.printer_lock:
            try:
                result = api.print_to_first_available(
                    image,
                    job_name=job_name,
                    track_timeout_s=self.config.track_timeout_s,
                )
            except PrintError as exc:
                log_event(
                    logger,
                    logging.ERROR,
                    "print failed: the label definitely did not reach a printer",
                    file=path.name,
                    error=describe_exception(exc),
                )
                self._submitted_at.pop(digest, None)
                return Submission("to_print", None, str(exc))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception(
                    "unexpected error while printing %s; treating as uncertain",
                    path.name,
                )
                return Submission("check_printer", None, f"unexpected error: {exc!r}")
        if result.outcome.ok:
            return Submission("printed", result)
        error = f"printer {result.printer_name} reported {result.outcome.value}" + (
            f" ({'; '.join(result.history)})" if result.history else ""
        )
        log_event(
            logger,
            logging.ERROR,
            "label reached the printer queue but did not finish cleanly",
            file=path.name,
            printer=result.printer_name,
            outcome=result.outcome.value,
            job_id=result.job_id,
            history=result.history,
        )
        return Submission("check_printer", result, error)

    def _release_in_flight(self, digest: str) -> None:
        with self._pending_lock:
            self._in_flight.pop(digest, None)
        self._save_state(clean=False)

    def move_to(self, path: Path, subdir: str) -> Optional[Path]:
        """Move ``path`` into ``<watch_dir>/<subdir>/`` with a timestamp prefix.

        Retries while another program (browser, antivirus, image viewer) holds
        the file open. Returns the new path, or None if it could not be moved.
        """
        target_dir = self.watch_dir / subdir
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        last_exc: Optional[BaseException] = None
        for attempt_no in range(1, MOVE_RETRIES + 1):
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / f"{stamp}_{path.name}"
                counter = 1
                while dest.exists():
                    dest = target_dir / f"{stamp}_{counter}_{path.name}"
                    counter += 1
                os.rename(path, dest)
            except PermissionError as exc:
                last_exc = exc
                log_event(
                    logger,
                    logging.WARNING,
                    "move blocked (file in use?); retrying",
                    file=path.name,
                    attempt=attempt_no,
                    error=describe_exception(exc),
                )
                self._sleep(MOVE_RETRY_DELAY_S * attempt_no)
                continue
            except OSError as exc:
                last_exc = exc
                break
            log_event(
                logger,
                logging.INFO,
                f"moved to {subdir}/",
                file=path.name,
                dest=str(dest),
            )
            return dest
        log_event(
            logger,
            logging.ERROR,
            "could not move file; leaving it in place",
            file=str(path),
            subdir=subdir,
            error=describe_exception(last_exc) if last_exc else None,
        )
        return None

    def _shape(self, size: tuple[int, int]) -> ShapeDecision:
        return classify_shape(
            size,
            aspect_min=self.config.aspect_min,
            aspect_max=self.config.aspect_max,
            min_short_side_px=self.config.min_short_side_px,
        )

    def _file_common(
        self, path: Path, digest: str, submission: Submission
    ) -> Optional[FileOutcome]:
        """File a printed or check-printer submission; None for a definite failure.

        The returned outcome has not been counted or logged yet.
        """
        result = submission.result
        if submission.kind == "printed":
            assert result is not None
            moved_to = self.move_to(path, PRINTED_DIR)
            if moved_to is None:
                # Printed but still sitting where it was: never print it again.
                self._stuck_hashes.add(digest)
            self._release_in_flight(digest)
            return FileOutcome(
                "printed",
                path,
                f"printed on {result.printer_name} ({result.outcome.value})",
                moved_to=moved_to,
                sha256=digest,
            )
        if submission.kind != "check_printer":
            return None
        moved_to = self.move_to(path, CHECK_PRINTER_DIR)
        self._release_in_flight(digest)
        self._notify_once(
            moved_to or path,
            "check_printer",
            messages.check_printer(path, moved_to, submission.error, self.to_print_dir),
        )
        return FileOutcome(
            "check_printer",
            path,
            submission.error,
            moved_to=moved_to,
            sha256=digest,
        )

    def _discover(self, reason: str) -> Optional[Discovery]:
        """One discovery pass under the printer lock; None (logged) on error."""
        try:
            with self.printer_lock:
                return api.discover()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.ERROR,
                f"discovery failed ({reason})",
                error=describe_exception(exc),
            )
            return None

    def _decode(
        self, path: Path, digest: str, data: bytes
    ) -> Union[FileOutcome, tuple[LoadedLabel, ShapeDecision]]:
        """Decode the bytes and check the shape, or return an unsupported outcome."""
        try:
            loaded = load_label(path, pdf_dpi=self.config.pdf_dpi, data=data)
        except UnsupportedFormat as exc:
            return FileOutcome("unsupported", path, str(exc), sha256=digest)
        return loaded, self._shape(loaded.image.size)
