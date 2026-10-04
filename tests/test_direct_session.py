"""Tests for the PM2411BT direct print session (outcome mapping)."""

import unittest
from unittest import mock
from typing import Any, Callable, Optional

from PIL import Image
from printing_helpers import quiet_logging

from ibp_printing.backends.base import PrintError
from ibp_printing.direct import tspl
from ibp_printing.direct.session import (
    PAPER_OUT_HINT,
    DirectPrintError,
    SessionOutcome,
    SessionTimeouts,
    print_label,
    probe_status,
)
from ibp_printing.models import JobOutcome

WHITE = b"\xff" * tspl.RASTER_SIZE
JOB = tspl.build_job(WHITE)
CAP = b"SSSGETCAP\r\n"
PAPER = b"SSSGETPAPER\r\n"


class VirtualClock:
    """A monotonic clock the fake transport advances."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeDeviceGone(Exception):
    """Stands in for transport.DeviceGone (carries ``bytes_accepted``)."""

    def __init__(self, bytes_accepted: int) -> None:
        super().__init__("device gone")
        self.bytes_accepted = bytes_accepted


COVER_REPLY = "<cover reply>"  # resolved to the cover state when delivered


class ScriptedPrinter:  # pylint: disable=too-many-instance-attributes
    """A PM2411BT model on a virtual clock, honouring the Transport contract.

    ``read_lines`` returns lines that are due within the timeout (advancing the
    clock to the first one) or advances the clock by the whole timeout.
    Behaviour knobs: cover state (``cover_change`` moves it at a given time,
    pushing SSSGETCAP and the realign DOING/DONE like the real printer), reply
    delay, job reactions, and write acceptance (``accept(data) -> int`` or an
    exception to raise).
    """

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        # (due time, sequence, line or None, action or None)
        self.pending: list[tuple[float, int, Optional[str], Any]] = []
        self._seq = 0
        self.writes: list[bytes] = []
        self.write_timeouts: list[float] = []
        self.cover = "CLOSE"
        self.answer_queries = True
        self.reply_delay_s = 0.1
        self.doing_after_s: Optional[float] = 1.0
        self.done_after_s: Optional[float] = 3.0
        self.job_replies: list[tuple[float, str]] = []
        self.accept: Callable[[bytes], int] = len
        self.write_raises: list[BaseException] = []
        self.read_raises: Optional[BaseException] = None
        self.delivered: list[tuple[float, str]] = []
        self.write_times: list[tuple[float, bytes]] = []
        # Called when the whole job has been accepted (to script reactions).
        self.on_job: Optional[Callable[[], None]] = None

    def _schedule(self, after_s: float, line: Optional[str], action: Any = None):
        self._seq += 1
        self.pending.append((self.clock.now + after_s, self._seq, line, action))
        self.pending.sort(key=lambda item: (item[0], item[1]))

    def push(self, after_s: float, *lines: str) -> None:
        """Lines the printer sends ``after_s`` from now."""
        for line in lines:
            self._schedule(after_s, line)

    def cover_change(
        self,
        after_s: float,
        value: str,
        *,
        realign_s: Optional[float] = 2.0,
        push: bool = True,
    ) -> None:
        """The cover moves to ``value`` ``after_s`` from now.

        The printer pushes ``SSSGETCAP:<value>`` (unless ``push`` is False)
        and, after a close, realigns: DOING at once, DONE ``realign_s`` later
        (None: the realign never reports DONE).
        """

        def change() -> None:
            self.cover = value

        self._schedule(after_s, f"SSSGETCAP:{value}" if push else None, change)
        if value == "CLOSE":
            self._schedule(after_s + 0.05, "SSSGETPRINTING:DOING")
            if realign_s is not None:
                self._schedule(after_s + realign_s, "SSSGETPRINTING:DONE")

    # Transport API -------------------------------------------------------

    def write_all(self, data: bytes, timeout_s: float) -> int:
        """Accept (part of) ``data`` and schedule the printer's reaction."""
        self.write_timeouts.append(timeout_s)
        if self.write_raises:
            raise self.write_raises.pop(0)
        accepted = self.accept(data)
        self.writes.append(data[:accepted])
        self.write_times.append((self.clock.now, data[:accepted]))
        if accepted < len(data):
            self.clock.now += timeout_s  # stalled for the whole timeout
        sent = data[:accepted]
        body = sent.lstrip(b"\r\n")
        if accepted == len(data) and self.answer_queries:
            if body == CAP:
                self.push(self.reply_delay_s, COVER_REPLY)
            elif body == PAPER:
                self.push(self.reply_delay_s, "SSSGETPAPER:YES")
        if sent.startswith(b"\r\nSIZE") and accepted == len(data):
            if self.doing_after_s is not None:
                self.push(self.doing_after_s, "SSSGETPRINTING:DOING")
            if self.done_after_s is not None:
                self.push(self.done_after_s, "SSSGETPRINTING:DONE")
            for after, line in self.job_replies:
                self.push(after, line)
            if self.on_job is not None:
                self.on_job()
        return accepted

    def read_lines(self, timeout_s: float) -> list[str]:
        """Due lines within ``timeout_s`` (returns as soon as one is due)."""
        if self.read_raises is not None:
            raise self.read_raises
        deadline = self.clock.now + timeout_s
        due_lines: list[str] = []
        while not due_lines:
            if not self.pending or self.pending[0][0] > deadline:
                self.clock.now = deadline
                return []
            self.clock.now = max(self.clock.now, self.pending[0][0])
            while self.pending and self.pending[0][0] <= self.clock.now:
                _, _, line, action = self.pending.pop(0)
                if action is not None:
                    action()
                if line == COVER_REPLY:
                    line = f"SSSGETCAP:{self.cover}"
                if line is not None:
                    due_lines.append(line)
                    self.delivered.append((self.clock.now, line))
        return due_lines

    def close(self) -> None:
        """Nothing to release."""

    # helpers ---------------------------------------------------------------

    @property
    def job_writes(self) -> list[bytes]:
        """Writes that carried (part of) the job."""
        return [data for data in self.writes if data.startswith(b"\r\nSIZE")]


