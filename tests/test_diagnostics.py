"""Tests for the ibp-print-diag report and CLI."""

import contextlib
import io
import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from printing_helpers import FakeBackend, PrintError, dymo, quiet_logging, reset_logging

import ibp_printing
from ibp_printing.diagnostics import (
    LABEL_SIZE,
    build_report,
    main,
    make_test_label,
    report_data,
)
from ibp_printing.discovery import discovery_from
from ibp_printing.models import PrinterCandidate, PrintQueue, UsbDevice

GOOD = "DYMO LabelWriter 4XL 0922:0028"
UNPLUGGED = "Zebra ZP450 0a5f:00d1"
OFFICE = "Office Laser"


def sample_discovery():
    """One usable, one unplugged and one non-label queue, plus a stray device."""
    queues = [
        PrintQueue(GOOD, port="USB001", driver="DYMO LabelWriter 4XL", status=0x80),
        PrintQueue(UNPLUGGED, port="USB002", driver="ZDesigner"),
        PrintQueue(OFFICE, port="IP_10.0.0.5", is_default=True),
    ]
    devices = [
        dymo("0922:0028", status="Error"),
        UsbDevice(device_id="USBPRINT\\DYMO\\7&1", name="no vid"),
        UsbDevice(
            device_id="USB\\VID_046D&PID_C52B\\6&1",
            name="Logitech receiver",
            status="OK",
            error_code=0,
            vid_pid="046D:C52B",
        ),
    ]
    return discovery_from(queues, devices, ["something odd"])


EVENTS = [
    {
        "channel": "Microsoft-Windows-PrintService/Admin",
        "event_id": "372",
        "level": "2",
        "time": "2026-09-30T12:00:00Z",
        "message": "The document Label failed\nto print.",
    },
    {"channel": "Microsoft-Windows-PrintService/Operational", "error": "disabled"},
]


class BuildReportTests(unittest.TestCase):
    """build_report / report_data."""

    def test_report_sections(self):
        report = build_report(sample_discovery(), EVENTS, log_dir=Path("/x/logs"))
        for text in (
            "-- Print queues --",
            f"name={GOOD!r}",
            "port='USB001'",
            "driver='DYMO LabelWriter 4XL'",
            "status=0x00000080 (OFFLINE)",
            "gate 1 (name ends in VID:PID): 0922:0028",
            "gate 2 (USB 0922:0028 present): YES (1 matching devices)",
            "status='Error' error_code=0",
            "device NOT OK, queue has a problem",
            "gate 2 (USB 0A5F:00D1 present): NO",
            f"name={OFFICE!r}  (default)",
            "gate 1 (name ends in VID:PID): NO MATCH",
            "-- USB devices with a VID:PID --",
            "046D:C52B",
            "(1 more USB entities without a VID:PID not shown)",
            "something odd",
            "usable label printers: 1",
            f"1. {GOOD}",
            "id=372 level=2: The document Label failed to print.",
            "Operational: unavailable: disabled",
            "/x/logs",
        ):
            with self.subTest(text=text):
                self.assertIn(text, report)
        self.assertEqual(report.count("=> usable: YES"), 1)
        self.assertEqual(report.count("=> usable: NO"), 2)

    def test_sections_are_in_order(self):
        report = build_report(sample_discovery(), EVENTS)
        positions = [
            report.index(heading)
            for heading in (
                "-- Print queues --",
                "-- USB devices",
                "-- Verdict --",
                "-- Recent PrintService events",
                "-- Logs --",
            )
        ]
        self.assertEqual(positions, sorted(positions))

    def test_no_usable_printer(self):
        report = build_report(discovery_from([PrintQueue(OFFICE)], []))
        self.assertIn("usable label printers: 0", report)
        self.assertIn("No label printer found plugged in.", report)
        self.assertIn("(none reported)", report)
        self.assertIn("(none, or not available on this platform)", report)

    def test_no_queues(self):
        self.assertIn("(no print queues found)", build_report(discovery_from([], [])))

    def test_platform_without_usb_matching(self):
        discovery = discovery_from([PrintQueue("Canon")], [])
        discovery.candidates = [
            PrinterCandidate(PrintQueue("Canon"), None, (), usb_matching=False)
        ]
        report = build_report(discovery)
        self.assertIn("not used on this platform", report)
        self.assertIn("1. Canon", report)

    def test_report_data_is_json(self):
        data = report_data(sample_discovery(), EVENTS, log_dir=Path("/x"))
        decoded = json.loads(json.dumps(data))
        self.assertEqual(decoded["usable"], [GOOD])
        self.assertEqual(len(decoded["candidates"]), 3)
        self.assertEqual(decoded["errors"], ["something odd"])
        self.assertEqual(decoded["log_dir"], str(Path("/x")))


