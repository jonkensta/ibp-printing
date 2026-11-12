"""Following CUPS jobs (pycups and lpstat), with fakes."""

import subprocess
import unittest
from unittest import mock

from PIL import Image
from printing_helpers import quiet_logging

from ibp_printing.backends import cups_jobs, linux
from ibp_printing.jobs import track_job
from ibp_printing.models import JobOutcome, PrintResult

QUEUE = "DYMO_LabelWriter_450_Turbo_0922:0021"


class IPPError(Exception):
    """Stands in for ``cups.IPPError`` (matched by name)."""


class FakeConnection:
    """A ``cups.Connection`` whose job states are scripted, one per poll."""

    def __init__(self, states, printer_states=None, reasons="none"):
        self.states = list(states)
        self.printer_states = list(printer_states or [3] * len(self.states))
        self.reasons = reasons
        self.polls = 0

    def getJobAttributes(self, job_id, requested_attributes=None):  # noqa: N802
        """The next scripted state (the last one repeats)."""
        del job_id, requested_attributes
        index = min(self.polls, len(self.states) - 1)
        self.polls += 1
        state = self.states[index]
        if isinstance(state, Exception):
            raise state
        return {"job-state": state, "job-state-reasons": self.reasons}

    def getPrinterAttributes(self, name, requested_attributes=None):  # noqa: N802
        """The printer state matching the last job poll."""
        del name, requested_attributes
        index = min(self.polls - 1, len(self.printer_states) - 1)
        return {
            "printer-state": self.printer_states[index],
            "printer-state-message": "Unable to open device",
        }