class SessionTestCase(unittest.TestCase):
    """Shared fixture: a virtual clock and a well-behaved printer."""

    def setUp(self):
        quiet_logging(self)
        self.clock = VirtualClock()
        self.printer = ScriptedPrinter(self.clock)

    def run_print(self, payload=WHITE, **overrides):
        """print_label on the fake printer with the virtual clock."""
        return print_label(
            self.printer, payload, job_name="test label", clock=self.clock, **overrides
        )

    def assert_not_sent(self, reason: str) -> DirectPrintError:
        """print_label raises DirectPrintError(reason) and sends no job."""
        with self.assertRaises(DirectPrintError) as caught:
            self.run_print()
        self.assertEqual(caught.exception.reason, reason)
        self.assertIsInstance(caught.exception, PrintError)
        self.assertEqual(self.printer.job_writes, [])
        self.assertTrue(caught.exception.history)
        return caught.exception


class SuccessTests(SessionTestCase):
    """DOING then DONE -> COMPLETED."""

    def test_full_success(self):
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertIs(result.job_outcome, JobOutcome.COMPLETED)
        self.assertTrue(result.ok)
        # Paper (logged), the gate's cover query, the job, the cover check.
        self.assertEqual(self.printer.writes, [PAPER, CAP, JOB, CAP])
        self.assertEqual(result.attribution_problems, [])
        self.assertTrue(any("cover still CLOSE" in line for line in result.history))
        self.assertEqual(result.bytes_written, len(JOB))
        self.assertEqual(result.job_bytes, len(JOB))
        self.assertEqual((result.cover, result.paper), ("CLOSE", "YES"))
        self.assertTrue(result.saw_doing and result.saw_done)
        self.assertAlmostEqual(result.timings["print_s"], 2.0, places=2)
        self.assertIn("doing_after_s", result.timings)
        self.assertTrue(any("finished (DONE" in line for line in result.history))
        self.assertGreater(result.elapsed_s, 3.0)
        self.assertEqual(self.printer.write_timeouts[2], SessionTimeouts().job_write_s)

    def test_image_input_and_timeout_override(self):
        img = Image.new("L", (1200, 1800), 255)
        img.paste(0, (100, 100, 300, 300))
        result = self.run_print(img, doing_s=2.0, job_write_s=7.0)
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertEqual(self.printer.write_timeouts[2], 7.0)
        job = self.printer.job_writes[0]
        self.assertEqual(tspl.parse_job(job), tspl.rasterize(img))

    def test_queued_lines_are_drained_and_logged(self):
        self.printer.push(
            0.0,
            "SSSGETCAP:OPEN",
            "SSSGETCAP:CLOSE",
            "SSSGETPRINTING:DOING",
            "SSSGETPRINTING:DONE",
            "Cmd error:junk",
        )
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertTrue(any("drained 5 queued line(s)" in h for h in result.history))
        self.assertEqual(result.command_errors, [])
        # The queued realign's DOING/DONE are not the label's.
        self.assertEqual(result.attribution_problems, [])

    def test_stale_done_before_job_is_not_counted(self):
        self.printer.push(0.0, "SSSGETPRINTING:DONE")
        self.printer.doing_after_s = None
        self.printer.done_after_s = None
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)

    def test_any_rasterize_failure_is_a_clean_failure(self):
        bomb = Image.DecompressionBombError("too many pixels")
        with mock.patch.object(tspl, "rasterize", side_effect=bomb):
            with self.assertRaises(DirectPrintError) as caught:
                self.run_print(Image.new("L", (10, 10), 255))
        self.assertEqual(caught.exception.reason, "bad_image")
        self.assertEqual(self.printer.writes, [])

    def test_bad_input_is_a_clean_failure(self):
        with self.assertRaises(DirectPrintError) as caught:
            self.run_print(b"\xff" * 10)
        self.assertEqual(caught.exception.reason, "bad_image")
        self.assertEqual(self.printer.writes, [])


