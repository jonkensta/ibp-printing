"""Shared state and steps of the label watcher: read, reserve, submit, file.

The safety rules every path follows:

* A label that **may** have reached a printer is never sent again
  automatically. Its content hash is reserved in the state file *before* it is
  handed to the printer API (status ``filing``) and stays reserved afterwards
  (``printed`` for ``duplicate_window_hours``, ``uncertain`` for ever).
* A label that **definitely** did not reach a printer is released and retried
  from ``to-print/``.
* If a printed file cannot be moved into its result folder, the watcher retries
  the *move* on later ticks (and after a restart), never the print.
* Availability first: if the state file cannot be written the watcher still
  prints, but logs CRITICAL and tells the volunteer once.
* A label's metadata sidecar (``<label>.png.json``) is never printed; it moves
  with its label, and the label journal (:mod:`ibp_printing.labels`) is told
  where the label went (best effort: the journal never blocks printing).
"""

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

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
from ibp_printing.watcher.sidecars import carry_sidecar
from ibp_printing.watcher.state import (
    FILING,
    INTERRUPTED,
    PRINTED,
    UNCERTAIN,
    FolderState,
    Reservation,
    StateFile,
    iso,
)

logger = get_logger(__name__)

MOVE_RETRIES = 5
MOVE_RETRY_DELAY_S = 0.5
READ_ATTEMPTS = 3
READ_RETRY_DELAY_S = 0.5
# A locked/unstable file is looked at again on this many later retry ticks.
MAX_DEFERRED_RETRIES = 3
# Upper bound on remembered reservations (printed ones go first).
MAX_RESERVATIONS = 5000
# A file whose name starts with this (any case) is a deliberate reprint.
REPRINT_PREFIX = "REPRINT"
# Prefix for copies of already-handled labels moved out of the way.
DUPLICATE_PREFIX = "duplicate_"


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
    # PrintError.reason for a "to_print" submission (e.g. "cover_open").
    reason: Optional[str] = None


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


def is_reprint(path: Path) -> bool:
    """True for a deliberate reprint: the name starts with ``REPRINT``."""
    return path.name.upper().startswith(REPRINT_PREFIX)


def same_path(first: Union[str, Path], second: Union[str, Path]) -> bool:
    """Path equality that ignores case on Windows."""
    if not first or not second:
        return False
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second)
    )


