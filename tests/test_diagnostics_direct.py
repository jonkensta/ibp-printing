"""ibp-print-diag with the direct USB layer: listing, probe, test print."""

import contextlib
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printing_helpers import dymo, quiet_logging, reset_logging
from test_direct_backend import (
    DYMO_QUEUE,
    PM_NAME,
    TWIN_QUEUE,
    Harness,
    pm_usb,
    printer,
)

import ibp_printing
from ibp_printing import PrintQueue
from ibp_printing import diagnostics
from ibp_printing.diagnostics import EXIT_DIRECT_NOT_SENT, build_report, main
from ibp_printing.direct import config as direct_config
from ibp_printing.direct.transport import (
    DeviceUnavailable,
    DirectDevice,
    FakeTransport,
    fake_device,
    parse_usbprint_path,
)

WIN_PATH = (
    "\\\\?\\usb#vid_2e3c&pid_5760#Q529E56G9290059#"
    "{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"
)
BUSY_TEXT = "BUSY: another program or the Windows print queue has it open"


def busy_windows_pm() -> DirectDevice:
    """The PM2411BT as Windows discovery lists it while the spooler holds it."""
    vid_pid, serial = parse_usbprint_path(WIN_PATH)
    return DirectDevice(
        path=WIN_PATH,
        vid_pid=vid_pid,
        serial=serial,
        kind="usbprint",
        errors=[f"{WIN_PATH} is in use by another program or the Windows spooler"],
        busy=True,
    )


def busy_open() -> DeviceUnavailable:
    """What opening a device held by the spooler raises."""
    return DeviceUnavailable("in use by the spooler", reason="busy", path="x")


class CoverOpenHarness(Harness):
    """A Harness whose printer always reports its cover open (every open)."""

    def _open(self, device: DirectDevice) -> FakeTransport:
        self.opened.append(device)
        if self.transport.closed:
            self.transport = printer(cover="OPEN")
        return self.transport.opener()(device)


