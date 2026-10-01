"""Tests for the spooler job-tracking state machine."""

import unittest
from typing import Optional

from printing_helpers import quiet_logging

from ibp_printing.jobs import ERROR_GRACE_S, outcome_when_gone, track_job
from ibp_printing.models import JobOutcome, JobSnapshot, PrintResult

SPOOLING = 0x0008
PRINTING = 0x0010
ERROR = 0x0002
PAPEROUT = 0x0040
DELETING = 0x0004
PRINTED = 0x0080
COMPLETE = 0x1000


class FakeTime:
    """A clock that only advances when slept on."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        """Current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time."""
        self.sleeps.append(seconds)
        self.now += seconds


def run(
    statuses: list[Optional[int]],
    timeout_s: float = 30.0,
    interval_s: float = 1.0,
    repeat_last: bool = False,
) -> tuple[JobOutcome, PrintResult, FakeTime]:
    """Track a job whose status follows ``statuses`` (None = gone from queue)."""
    fake = FakeTime()
    result = PrintResult(printer_name="DYMO 0922:0028", job_name="Label [abc]")
    script = list(statuses)

    def poll() -> Optional[JobSnapshot]:
        status = script[0] if (repeat_last and len(script) == 1) else script.pop(0)
        if status is None:
            return None
        return JobSnapshot(job_id=42, document="Label [abc]", status=status)

    outcome = track_job(
        result,
        poll,
        timeout_s=timeout_s,
        interval_s=interval_s,
        clock=fake.clock,
        sleep=fake.sleep,
    )
    return outcome, result, fake


class TrackJobTests(unittest.TestCase):
    """track_job."""

    def setUp(self):
        quiet_logging(self)

    def test_completed_when_job_leaves_queue(self):
        outcome, result, _ = run([SPOOLING, PRINTING, PRINTING, None])
        self.assertEqual(outcome, JobOutcome.COMPLETED)
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual(result.job_id, 42)
        # Only changes are recorded.
        self.assertEqual(len(result.history), 2)
        self.assertIn("SPOOLING", result.history[0])
        self.assertIn("PRINTING", result.history[1])

    def test_queued_status_zero_is_named(self):
        _, result, _ = run([0, None])
        self.assertIn("QUEUED", result.history[0])

    def test_vanished_before_first_poll(self):
        outcome, result, fake = run([None])
        self.assertEqual(outcome, JobOutcome.VANISHED_UNSEEN)
        self.assertTrue(outcome.ok)
        self.assertIsNone(result.job_id)
        self.assertEqual(result.history, [])
        self.assertEqual(fake.sleeps, [])

    def test_printed_bit_completes_while_still_queued(self):
        outcome, _, fake = run([PRINTING, PRINTING | PRINTED])
        self.assertEqual(outcome, JobOutcome.COMPLETED)
        self.assertEqual(len(fake.sleeps), 1)

    def test_complete_bit_completes(self):
        outcome, _, _ = run([COMPLETE])
        self.assertEqual(outcome, JobOutcome.COMPLETED)

    def test_deleted(self):
        outcome, _, _ = run([PRINTING, DELETING])
        self.assertEqual(outcome, JobOutcome.DELETED)
        self.assertFalse(outcome.ok)

    def test_outcome_when_gone(self):
        self.assertEqual(outcome_when_gone(None), JobOutcome.VANISHED_UNSEEN)
        self.assertEqual(outcome_when_gone(DELETING | ERROR), JobOutcome.DELETED)
        self.assertEqual(outcome_when_gone(PAPEROUT), JobOutcome.ERROR)
        self.assertEqual(outcome_when_gone(PRINTING), JobOutcome.COMPLETED)
        self.assertEqual(outcome_when_gone(0), JobOutcome.COMPLETED)

    def test_error_must_persist_for_grace_period(self):
        outcome, _, fake = run([ERROR], repeat_last=True, timeout_s=600)
        self.assertEqual(outcome, JobOutcome.ERROR)
        self.assertFalse(outcome.ok)
        self.assertGreaterEqual(sum(fake.sleeps), ERROR_GRACE_S)
        self.assertLess(sum(fake.sleeps), ERROR_GRACE_S + 2)

    def test_error_blip_clears(self):
        # Two 7s error spells: only an uninterrupted spell counts toward the grace.
        statuses = [PAPEROUT] * 8 + [PRINTING] + [PAPEROUT] * 8 + [PRINTING, None]
        outcome, result, _ = run(statuses, timeout_s=600)
        self.assertEqual(outcome, JobOutcome.COMPLETED)
        self.assertEqual(len(result.history), 4)

    def test_error_then_gone_is_error(self):
        outcome, _, _ = run([PRINTING, ERROR, None])
        self.assertEqual(outcome, JobOutcome.ERROR)

    def test_timeout(self):
        outcome, result, fake = run([PRINTING], repeat_last=True, timeout_s=5)
        self.assertEqual(outcome, JobOutcome.TIMEOUT)
        self.assertFalse(outcome.ok)
        self.assertEqual(sum(fake.sleeps), 5)
        self.assertEqual(len(result.history), 1)

    def test_timeout_wins_over_shorter_error_grace(self):
        outcome, _, _ = run([ERROR], repeat_last=True, timeout_s=3)
        self.assertEqual(outcome, JobOutcome.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
