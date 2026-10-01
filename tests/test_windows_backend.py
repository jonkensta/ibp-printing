"""Tests for the Windows backend against fake pywin32 / WMI modules."""

import functools
import sys
import types
import unittest
from typing import Any, Optional
from unittest import mock

from PIL import Image
from printing_helpers import quiet_logging

from ibp_printing import jobs
from ibp_printing.backends import PrintError
from ibp_printing.backends import windows
from ibp_printing.models import JobOutcome
from ibp_printing.render import compute_draw_rect
from ibp_printing.winconst import (
    DEVCAP_HORZRES,
    DEVCAP_PHYSICALHEIGHT,
    DEVCAP_PHYSICALWIDTH,
    DEVCAP_VERTRES,
)

PRINTER = "DYMO LabelWriter 4XL 0922:0028"
JOB_NAME = "Shipping Label [abc123]"
PRINTING = 0x0010
PRINTED = 0x0080
ERROR = 0x0002

CAPS = {
    DEVCAP_HORZRES: 1200,
    DEVCAP_VERTRES: 1800,
    DEVCAP_PHYSICALWIDTH: 1248,
    DEVCAP_PHYSICALHEIGHT: 1872,
}


class FakeClock:
    """Monotonic clock advanced only by sleep()."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        """Current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time."""
        self.now += seconds


class FakeWindows:  # pylint: disable=too-many-instance-attributes
    """State behind the fake win32print / win32ui / wmi / pythoncom modules."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.printers: list[dict[str, Any]] = []
        self.default: Optional[str] = None
        self.usb_rows: list[Any] = []
        self.wmi_error: Optional[Exception] = None
        self.dc_fail: dict[str, Exception] = {}
        self.draw_error: Optional[Exception] = None
        self.other_jobs: list[dict[str, Any]] = []
        # Status of our job on successive EnumJobs calls after EndDoc;
        # None means it is not in the queue. The last entry repeats.
        self.job_statuses: list[Optional[int]] = []
        self.spooled = False
        self.open_handles = 0

    # -- win32print ---------------------------------------------------------

    def enum_printers(self, flags, name, level):
        """EnumPrinters."""
        self.calls.append(("EnumPrinters", flags, name, level))
        return list(self.printers)

    def get_default_printer(self):
        """GetDefaultPrinter."""
        if self.default is None:
            raise RuntimeError("no default printer")
        return self.default

    def open_printer(self, name):
        """OpenPrinter."""
        self.calls.append(("OpenPrinter", name))
        self.open_handles += 1
        return f"handle:{name}"

    def close_printer(self, handle):
        """ClosePrinter."""
        self.calls.append(("ClosePrinter", handle))
        self.open_handles -= 1

    def get_printer(self, handle, level):
        """GetPrinter."""
        del handle, level
        return {"Status": 0, "Attributes": 0x40, "pPortName": "USB001"}

    def enum_jobs(self, handle, first, count, level):
        """EnumJobs: other jobs first, then ours once spooled."""
        del handle, first, count, level
        self.calls.append(("EnumJobs",))
        result = list(self.other_jobs)
        if self.spooled and self.job_statuses:
            status = (
                self.job_statuses.pop(0)
                if len(self.job_statuses) > 1
                else self.job_statuses[0]
            )
            if status is not None:
                result.append(
                    {
                        "JobId": 99,
                        "pDocument": JOB_NAME,
                        "Status": status,
                        "pStatus": None,
                        "PagesPrinted": 0,
                        "TotalPages": 1,
                    }
                )
        return tuple(result)

    # -- win32ui ------------------------------------------------------------

    def create_dc(self):
        """win32ui.CreateDC."""
        return FakeDC(self)

    # -- wmi ----------------------------------------------------------------

    def wmi_query(self, query):
        """WMI().query."""
        self.calls.append(("WMI.query", query))
        if self.wmi_error is not None:
            raise self.wmi_error
        return list(self.usb_rows)

    def modules(self) -> dict[str, Any]:
        """Fake modules to inject into sys.modules."""
        win32print = types.ModuleType("win32print")
        win32print.PRINTER_ENUM_LOCAL = 2
        win32print.PRINTER_ENUM_CONNECTIONS = 4
        win32print.EnumPrinters = self.enum_printers
        win32print.GetDefaultPrinter = self.get_default_printer
        win32print.OpenPrinter = self.open_printer
        win32print.ClosePrinter = self.close_printer
        win32print.GetPrinter = self.get_printer
        win32print.EnumJobs = self.enum_jobs

        win32ui = types.ModuleType("win32ui")
        win32ui.CreateDC = self.create_dc

        wmi = types.ModuleType("wmi")
        wmi.WMI = lambda: types.SimpleNamespace(query=self.wmi_query)
        wmi.x_wmi_timed_out = type("x_wmi_timed_out", (Exception,), {})

        pythoncom = types.ModuleType("pythoncom")
        pythoncom.com_error = type("com_error", (Exception,), {})
        pythoncom.CoInitialize = lambda: self.calls.append(("CoInitialize",))
        pythoncom.CoUninitialize = lambda: self.calls.append(("CoUninitialize",))

        return {
            "win32print": win32print,
            "win32ui": win32ui,
            "wmi": wmi,
            "pythoncom": pythoncom,
            "win32evtlog": None,  # import raises ImportError -> no events
        }

    def names(self) -> list[str]:
        """Just the names of the recorded calls."""
        return [call[0] for call in self.calls]


