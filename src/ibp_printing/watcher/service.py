"""The watcher itself: folder events -> queue -> one worker that prints and files.

Under the watch folder (names from :mod:`ibp_printing.paths`):

* ``printed/``       - labels a printer accepted (outcome ``ok``).
* ``to-print/``      - labels that definitely did not reach any printer. shippy
  and shippy-gui save labels here too. The watcher retries them every
  ``retry_seconds`` whenever discovery finds a usable printer.
* ``check-printer/`` - labels sent to a printer whose queue then reported a
  problem, or whose fate is unknown. Never retried automatically.
"""

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Optional

from watchdog.events import FileSystemEventHandler

from ibp_printing.log import attempt, describe_exception, get_logger, log_event
from ibp_printing.paths import (
    CHECK_PRINTER_DIR,
    PRINTED_DIR,
    TO_PRINT_DIR,
)
from ibp_printing.watcher import messages
from ibp_printing.watcher.core import (
    MAX_DEFERRED_RETRIES,
    RETRY,
    FileOutcome,
    Submission,
    _RetryRequest,
    arrival_time,
    file_signature,
)
from ibp_printing.watcher.detect import (
    RAW_PRINTER_SUFFIXES,
    classify_shape,
    download_in_progress,
    is_temp_name,
    matches_globs,
    peek_size,
)
from ibp_printing.watcher.state import FolderState, iso
from ibp_printing.watcher.toprint import ToPrintQueue

logger = get_logger(__name__)

__all__ = [
    "CHECK_PRINTER_DIR",
    "PRINTED_DIR",
    "TO_PRINT_DIR",
    "FileOutcome",
    "LabelWatcher",
    "MAX_DEFERRED_RETRIES",
]

# mtime/ctime resolution slack when comparing with the saved high-water mark.
ARRIVAL_SLACK_S = 2.0
# At most this many ignored files are examined for the startup warning.
IGNORED_SCAN_LIMIT = 50

# Statuses after which a Downloads file needs no further attention.
_UNFINISHED = frozenset({"shutdown", "unstable", "unreadable"})
# Statuses remembered by file signature so an unchanged file is not re-decided.
_DECIDED = frozenset(
    {"not_label", "unsupported", "dry_run", "not_matching", "duplicate", "move_pending"}
)


