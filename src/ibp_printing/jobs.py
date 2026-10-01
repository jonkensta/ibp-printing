"""Follow a spooled job until it leaves the queue, errors, or times out.

``EndDoc`` returning only means the spooler accepted the job. Whether a label
actually came out is only visible by watching the job's status afterwards.
"""

import logging
import time
from typing import Callable, Optional

from ibp_printing.log import get_logger, log_event
from ibp_printing.models import JobOutcome, JobSnapshot, PrintResult
from ibp_printing.winconst import (
    JOB_STATUS_BITS,
    JOB_STATUS_DELETED_MASK,
    JOB_STATUS_DONE_MASK,
    JOB_STATUS_ERROR_MASK,
    decode_bits,
)

logger = get_logger(__name__)

# Errors this long in a row are reported; shorter blips (e.g. a busy USB
# port) often clear on their own.
ERROR_GRACE_S = 10.0


def track_job(
    result: PrintResult,
    poll: Callable[[], Optional[JobSnapshot]],
    timeout_s: float,
    interval_s: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> JobOutcome:
    """Poll the spooler for one job and record each status change.

    Args:
        result: Updated in place with ``job_id``, ``history`` and ``outcome``.
        poll: Returns the job's current snapshot, or None once it is gone.
        timeout_s: How long to wait before giving up (the job is left alone).
    """
    started = clock()
    last_status: Optional[int] = None
    error_since: Optional[float] = None

    while True:
        snapshot = poll()
        now = clock()

        if snapshot is None:
            outcome = outcome_when_gone(last_status)
            break

        result.job_id = snapshot.job_id
        if snapshot.status != last_status:
            _record_change(result, snapshot, now - started)
            last_status = snapshot.status

        if snapshot.status & JOB_STATUS_ERROR_MASK:
            error_since = now if error_since is None else error_since
        else:
            error_since = None

        classified = classify_status(
            snapshot.status,
            error_for_s=None if error_since is None else now - error_since,
            elapsed_s=now - started,
            timeout_s=timeout_s,
        )
        if classified is not None:
            outcome = classified
            break
        sleep(interval_s)

    result.outcome = outcome
    log_event(
        logger,
        logging.INFO if outcome.ok else logging.ERROR,
        f"job outcome: {outcome.value}",
        printer=result.printer_name,
        job_id=result.job_id,
        history=result.history,
        waited_s=round(clock() - started, 2),
    )
    return outcome


def outcome_when_gone(last_status: Optional[int]) -> JobOutcome:
    """Classify a job that has left the queue, given the last status we saw.

    ``None`` means the job was never observed (it printed before the first poll
    or was never queued under the expected name).
    """
    if last_status is None:
        return JobOutcome.VANISHED_UNSEEN
    if last_status & JOB_STATUS_DELETED_MASK:
        return JobOutcome.DELETED
    if last_status & JOB_STATUS_ERROR_MASK:
        return JobOutcome.ERROR
    return JobOutcome.COMPLETED


def classify_status(
    status: int,
    *,
    error_for_s: Optional[float],
    elapsed_s: float,
    timeout_s: float,
) -> Optional[JobOutcome]:
    """Return the final outcome for a job still in the queue, or None to keep waiting.

    Args:
        status: The job's current ``JOB_STATUS_*`` bits.
        error_for_s: How long the job has continuously been in an error state.
        elapsed_s: Time since tracking started.
        timeout_s: Give up after this long.
    """
    if status & JOB_STATUS_DONE_MASK:
        return JobOutcome.COMPLETED
    if status & JOB_STATUS_DELETED_MASK:
        return JobOutcome.DELETED
    if error_for_s is not None and error_for_s >= ERROR_GRACE_S:
        return JobOutcome.ERROR
    if elapsed_s >= timeout_s:
        return JobOutcome.TIMEOUT
    return None


def _record_change(
    result: PrintResult, snapshot: JobSnapshot, elapsed_s: float
) -> None:
    """Append a status change to the result history and log it."""
    flags = decode_bits(snapshot.status, JOB_STATUS_BITS)
    entry = f"+{elapsed_s:.1f}s {','.join(flags) or 'QUEUED'}"
    if snapshot.status_text:
        entry += f" ({snapshot.status_text})"
    result.history.append(entry)
    log_event(
        logger,
        logging.INFO,
        "job status changed",
        printer=result.printer_name,
        job_id=snapshot.job_id,
        status=flags,
        status_text=snapshot.status_text,
        pages=f"{snapshot.pages_printed}/{snapshot.total_pages}",
        t=round(elapsed_s, 2),
    )