class Clock:
    """A fake monotonic clock that advances only when slept."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        """Advance the clock."""
        self.now += seconds


def track(poll, timeout_s=60.0) -> PrintResult:
    """Run track_job on ``poll`` with a fake clock."""
    result = PrintResult(printer_name=QUEUE, job_name="IBP Label [abc]", job_id=12)
    clock = Clock()
    track_job(result, poll, timeout_s=timeout_s, clock=clock, sleep=clock.sleep)
    return result


def poller(connection=None, run=None):
    """A CupsJobPoller for job 12 on QUEUE."""
    kwargs = {"run": run} if run else {}
    return cups_jobs.CupsJobPoller(
        QUEUE, 12, "IBP Label [abc]", connection=connection, **kwargs
    )


class HelperTests(unittest.TestCase):
    """job_id_from_lp and job_bits."""

    def test_job_id_from_lp(self):
        self.assertEqual(
            cups_jobs.job_id_from_lp(f"request id is {QUEUE}-8032 (1 file(s))\n"),
            8032,
        )
        self.assertIsNone(cups_jobs.job_id_from_lp(""))

    def test_stopped_printer_marks_unfinished_jobs_as_error(self):
        self.assertEqual(cups_jobs.job_bits(3), 0)
        self.assertEqual(cups_jobs.job_bits(3, printer_stopped=True), cups_jobs.ERROR)
        self.assertEqual(cups_jobs.job_bits(9, printer_stopped=True), cups_jobs.DONE)


class IppTrackingTests(unittest.TestCase):
    """track_job outcomes from pycups job states."""

    def setUp(self):
        quiet_logging(self)

    def test_completed(self):
        result = track(poller(FakeConnection([3, 5, 5, 9])))
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual(len(result.history), 3)
        self.assertIn("QUEUED (pending)", result.history[0])
        self.assertIn("PRINTING (processing)", result.history[1])
        self.assertIn("PRINTED,COMPLETE (completed)", result.history[2])

    def test_canceled(self):
        result = track(poller(FakeConnection([5, 7])))
        self.assertEqual(result.outcome, JobOutcome.DELETED)

    def test_aborted_ends_at_once_in_error(self):
        connection = FakeConnection([5, 8])
        result = track(poller(connection))
        self.assertEqual(result.outcome, JobOutcome.ERROR)
        self.assertEqual(connection.polls, 2)

    def test_stopped_printer_is_error_after_the_grace_period(self):
        result = track(poller(FakeConnection([3], printer_states=[5])))
        self.assertEqual(result.outcome, JobOutcome.ERROR)
        self.assertIn("printer stopped; Unable to open device", result.history[0])

    def test_still_pending_times_out(self):
        result = track(poller(FakeConnection([3])), timeout_s=5)
        self.assertEqual(result.outcome, JobOutcome.TIMEOUT)

    def test_job_not_found(self):
        result = track(poller(FakeConnection([IPPError(0x0406, "not found")])))
        self.assertEqual(result.outcome, JobOutcome.VANISHED_UNSEEN)

    def test_other_ipp_errors_raise(self):
        with self.assertRaises(IPPError):
            poller(FakeConnection([IPPError(0x0500, "internal")]))()


class LpstatTrackingTests(unittest.TestCase):
    """Without pycups: lpstat -o / -p."""

    def setUp(self):
        quiet_logging(self)

    @staticmethod
    def runner(steps):
        """``run`` answering ``(jobs, printer)`` lpstat outputs, one pair per poll."""
        calls = []

        def run(args):
            poll = sum(1 for call in calls if call[0] == "-o") - (args[0] == "-p")
            calls.append(args)
            jobs, printer = steps[min(poll, len(steps) - 1)]
            return jobs if args[0] == "-o" else printer

        return run

    def test_printing_then_gone_is_completed(self):
        job = f"{QUEUE}-12   ibp   4096   Thu 12 Nov 2026 10:00:00 AM CST\n"
        steps = [
            (job, f"printer {QUEUE} now printing {QUEUE}-12.  enabled since ...\n"),
            ("", f"printer {QUEUE} is idle.  enabled since ...\n"),
        ]
        result = track(poller(run=self.runner(steps)))
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertIn("PRINTING", result.history[0])

    def test_disabled_queue_is_error(self):
        job = f"{QUEUE}-12   ibp   4096   Thu 12 Nov 2026 10:00:00 AM CST\n"
        printer = f"printer {QUEUE} disabled since ...\n\tUnable to open device\n"
        result = track(poller(run=self.runner([(job, printer)])))
        self.assertEqual(result.outcome, JobOutcome.ERROR)
        self.assertIn("printer disabled; Unable to open device", result.history[0])

    def test_other_jobs_do_not_count(self):
        other = f"{QUEUE}-120   ibp   4096   Thu 12 Nov 2026 10:00:00 AM CST\n"
        result = track(poller(run=self.runner([(other, "")])))
        self.assertEqual(result.outcome, JobOutcome.VANISHED_UNSEEN)


class LinuxBackendTrackingTests(unittest.TestCase):
    """LinuxPrinterBackend.print_image with tracking."""

    def setUp(self):
        quiet_logging(self)
        self.backend = linux.LinuxPrinterBackend()
        patcher = mock.patch.object(
            linux.LinuxPrinterBackend,
            "default_media",
            return_value="custom_35.73x88.56mm_35.73x88.56mm",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def print(self, lp_stdout, connection, track_timeout_s=30.0):
        """Print with lp answering ``lp_stdout`` and ``connection`` as pycups."""
        lp = subprocess.CompletedProcess(["lp"], 0, stdout=lp_stdout, stderr="")
        with (
            mock.patch.object(linux.subprocess, "run", return_value=lp),
            mock.patch.object(linux, "_cups_connection", return_value=connection),
        ):
            return self.backend.print_image(
                Image.new("L", (1004, 378), 255),
                QUEUE,
                job_name="IBP Label [abc]",
                track_timeout_s=track_timeout_s,
            )

    def test_completed_job(self):
        result = self.print(
            f"request id is {QUEUE}-12 (1 file(s))\n", FakeConnection([9])
        )
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual(result.job_id, 12)

    def test_untracked_print_still_records_the_job_id(self):
        result = self.print(
            f"request id is {QUEUE}-12 (1 file(s))\n", None, track_timeout_s=0
        )
        self.assertEqual((result.outcome, result.job_id), (JobOutcome.NOT_TRACKED, 12))

    def test_tracking_error_is_tracking_failed_not_print_error(self):
        connection = FakeConnection([IPPError(0x0500, "server error")])
        result = self.print(f"request id is {QUEUE}-12 (1 file(s))\n", connection)
        self.assertEqual(result.outcome, JobOutcome.TRACKING_FAILED)

    def test_no_job_id_is_not_tracked(self):
        result = self.print("", FakeConnection([9]))
        self.assertEqual(result.outcome, JobOutcome.NOT_TRACKED)
        self.assertIn("lp did not report a job id; not tracked", result.history)


if __name__ == "__main__":
    unittest.main()
