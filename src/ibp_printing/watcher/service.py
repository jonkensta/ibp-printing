"""The watcher itself: folder events -> queue -> one worker that prints and files."""

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from watchdog.events import FileSystemEventHandler

from ibp_printing import api
from ibp_printing.backends import PrintError
from ibp_printing.log import attempt, describe_exception, get_logger, log_event
from ibp_printing.models import PrintResult
from ibp_printing.watcher.config import WatcherConfig
from ibp_printing.watcher.detect import (
    RAW_PRINTER_SUFFIXES,
    UnsupportedFormat,
    classify_shape,
    is_temp_name,
    load_label,
    matches_globs,
    sha256_file,
    wait_until_stable,
)
from ibp_printing.watcher.notify import Notifier

logger = get_logger(__name__)

PRINTED_DIR = "printed"
FAILED_DIR = "failed"
MOVE_RETRIES = 5
MOVE_RETRY_DELAY_S = 0.5


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


def _signature(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


class LabelWatcher:  # pylint: disable=too-many-instance-attributes
    """Watch one folder and print every new 4x6 label that lands in it.

    All printing happens on a single worker thread, so the printer is never
    asked to do two things at once.
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
    ) -> None:
        self.config = config
        self.watch_dir = Path(watch_dir)
        self.notifier = notifier or Notifier(enabled=config.notify_on_failure)
        self.stop_event = stop_event or threading.Event()
        self._clock = clock
        self._sleep = sleep

        self._queue: "queue.Queue[Optional[Path]]" = queue.Queue()
        self._pending: set[Path] = set()
        self._pending_lock = threading.Lock()
        # Serializes printer access between the worker and the heartbeat.
        self.printer_lock = threading.Lock()

        self._printed_at: dict[str, float] = {}
        self._stuck_hashes: set[str] = set()
        self._startup_files: dict[Path, Optional[tuple[int, int]]] = {}
        self._decided: dict[Path, tuple[int, int]] = {}
        self.stats: dict[str, int] = {}

        self._worker: Optional[threading.Thread] = None
        self._observer: Any = None

    # -- lifecycle -----------------------------------------------------------

    def existing_files(self) -> list[Path]:
        """Regular files directly inside the watch folder, oldest first."""
        files = []
        for entry in self.watch_dir.iterdir():
            try:
                if entry.is_file():
                    files.append(entry)
            except OSError:
                continue
        return sorted(files, key=lambda p: (_signature(p) or (0, 0))[1])

    def snapshot_existing(self) -> list[Path]:
        """Remember files already present so later events for them are ignored."""
        files = self.existing_files()
        self._startup_files = {path: _signature(path) for path in files}
        candidates = [
            path.name for path in files if matches_globs(path, self.config.globs)
        ]
        log_event(
            logger,
            logging.INFO,
            "files already in watch folder",
            total=len(files),
            matching_globs=candidates,
            will_process=self.config.process_existing,
        )
        return files

    def start(self) -> None:
        """Snapshot the folder, start the worker, and start watching for events."""
        # pylint: disable=import-outside-toplevel
        from watchdog.observers import Observer

        self.watch_dir.mkdir(parents=True, exist_ok=True)
        existing = self.snapshot_existing()

        self._worker = threading.Thread(
            target=self._worker_loop, name="label-worker", daemon=True
        )
        self._worker.start()

        observer = Observer()
        observer.schedule(_EventHandler(self), str(self.watch_dir), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        log_event(
            logger,
            logging.INFO,
            "watching folder",
            watch_dir=str(self.watch_dir),
            observer=type(observer).__name__,
        )

        if self.config.process_existing:
            for path in existing:
                self.enqueue(path, "existing", force=True)

    def stop(self, timeout_s: float = 10.0) -> None:
        """Stop watching, let the worker finish its current file, and join."""
        log_event(logger, logging.INFO, "stopping watcher", stats=self.stats)
        self.stop_event.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout_s)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log_event(
                    logger,
                    logging.WARNING,
                    "observer stop failed",
                    error=describe_exception(exc),
                )
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout_s)
            if self._worker.is_alive():
                log_event(
                    logger,
                    logging.WARNING,
                    "worker still busy at shutdown (left running as daemon)",
                )
        log_event(logger, logging.INFO, "watcher stopped", stats=self.stats)

    def observer_alive(self) -> bool:
        """True while the folder observer thread is running."""
        return self._observer is not None and self._observer.is_alive()

    def wait_idle(self, timeout_s: float = 30.0) -> bool:
        """Block until nothing is queued or being processed (used by tests)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._pending_lock:
                if not self._pending:
                    return True
            time.sleep(0.05)
        return False

    def process_existing_now(self) -> list[FileOutcome]:
        """Process every file currently in the folder on this thread (``--once``)."""
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        files = self.existing_files()
        log_event(
            logger,
            logging.INFO,
            "processing existing files once",
            count=len(files),
            files=[path.name for path in files],
        )
        return [self.process_path(path) for path in files]

    # -- events and queue ----------------------------------------------------

    def on_fs_event(self, kind: str, raw_path: str) -> None:
        """Entry point for watchdog events (already filtered to files)."""
        path = Path(raw_path)
        log_event(logger, logging.DEBUG, f"fs event: {kind}", path=str(path))
        if path.parent != self.watch_dir:
            log_event(
                logger,
                logging.DEBUG,
                "ignored: not directly in watch folder",
                event=kind,
                path=str(path),
            )
            return
        if is_temp_name(path):
            log_event(
                logger,
                logging.DEBUG,
                "ignored: temporary/partial download",
                event=kind,
                file=path.name,
            )
            return
        self.enqueue(path, kind)

    def enqueue(self, path: Path, reason: str, *, force: bool = False) -> bool:
        """Queue a file for the worker unless it is already pending."""
        if not force and path in self._startup_files:
            if _signature(path) == self._startup_files[path]:
                log_event(
                    logger,
                    logging.DEBUG,
                    "ignored: unchanged since startup",
                    event=reason,
                    file=path.name,
                )
                return False
            del self._startup_files[path]
        with self._pending_lock:
            if path in self._pending:
                log_event(
                    logger,
                    logging.DEBUG,
                    "already pending",
                    event=reason,
                    file=path.name,
                )
                return False
            self._pending.add(path)
        log_event(
            logger,
            logging.INFO,
            "queued file",
            event=reason,
            file=path.name,
            queue_size=self._queue.qsize() + 1,
        )
        self._queue.put(path)
        return True

    def _worker_loop(self) -> None:
        log_event(logger, logging.INFO, "worker started")
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            if item is None:
                break
            try:
                if self.stop_event.is_set():
                    log_event(
                        logger, logging.INFO, "shutdown: not processing", file=item.name
                    )
                    continue
                self.process_path(item)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("unexpected error processing %s", item)
            finally:
                with self._pending_lock:
                    self._pending.discard(item)
        log_event(logger, logging.INFO, "worker exiting")

    # -- processing ----------------------------------------------------------

    def _finish(self, outcome: FileOutcome, level: int = logging.INFO) -> FileOutcome:
        self.stats[outcome.status] = self.stats.get(outcome.status, 0) + 1
        if outcome.status in {"not_label", "unsupported", "dry_run", "not_matching"}:
            signature = _signature(outcome.path)
            if signature is not None:
                self._decided[outcome.path] = signature
        log_event(logger, level, f"file outcome: {outcome.status}", **outcome.to_log())
        return outcome

    def process_path(  # pylint: disable=too-many-return-statements
        self, path: Path
    ) -> FileOutcome:
        """Run every check on one file and print it if it is a label."""
        log_event(logger, logging.INFO, "considering file", file=str(path))
        if path.parent != self.watch_dir:
            return self._finish(FileOutcome("ignored", path, "not in watch folder"))
        if is_temp_name(path):
            return self._finish(FileOutcome("ignored", path, "temporary download"))
        if not path.is_file():
            return self._finish(FileOutcome("missing", path, "gone or not a file"))

        signature = _signature(path)
        if signature is not None and self._decided.get(path) == signature:
            log_event(
                logger,
                logging.DEBUG,
                "unchanged since last decision; skipping",
                file=path.name,
            )
            return FileOutcome("unchanged", path, "already decided")

        suffix = path.suffix.lower()
        if suffix in RAW_PRINTER_SUFFIXES:
            return self._finish(
                FileOutcome(
                    "unsupported",
                    path,
                    f"{suffix} is a raw printer language (ZPL/EPL); download the "
                    "PNG or PDF label instead",
                ),
                logging.WARNING,
            )
        pattern = matches_globs(path, self.config.globs)
        if pattern is None:
            return self._finish(
                FileOutcome(
                    "not_matching", path, f"name matches none of {self.config.globs}"
                )
            )
        log_event(logger, logging.DEBUG, "name matches", file=path.name, glob=pattern)

        stability = wait_until_stable(
            path,
            stable_s=self.config.stable_seconds,
            timeout_s=self.config.stable_timeout_s,
            interval_s=max(0.02, min(0.25, self.config.stable_seconds / 4)),
            clock=self._clock,
            sleep=self._sleep,
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
            return self._finish(
                FileOutcome("unstable", path, stability.reason), logging.WARNING
            )

        try:
            digest = sha256_file(path)
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "could not read file to hash it",
                file=path.name,
                error=describe_exception(exc),
            )
            return self._finish(FileOutcome("unreadable", path, repr(exc)))

        with attempt(
            "watcher label", file=str(path), sha256=digest, size_bytes=stability.size
        ):
            return self._process_candidate(path, digest)

    def _process_candidate(self, path: Path, digest: str) -> FileOutcome:
        if digest in self._stuck_hashes:
            return self._finish(
                FileOutcome(
                    "duplicate",
                    path,
                    "content already handled but could not be moved earlier",
                    sha256=digest,
                )
            )
        now = self._clock()
        self._printed_at = {
            key: when
            for key, when in self._printed_at.items()
            if now - when < self.config.dedupe_seconds
        }
        if digest in self._printed_at:
            return self._finish(
                FileOutcome(
                    "duplicate",
                    path,
                    f"identical content printed {now - self._printed_at[digest]:.1f}s "
                    f"ago (dedupe window {self.config.dedupe_seconds}s)",
                    sha256=digest,
                )
            )

        try:
            loaded = load_label(path, pdf_dpi=self.config.pdf_dpi)
        except UnsupportedFormat as exc:
            return self._finish(
                FileOutcome("unsupported", path, str(exc), sha256=digest),
                logging.WARNING,
            )
        decision = classify_shape(
            loaded.image.size,
            aspect_min=self.config.aspect_min,
            aspect_max=self.config.aspect_max,
            min_short_side_px=self.config.min_short_side_px,
        )
        log_event(
            logger,
            logging.INFO,
            (
                "label decision: LABEL"
                if decision.is_label
                else "label decision: NOT a label"
            ),
            file=path.name,
            source_format=loaded.source_format,
            load_info=loaded.info,
            **decision.to_log(),
        )
        if not decision.is_label:
            return self._finish(
                FileOutcome(
                    "not_label", path, "; ".join(decision.reasons), sha256=digest
                )
            )

        job_name = f"EasyPost {path.name}"
        if self.config.dry_run:
            log_event(
                logger,
                logging.WARNING,
                "DRY RUN: would print (nothing printed, nothing moved)",
                file=path.name,
                job_name=job_name,
                image_size=list(loaded.image.size),
                track_timeout_s=self.config.track_timeout_s,
            )
            self._printed_at[digest] = self._clock()
            return self._finish(
                FileOutcome("dry_run", path, "would print", sha256=digest)
            )

        return self._print_and_file(path, digest, loaded.image, job_name)

    def _print_and_file(
        self, path: Path, digest: str, image: Any, job_name: str
    ) -> FileOutcome:
        result: Optional[PrintResult] = None
        error = ""
        with self.printer_lock:
            try:
                result = api.print_to_first_available(
                    image,
                    job_name=job_name,
                    track_timeout_s=self.config.track_timeout_s,
                )
            except PrintError as exc:
                error = str(exc)
                log_event(
                    logger,
                    logging.ERROR,
                    "print failed",
                    file=path.name,
                    error=describe_exception(exc),
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                error = f"unexpected error: {exc!r}"
                logger.exception("unexpected error while printing %s", path.name)

        ok = result is not None and result.outcome.ok
        if result is not None and not ok:
            error = f"printer {result.printer_name} reported {result.outcome.value}" + (
                f" ({'; '.join(result.history)})" if result.history else ""
            )
        if ok:
            self._printed_at[digest] = self._clock()

        moved_to = self.move_to(path, PRINTED_DIR if ok else FAILED_DIR)
        if moved_to is None:
            self._stuck_hashes.add(digest)

        if not ok:
            self._notify_failure(path, moved_to, error, spooled=result is not None)

        outcome = FileOutcome(
            "printed" if ok else "failed",
            path,
            (
                f"printed on {result.printer_name} ({result.outcome.value})"
                if ok and result is not None
                else error
            ),
            moved_to=moved_to,
            sha256=digest,
        )
        return self._finish(outcome, logging.INFO if ok else logging.ERROR)

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
            "could not move file; leaving it in place (will not reprint it)",
            file=str(path),
            subdir=subdir,
            error=describe_exception(last_exc) if last_exc else None,
        )
        return None

    def _notify_failure(
        self, path: Path, moved_to: Optional[Path], error: str, *, spooled: bool
    ) -> None:
        where = (
            f"It was moved to:\n{moved_to}" if moved_to else f"It is still at:\n{path}"
        )
        # A spooled job that errored or timed out may still come out once the
        # printer is fixed, so warn before the volunteer reprints it.
        headline = (
            "The shipping label may NOT have printed. It was sent to the printer, "
            "but the print queue reported a problem. If it is still waiting in "
            "the queue it may print once the printer is fixed, so check before "
            "printing it again."
            if spooled
            else "The shipping label did NOT print."
        )
        text = (
            f"{headline}\n\n"
            f"File: {path.name}\n{where}\n\n"
            f"Problem: {error}\n\n"
            "Check that the label printer is plugged in, turned on and has "
            "labels, then download the label again (or drag the file from the "
            "failed folder back into Downloads) to print it."
        )
        if not self.config.notify_on_failure:
            log_event(logger, logging.INFO, "failure notification disabled", text=text)
            return
        self.notifier.notify(text)

    # -- diagnostics ---------------------------------------------------------

    def log_discovery(self, reason: str) -> None:
        """Log a full printer discovery snapshot."""
        try:
            with self.printer_lock:
                discovery = api.discover()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.ERROR,
                f"discovery failed ({reason})",
                error=describe_exception(exc),
            )
            return
        log_event(
            logger,
            logging.INFO,
            f"printer snapshot ({reason})",
            usable=[candidate.name for candidate in discovery.usable],
            queues=len(discovery.queues),
            usb_devices=len(discovery.usb_devices),
            errors=discovery.errors,
        )
        for candidate in discovery.candidates:
            log_event(logger, logging.INFO, "printer candidate", **candidate.to_log())

    def heartbeat_loop(self) -> None:
        """Every ``heartbeat_minutes``, log liveness, stats and a printer snapshot."""
        interval = max(self.config.heartbeat_minutes, 0.05) * 60.0
        while not self.stop_event.wait(interval):
            log_event(
                logger,
                logging.INFO,
                "heartbeat",
                observer_alive=self.observer_alive(),
                worker_alive=self._worker is not None and self._worker.is_alive(),
                queue_size=self._queue.qsize(),
                stats=self.stats,
            )
            if self._observer is not None and not self.observer_alive():
                log_event(
                    logger,
                    logging.CRITICAL,
                    "folder observer has died; new downloads are NOT being seen",
                )
            self.log_discovery("heartbeat")


class _EventHandler(FileSystemEventHandler):
    """Adapts watchdog callbacks to ``LabelWatcher.on_fs_event``."""

    def __init__(self, watcher: LabelWatcher) -> None:
        super().__init__()
        self.watcher = watcher

    def dispatch(self, event: Any) -> None:
        """Called by watchdog for every event in the folder."""
        try:
            if event.is_directory:
                return
            kind = event.event_type
            if kind == "moved":
                self.watcher.on_fs_event(kind, os.fsdecode(event.dest_path))
            elif kind in ("created", "modified"):
                self.watcher.on_fs_event(kind, os.fsdecode(event.src_path))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("error handling filesystem event %r", event)
