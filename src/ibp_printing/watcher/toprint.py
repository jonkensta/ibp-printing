"""Retrying the labels that wait in <watch_dir>/to-print/."""

import logging
from pathlib import Path

from ibp_printing.log import attempt, get_logger, log_event
from ibp_printing.paths import (
    CHECK_PRINTER_DIR,
    PARTIAL_SUFFIX,
)
from ibp_printing.watcher import messages
from ibp_printing.watcher.core import (
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
    """Retries labels waiting in ``<watch_dir>/to-print/``.

    shippy and shippy-gui save a label there when no printer could take it, and
    the watcher moves its own definite failures there. Every ``retry_seconds``
    the worker checks the folder and, if discovery finds a usable printer,
    prints the files oldest first.
    """

    def queued_files(self) -> list[Path]:
        """Files waiting in to-print/, oldest first (half-written ones skipped)."""
        folder = self.to_print_dir
        if not folder.is_dir():
            return []
        files = []
        for path in self.existing_files(folder):
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
        files = self.queued_files()
        if not files:
            log_event(logger, logging.DEBUG, "to-print is empty", reason=reason)
            return []
        discovery = self._discover("to-print retry")
        if discovery is None:
            return []
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
            return []
        log_event(
            logger,
            logging.INFO,
            "printing labels waiting in to-print",
            reason=reason,
            waiting=[path.name for path in files],
            usable_printers=usable,
        )
        outcomes = []
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
            if outcome.status in ("still_queued", "check_printer"):
                log_event(
                    logger,
                    logging.WARNING,
                    "stopping this pass through to-print after a failure",
                    remaining=len(files) - len(outcomes),
                )
                break
        return outcomes

    def _queued_outcome(
        self, outcome: FileOutcome, level: int = logging.INFO
    ) -> FileOutcome:
        self._count(f"queued_{outcome.status}")
        if outcome.status in ("unsupported", "not_matching", "dry_run"):
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
            return self._queued_outcome(loaded, logging.WARNING)
        data, digest, size = loaded
        with attempt(
            "watcher queued label", file=str(path), sha256=digest, size_bytes=size
        ):
            return self._print_queued(path, digest, data)

    def _print_queued(self, path: Path, digest: str, data: bytes) -> FileOutcome:
        if digest in self._prior_in_flight:
            outcome = self._file_interrupted(path, digest)
            return self._queued_outcome(outcome, logging.WARNING)
        age = self._recent_submission(digest)
        if age is not None or digest in self._stuck_hashes:
            age = age or 0.0
            log_event(
                logger,
                logging.WARNING,
                "queued label matches one just sent to a printer; moving it to "
                "check-printer/ instead of printing a second copy",
                file=path.name,
                age_s=round(age, 1),
            )
            moved_to = self.move_to(path, CHECK_PRINTER_DIR)
            self._notify_once(
                moved_to or path,
                "duplicate",
                messages.queued_duplicate(path, moved_to, age),
            )
            return self._queued_outcome(
                FileOutcome(
                    "duplicate",
                    path,
                    f"identical content sent {age:.1f}s ago",
                    moved_to=moved_to,
                    sha256=digest,
                ),
                logging.WARNING,
            )
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
        self._release_in_flight(digest)
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