class PrecheckTests(SessionTestCase):
    """Pre-check failures raise DirectPrintError before any job byte."""

    def test_cover_open(self):
        self.printer.cover = "OPEN"
        error = self.assert_not_sent("cover_open")
        self.assertTrue(str(error).startswith("Close the printer cover"))
        self.assertTrue(any("cover is OPEN" in line for line in error.history))
        self.assertFalse(error.same_printer_fallback)

    def test_no_cover_reply(self):
        self.printer.answer_queries = False
        error = self.assert_not_sent("no_cover_reply")
        self.assertEqual(self.printer.writes, [PAPER, CAP, CAP])
        self.assertIn("off and on", str(error))
        self.assertFalse(error.same_printer_fallback)

    def test_cover_reply_on_second_attempt(self):
        self.printer.reply_delay_s = 2.0  # later than query_reply_s
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)

    def test_realign_in_progress_then_done(self):
        self.printer.push(0.0, "SSSGETCAP:CLOSE", "SSSGETPRINTING:DOING")
        self.printer.push(4.0, "SSSGETPRINTING:DONE")
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertTrue(any("realign in progress" in line for line in result.history))
        self.assertTrue(any("realign finished" in line for line in result.history))
        self.assertGreaterEqual(result.timings["precheck_s"], 4.0)

    def test_realign_never_finishes(self):
        self.printer.push(0.0, "SSSGETPRINTING:DOING")
        self.assert_not_sent("realign_busy")

    def test_cover_opened_during_realign(self):
        self.printer.push(0.0, "SSSGETPRINTING:DOING")
        self.printer.cover_change(2.0, "OPEN")
        self.printer.push(2.0, "SSSGETPRINTING:DONE")
        self.assert_not_sent("cover_open")

    def test_realign_before_job(self):
        """Cover closed just before the print: wait for the realign, then send."""
        self.printer.cover = "OPEN"
        self.printer.cover_change(0.3, "CLOSE", realign_s=2.0)
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED, result.history)
        self.assertEqual(result.attribution_problems, [])
        realign_done = next(
            at for at, line in self.printer.delivered if line.endswith(":DONE")
        )
        job_at = next(at for at, data in self.printer.write_times if data == JOB)
        self.assertGreater(job_at, realign_done)
        self.assertTrue(any("realign finished" in h for h in result.history))

    def test_close_without_realign_report_is_refused(self):
        """A pushed CLOSE whose realign never shows up: do not send."""
        self.printer.push(0.0, "SSSGETCAP:OPEN", "SSSGETCAP:CLOSE")
        error = self.assert_not_sent("realign_busy")
        self.assertTrue(str(error).startswith("Close the printer cover"))
        self.assertFalse(error.same_printer_fallback)

    def test_cover_closed_during_gate_waits_for_realign(self):
        self.printer.cover = "OPEN"
        self.printer.cover_change(1.0, "CLOSE", realign_s=2.0)
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED, result.history)
        self.assertEqual([w for w in self.printer.writes if w == CAP], [CAP] * 3)
        job_at = next(at for at, data in self.printer.write_times if data == JOB)
        self.assertGreater(job_at, self.printer.delivered[0][0] + 3.0)

    def test_cover_opened_during_paper_query(self):
        self.printer.cover_change(0.55, "OPEN")
        self.assert_not_sent("cover_open")

    def test_stale_close_push_does_not_answer_the_cover_query(self):
        """The reply that follows a racing push wins (cover really OPEN)."""
        self.printer.cover = "OPEN"
        self.printer.reply_delay_s = 0.4
        self.printer.push(1.0, "SSSGETCAP:CLOSE")  # before the real reply
        self.assert_not_sent("cover_open")

    def test_read_failure_is_a_clean_failure(self):
        self.printer.read_raises = FakeDeviceGone(0)
        self.assert_not_sent("transport_error")
        self.assertEqual(self.printer.writes, [])