class LabelWatcher(ToPrintQueue):
    """Watch one folder and print every new 4x6 label that lands in it.

    All printing happens on a single worker thread, so the printer is never
    asked to do two things at once.
    """

    def snapshot_existing(self) -> list[Path]:
        """Remember files already present so later events for them are ignored."""
        files = self.existing_files()
        self._startup_files = {path: file_signature(path) for path in files}
        candidates = [
            path.name for path in files if matches_globs(path, self.config.globs)
        ]
        log_event(
            logger,
            logging.INFO,
            "files already in watch folder",
            total=len(files),
            matching_globs=candidates[-IGNORED_SCAN_LIMIT:],
            matching_count=len(candidates),
            will_process_all=self.config.process_existing,
        )
        return files

    def _select_startup_files(
        self, files: list[Path], prior: Optional[FolderState]
    ) -> list[Path]:
        """Pick the files present at startup that still need processing."""
        if self.config.process_existing:
            chosen = list(files)
            log_event(
                logger,
                logging.INFO,
                "process_existing: every file in the folder will be considered",
                count=len(chosen),
            )
        elif prior is None:
            chosen = []
            log_event(
                logger,
                logging.INFO,
                "no history for this folder (first run): existing files are "
                "ignored unless they change",
                count=len(files),
            )
        else:
            cutoff = prior.seen_until - ARRIVAL_SLACK_S
            chosen = [path for path in files if (arrival_time(path) or 0.0) > cutoff]
            if not prior.clean_shutdown:
                log_event(
                    logger,
                    logging.WARNING,
                    "previous watcher run did not shut down cleanly (crash, "
                    "power loss or logoff)",
                    last_update=iso(prior.updated),
                )
            log_event(
                logger,
                logging.INFO if not chosen else logging.WARNING,
                "files that arrived while the watcher was not running",
                seen_until=iso(prior.seen_until),
                files=[path.name for path in chosen],
            )
        for path in chosen:
            self._startup_files.pop(path, None)
        self._warn_ignored(files, set(chosen))
        return chosen

    def _warn_ignored(self, files: list[Path], chosen: set[Path]) -> None:
        """Log a WARNING naming label-shaped files that startup is ignoring."""
        ignored = [
            path
            for path in files
            if path not in chosen
            and not is_temp_name(path)
            and matches_globs(path, self.config.globs)
        ]
        if not ignored:
            return
        newest = ignored[-IGNORED_SCAN_LIMIT:]
        label_shaped = []
        for path in reversed(newest):
            size = peek_size(path)
            if size is None:
                if path.suffix.lower() == ".pdf":
                    label_shaped.append(f"{path.name} (PDF, shape not checked)")
                continue
            decision = classify_shape(
                size,
                aspect_min=self.config.aspect_min,
                aspect_max=self.config.aspect_max,
                min_short_side_px=self.config.min_short_side_px,
            )
            if decision.is_label:
                label_shaped.append(path.name)
        if label_shaped:
            log_event(
                logger,
                logging.WARNING,
                "label-shaped files already in the watch folder are being IGNORED "
                "(they were already there when the watcher last looked, or this is "
                "its first run); "
                "move one into to-print/ to print it",
                files=label_shaped,
                examined=len(newest),
                ignored_total=len(ignored),
            )

    def start(self) -> None:
        """Start the worker and observer, then catch up on missed files.

        Order matters (no download may slip between the snapshot and the
        observer): snapshot, start watching, then rescan and queue anything
        that appeared or changed in between.
        """
        # pylint: disable=import-outside-toplevel
        from watchdog.observers import Observer

        self.watch_dir.mkdir(parents=True, exist_ok=True)
        prior = self.load_state()
        existing = self.snapshot_existing()
        snapshot = dict(self._startup_files)
        chosen = self._select_startup_files(existing, prior)

        self._worker = threading.Thread(
            target=self._worker_loop, name="label-worker", daemon=True
        )
        self._worker.start()

        observer = Observer()
        observer.schedule(_EventHandler(self), str(self.watch_dir), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer
        self._observer_ok_since = time.time()
        log_event(
            logger,
            logging.INFO,
            "watching folder",
            watch_dir=str(self.watch_dir),
            observer=type(observer).__name__,
        )

        for path in chosen:
            self.enqueue(path, "startup catch-up", force=True)
        rescued = []
        for path in self.existing_files():
            if path not in snapshot or file_signature(path) != snapshot[path]:
                if self.enqueue(path, "startup rescan"):
                    rescued.append(path.name)
        log_event(
            logger,
            logging.INFO,
            "startup rescan after observer start",
            newly_seen=rescued,
        )
        self._save_state(clean=False)

        self._retry_thread = threading.Thread(
            target=self._retry_loop, name="retry-timer", daemon=True
        )
        self._retry_thread.start()
        self.request_retry("startup")

    def stop(self, timeout_s: Optional[float] = None) -> None:
        """Stop accepting work, let the in-flight file finish, and join.

        The caller should keep the single-instance lock until this returns.
        """
        if timeout_s is None:
            timeout_s = self.config.stable_timeout_s + self.config.track_timeout_s + 30
        with self._pending_lock:
            self._accepting = False
            current = self._current
        log_event(
            logger,
            logging.INFO,
            "stopping watcher",
            stats=self.stats,
            in_progress=str(current) if current else None,
            wait_up_to_s=timeout_s,
        )
        self.stop_event.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(5.0)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log_event(
                    logger,
                    logging.WARNING,
                    "observer stop failed",
                    error=describe_exception(exc),
                )
        if self._retry_thread is not None:
            self._retry_thread.join(2.0)
        self._queue.put(None)
        worker_alive = False
        if self._worker is not None:
            self._worker.join(timeout_s)
            worker_alive = self._worker.is_alive()
            if worker_alive:
                log_event(
                    logger,
                    logging.ERROR,
                    "worker still busy at shutdown; giving up waiting (its label "
                    "is recorded as in flight and will go to check-printer/ if "
                    "seen again)",
                    in_progress=str(self._current) if self._current else None,
                )
        self._hold_back_unseen()
        with self._pending_lock:
            unfinished = sorted(path.name for path in self._first_seen)
        if unfinished:
            log_event(
                logger,
                logging.WARNING,
                "files not processed before shutdown; they will be processed at "
                "the next start",
                files=unfinished,
            )
        self._save_state(clean=not worker_alive)
        log_event(logger, logging.INFO, "watcher stopped", stats=self.stats)

    def _hold_back_unseen(self) -> None:
        """Final scan after the observer stopped: hold the checkpoint back for
        every candidate file that arrived but was never queued or decided
        (events still inside watchdog at stop are lost), so the next start
        processes it."""
        late = []
        for path in self.existing_files():
            if is_temp_name(path) or matches_globs(path, self.config.globs) is None:
                continue
            signature = file_signature(path)
            if signature is None:
                continue
            if self._decided.get(path) == signature:
                continue
            if path in self._startup_files and self._startup_files[path] == signature:
                continue
            with self._pending_lock:
                if path in self._first_seen:
                    continue
                when = min(arrival_time(path) or time.time(), time.time())
                self._first_seen[path] = when
            late.append({"file": path.name, "arrived": iso(when)})
        log_event(
            logger,
            logging.WARNING if late else logging.DEBUG,
            "final scan after stopping the observer: files never processed (the "
            "next start handles them)",
            files=late,
        )

    def observer_alive(self) -> bool:
        """True while the folder observer and all of its emitter threads run."""
        observer = self._observer
        if observer is None or not observer.is_alive():
            return False
        emitters = list(getattr(observer, "emitters", ()))
        return bool(emitters) and all(emitter.is_alive() for emitter in emitters)

    def wait_idle(self, timeout_s: float = 30.0) -> bool:
        """Block until nothing is queued or being processed (used by tests)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._pending_lock:
                if not self._pending and not self._retry_queued:
                    return True
            time.sleep(0.05)
        return False

    def process_existing_now(self) -> list[FileOutcome]:
        """Process every file in the folder, then to-print/, on this thread.

        Used by ``--once``: content reservations are read and written (so a
        label is never printed twice across runs), but the Downloads
        checkpoint is left alone unless this watcher was built with
        ``checkpoint=True``.
        """
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.load_state()
        files = self.existing_files()
        log_event(
            logger,
            logging.INFO,
            "processing existing files once",
            count=len(files),
            files=[path.name for path in files],
        )
        outcomes = [self.process_path(path) for path in files]
        outcomes.extend(self.retry_queue_once("--once"))
        self._save_state(clean=True)
        return outcomes

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
        """Queue a Downloads file for the worker unless it is already pending.

        An event for the file currently being processed marks it dirty, so it
        is looked at again once the current pass finishes.
        """
        if not force and path in self._startup_files:
            if file_signature(path) == self._startup_files[path]:
                log_event(
                    logger,
                    logging.DEBUG,
                    "ignored: unchanged since startup",
                    event=reason,
                    file=path.name,
                )
                return False
            del self._startup_files[path]
        arrived = min(arrival_time(path) or time.time(), time.time())
        with self._pending_lock:
            # The arrival time, not "now": the checkpoint must never pass a
            # file that was seen but not processed (it may be days old when a
            # backlog is queued at startup).
            self._first_seen.setdefault(path, arrived)
            if not self._accepting:
                log_event(
                    logger,
                    logging.INFO,
                    "shutting down: not queued (will be handled at next start)",
                    event=reason,
                    file=path.name,
                )
                return False
            if path in self._pending:
                if path == self._current:
                    self._dirty.add(path)
                    log_event(
                        logger,
                        logging.DEBUG,
                        "changed while being processed; will look again after",
                        event=reason,
                        file=path.name,
                    )
                else:
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

    def request_retry(self, reason: str) -> bool:
        """Ask the worker to retry to-print/ (and re-queue deferred downloads)."""
        with self._pending_lock:
            if not self._accepting:
                return False
            deferred = list(self._deferred)
        for path in deferred:
            if path.exists():
                self.enqueue(path, "deferred retry")
            else:
                self._forget(path)
        with self._pending_lock:
            if self._retry_queued:
                log_event(logger, logging.DEBUG, "retry already queued", reason=reason)
                return False
            self._retry_queued = True
            self._retry_reason = reason
        log_event(logger, logging.DEBUG, "retry of to-print queued", reason=reason)
        self._queue.put(RETRY)
        return True

    def _retry_loop(self) -> None:
        interval = max(self.config.retry_seconds, 1.0)
        log_event(logger, logging.INFO, "retry timer started", interval_s=interval)
        while not self.stop_event.wait(interval):
            self.request_retry("timer")
        log_event(logger, logging.INFO, "retry timer exiting")

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
            if isinstance(item, _RetryRequest):
                with self._pending_lock:
                    self._retry_queued = False
                    reason = self._retry_reason
                if self.stop_event.is_set():
                    continue
                try:
                    self.retry_queue_once(reason)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.exception("unexpected error retrying to-print")
                continue
            self._work_on(item)
        log_event(logger, logging.INFO, "worker exiting")

    def _work_on(self, item: Path) -> None:
        if self.stop_event.is_set():
            log_event(
                logger,
                logging.INFO,
                "shutdown: not processing (will be handled at next start)",
                file=item.name,
            )
            with self._pending_lock:
                self._pending.discard(item)
            return
        with self._pending_lock:
            self._current = item
        try:
            self.process_path(item)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("unexpected error processing %s", item)
        finally:
            with self._pending_lock:
                self._current = None
                self._pending.discard(item)
                dirty = item in self._dirty
                self._dirty.discard(item)
        if dirty and item.exists():
            self.enqueue(item, "changed while processing")

    def _finish(self, outcome: FileOutcome, level: int = logging.INFO) -> FileOutcome:
        """Count, remember and log a Downloads file's outcome."""
        self._count(outcome.status)
        if outcome.status in _DECIDED:
            signature = file_signature(outcome.path)
            if signature is not None:
                self._decided[outcome.path] = signature
        if outcome.status not in _UNFINISHED:
            self._forget(outcome.path)
        log_event(logger, level, f"file outcome: {outcome.status}", **outcome.to_log())
        self._save_state(clean=False)
        return outcome

    def _defer(self, outcome: FileOutcome) -> FileOutcome:
        """A transient problem: look at the file again on a later retry tick."""
        with self._pending_lock:
            tries = self._deferred.get(outcome.path, 0) + 1
            self._deferred[outcome.path] = tries
        if tries <= MAX_DEFERRED_RETRIES:
            log_event(
                logger,
                logging.WARNING,
                "will look at this file again on a later retry tick",
                file=outcome.path.name,
                deferred_try=tries,
                of=MAX_DEFERRED_RETRIES,
                retry_seconds=self.config.retry_seconds,
            )
            return self._finish(outcome, logging.WARNING)
        log_event(
            logger,
            logging.ERROR,
            "giving up on this file after repeated transient problems",
            file=outcome.path.name,
            tries=tries,
        )
        self._notify_once(
            outcome.path,
            "unreadable",
            messages.unreadable(outcome.path, outcome.detail),
        )
        final = FileOutcome(
            "gave_up", outcome.path, outcome.detail, sha256=outcome.sha256
        )
        return self._finish(final, logging.ERROR)

    def process_path(  # pylint: disable=too-many-return-statements
        self, path: Path
    ) -> FileOutcome:
        """Run every check on one Downloads file and print it if it is a label."""
        log_event(logger, logging.INFO, "considering file", file=str(path))
        if path.parent != self.watch_dir:
            return self._finish(FileOutcome("ignored", path, "not in watch folder"))
        if is_temp_name(path):
            return self._finish(FileOutcome("ignored", path, "temporary download"))
        if not path.is_file():
            return self._finish(FileOutcome("missing", path, "gone or not a file"))

        signature = file_signature(path)
        if signature is not None and self._decided.get(path) == signature:
            log_event(
                logger,
                logging.DEBUG,
                "unchanged since last decision; skipping",
                file=path.name,
            )
            self._forget(path)
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

        in_progress = download_in_progress(path)
        if in_progress is not None:
            # The browser's final rename/write fires a fresh event; don't block.
            return self._finish(FileOutcome("in_progress", path, in_progress))

        loaded = self._read_stable(path)
        if isinstance(loaded, FileOutcome):
            if loaded.status in ("unstable", "unreadable"):
                return self._defer(loaded)
            return self._finish(loaded, logging.INFO)
        data, digest, size = loaded

        with attempt("watcher label", file=str(path), sha256=digest, size_bytes=size):
            return self._process_candidate(path, digest, data)

    def _process_candidate(  # pylint: disable=too-many-return-statements
        self, path: Path, digest: str, data: bytes
    ) -> FileOutcome:
        reserved = self._check_reservation(path, digest)
        if reserved is not None:
            return self._finish(reserved, logging.WARNING)

        decoded = self._decode(path, digest, data)
        if isinstance(decoded, FileOutcome):
            return self._finish(decoded, logging.WARNING)
        loaded, decision = decoded
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
            return self._finish(self._dry_run(path, digest, loaded.image, job_name))
        if self.stop_event.is_set():
            return self._finish(
                FileOutcome("shutdown", path, "shutting down; not printed")
            )

        submission = self._submit(path, digest, loaded.image, job_name)
        return self._file_download(path, digest, submission)

    def _file_download(
        self, path: Path, digest: str, submission: Submission
    ) -> FileOutcome:
        """Move a Downloads file to the folder that matches what happened."""
        filed = self._file_common(path, digest, submission)
        if filed is not None:
            return self._finish(
                filed, logging.INFO if filed.status == "printed" else logging.ERROR
            )
        moved_to = self.move_to(path, TO_PRINT_DIR)
        self._notify_once(
            moved_to or path,
            "did_not_print",
            messages.did_not_print(
                path,
                moved_to,
                submission.error,
                self.to_print_dir,
                reason=submission.reason,
            ),
        )
        note = (
            "; queued in to-print/ for automatic retry"
            if moved_to
            else "; could not be moved to to-print/"
        )
        return self._finish(
            FileOutcome(
                "to_print",
                path,
                submission.error + note,
                moved_to=moved_to,
                sha256=digest,
            ),
            logging.ERROR,
        )

    def log_discovery(self, reason: str) -> None:
        """Log a full printer discovery snapshot."""
        discovery = self._discover(reason)
        if discovery is None:
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

    def check_observer(self) -> bool:
        """Restart the folder observer if it or any emitter thread has died.

        Returns True if the observer was healthy.
        """
        if self._observer is None or self.stop_event.is_set():
            return True
        if self.observer_alive():
            self._observer_ok_since = time.time()
            return True
        emitters = list(getattr(self._observer, "emitters", ()))
        log_event(
            logger,
            logging.CRITICAL,
            "folder observer has died; new downloads are NOT being seen; restarting it",
            observer_alive=self._observer.is_alive(),
            emitters=[
                {"name": emitter.name, "alive": emitter.is_alive()}
                for emitter in emitters
            ],
            healthy_since=iso(self._observer_ok_since),
        )
        self._restart_observer()
        return False

    def _restart_observer(self) -> None:
        # pylint: disable=import-outside-toplevel
        from watchdog.observers import Observer

        old, since = self._observer, self._observer_ok_since
        try:
            old.stop()
            old.join(2.0)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.WARNING,
                "stopping dead observer failed",
                error=describe_exception(exc),
            )
        try:
            observer = Observer()
            observer.schedule(_EventHandler(self), str(self.watch_dir), recursive=False)
            observer.daemon = True
            observer.start()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.CRITICAL,
                "could not restart folder observer; will try again at next heartbeat",
                error=describe_exception(exc),
            )
            return
        self._observer = observer
        self._observer_ok_since = time.time()
        rescanned = []
        for path in self.existing_files():
            if (arrival_time(path) or 0.0) >= since - ARRIVAL_SLACK_S:
                if self.enqueue(path, "observer restart rescan"):
                    rescanned.append(path.name)
        log_event(
            logger,
            logging.WARNING,
            "folder observer restarted",
            rescanned=rescanned,
            since=iso(since),
        )

    def heartbeat_loop(self) -> None:
        """Every ``heartbeat_minutes``: liveness, stats, state and a printer snapshot."""
        interval = max(self.config.heartbeat_minutes, 0.05) * 60.0
        while not self.stop_event.wait(interval):
            self.heartbeat()

    def heartbeat(self) -> None:
        """One heartbeat (see ``heartbeat_loop``)."""
        log_event(
            logger,
            logging.INFO,
            "heartbeat",
            observer_alive=self.observer_alive(),
            worker_alive=self._worker is not None and self._worker.is_alive(),
            queue_size=self._queue.qsize(),
            waiting_in_to_print=len(self.queued_files()),
            stats=self.stats,
        )
        self.check_observer()
        self._save_state(clean=False)
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