class FakeDC:
    """Records GDI calls; raises when told to."""

    def __init__(self, state: FakeWindows) -> None:
        self.state = state

    def _call(self, name: str, *args: Any) -> None:
        self.state.calls.append((name, *args))
        if name in self.state.dc_fail:
            raise self.state.dc_fail[name]

    def CreatePrinterDC(self, name):  # pylint: disable=invalid-name
        """Record."""
        self._call("CreatePrinterDC", name)

    def GetDeviceCaps(self, index):  # pylint: disable=invalid-name
        """Return the fake caps."""
        return CAPS.get(index, 0)

    def StartDoc(self, name, output=None):  # pylint: disable=invalid-name
        """Record."""
        self._call("StartDoc", name, output)

    def StartPage(self):  # pylint: disable=invalid-name
        """Record."""
        self._call("StartPage")

    def GetHandleOutput(self):  # pylint: disable=invalid-name
        """Return a fake HDC."""
        return "hdc"

    def EndPage(self):  # pylint: disable=invalid-name
        """Record."""
        self._call("EndPage")

    def EndDoc(self):  # pylint: disable=invalid-name
        """Record and put the job in the queue."""
        self._call("EndDoc")
        self.state.spooled = True

    def AbortDoc(self):  # pylint: disable=invalid-name
        """Record."""
        self._call("AbortDoc")

    def DeleteDC(self):  # pylint: disable=invalid-name
        """Record."""
        self._call("DeleteDC")


class WindowsBackendTestCase(unittest.TestCase):
    """Installs the fakes for every test."""

    def setUp(self):
        quiet_logging(self)
        self.fake = FakeWindows()
        patcher = mock.patch.dict(sys.modules, self.fake.modules())
        patcher.start()
        self.addCleanup(patcher.stop)

        fake = self.fake

        class FakeDib:  # pylint: disable=too-few-public-methods
            """Stands in for PIL.ImageWin.Dib (Windows-only)."""

            def __init__(self, img):
                fake.calls.append(("Dib", img.mode, img.size))

            def draw(self, hdc, rect):
                """Record, or raise the scripted error."""
                fake.calls.append(("Dib.draw", hdc, tuple(rect)))
                if fake.draw_error is not None:
                    raise fake.draw_error

        dib_patcher = mock.patch("PIL.ImageWin.Dib", FakeDib)
        dib_patcher.start()
        self.addCleanup(dib_patcher.stop)

        self.clock = FakeClock()
        track_patcher = mock.patch.object(
            windows,
            "track_job",
            functools.partial(
                jobs.track_job, clock=self.clock.clock, sleep=self.clock.sleep
            ),
        )
        track_patcher.start()
        self.addCleanup(track_patcher.stop)

        self.backend = windows.WindowsPrinterBackend()

    def print(self, track_timeout_s: float = 0.0, size=(1800, 1200), mode="RGBA"):
        """Print a test image to PRINTER."""
        img = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else 0)
        return self.backend.print_image(
            img, PRINTER, job_name=JOB_NAME, track_timeout_s=track_timeout_s
        )

    def gdi_calls(self) -> list[str]:
        """Names of the GDI calls made, in order."""
        gdi = {
            "CreatePrinterDC",
            "StartDoc",
            "StartPage",
            "Dib.draw",
            "EndPage",
            "EndDoc",
            "AbortDoc",
            "DeleteDC",
        }
        return [name for name in self.fake.names() if name in gdi]