class QueryEagainTests(SessionTestCase):
    """A query the printer will not take never crashes the session."""

    def test_eagain_exception_on_first_query(self):
        self.printer.write_raises = [
            BlockingIOError(11, "Resource temporarily unavailable")
        ]
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertTrue(any("printer busy?" in line for line in result.history))

    def test_queries_never_accepted(self):
        self.printer.accept = lambda data: (
            0 if not data.startswith(b"\r\nSIZE") else len(data)
        )
        self.assert_not_sent("no_cover_reply")

    def test_partial_query_is_terminated_and_its_echo_ignored(self):
        calls = {"n": 0}

        def accept(data: bytes) -> int:
            calls["n"] += 1
            return 5 if calls["n"] == 1 else len(data)

        self.printer.accept = accept
        # The printer answers the fragment once the next line ends it.
        self.printer.job_replies = [(0.05, "Cmd error:SSSGE")]
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertEqual(self.printer.writes[0], b"SSSGE")
        self.assertEqual(self.printer.writes[1], b"\r\n" + CAP)
        self.assertEqual(result.command_errors, [])


class JobWriteTests(SessionTestCase):
    """Partial / failed job writes."""

    def test_partial_write_stall_is_uncertain(self):
        self.printer.accept = lambda data: (
            1000 if data.startswith(b"\r\nSIZE") else len(data)
        )
        result = self.run_print(job_write_s=5.0)
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertIs(result.job_outcome, JobOutcome.UNCERTAIN)
        self.assertEqual(result.bytes_written, 1000)
        self.assertTrue(any(PAPER_OUT_HINT in line for line in result.history))
        self.assertTrue(any("stalled at 1000/" in line for line in result.history))
        self.assertEqual(len(self.printer.job_writes), 1)  # never resent

    def test_nothing_accepted_is_a_clean_failure(self):
        self.printer.accept = lambda data: (
            0 if data.startswith(b"\r\nSIZE") else len(data)
        )
        self.assert_not_sent("job_not_accepted")

    def test_device_gone_mid_job_is_uncertain(self):
        def accept(data: bytes) -> int:
            if data.startswith(b"\r\nSIZE"):
                raise FakeDeviceGone(1234)
            return len(data)

        self.printer.accept = accept
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertEqual(result.bytes_written, 1234)
        self.assertTrue(any("1234 bytes of the job" in line for line in result.history))

    def test_device_gone_before_any_job_byte_is_clean(self):
        def accept(data: bytes) -> int:
            if data.startswith(b"\r\nSIZE"):
                raise FakeDeviceGone(0)
            return len(data)

        self.printer.accept = accept
        with self.assertRaises(DirectPrintError) as caught:
            self.run_print()
        self.assertEqual(caught.exception.reason, "transport_error")

    def test_unknown_write_failure_is_uncertain(self):
        def accept(data: bytes) -> int:
            if data.startswith(b"\r\nSIZE"):
                raise RuntimeError("driver exploded")
            return len(data)

        self.printer.accept = accept
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertEqual(result.bytes_written, -1)


