"""Retrying the labels that wait in <watch_dir>/to-print/."""

import logging
from pathlib import Path

from ibp_printing.log import attempt, get_logger, log_event
from ibp_printing.paths import PARTIAL_SUFFIX
from ibp_printing.watcher import messages
from ibp_printing.watcher.core import (
    MAX_DEFERRED_RETRIES,
    FileOutcome,
    Submission,
    WatcherCore,
    file_signature,
)
from ibp_printing.watcher.detect import (
    download_in_progress,
    is_temp_name,
    matches_globs,
)

logger = get_logger(__name__)


class ToPrintQueue(WatcherCore):
    """Retries labels waiting in the to-print folders.

    ``<watch_dir>/to-print/`` and, when different, ``paths.to_print_dir()``
    (where shippy and shippy-gui save a label no printer could take). The
    watcher moves its own definite failures into the first. Every
    ``retry_seconds`` the worker checks the folders and, if discovery finds a
    usable printer, prints the files oldest first.
    """

    def queued_files(self) -> list[Path]:
        """Files waiting in the to-print folders, oldest first (half-written ones
        skipped)."""
        found = []
        for folder in self.to_print_dirs:
            try:
                if not folder.is_dir():
                    continue
            except OSError:
                continue
            found.extend(self.existing_files(folder))
        found.sort(key=lambda p: (file_signature(p) or (0, 0))[1])
        files = []
        for path in found:
            if path.name.endswith(PARTIAL_SUFFIX) or is_temp_name(path):
                log_event(
                    logger,
                    logging.DEBUG,
                    "to-print: skipping partial/temporary file",
                    file=path.name,
                )
                continue
            signature = file_signature(path)
            if signature is not None and self._decided.get(path) == signature:
                continue
            files.append(path)
        return files

    def retry_queue_once(self, reason: str) -> list[FileOutcome]:
        """Print what is waiting in to-print/ if a usable printer exists.

        Runs on the worker thread (or the main thread for ``--once``). Stops at
        the first label that fails, so a broken printer is not fed the whole
        queue.
        """
        self._notified = {key for key in self._notified if Path(key[0]).exists()}
        # First, move labels that printed earlier but could not be filed.
        outcomes = self.reconcile_filing()
        files = self.queued_files()
        if not files:
            log_event(logger, logging.DEBUG, "to-print is empty", reason=reason)
            return outcomes
        discovery = self._discover("to-print retry")
        if discovery is None:
            return outcomes
        usable = [candidate.name for candidate in discovery.usable]
        available = bool(usable)
        changed = available != self._printers_available
        self._printers_available = available
        if not available and not self.config.dry_run:
            log_event(
                logger,
                logging.INFO if changed else logging.DEBUG,
                "labels waiting in to-print, but no usable printer; will retry",
                waiting=[path.name for path in files],
                retry_seconds=self.config.retry_seconds,
                discovery_errors=discovery.errors,
            )
            return outcomes
        log_event(
            logger,
            logging.INFO,
            "printing labels waiting in to-print",
            reason=reason,
            waiting=[path.name for path in files],
            usable_printers=usable,
        )
        attempted = 0
        for path in files:
            if self.stop_event.is_set():
                log_event(logger, logging.INFO, "shutdown: leaving to-print as is")
                break
            try:
                outcome = self.process_queued(path)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("unexpected error processing queued %s", path)
                break
            outcomes.append(outcome)
            attempted += 1
            if outcome.status in ("still_queued", "check_printer"):
                log_event(
                    logger,
                    logging.WARNING,
                    "stopping this pass through to-print after a failure",
                    remaining=len(files) - attempted,
                )
                break
        return outcomes

    def _queued_outcome(
        self, outcome: FileOutcome, level: int = logging.INFO
    ) -> FileOutcome:
        self._count(f"queued_{outcome.status}")
        if outcome.status in ("unsupported", "not_matching", "dry_run", "gave_up") or (
            # Already sent to a printer but stuck here: reconcile_filing() retries
            # the move each tick; scanning it again would only repeat that.
            outcome.moved_to is None
            and outcome.status
            in ("duplicate", "move_pending", "printed", "check_printer")
        ):
            signature = file_signature(outcome.path)
            if signature is not None:
                self._decided[outcome.path] = signature
        log_event(
            logger, level, f"queued file outcome: {outcome.status}", **outcome.to_log()
        )
        return outcome

    def process_queued(  # pylint: disable=too-many-return-statements
        self, path: Path
    ) -> FileOutcome:
        """Print one file from to-print/ and file it by outcome."""
        log_event(logger, logging.INFO, "considering queued file", file=str(path))
        if not path.is_file():
            return self._queued_outcome(FileOutcome("missing", path, "gone"))
        if matches_globs(path, self.config.globs) is None:
            reason = f"name matches none of {self.config.globs}"
            self._notify_once(
                path, "unprintable", messages.queued_unprintable(path, reason)
            )
            return self._queued_outcome(
                FileOutcome("not_matching", path, reason), logging.WARNING
            )
        in_progress = download_in_progress(path)
        if in_progress is not None:
            return self._queued_outcome(FileOutcome("in_progress", path, in_progress))
        loaded = self._read_stable(path)
        if isinstance(loaded, FileOutcome):
            if loaded.status in ("unstable", "unreadable"):
                return self._defer_queued(loaded)
            return self._queued_outcome(loaded, logging.WARNING)
        self._queued_deferred.pop(path, None)
        data, digest, size = loaded
        with attempt(
            "watcher queued label", file=str(path), sha256=digest, size_bytes=size
        ):
            return self._print_queued(path, digest, data)

    def _defer_queued(self, outcome: FileOutcome) -> FileOutcome:
        """A locked or still-changing to-print file: retry on a few later ticks,
        then tell the volunteer once and leave it alone until it changes."""
        path = outcome.path
        tries = self._queued_deferred.get(path, 0) + 1
        self._queued_deferred[path] = tries
        if tries <= MAX_DEFERRED_RETRIES:
            log_event(
                logger,
                logging.WARNING,
                "will look at this queued file again on a later retry tick",
                file=str(path),
                deferred_try=tries,
                of=MAX_DEFERRED_RETRIES,
                retry_seconds=self.config.retry_seconds,
            )
            return self._queued_outcome(outcome, logging.WARNING)
        self._queued_deferred.pop(path, None)
        log_event(
            logger,
            logging.ERROR,
            "giving up on this queued file after repeated transient problems; "
            "it is retried once it changes (or is renamed)",
            file=str(path),
            tries=tries,
        )
        self._notify_once(
            path, "unreadable", messages.queued_unreadable(path, outcome.detail)
        )
        return self._queued_outcome(
            FileOutcome("gave_up", path, outcome.detail), logging.ERROR
        )

    def _print_queued(self, path: Path, digest: str, data: bytes) -> FileOutcome:
        reserved = self._check_reservation(path, digest)
        if reserved is not None:
            return self._queued_outcome(reserved, logging.WARNING)
        decoded = self._decode(path, digest, data)
        if isinstance(decoded, FileOutcome):
            self._notify_once(
                path, "unprintable", messages.queued_unprintable(path, decoded.detail)
            )
            return self._queued_outcome(decoded, logging.WARNING)
        loaded, decision = decoded
        log_event(
            logger,
            logging.INFO if decision.is_label else logging.WARNING,
            (
                "queued file shape: 4x6 label"
                if decision.is_label
                else "queued file is not 4x6-shaped; printing anyway (it was put "
                "in to-print on purpose)"
            ),
            file=path.name,
            source_format=loaded.source_format,
            load_info=loaded.info,
            **decision.to_log(),
        )
        job_name = f"Queued {path.name}"
        if self.config.dry_run:
            outcome = self._dry_run(path, digest, loaded.image, job_name)
            return self._queued_outcome(outcome)
        if self.stop_event.is_set():
            return self._queued_outcome(FileOutcome("shutdown", path, "not printed"))

        submission = self._submit(path, digest, loaded.image, job_name)
        return self._file_queued(path, digest, submission)

    def _file_queued(
        self, path: Path, digest: str, submission: Submission
    ) -> FileOutcome:
        filed = self._file_common(path, digest, submission)
        if filed is not None:
            level = logging.INFO if filed.status == "printed" else logging.ERROR
            return self._queued_outcome(filed, level)
        self._notify_once(
            path, "retry_failed", messages.retry_failed(path, submission.error)
        )
        return self._queued_outcome(
            FileOutcome(
                "still_queued",
                path,
                submission.error + "; left in to-print/ for the next retry",
                sha256=digest,
            ),
            logging.ERROR,
        )
