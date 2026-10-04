"""ibp-print-diag with the direct USB layer: listing, probe, test print."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from printing_helpers import quiet_logging, reset_logging
from test_direct_backend import PM_NAME, Harness, printer

import ibp_printing
from ibp_printing.diagnostics import build_report, main
from ibp_printing.direct import config as direct_config
from ibp_printing.direct.transport import DeviceUnavailable, fake_device


class DiagDirectTests(unittest.TestCase):
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

    def test_build_report_without_direct_layer_has_no_section(self):
        discovery = Harness(enabled=None).inner.discover()
        self.assertNotIn("Direct USB", build_report(discovery))


if __name__ == "__main__":
    unittest.main()
