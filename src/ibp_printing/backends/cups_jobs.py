"""Following a CUPS job, for :func:`ibp_printing.jobs.track_job`.

``track_job`` classifies Windows ``JOB_STATUS_*`` bits, so a CUPS job state
(and the queue's state) is translated into those bits:

=====================  ================================  ====================
CUPS job-state         bits                              track_job outcome
=====================  ================================  ====================
3 pending              none ("QUEUED")                   keeps waiting
4 pending-held         PAUSED                            keeps waiting
5 processing           PRINTING                          keeps waiting
6 processing-stopped   ERROR                             ERROR after 10 s
7 canceled             DELETED                           DELETED
8 aborted              ERROR, then gone                  ERROR
9 completed            PRINTED | COMPLETE                COMPLETED
=====================  ================================  ====================

A queue that CUPS has stopped (``printer-state`` 5, ``lpstat``: "disabled",
which is what CUPS does by default when the printer fails, the job staying
pending) also sets ERROR, so a stopped printer ends in ERROR after 10 s
rather than waiting for the timeout.

With pycups the job is read with ``getJobAttributes``. Without it ``lpstat``
is used: ``lpstat -o QUEUE`` lists the queue's unfinished jobs and
``lpstat -p QUEUE`` says whether the queue is printing that job or is
disabled. ``lpstat`` cannot tell a completed job from a canceled or aborted
one, so without pycups a job that leaves the queue counts as COMPLETED, as a
Windows job that leaves the spooler does.
"""

import re
import subprocess
from typing import Any, Callable, Optional

from ibp_printing.models import JobSnapshot

PAUSED = 0x00000001
ERROR = 0x00000002
PRINTING = 0x00000010
DELETED = 0x00000100
DONE = 0x00000080 | 0x00001000  # PRINTED | COMPLETE

JOB_STATE_NAMES = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}
_JOB_STATE_BITS = {
    3: 0,
    4: PAUSED,
    5: PRINTING,
    6: ERROR,
    7: DELETED,
    8: ERROR,
    9: DONE,
}
JOB_ABORTED = 8
PRINTER_STOPPED = 5
# IPP status-code client-error-not-found.
IPP_NOT_FOUND = 0x0406

_REQUEST_ID = re.compile(r"request id is (\S+)-(\d+)")

Runner = Callable[[list[str]], str]


def job_id_from_lp(stdout: str) -> Optional[int]:
    """The job id in ``lp``'s "request id is QUEUE-8032 (1 file(s))"."""
    match = _REQUEST_ID.search(stdout)
    return int(match.group(2)) if match else None


def job_bits(job_state: int, printer_stopped: bool = False) -> int:
    """The ``JOB_STATUS_*`` bits for a CUPS job state (see the module table)."""
    bits = _JOB_STATE_BITS.get(job_state, 0)
    if printer_stopped and job_state in (3, 4, 5):
        bits |= ERROR
    return bits


def _run_lpstat(args: list[str]) -> str:
    proc = subprocess.run(
        ["lpstat", *args], capture_output=True, text=True, timeout=10, check=False
    )
    return proc.stdout


class CupsJobPoller:  # pylint: disable=too-few-public-methods
    """``poll()`` for one CUPS job: its snapshot, or None once it is gone."""

    def __init__(
        self,
        printer_name: str,
        job_id: int,
        job_name: str,
        *,
        connection: Any = None,
        run: Runner = _run_lpstat,
    ) -> None:
        """``connection`` is a ``cups.Connection``; None uses ``lpstat`` (``run``)."""
        self.printer_name = printer_name
        self.job_id = job_id
        self.job_name = job_name
        self.connection = connection
        self.run = run
        self._aborted_seen = False

    def __call__(self) -> Optional[JobSnapshot]:
        if self._aborted_seen:
            # Reported once as ERROR; "gone" now makes track_job end in ERROR
            # instead of waiting out the error grace period for a dead job.
            return None
        if self.connection is not None:
            snapshot = self._poll_ipp()
        else:
            snapshot = self._poll_lpstat()
        return snapshot

    def _snapshot(self, bits: int, text: str) -> JobSnapshot:
        return JobSnapshot(
            job_id=self.job_id, document=self.job_name, status=bits, status_text=text
        )

    def _poll_ipp(self) -> Optional[JobSnapshot]:
        try:
            job = self.connection.getJobAttributes(
                self.job_id,
                requested_attributes=[
                    "job-state",
                    "job-state-reasons",
                    "job-printer-state-message",
                ],
            )
        except Exception as exc:
            if _ipp_status(exc) == IPP_NOT_FOUND:
                return None
            raise
        state = int(job.get("job-state", 0))
        printer = self.connection.getPrinterAttributes(
            self.printer_name,
            requested_attributes=["printer-state", "printer-state-message"],
        )
        stopped = int(printer.get("printer-state", 0)) == PRINTER_STOPPED
        reasons = job.get("job-state-reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        parts = [JOB_STATE_NAMES.get(state, f"state {state}")]
        parts += [str(reason) for reason in reasons if reason != "none"]
        message = job.get("job-printer-state-message") or ""
        if stopped:
            message = message or printer.get("printer-state-message") or ""
            parts.append("printer stopped")
        if message:
            parts.append(str(message))
        if state == JOB_ABORTED:
            self._aborted_seen = True
        return self._snapshot(job_bits(state, stopped), "; ".join(parts))

    def _poll_lpstat(self) -> Optional[JobSnapshot]:
        request = f"{self.printer_name}-{self.job_id}"
        jobs = self.run(["-o", self.printer_name])
        if not any(line.split()[:1] == [request] for line in jobs.splitlines()):
            return None
        status = self.run(["-p", self.printer_name])
        first, _, rest = status.partition("\n")
        if " disabled" in first:
            detail = rest.strip().splitlines()[0].strip() if rest.strip() else ""
            text = "pending; printer disabled" + (f"; {detail}" if detail else "")
            return self._snapshot(ERROR, text)
        if f"now printing {request}" in first:
            return self._snapshot(PRINTING, "processing")
        return self._snapshot(0, "pending")


def _ipp_status(exc: BaseException) -> Optional[int]:
    """The IPP status code of a ``cups.IPPError`` (``args[0]``), if any."""
    if type(exc).__name__ != "IPPError" or not exc.args:
        return None
    try:
        return int(exc.args[0])
    except (TypeError, ValueError):
        return None