class WatcherCore:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """State and shared steps of the label watcher (see ``LabelWatcher``).

    Everything here is used by both the Downloads path and the to-print queue:
    bookkeeping, the state file and reservations, notifications, reading a
    file, submitting it to a printer and moving it into a result folder.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        config: WatcherConfig,
        watch_dir: Path,
        *,
        notifier: Optional[Notifier] = None,
        stop_event: Optional[threading.Event] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        state: Optional[StateFile] = None,
        to_print_dirs: Optional[Sequence[Path]] = None,
        checkpoint: bool = True,
    ) -> None:
        self.config = config
        self.watch_dir = Path(watch_dir)
        self.notifier = notifier or Notifier(enabled=config.notify_on_failure)
        self.stop_event = stop_event or threading.Event()
        self._clock = clock
        self._sleep = sleep
        # Wall clock for anything persisted (tests may replace it).
        self._now: Callable[[], float] = time.time
        self._state_file = state
        # False for --once: reservations are kept, the Downloads checkpoint is not.
        self._checkpoint = checkpoint
        self.to_print_dirs = self._unique_dirs(
            [self.watch_dir / TO_PRINT_DIR, *(to_print_dirs or [])]
        )

        self._queue: "queue.Queue[QueueItem]" = queue.Queue()
        # Downloads paths queued or being processed, and the one in progress.
        self._pending: set[Path] = set()
        self._current: Optional[Path] = None
        self._dirty: set[Path] = set()
        # Arrival time of each Downloads file seen but not yet finished; the
        # minimum holds back the saved checkpoint (seen_until).
        self._first_seen: dict[Path, float] = {}
        self._deferred: dict[Path, int] = {}
        self._queued_deferred: dict[Path, int] = {}
        self._retry_queued = False
        self._retry_reason = ""
        self._accepting = True
        self._pending_lock = threading.Lock()
        # Serializes printer access between the worker and the heartbeat.
        self.printer_lock = threading.Lock()
        # Guards reservations and spans every state snapshot *and* its write,
        # so an older snapshot can never overwrite a newer one.
        self._state_lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}
        self._state_broken = False
        self._state_notified = False

        self._startup_files: dict[Path, Optional[tuple[int, int]]] = {}
        self._decided: dict[Path, tuple[int, int]] = {}
        self._notified: set[tuple[str, str]] = set()
        # (recipient, tracking) of queued labels printed in this to-print pass;
        # shown together in one box at the end of the pass.
        self._printed_notices: list[tuple[str, str]] = []
        self._printers_available: Optional[bool] = None
        self._stats: dict[str, int] = {}
        self._stats_lock = threading.Lock()

        self._worker: Optional[threading.Thread] = None
        self._retry_thread: Optional[threading.Thread] = None
        self._observer: Any = None
        self._observer_ok_since = time.time()

    @staticmethod
    def _unique_dirs(dirs: Sequence[Path]) -> list[Path]:
        unique: list[Path] = []
        for folder in dirs:
            if not any(same_path(folder, seen) for seen in unique):
                unique.append(Path(folder))
        return unique

    @property
    def stats(self) -> dict[str, int]:
        """A copy of the per-status counters (safe to log from any thread)."""
        with self._stats_lock:
            return dict(self._stats)

    @property
    def to_print_dir(self) -> Path:
        """``<watch_dir>/to-print`` (where the watcher puts its own failures)."""
        return self.to_print_dirs[0]

    def reservations(self) -> dict[str, Reservation]:
        """A copy of the content reservations (for logs and tests)."""
        with self._state_lock:
            return dict(self._reservations)

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

    # ----------------------------------------------------------------- state

    def load_state(self) -> Optional[FolderState]:
        """Read the state file: reservations, and this folder's checkpoint.

        A ``filing`` reservation whose outcome was never recorded means the
        previous run died while printing it: it becomes "move it to
        check-printer/" (status uncertain once moved), never "print it again".
        """
        if self._state_file is None:
            return None
        loaded = self._state_file.load(self.watch_dir)
        with self._state_lock:
            self._reservations = dict(loaded.reservations)
            interrupted, unfiled = [], []
            for digest, reservation in self._reservations.items():
                if reservation.status != FILING:
                    continue
                if reservation.dest is None:
                    reservation.dest = CHECK_PRINTER_DIR
                    reservation.final = UNCERTAIN
                    reservation.reason = INTERRUPTED
                    interrupted.append(reservation.to_log(digest))
                else:
                    unfiled.append(reservation.to_log(digest))
            self._prune_reservations()
            counts = self._reservation_counts()
        if interrupted:
            log_event(
                logger,
                logging.WARNING,
                "labels were being printed when the watcher last stopped; they "
                "go to check-printer/ and are NEVER printed again automatically",
                labels=interrupted,
            )
        if unfiled:
            log_event(
                logger,
                logging.WARNING,
                "labels sent to a printer earlier could not be moved yet; the "
                "watcher keeps trying to move them (it will not print them again)",
                labels=unfiled,
            )
        log_event(logger, logging.INFO, "content reservations", **counts)
        return loaded.folder

    def _reservation_counts(self) -> dict[str, int]:
        counts = {FILING: 0, PRINTED: 0, UNCERTAIN: 0}
        for reservation in self._reservations.values():
            counts[reservation.status] = counts.get(reservation.status, 0) + 1
        return counts

    def _prune_reservations(self) -> None:
        """Forget printed labels past the duplicate window; cap the map size."""
        with self._state_lock:
            now = self._now()
            window = self.config.duplicate_window_hours * 3600.0
            for digest, reservation in list(self._reservations.items()):
                if reservation.status != PRINTED:
                    continue
                started = reservation.completed or reservation.updated
                if now - started >= window:
                    del self._reservations[digest]
                    log_event(
                        logger,
                        logging.INFO,
                        "duplicate window over; forgetting a printed label",
                        sha256=digest,
                        printed_at=iso(started),
                        file=reservation.file,
                    )
            excess = len(self._reservations) - MAX_RESERVATIONS
            for status, level in (
                (PRINTED, logging.WARNING),
                (UNCERTAIN, logging.ERROR),
            ):
                if excess <= 0:
                    break
                oldest = sorted(
                    (
                        (reservation.updated, digest)
                        for digest, reservation in self._reservations.items()
                        if reservation.status == status
                    )
                )[:excess]
                for _, digest in oldest:
                    dropped = self._reservations.pop(digest)
                    log_event(
                        logger,
                        level,
                        "too many remembered labels; forgetting the oldest",
                        limit=MAX_RESERVATIONS,
                        reservation=dropped.to_log(digest),
                    )
                excess -= len(oldest)

    def _save_state(self, *, clean: bool, critical: bool = False) -> bool:
        """Snapshot and write the state under one lock. True if it was saved.

        ``critical`` marks the save made right before a submission: if it fails
        the label still prints (availability), but duplicate protection after
        a crash is weakened, so that is logged at CRITICAL and shown once.
        """
        if self._state_file is None:
            return True
        with self._state_lock:
            self._prune_reservations()
            folder = None
            if self._checkpoint:
                now = self._now()
                with self._pending_lock:
                    seen_until = min([now, *self._first_seen.values()])
                folder = FolderState(
                    seen_until=seen_until, updated=now, clean_shutdown=clean
                )
            saved = self._state_file.save(self.watch_dir, folder, self._reservations)
            self._note_save_result(saved, critical)
        return saved

    def _note_save_result(self, saved: bool, critical: bool) -> None:
        if saved:
            if self._state_broken:
                log_event(
                    logger,
                    logging.WARNING,
                    "watcher state file can be written again",
                    path=str(self._state_file.path) if self._state_file else None,
                )
            self._state_broken = False
            return
        self._state_broken = True
        log_event(
            logger,
            logging.CRITICAL,
            "could not save watcher state; printing continues, but protection "
            "against printing a label twice after a crash or restart is degraded",
            path=str(self._state_file.path) if self._state_file else None,
            before_submission=critical,
        )
        if self._state_notified:
            return
        assert self._state_file is not None
        text = messages.state_unsaved(
            self._state_file.path, "the state file could not be written"
        )
        if self._deliver(text, kind="state_unsaved", file=str(self._state_file.path)):
            self._state_notified = True

    # ---------------------------------------------------------- notifications

    def _deliver(self, text: str, *, info: bool = False, **context: Any) -> bool:
        """Hand one message to the notifier. True once nothing more can be done.

        Returns True when the notifier accepted it, or when boxes are switched
        off or impossible here (it is logged instead); False when it was
        refused and should be tried again later. ``info`` is good news (an
        information box instead of a warning).
        """
        if not self.config.notify_on_failure:
            log_event(
                logger,
                logging.INFO,
                "failure notification disabled",
                text=text,
                **context,
            )
            return True
        if not self.notifier.available:
            log_event(
                logger,
                logging.WARNING,
                "message boxes are not available here; logging the message instead",
                text=text,
                **context,
            )
            return True
        if self.notifier.notify(text, info=info):
            log_event(logger, logging.INFO, "notifying volunteer", **context)
            return True
        log_event(
            logger,
            logging.WARNING,
            "notification was not accepted; will try again next time",
            **context,
        )
        return False

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
        if self._deliver(text, kind=kind, file=str(path)):
            self._notified.add(key)

    # ------------------------------------------------------------ reading

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
            "DRY RUN: would print (nothing printed, nothing moved, nothing "
            "remembered)",
            file=str(path),
            job_name=job_name,
            image_size=list(image.size),
            track_timeout_s=self.config.track_timeout_s,
        )
        return FileOutcome("dry_run", path, "would print", sha256=digest)

    def _decode(
        self, path: Path, digest: str, data: bytes
    ) -> Union[FileOutcome, tuple[LoadedLabel, ShapeDecision]]:
        """Decode the bytes and check the shape, or return an unsupported outcome."""
        try:
            loaded = load_label(path, pdf_dpi=self.config.pdf_dpi, data=data)
        except UnsupportedFormat as exc:
            return FileOutcome("unsupported", path, str(exc), sha256=digest)
        return loaded, self._shape(loaded.image.size)

    def _shape(self, size: tuple[int, int]) -> ShapeDecision:
        return classify_shape(
            size,
            aspect_min=self.config.aspect_min,
            aspect_max=self.config.aspect_max,
            min_short_side_px=self.config.min_short_side_px,
        )

    # ------------------------------------------------------- reservations

    def _check_reservation(self, path: Path, digest: str) -> Optional[FileOutcome]:
        """Handle a file whose content is reserved, instead of printing it.

        Returns None when the file may be submitted (no reservation, or a
        deliberate ``REPRINT`` copy). Otherwise the file is filed, and the
        (not yet counted or logged) outcome is returned:

        * the very file of a ``filing`` reservation: moved to its result folder;
        * a copy of a ``printed`` label: moved to ``printed/duplicate_...``;
        * a copy of an ``uncertain`` label: moved to ``check-printer/``.
        """
        if self.config.dry_run:
            return None
        with self._state_lock:
            self._prune_reservations()
            reservation = self._reservations.get(digest)
            if reservation is None:
                return None
            own_file = reservation.status == FILING and same_path(
                reservation.file, path
            )
            if not own_file and is_reprint(path):
                log_event(
                    logger,
                    logging.WARNING,
                    "REPRINT: printing this label again on purpose (the name "
                    "starts with REPRINT)",
                    reprint_file=str(path),
                    reservation=reservation.to_log(digest),
                )
                return None
            if not own_file:
                reservation.last_seen_path = str(path)
                reservation.copies_seen += 1
            effective = reservation.effective
            since = iso(reservation.completed or reservation.first_submitted) or "?"
        if own_file:
            return self._complete_filing(digest, path, later=True)
        to_print = self._to_print_for(path)
        if effective == PRINTED:
            moved_to = self.move_to(path, PRINTED_DIR, prefix=DUPLICATE_PREFIX)
            log_event(
                logger,
                logging.WARNING,
                "DUPLICATE not printed: this label already printed. To print "
                "another copy on purpose, rename it to start with REPRINT.",
                file=str(path),
                printed_at=since,
                moved_to=str(moved_to) if moved_to else None,
            )
            text = messages.already_printed(path, moved_to, since, to_print)
            detail = f"identical label already printed at {since}"
        else:
            moved_to = self.move_to(path, CHECK_PRINTER_DIR, prefix=DUPLICATE_PREFIX)
            log_event(
                logger,
                logging.WARNING,
                "DUPLICATE not printed: the same label was sent to a printer "
                "earlier and may or may not have printed; moved to "
                "check-printer/. Rename it to start with REPRINT to print it.",
                file=str(path),
                sent_at=since,
                moved_to=str(moved_to) if moved_to else None,
            )
            text = messages.uncertain_copy(path, moved_to, since, to_print)
            detail = f"identical label sent at {since}; outcome uncertain"
        self._notify_once(moved_to or path, "duplicate", text)
        self._save_state(clean=False)
        return FileOutcome("duplicate", path, detail, moved_to=moved_to, sha256=digest)

    def _reserve(self, path: Path, digest: str) -> Optional[Reservation]:
        """Record "about to print this content" durably. Returns what it replaces."""
        with self._state_lock:
            now = self._now()
            previous = self._reservations.get(digest)
            self._reservations[digest] = Reservation(
                status=FILING,
                first_submitted=now,
                updated=now,
                file=str(path),
                last_seen_path=str(path),
                reason="submitting" + (" (REPRINT)" if previous else ""),
            )
            log_event(
                logger,
                logging.INFO,
                "content reserved before printing",
                sha256=digest,
                file=str(path),
                replaces=previous.to_log(digest) if previous else None,
            )
            self._save_state(clean=False, critical=True)
        return previous

    def _release(self, digest: str, previous: Optional[Reservation]) -> None:
        """A definite failure: drop the reservation (or restore the older one)."""
        with self._state_lock:
            if previous is None:
                self._reservations.pop(digest, None)
            else:
                self._reservations[digest] = previous
            log_event(
                logger,
                logging.INFO,
                "content reservation released (the label definitely did not print)",
                sha256=digest,
                restored=previous.status if previous else None,
            )
            self._save_state(clean=False)

    def _submit(self, path: Path, digest: str, image: Any, job_name: str) -> Submission:
        """Reserve the content, hand the image to the printer API, classify.

        Only a definite failure (``PrintError``) releases the reservation.
        """
        previous = self._reserve(path, digest)
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
                self._release(digest, previous)
                return Submission("to_print", None, str(exc), reason=exc.reason)
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

    def _file_common(
        self, path: Path, digest: str, submission: Submission
    ) -> Optional[FileOutcome]:
        """File a printed or check-printer submission; None for a definite failure.

        The outcome is recorded in the reservation (and saved) before the file
        is moved, so a crash or a failed move never leads to a second print.
        The returned outcome has not been counted or logged yet.
        """
        if submission.kind not in ("printed", "check_printer"):
            return None
        result = submission.result
        printed = submission.kind == "printed"
        with self._state_lock:
            reservation = self._reservations.get(digest)
            if reservation is None:  # pruned or replaced; recreate conservatively
                now = self._now()
                reservation = Reservation(
                    status=FILING, first_submitted=now, updated=now, file=str(path)
                )
                self._reservations[digest] = reservation
            reservation.dest = PRINTED_DIR if printed else CHECK_PRINTER_DIR
            reservation.final = PRINTED if printed else UNCERTAIN
            reservation.reason = submission.error or (
                result.outcome.value if result else ""
            )
            reservation.printer = result.printer_name if result else None
            reservation.updated = self._now()
            self._save_state(clean=False)
        if printed:
            assert result is not None
            detail = f"printed on {result.printer_name} ({result.outcome.value})"
        else:
            detail = submission.error
        return self._complete_filing(digest, path, later=False, detail=detail)

    def _complete_filing(
        self, digest: str, path: Path, *, later: bool, detail: str = ""
    ) -> FileOutcome:
        """Move a ``filing`` reservation's file to its result folder.

        On success the reservation becomes ``printed`` or ``uncertain``; on
        failure it stays ``filing`` and the move is retried later (the label is
        never printed again). ``later`` is True for a retried move.
        """
        with self._state_lock:
            reservation = self._reservations.get(digest)
            if reservation is None or reservation.status != FILING:
                return FileOutcome("missing", path, "no pending filing", sha256=digest)
            dest = reservation.dest or CHECK_PRINTER_DIR
            final = reservation.final or UNCERTAIN
            interrupted = reservation.reason == INTERRUPTED
            since = iso(reservation.first_submitted) or "?"
        moved_to = self.move_to(path, dest, quiet=later)
        with self._state_lock:
            now = self._now()
            if moved_to is None:
                reservation.move_failures += 1
                reservation.updated = now
                failures = reservation.move_failures
            else:
                reservation.status = final
                reservation.dest = None
                reservation.last_seen_path = str(moved_to)
                reservation.updated = now
                if final == PRINTED and reservation.completed is None:
                    reservation.completed = now
                failures = 0
            self._save_state(clean=False)
        if moved_to is None:
            log_event(
                logger,
                (
                    logging.ERROR
                    if failures in (1, 2) or failures % 10 == 0
                    else logging.DEBUG
                ),
                "label already sent to a printer could not be moved; it will NOT "
                "be printed again, and the move will be retried",
                file=str(path),
                dest=dest,
                move_failures=failures,
                sha256=digest,
            )
        elif later:
            log_event(
                logger,
                logging.WARNING if final == UNCERTAIN else logging.INFO,
                f"moved a label sent to a printer earlier into {dest}/ (not printed "
                "again)",
                file=str(path),
                moved_to=str(moved_to),
                sent_at=since,
                reason=INTERRUPTED if interrupted else None,
            )
        to_print = self._to_print_for(path)
        if interrupted:
            self._notify_once(
                path,
                "interrupted",
                messages.interrupted(path, moved_to, since, to_print),
            )
        elif final == UNCERTAIN and not later:
            self._notify_once(
                moved_to or path,
                "check_printer",
                messages.check_printer(path, moved_to, detail, to_print),
            )
        if not later:
            status = "printed" if final == PRINTED else "check_printer"
        else:
            status = "filed" if moved_to else "move_pending"
        if later and not detail:
            detail = (
                f"sent to a printer at {since}"
                + (" (watcher stopped mid-print)" if interrupted else "")
                + ("; moved" if moved_to else "; still could not be moved")
            )
        return FileOutcome(status, path, detail, moved_to=moved_to, sha256=digest)

    def reconcile_filing(self) -> list[FileOutcome]:
        """Retry moving labels that were sent to a printer but not filed yet.

        Runs on the worker thread at every retry tick (and in ``--once``). A
        file that is gone or now holds different content just completes the
        reservation; nothing is ever printed here.
        """
        with self._state_lock:
            pending = [
                (digest, reservation.file)
                for digest, reservation in self._reservations.items()
                if reservation.status == FILING and reservation.dest is not None
            ]
        outcomes = []
        for digest, file in pending:
            path = Path(file)
            current: Optional[str] = None
            try:
                current = sha256_bytes(
                    read_file_bytes(path, attempts=1, sleep=self._sleep)
                )
            except FileNotFoundError:
                current = None
            except TransientReadError as exc:
                log_event(
                    logger,
                    logging.DEBUG,
                    "unfiled label not readable right now; will retry",
                    file=file,
                    error=str(exc),
                )
                continue
            if current == digest:
                outcomes.append(self._complete_filing(digest, path, later=True))
                continue
            with self._state_lock:
                reservation = self._reservations.get(digest)
                if reservation is None or reservation.status != FILING:
                    continue
                reservation.status = reservation.final or UNCERTAIN
                reservation.dest = None
                reservation.updated = self._now()
                if reservation.status == PRINTED and reservation.completed is None:
                    reservation.completed = reservation.updated
                self._save_state(clean=False)
            log_event(
                logger,
                logging.WARNING,
                "the file of a label sent to a printer earlier is gone (or was "
                "replaced); its content stays reserved",
                reservation=reservation.to_log(digest),
            )
        return outcomes

    # --------------------------------------------------------------- moving

    def _results_base(self, path: Path) -> Path:
        """Folder holding the result folders for ``path``.

        A file in an extra to-print folder (``paths.to_print_dir()`` when it is
        not under the watch folder) is filed next to that folder.
        """
        for folder in self.to_print_dirs[1:]:
            if same_path(path.parent, folder):
                return folder.parent
        return self.watch_dir

    def _to_print_for(self, path: Path) -> Path:
        """The to-print folder the messages about ``path`` should name."""
        for folder in self.to_print_dirs[1:]:
            if same_path(path.parent, folder):
                return folder
        return self.to_print_dir

    def move_to(
        self, path: Path, subdir: str, *, prefix: str = "", quiet: bool = False
    ) -> Optional[Path]:
        """Move ``path`` into ``<base>/<subdir>/`` with a timestamp prefix.

        Retries while another program (browser, antivirus, image viewer) holds
        the file open. Returns the new path, or None if it could not be moved.
        """
        target_dir = self._results_base(path) / subdir
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Keep a deliberate reprint recognisable after the timestamp is added,
        # so a failed REPRINT that lands in to-print/ is still printed later.
        if is_reprint(path):
            prefix = f"{REPRINT_PREFIX}_{prefix}"
        last_exc: Optional[BaseException] = None
        for attempt_no in range(1, MOVE_RETRIES + 1):
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / f"{prefix}{stamp}_{path.name}"
                counter = 1
                while dest.exists():
                    dest = target_dir / f"{prefix}{stamp}_{counter}_{path.name}"
                    counter += 1
                os.rename(path, dest)
            except PermissionError as exc:
                last_exc = exc
                log_event(
                    logger,
                    logging.DEBUG if quiet else logging.WARNING,
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
            carry_sidecar(path, dest, subdir)
            return dest
        log_event(
            logger,
            logging.DEBUG if quiet else logging.ERROR,
            "could not move file; leaving it in place",
            file=str(path),
            subdir=subdir,
            error=describe_exception(last_exc) if last_exc else None,
        )
        return None

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