class DiagTestCase(unittest.TestCase):
    """main() against a DirectFirstBackend with a fake PM2411BT."""

    def setUp(self):
        reset_logging()
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.addCleanup(reset_logging)
        quiet_logging(self)
        self.log_dir = Path(tmp.name)
        self.addCleanup(ibp_printing.set_backend, None)
        self.addCleanup(direct_config.set_direct_enabled, None)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(direct_config.ENV_VAR, None)

    def use(self, harness: Harness) -> Harness:
        """Install the harness backend."""
        ibp_printing.set_backend(harness.backend)
        return harness

    def run_main(self, *args: str) -> tuple[int, str]:
        """Run main() and capture stdout."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--log-dir", str(self.log_dir), "--events", "0", *args])
        return code, out.getvalue()


class DiagDirectTests(DiagTestCase):
    """Report, --direct-status and --test-print with the direct layer."""

    def test_report_lists_and_probes_direct_printer(self):
        harness = self.use(Harness())
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("-- Direct USB printers", out)
        self.assertIn("direct USB printing: ON", out)
        self.assertIn("1284 ID: MFG: ;CMD:XPP,XL;MDL:PM2411BT;", out)
        self.assertIn("serial=TESTSERIAL", out)
        self.assertIn("status probe: cover CLOSE, paper sensor YES", out)
        self.assertIn(f"=> usable as {PM_NAME!r}: YES", out)
        self.assertIn(f"1. {PM_NAME}  [direct USB]", out)
        # Label-safe: only the two status queries reached the printer.
        self.assertEqual(harness.transport.written_lines, ["SSSGETCAP", "SSSGETPAPER"])
        self.assertEqual(harness.queue_prints, [])

    def test_report_shows_open_cover(self):
        self.use(Harness(transport=printer(cover="OPEN")))
        _, out = self.run_main()
        self.assertIn("cover OPEN", out)
        self.assertIn("-> close the printer cover", out)

    def test_unsupported_device_is_listed_not_probed(self):
        harness = self.use(Harness(devices=[fake_device(model="XP-420B")]))
        _, out = self.run_main()
        self.assertIn("supported model: NO", out)
        self.assertEqual(harness.opened, [])

    def test_json_includes_direct_devices(self):
        self.use(Harness())
        code, out = self.run_main("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["direct_enabled"])
        (device,) = data["direct_devices"]
        self.assertTrue(device["supported"])
        self.assertEqual(device["probe"]["cover"], "CLOSE")
        self.assertEqual(data["usable"][0], PM_NAME)

    def test_direct_status_only(self):
        harness = self.use(Harness())
        code, out = self.run_main("--direct-status")
        self.assertEqual(code, 0)
        self.assertIn("status probe: cover CLOSE", out)
        self.assertIn("ready: 1 of 1", out)
        self.assertNotIn("-- Print queues --", out)
        self.assertEqual(harness.inner.printed, [])
        self.assertEqual(harness.transport.written_lines, ["SSSGETCAP", "SSSGETPAPER"])

    def test_direct_status_json_and_failures(self):
        self.use(Harness(transport=printer(cover="OPEN")))
        code, out = self.run_main("--direct-status", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["devices"][0]["probe"]["cover"], "OPEN")
        self.assertEqual(data["ready"], [])

    def test_direct_status_busy_and_none(self):
        busy = DeviceUnavailable("in use by the spooler", reason="busy", path="x")
        self.use(Harness(transport=printer(raise_on_open=busy)))
        code, out = self.run_main("--direct-status")
        self.assertEqual(code, 1)
        self.assertIn("probe failed: cannot open fake:lp0: in use by the spooler", out)
        self.use(Harness(devices=[]))
        code, out = self.run_main("--direct-status")
        self.assertEqual(code, 1)
        self.assertIn("no USB printer-class devices found", out)

    def test_test_print_goes_direct(self):
        harness = self.use(Harness())
        code, out = self.run_main("--test-print")
        self.assertEqual(code, 0)
        self.assertIn(f"printer={PM_NAME!r}", out)
        self.assertIn("outcome=completed", out)
        self.assertIn("history: direct USB: fake:lp0", out)
        self.assertEqual(
            harness.transport.written_lines[-2:], ["PRINT 1,1", "SSSGETCAP"]
        )
        self.assertEqual(harness.queue_prints, [])

    def test_test_print_named_direct_printer(self):
        harness = self.use(Harness())
        code, _ = self.run_main("--test-print", PM_NAME)
        self.assertEqual(code, 0)
        self.assertEqual(
            harness.transport.written_lines[-2:], ["PRINT 1,1", "SSSGETCAP"]
        )

    def test_no_direct_flag(self):
        harness = self.use(Harness())
        code, out = self.run_main("--no-direct", "--test-print")
        self.assertEqual(code, 0)
        self.assertIn("direct USB printing: OFF", out)
        self.assertEqual(harness.opened, [])
        self.assertEqual(harness.queue_prints, ["DYMO 0922:0028"])

    def test_direct_status_prints_test_print_name(self):
        self.use(Harness())
        code, out = self.run_main("--direct-status")
        self.assertEqual(code, 0)
        self.assertIn(f"        printer name: {PM_NAME}\n", out)
        self.assertIn(f"=> usable as {PM_NAME!r}: YES", out)
        _, out = self.run_main("--direct-status", "--json")
        (device,) = json.loads(out)["devices"]
        self.assertEqual(device["printer_name"], PM_NAME)
        self.assertTrue(device["usable"])

    def test_busy_pm2411bt_is_identified_by_vid_pid(self):
        harness = self.use(Harness(devices=[busy_windows_pm()]))
        code, out = self.run_main("--direct-status")
        self.assertEqual(code, 1)
        self.assertIn(f"[1] PM2411BT (probably) - {BUSY_TEXT}", out)
        self.assertIn("serial=Q529E56G9290059", out)
        self.assertIn("supported model: probably (VID:PID 2E3C:5760", out)
        self.assertNotIn("(unknown model)", out)
        self.assertNotIn("supported model: NO", out)
        # Not openable, not probed: nothing was sent.
        self.assertEqual(harness.opened, [])
        _, out = self.run_main("--direct-status", "--json")
        (device,) = json.loads(out)["devices"]
        self.assertEqual(device["identified_as"], "PM2411BT (probably)")
        self.assertEqual(device["open_problem"], BUSY_TEXT)
        self.assertFalse(device["usable"])
        # The full report says the same.
        _, out = self.run_main()
        self.assertIn(f"PM2411BT (probably) - {BUSY_TEXT}", out)

    def test_busy_unknown_device_stays_unknown(self):
        other = busy_windows_pm()
        other.vid_pid = "04F9:0042"
        self.use(Harness(devices=[other]))
        _, out = self.run_main("--direct-status")
        self.assertIn(f"[1] (unknown model) - {BUSY_TEXT}", out)
        self.assertIn("supported model: NO", out)

    def test_direct_status_respects_kill_switch(self):
        for args, why in (
            (("--no-direct",), "set_direct_enabled(False)"),
            ((), "IBP_PRINTING_DIRECT=0"),
        ):
            with self.subTest(args=args):
                direct_config.set_direct_enabled(None)
                harness = self.use(Harness())
                with mock.patch.dict(
                    os.environ, {direct_config.ENV_VAR: "0" if not args else ""}
                ):
                    code, out = self.run_main("--direct-status", *args)
                self.assertEqual(code, 1)
                self.assertIn(f"direct USB printing: OFF ({why})", out)
                self.assertIn("no USB device was opened", out)
                self.assertEqual(harness.discoveries, 0)
                self.assertEqual(harness.opened, [])
        _, out = self.run_main("--direct-status", "--no-direct", "--json")
        self.assertFalse(json.loads(out)["direct_enabled"])

    # -- --direct-only --------------------------------------------------------

    def twin_harness(self, harness: type[Harness] = Harness, **kwargs) -> Harness:
        """The PM2411BT plus its own vendor queue and a DYMO queue."""
        return self.use(
            harness(
                queues=[PrintQueue(TWIN_QUEUE), PrintQueue(DYMO_QUEUE)],
                usb=[pm_usb(), dymo()],
                **kwargs,
            )
        )

    def test_direct_only_prints_directly(self):
        harness = self.twin_harness()
        code, out = self.run_main("--test-print", "--direct-only")
        self.assertEqual(code, 0)
        self.assertIn(f"printer={PM_NAME!r}", out)
        self.assertIn("outcome=completed", out)
        self.assertEqual(harness.transport.written_lines[-2], "PRINT 1,1")
        self.assertEqual(harness.queue_prints, [])
        code, _ = self.run_main("--test-print", PM_NAME, "--direct-only")
        self.assertEqual(code, 0)
        self.assertEqual(harness.queue_prints, [])

    def test_busy_open_falls_back_without_direct_only(self):
        # The contrast for the next test: a busy device is a not-sent, so
        # print_to_first_available tries the queues.
        harness = self.twin_harness(transport=printer(raise_on_open=busy_open()))
        code, _ = self.run_main("--test-print")
        self.assertEqual(code, 0)
        self.assertEqual(harness.queue_prints, [DYMO_QUEUE])

    def test_direct_only_never_falls_back(self):
        cases = {
            "busy": (Harness, {"raise_on_open": busy_open()}, "cannot open fake:lp0"),
            "cover open": (
                CoverOpenHarness,
                {"cover": "OPEN"},
                "(cover_open): Close the printer cover",
            ),
        }
        for label, (kind, fake, why) in cases.items():
            for args in (("--test-print",), ("--test-print", PM_NAME)):
                with self.subTest(case=label, args=args):
                    harness = self.twin_harness(kind, transport=printer(**fake))
                    code, out = self.run_main(*args, "--direct-only")
                    self.assertEqual(code, EXIT_DIRECT_NOT_SENT)
                    self.assertIn("FAILED: --direct-only: direct USB print not", out)
                    self.assertIn(why, out)
                    self.assertIn("no print queue was tried", out)
                    self.assertEqual(harness.queue_prints, [])
                    self.assertNotIn("PRINT 1,1", harness.transport.written_lines)

    def test_direct_only_without_direct_printer(self):
        harness = self.use(Harness(devices=[]))
        code, out = self.run_main("--test-print", "--direct-only", "--json")
        self.assertEqual(code, EXIT_DIRECT_NOT_SENT)
        error = json.loads(out)["test_print"]["error"]
        self.assertIn("no usable direct USB printer found", error)
        self.assertEqual(harness.queue_prints, [])
        # A busy (unidentified) PM2411BT is not usable either.
        harness = self.twin_harness(devices=[busy_windows_pm()])
        code, _ = self.run_main("--test-print", "--direct-only")
        self.assertEqual(code, EXIT_DIRECT_NOT_SENT)
        self.assertEqual(harness.queue_prints, [])

    def test_direct_only_refuses_queue_name_and_kill_switch(self):
        harness = self.twin_harness()
        code, out = self.run_main("--test-print", TWIN_QUEUE, "--direct-only")
        self.assertEqual(code, EXIT_DIRECT_NOT_SENT)
        self.assertIn("is not a direct USB printer name", out)
        self.assertEqual(harness.queue_prints, [])
        harness = self.twin_harness()
        code, out = self.run_main("--no-direct", "--test-print", "--direct-only")
        self.assertEqual(code, EXIT_DIRECT_NOT_SENT)
        self.assertIn("direct USB printing is OFF", out)
        self.assertEqual(harness.queue_prints, [])
        self.assertEqual(harness.opened, [])

    def test_direct_only_needs_test_print(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_main("--direct-only")

    def test_build_report_without_direct_layer_has_no_section(self):
        discovery = Harness(enabled=None).inner.discover()
        self.assertNotIn("Direct USB", build_report(discovery))


class FakeClock:
    """A clock that advances ``step`` seconds on every call."""

    def __init__(self, step: float = 0.1) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class InterruptingTransport(FakeTransport):
    """Delivers a real SIGINT (Ctrl+C) during the ``at``-th read."""

    def __init__(self, at: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.at = at
        self.reads = 0

    def read_lines(self, timeout_s: float) -> list[str]:
        self.reads += 1
        if self.reads == self.at:
            signal.raise_signal(signal.SIGINT)
        return super().read_lines(timeout_s)


class ListenTests(DiagTestCase):
    """``--listen SECONDS``."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(diagnostics, "_now", FakeClock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_listen_shows_pushed_lines(self):
        transport = printer()
        transport.queue_after_reads(3, "SSSGETCAP:OPEN")
        transport.queue_after_reads(6, "SSSGETCAP:CLOSE", "SSSGETPRINTING:DOING")
        transport.queue_after_reads(9, "SSSGETPRINTING:DONE")
        harness = self.use(Harness(transport=transport))
        code, out = self.run_main("--listen", "30")
        self.assertEqual(code, 0)
        self.assertIn(f"-- Listening to {PM_NAME} for 30 s --", out)
        for text in (
            "sent SSSGETCAP",
            "'SSSGETCAP:OPEN'",
            "cover OPEN",
            "'SSSGETPRINTING:DOING'",
            "printing DOING",
            "printing DONE",
            "printer released",
        ):
            self.assertIn(text, out)
        self.assertIn("cover CLOSE x3", out)  # first reply, push, final reply
        # Only the two cover queries were written; the device was released.
        self.assertEqual(harness.transport.written_lines, ["SSSGETCAP", "SSSGETCAP"])
        self.assertTrue(harness.transport.closed)
        self.assertEqual(harness.queue_prints, [])

    def test_listen_json(self):
        transport = printer()
        transport.queue_after_reads(2, "SSSGETPRINTING:DOING")
        self.use(Harness(transport=transport))
        code, out = self.run_main("--listen", "5", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["printer"], PM_NAME)
        lines = [item["line"] for item in data["heard"]]
        self.assertEqual(
            lines, ["SSSGETCAP:CLOSE", "SSSGETPRINTING:DOING", "SSSGETCAP:CLOSE"]
        )
        self.assertTrue(data["device_released"])
        self.assertFalse(data["interrupted"])

    def test_listen_ctrl_c_releases_the_device(self):
        transport = InterruptingTransport(3)
        transport.add_reply("SSSGETCAP", "SSSGETCAP:CLOSE")
        harness = self.use(Harness(transport=transport))
        previous = signal.getsignal(signal.SIGINT)
        code, out = self.run_main("--listen", "30")
        self.assertEqual(code, 130)
        self.assertIn("stopped early (Ctrl+C)", out)
        self.assertIn("printer released", out)
        self.assertTrue(harness.transport.closed)
        self.assertEqual(harness.transport.written_lines, ["SSSGETCAP"])
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_listen_keyboard_interrupt_fallback(self):
        transport = printer(read_error=KeyboardInterrupt())
        harness = self.use(Harness(transport=transport))
        code, out = self.run_main("--listen", "30")
        self.assertEqual(code, 130)
        self.assertIn("stopped early (Ctrl+C)", out)
        self.assertTrue(harness.transport.closed)

    def test_listen_busy_and_no_printer(self):
        harness = self.use(Harness(transport=printer(raise_on_open=busy_open())))
        code, out = self.run_main("--listen", "30")
        self.assertEqual(code, 1)
        self.assertIn(f"--listen: cannot open {PM_NAME}: BUSY", out)
        harness = self.use(Harness(devices=[busy_windows_pm()]))
        code, out = self.run_main("--listen", "30")
        self.assertEqual(code, 1)
        self.assertIn("no usable direct USB printer", out)
        self.assertIn(f"PM2411BT (probably) - {BUSY_TEXT}", out)
        self.assertEqual(harness.opened, [])

    def test_listen_respects_kill_switch(self):
        harness = self.use(Harness())
        with mock.patch.dict(os.environ, {direct_config.ENV_VAR: "0"}):
            code, out = self.run_main("--listen", "30")
        self.assertEqual(code, 1)
        self.assertIn("direct USB printing: OFF", out)
        self.assertEqual(harness.opened, [])
        self.assertEqual(harness.discoveries, 0)

    def test_listen_silent_printer_and_bad_args(self):
        transport = FakeTransport()
        self.use(Harness(transport=transport))
        code, out = self.run_main("--listen", "2")
        self.assertEqual(code, 1)
        self.assertIn("Heard 0 line(s): nothing", out)
        for args in (("--listen", "0"), ("--listen", "5", "--direct-status")):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.run_main(*args)


if __name__ == "__main__":
    unittest.main()