class WatchTests(SessionTestCase):
    """After a complete write: DOING / DONE timeouts and printer errors."""

    def test_no_doing_is_uncertain(self):
        self.printer.doing_after_s = None
        self.printer.done_after_s = None
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertIn("doing_wait_s", result.timings)
        self.assertAlmostEqual(result.timings["doing_wait_s"], 10.0, places=2)

    def test_doing_too_late_is_uncertain(self):
        self.printer.doing_after_s = 12.0
        self.printer.done_after_s = 14.0
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)

    def test_doing_without_done_is_timeout(self):
        self.printer.done_after_s = None
        result = self.run_print(done_s=8.0)
        self.assertIs(result.outcome, SessionOutcome.TIMEOUT)
        self.assertIs(result.job_outcome, JobOutcome.TIMEOUT)
        self.assertFalse(result.ok)
        self.assertTrue(result.saw_doing)
        self.assertAlmostEqual(result.timings["done_wait_s"], 8.0, places=2)

    def test_done_without_doing_is_not_success(self):
        self.printer.doing_after_s = None
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertTrue(any("DONE without DOING" in line for line in result.history))

    def test_cmd_error_on_job_line_is_error(self):
        self.printer.job_replies = [(0.2, "Cmd error:SET TEAR ON")]
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.ERROR)
        self.assertIs(result.job_outcome, JobOutcome.ERROR)
        self.assertEqual(result.command_errors, ["Cmd error:SET TEAR ON"])

    def test_cmd_error_and_no_doing_is_error(self):
        self.printer.doing_after_s = None
        self.printer.done_after_s = None
        self.printer.job_replies = [(0.2, "Cmd error:BITMAP 0,0")]
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.ERROR)

    def test_empty_cmd_error_is_ignored(self):
        self.printer.job_replies = [(0.05, "Cmd error:")]
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)

    def test_cover_opened_mid_print_is_uncertain(self):
        self.printer.done_after_s = None
        self.printer.on_job = lambda: self.printer.cover_change(2.0, "OPEN")
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertEqual(result.cover, "OPEN")
        self.assertTrue(
            any("cover changed: CLOSE -> OPEN" in h for h in result.history)
        )
        self.assertEqual(len(self.printer.job_writes), 1)

    def test_realign_during_job_is_never_completed(self):
        """The label never prints; a cover cycle's realign supplies DOING/DONE."""
        self.printer.doing_after_s = None
        self.printer.done_after_s = None

        def cycle() -> None:
            self.printer.cover_change(0.5, "OPEN")
            self.printer.cover_change(1.0, "CLOSE", realign_s=2.0)

        self.printer.on_job = cycle
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN, result.history)
        self.assertTrue(result.saw_doing and result.saw_done)
        self.assertTrue(result.attribution_problems)
        self.assertNotIn(CAP, self.printer.writes[3:])  # no query after the job

    def test_cover_cycle_after_label_is_uncertain(self):
        """Label prints, then the cover is opened and closed: not provable."""

        def cycle() -> None:
            self.printer.cover_change(3.5, "OPEN")
            self.printer.cover_change(3.8, "CLOSE")

        self.printer.on_job = cycle
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN, result.history)

    def test_cover_open_after_done_without_push(self):
        """A missed push is caught by the cover query after DONE."""
        self.printer.on_job = lambda: self.printer.cover_change(2.5, "OPEN", push=False)
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertTrue(
            any("cover is OPEN after DONE" in p for p in result.attribution_problems)
        )

    def test_unpushed_realign_then_label_is_uncertain(self):
        """Cover cycle with no pushes: the realign's DONE comes first, then the
        label's DOING shows up while the cover is checked."""
        self.printer.doing_after_s = 2.5
        self.printer.done_after_s = 4.0

        def cycle() -> None:
            self.printer.cover_change(0.2, "OPEN", push=False)
            self.printer.cover_change(0.4, "CLOSE", push=False, realign_s=1.0)

        self.printer.on_job = cycle
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN, result.history)
        self.assertIn("a second DOING", result.attribution_problems)

    def test_second_doing_is_uncertain(self):
        self.printer.job_replies = [(1.5, "SSSGETPRINTING:DOING")]
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)

    def test_no_reply_to_cover_check_is_uncertain(self):
        self.printer.on_job = lambda: setattr(self.printer, "answer_queries", False)
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)

    def test_read_failure_while_watching_is_uncertain(self):
        original = self.printer.read_lines

        def read_lines(timeout_s: float) -> list[str]:
            if self.printer.job_writes:
                raise FakeDeviceGone(0)
            return original(timeout_s)

        self.printer.read_lines = read_lines  # type: ignore[method-assign]
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertTrue(any("lost the printer" in line for line in result.history))