def wmi_row(device_id: str, name: str = "", status: str = "OK", error: int = 0):
    """A Win32_PnPEntity-like row."""
    return types.SimpleNamespace(
        DeviceID=device_id,
        Name=name,
        Status=status,
        ConfigManagerErrorCode=error,
        PNPClass="USB",
    )


class DiscoverTests(WindowsBackendTestCase):
    """WindowsPrinterBackend.discover."""

    def setUp(self):
        super().setUp()
        self.fake.printers = [
            {
                "pPrinterName": PRINTER,
                "pPortName": "USB001",
                "pDriverName": "DYMO LabelWriter 4XL",
                "Status": 0,
                "Attributes": 0x40,
                "cJobs": 1,
            },
            {
                "pPrinterName": "Microsoft Print to PDF",
                "pPortName": "PORTPROMPT:",
                "pDriverName": "Microsoft Print To PDF",
                "Status": 0,
                "Attributes": 0x40,
                "cJobs": 0,
            },
        ]
        self.fake.default = "Microsoft Print to PDF"
        self.fake.usb_rows = [
            wmi_row("USB\\VID_0922&PID_0028\\5&1&0&1", "DYMO LabelWriter 4XL"),
            wmi_row("USBPRINT\\DYMOLABELWRITER_4XL\\7&2&0&USB001", "DYMO 4XL"),
            wmi_row("USB\\ROOT_HUB30\\4&3&0&0", "Root hub", error=None),
        ]

    def test_matches_queue_to_usb_device(self):
        discovery = self.backend.discover()
        self.assertEqual(discovery.errors, [])
        self.assertEqual(len(discovery.queues), 2)
        self.assertEqual(len(discovery.usb_devices), 3)
        self.assertEqual([c.name for c in discovery.usable], [PRINTER])

        dymo, pdf = discovery.candidates
        self.assertEqual(dymo.vid_pid, "0922:0028")
        self.assertEqual(dymo.queue.port, "USB001")
        self.assertEqual(dymo.queue.jobs, 1)
        self.assertEqual(len(dymo.usb_devices), 1)
        self.assertTrue(pdf.queue.is_default)
        self.assertFalse(pdf.usable)

        devices = {d.device_id.split("\\")[0]: d for d in discovery.usb_devices}
        self.assertIsNone(devices["USBPRINT"].vid_pid)
        self.assertEqual(self.fake.names().count("CoInitialize"), 1)
        self.assertEqual(self.fake.names().count("CoUninitialize"), 1)
        self.assertIn(("EnumPrinters", 6, None, 2), self.fake.calls)

    def test_wmi_failure_is_reported_not_raised(self):
        self.fake.wmi_error = RuntimeError("WMI is broken")
        discovery = self.backend.discover()
        self.assertEqual(len(discovery.candidates), 2)
        self.assertEqual(discovery.usable, [])
        self.assertEqual(len(discovery.errors), 1)
        self.assertIn("WMI USB query failed", discovery.errors[0])

    def test_no_default_printer(self):
        self.fake.default = None
        discovery = self.backend.discover()
        self.assertFalse(any(q.is_default for q in discovery.queues))
        self.assertIsNone(self.backend.get_default_printer())