class TestLabelTests(unittest.TestCase):
    """make_test_label."""

    def test_label(self):
        img = make_test_label("DYMO 0922:0028", datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(img.size, LABEL_SIZE)
        self.assertEqual(img.size, (1200, 1800))
        self.assertEqual(img.info["dpi"], (300, 300))
        # Border is black right at the edges.
        self.assertEqual(img.getpixel((0, 0)), 0)
        self.assertEqual(img.getpixel((1199, 1799)), 0)


class MainTests(unittest.TestCase):
    """main() end to end against a fake backend."""

    def setUp(self):
        reset_logging()
        self.addCleanup(reset_logging)
        quiet_logging(self)
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.log_dir = Path(tmp.name)
        self.backend = FakeBackend(
            [PrintQueue(GOOD), PrintQueue(OFFICE)], [dymo()], events=list(EVENTS)
        )
        ibp_printing.set_backend(self.backend)
        self.addCleanup(ibp_printing.set_backend, None)

    def run_main(self, *args: str) -> tuple[int, str]:
        """Run main() and capture stdout."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--log-dir", str(self.log_dir), *args])
        return code, out.getvalue()

    def test_default_report(self):
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(f"1. {GOOD}", out)
        self.assertIn("id=372", out)
        self.assertEqual(self.backend.printed, [])
        for handler in logging.getLogger("ibp_printing").handlers:
            handler.flush()
        logged = (self.log_dir / "printer.log").read_text(encoding="utf-8")
        self.assertIn("diagnostics report", logged)
        self.assertIn(f"1. {GOOD}", logged)

    def test_json(self):
        code, out = self.run_main("--json", "--events", "0")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["usable"], [GOOD])
        self.assertEqual(data["events"], [])
        self.assertNotIn("test_print", data)

    def test_exit_code_without_usable_printer(self):
        self.backend.devices = []
        code, _ = self.run_main()
        self.assertEqual(code, 1)

    def test_test_print_first_available(self):
        code, out = self.run_main("--test-print")
        self.assertEqual(code, 0)
        ((printer, job_name, timeout),) = self.backend.printed
        self.assertEqual(printer, GOOD)
        self.assertTrue(job_name.startswith("IBP Test Print ["))
        self.assertEqual(timeout, 60)
        self.assertIn("-- Test print --", out)
        self.assertIn("outcome=completed", out)

    def test_test_print_named_printer_json(self):
        code, out = self.run_main("--json", "--test-print", OFFICE)
        self.assertEqual(code, 0)
        self.assertEqual(self.backend.printed[0][0], OFFICE)
        data = json.loads(out)
        self.assertTrue(data["test_print"]["ok"])
        self.assertEqual(data["test_print"]["printer"], OFFICE)

    def test_test_print_failure(self):
        self.backend.fail = {GOOD: PrintError("StartDoc failed")}
        code, out = self.run_main("--test-print", GOOD)
        self.assertEqual(code, 1)
        self.assertIn("FAILED: StartDoc failed", out)


if __name__ == "__main__":
    unittest.main()