class MappingTests(unittest.TestCase):
    """SessionOutcome <-> JobOutcome."""

    def test_every_outcome_maps(self):
        for outcome in SessionOutcome:
            self.assertEqual(outcome.job_outcome.value, outcome.value)
        self.assertEqual(
            [o for o in SessionOutcome if o.ok], [SessionOutcome.COMPLETED]
        )
        self.assertTrue(SessionOutcome.COMPLETED.job_outcome.ok)
        self.assertFalse(SessionOutcome.UNCERTAIN.job_outcome.ok)


try:
    from ibp_printing.direct.transport import FakeTransport
except ImportError:  # the transport module is optional for these tests
    FakeTransport = None  # type: ignore[assignment,misc]


@unittest.skipIf(FakeTransport is None, "ibp_printing.direct.transport not available")
class RealFakeTransportTests(unittest.TestCase):
    """Smoke test against the transport module's own FakeTransport."""

    def setUp(self):
        quiet_logging(self)

    def test_success_and_stall(self):
        fast = SessionTimeouts(
            drain_s=0.01,
            query_reply_s=0.05,
            doing_s=0.2,
            done_s=0.2,
            stall_listen_s=0.01,
            poll_s=0.01,
            job_write_s=0.05,
        )
        transport = FakeTransport(
            replies={
                "SSSGETCAP": ["SSSGETCAP:CLOSE"],
                "SSSGETPAPER": ["SSSGETPAPER:YES"],
                "PRINT 1,1": ["SSSGETPRINTING:DOING", "SSSGETPRINTING:DONE"],
            }
        )
        result = print_label(transport, WHITE, job_name="fake", timeouts=fast)
        self.assertIs(result.outcome, SessionOutcome.COMPLETED, result.history)

        stalled = FakeTransport(
            replies={"SSSGETCAP": ["SSSGETCAP:CLOSE"]},
            stall_after_bytes=len(CAP) + len(PAPER) + 500,
        )
        result = print_label(stalled, WHITE, job_name="fake", timeouts=fast)
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN, result.history)
        self.assertEqual(result.bytes_written, 500)