class PrintImageTests(WindowsBackendTestCase):
    """WindowsPrinterBackend.print_image."""

    def test_happy_path(self):
        result = self.print()
        self.assertEqual(
            self.gdi_calls(),
            [
                "CreatePrinterDC",
                "StartDoc",
                "StartPage",
                "Dib.draw",
                "EndPage",
                "EndDoc",
                "DeleteDC",
            ],
        )
        self.assertIn(("StartDoc", JOB_NAME, None), self.fake.calls)
        self.assertIn(("CreatePrinterDC", PRINTER), self.fake.calls)
        # Landscape RGBA was rotated to portrait and flattened to RGB.
        self.assertIn(("Dib", "RGB", (1200, 1800)), self.fake.calls)
        caps = windows._read_caps(FakeDC(self.fake))  # pylint: disable=protected-access
        expected_rect = compute_draw_rect((1200, 1800), caps)
        self.assertIn(("Dib.draw", "hdc", expected_rect), self.fake.calls)

        self.assertEqual(result.printer_name, PRINTER)
        self.assertEqual(result.job_name, JOB_NAME)
        self.assertIsNone(result.job_id)
        self.assertEqual(result.outcome, JobOutcome.NOT_TRACKED)
        self.assertEqual(self.fake.open_handles, 0)

    def test_draw_failure_aborts_and_wraps(self):
        self.fake.draw_error = OSError("GDI draw failed")
        with self.assertRaises(PrintError) as ctx:
            self.print()
        self.assertEqual(
            self.gdi_calls(),
            [
                "CreatePrinterDC",
                "StartDoc",
                "StartPage",
                "Dib.draw",
                "AbortDoc",
                "DeleteDC",
            ],
        )
        self.assertIn(PRINTER, str(ctx.exception))
        self.assertIn("GDI draw failed", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, OSError)
        self.assertEqual(self.fake.open_handles, 0)

    def test_abort_failure_does_not_mask_original_error(self):
        self.fake.dc_fail["EndPage"] = RuntimeError("EndPage failed")
        self.fake.dc_fail["AbortDoc"] = RuntimeError("AbortDoc failed")
        with self.assertRaises(PrintError) as ctx:
            self.print()
        self.assertIn("EndPage failed", str(ctx.exception))
        self.assertEqual(self.gdi_calls()[-1], "DeleteDC")

    def test_start_doc_failure_wraps(self):
        self.fake.dc_fail["StartDoc"] = RuntimeError("StartDoc: access denied")
        with self.assertRaises(PrintError) as ctx:
            self.print()
        self.assertIn("access denied", str(ctx.exception))
        self.assertNotIn("StartPage", self.gdi_calls())
        self.assertEqual(self.gdi_calls()[-1], "DeleteDC")

    def test_create_printer_dc_failure_wraps(self):
        self.fake.dc_fail["CreatePrinterDC"] = RuntimeError("invalid printer name")
        with self.assertRaises(PrintError):
            self.print()
        self.assertNotIn("StartDoc", self.gdi_calls())


class JobTrackingTests(WindowsBackendTestCase):
    """Job lookup by document name through EnumJobs."""

    def setUp(self):
        super().setUp()
        # Someone else's stuck job is always in the queue too.
        self.fake.other_jobs = [
            {"JobId": 5, "pDocument": "Other document", "Status": ERROR},
        ]

    def test_completed(self):
        self.fake.job_statuses = [PRINTING, PRINTING, PRINTED]
        result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual(result.job_id, 99)
        self.assertEqual(len(result.history), 2)
        self.assertEqual(self.fake.open_handles, 0)

    def test_job_leaves_queue(self):
        self.fake.job_statuses = [PRINTING, None]
        result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual(result.job_id, 99)

    def test_never_seen(self):
        self.fake.job_statuses = [None]
        result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.VANISHED_UNSEEN)
        self.assertIsNone(result.job_id)

    def test_stuck_in_error(self):
        self.fake.job_statuses = [PRINTING, ERROR]
        enum_jobs_before = self.fake.names().count("EnumJobs")
        result = self.print(track_timeout_s=60)
        self.assertEqual(result.outcome, JobOutcome.ERROR)
        self.assertEqual(result.job_id, 99)
        self.assertGreaterEqual(self.clock.now, jobs.ERROR_GRACE_S)
        # The queue was dumped again after the problem.
        self.assertGreater(self.fake.names().count("EnumJobs"), enum_jobs_before + 3)

    def test_timeout(self):
        self.fake.job_statuses = [PRINTING]
        result = self.print(track_timeout_s=3)
        self.assertEqual(result.outcome, JobOutcome.TIMEOUT)

    def test_tracking_failure_is_not_a_print_failure(self):
        self.fake.job_statuses = [PRINTING]
        with mock.patch.object(
            windows, "_find_job", side_effect=RuntimeError("EnumJobs failed")
        ):
            result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.NOT_TRACKED)
        self.assertEqual(self.fake.open_handles, 0)


class EventTests(WindowsBackendTestCase):
    """recent_print_events without win32evtlog."""

    def test_no_win32evtlog(self):
        self.assertEqual(self.backend.recent_print_events(), [])


if __name__ == "__main__":
    unittest.main()