class WindowsAmbiguousCancelTests(unittest.TestCase):
    """The session over the Windows transport with a simulated usbprint.sys."""

    def setUp(self):
        quiet_logging(self)
        # pylint: disable=import-outside-toplevel
        from test_direct_transport import PRINTER_PATH, FakeWin32

        from ibp_printing.direct import transport as tr

        self.tr = tr
        self.api = FakeWin32()
        patcher = mock.patch.object(tr, "ABANDONED", tr.AbandonedRequests())
        self.abandoned = patcher.start()
        self.addCleanup(patcher.stop)

        def answer(data: bytes) -> None:
            body = data.lstrip(b"\r\n")
            if body == CAP:
                self.api.incoming.append(b"SSSGETCAP:CLOSE\r\n")
            elif body == PAPER:
                self.api.incoming.append(b"SSSGETPAPER:YES\r\n")
            if body in (CAP, PAPER):
                # The job's first chunk: 100 bytes go out, then it pends.
                self.api.write_capacity = len(self.api.written) + 100

        self.api.on_write = answer
        self.api.partial_pending = True
        self.transport = tr.WindowsUsbprintTransport(PRINTER_PATH, api=self.api)
        self.addCleanup(self.transport.close)
        self.timeouts = SessionTimeouts(
            drain_s=0.02,
            query_reply_s=0.05,
            job_write_s=0.05,
            doing_s=0.05,
            done_s=0.05,
            stall_listen_s=0.01,
            poll_s=0.01,
        )

    def test_unconfirmed_cancel_of_job_write_is_uncertain(self):
        """Not a PrintError: 100 bytes reached the printer, more may follow."""
        self.api.cancel_stuck_writes = True
        result = print_label(
            self.transport, WHITE, job_name="win", timeouts=self.timeouts
        )
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN, result.history)
        self.assertEqual(result.bytes_written, -1)  # unknown
        self.assertTrue(any("unknown part" in line for line in result.history))
        self.assertEqual(len(self.abandoned), 1)

    def test_confirmed_cancel_of_partial_job_is_uncertain(self):
        result = print_label(
            self.transport, WHITE, job_name="win", timeouts=self.timeouts
        )
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN, result.history)
        self.assertEqual(result.bytes_written, 100)
        self.assertEqual(len(self.abandoned), 0)


class ProbeTests(SessionTestCase):
    """probe_status: label-safe queries only, never raises."""

    def probe(self):
        """probe_status on the fake printer with the virtual clock."""
        return probe_status(self.printer, clock=self.clock)

    def test_ready_printer(self):
        probe = self.probe()
        self.assertEqual(self.printer.writes, [CAP, PAPER])
        self.assertEqual((probe.cover, probe.paper), ("CLOSE", "YES"))
        self.assertTrue(probe.ready)
        self.assertIsNone(probe.error)
        self.assertEqual(probe.lines, ["SSSGETCAP:CLOSE", "SSSGETPAPER:YES"])
        self.assertIn("cover CLOSE", probe.summary())
        self.assertTrue(probe.to_log()["ready"])

    def test_cover_open_and_realign(self):
        self.printer.cover = "OPEN"
        self.printer.push(0.0, "SSSGETPRINTING:DOING")
        probe = self.probe()
        self.assertEqual(probe.cover, "OPEN")
        self.assertEqual(probe.printing, "DOING")
        self.assertFalse(probe.ready)
        self.assertIn("printing DOING", probe.summary())

    def test_no_reply_is_retried_then_reported(self):
        self.printer.answer_queries = False
        probe = self.probe()
        self.assertEqual(self.printer.writes, [CAP, CAP, PAPER])
        self.assertIsNone(probe.cover)
        self.assertFalse(probe.ready)
        self.assertIn("cover no reply", probe.summary())

    def test_transport_failure_never_raises(self):
        self.printer.read_raises = FakeDeviceGone(0)
        probe = self.probe()
        self.assertFalse(probe.ready)
        self.assertIn("device gone", probe.error or "")
        self.assertIn("probe failed", probe.summary())
        self.assertEqual(self.printer.job_writes, [])


if __name__ == "__main__":
    unittest.main()
