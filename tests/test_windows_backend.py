"""Tests for the Windows backend against fake pywin32 / WMI modules."""

import functools
import sys
import threading
import types
import unittest
from typing import Any, Optional
from unittest import mock

from PIL import Image
from printing_helpers import quiet_logging

from ibp_printing import jobs
from ibp_printing.backends import PrintError
from ibp_printing.backends import windows
from ibp_printing.models import JobOutcome, JobSnapshot
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
PAPEROUT = 0x0040

CAPS = {
    DEVCAP_HORZRES: 1200,
    DEVCAP_VERTRES: 1800,
    DEVCAP_PHYSICALWIDTH: 1248,
    DEVCAP_PHYSICALHEIGHT: 1872,
}


X_WMI_TIMED_OUT = type("x_wmi_timed_out", (Exception,), {})


class COM_ERROR(Exception):  # pylint: disable=invalid-name
    """Stands in for pythoncom.com_error (hresult as the first arg)."""

    @property
    def hresult(self):
        """The HRESULT."""
        return self.args[0] if self.args else None


def event_xml(event_id: str, level: str, provider: str = "PrintService") -> str:
    """A minimal rendered PrintService event."""
    return (
        '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
        f'<System><Provider Name="{provider}"/><EventID>{event_id}</EventID>'
        f"<Level>{level}</Level>"
        '<TimeCreated SystemTime="2026-09-30T12:00:00Z"/></System>'
        "<UserData><DocumentPrinted><Param1>7</Param1>"
        "<Param2>Shipping Label</Param2></DocumentPrinted></UserData>"
        "</Event>"
    )


class FakeEvtHandle:  # pylint: disable=too-few-public-methods
    """Stands in for PyEVT_HANDLE; Close() records the EvtClose."""

    def __init__(self, state, name, channel="", xml=""):
        self.state = state
        self.name = name
        self.channel = channel
        self.xml = xml

    def Close(self):  # pylint: disable=invalid-name
        """Record the close."""
        self.state.evt_closed.append(self.name)


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
        # Our job shows up in EnumJobs as soon as StartDoc succeeds.
        self.visible_after_start_doc = False
        self.job_document = JOB_NAME
        self.open_handles = 0
        self.open_error: Optional[Exception] = None
        self.coinit_error: Optional[Exception] = None
        # PrintService events: (channel, xml) pairs returned by EvtNext.
        self.event_xml: dict[str, list[str]] = {}
        self.evt_query_error: dict[str, Exception] = {}
        self.evt_closed: list[str] = []
        self.evt_metadata_opened: list[str] = []
        # USB monitor: script for each WMI watch_for() subscription.
        self.watch_scripts: list[list[Any]] = []
        self.watch_calls: list[dict[str, Any]] = []
        self.stop: Optional[Any] = None

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
        if self.open_error is not None:
            raise self.open_error
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
                        "pDocument": self.job_document,
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
        wmi.WMI = lambda: types.SimpleNamespace(
            query=self.wmi_query, watch_for=self.watch_for
        )
        wmi.x_wmi_timed_out = X_WMI_TIMED_OUT

        pythoncom = types.ModuleType("pythoncom")
        pythoncom.com_error = COM_ERROR
        pythoncom.COINIT_APARTMENTTHREADED = 2
        pythoncom.CoInitializeEx = self.co_initialize_ex
        pythoncom.CoUninitialize = lambda: self.calls.append(("CoUninitialize",))

        return {
            "win32print": win32print,
            "win32ui": win32ui,
            "wmi": wmi,
            "pythoncom": pythoncom,
            "win32evtlog": self.evtlog_module(),
        }

    # -- pythoncom ----------------------------------------------------------

    def co_initialize_ex(self, flags):
        """CoInitializeEx: raises (like pywin32) on RPC_E_CHANGED_MODE."""
        self.calls.append(("CoInitializeEx", flags))
        if self.coinit_error is not None:
            raise self.coinit_error

    # -- wmi events ---------------------------------------------------------

    def watch_for(self, **kwargs):
        """WMI().watch_for: each call consumes one scripted subscription."""
        self.watch_calls.append(kwargs)
        if not self.watch_scripts:
            assert self.stop is not None
            self.stop.set()
            raise RuntimeError("no more scripted subscriptions")
        script = self.watch_scripts.pop(0)
        if isinstance(script, Exception):
            raise script

        def next_event(timeout_ms):
            del timeout_ms
            if not script:
                assert self.stop is not None
                self.stop.set()
                raise X_WMI_TIMED_OUT()
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        return next_event

    # -- win32evtlog --------------------------------------------------------

    def evtlog_module(self) -> Any:
        """A fake win32evtlog whose queries return ``event_xml``."""
        evtlog = types.ModuleType("win32evtlog")
        evtlog.EvtQueryChannelPath = 1
        evtlog.EvtQueryReverseDirection = 0x200
        evtlog.EvtRenderEventXml = 1
        evtlog.EvtFormatMessageEvent = 1
        fake = self

        def evt_query(channel, flags, query):
            del flags, query
            if channel in fake.evt_query_error:
                raise fake.evt_query_error[channel]
            return FakeEvtHandle(fake, f"query:{channel}", channel=channel)

        def evt_next(handle, count):
            xmls = fake.event_xml.get(handle.channel, [])[:count]
            return tuple(
                FakeEvtHandle(fake, f"event:{handle.channel}:{i}", xml=xml)
                for i, xml in enumerate(xmls)
            )

        def evt_render(event, flags):
            del flags
            return event.xml

        def evt_open_publisher_metadata(provider):
            fake.evt_metadata_opened.append(provider)
            if provider == "NoMetadata":
                raise RuntimeError("no publisher metadata")
            return FakeEvtHandle(fake, f"metadata:{provider}")

        def evt_format_message(metadata, event, flags):
            del metadata, flags
            return f"formatted {event.name}"

        evtlog.EvtQuery = evt_query
        evtlog.EvtNext = evt_next
        evtlog.EvtRender = evt_render
        evtlog.EvtOpenPublisherMetadata = evt_open_publisher_metadata
        evtlog.EvtFormatMessage = evt_format_message
        return evtlog

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
        """Record; optionally make the job visible in the queue."""
        self._call("StartDoc", name, output)
        if self.state.visible_after_start_doc:
            self.state.spooled = True

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
        self.backend.abort_settle_s = 0.0
        self.backend.abort_poll_interval_s = 0.0

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


def wmi_row(
    device_id: str,
    name: str = "",
    status: str = "OK",
    error: Optional[int] = 0,
    present: Optional[bool] = True,
    event_type: Optional[str] = None,
):
    """A Win32_PnPEntity-like row (``present=None`` omits the property)."""
    row = types.SimpleNamespace(
        DeviceID=device_id,
        Name=name,
        Status=status,
        ConfigManagerErrorCode=error,
        PNPClass="USB",
    )
    if present is not None:
        row.Present = present
    if event_type is not None:
        row.event_type = event_type
    return row


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
        self.assertIn(("CoInitializeEx", 2), self.fake.calls)
        self.assertEqual(self.fake.names().count("CoUninitialize"), 1)
        # Local queues only (C11).
        self.assertIn(("EnumPrinters", 2, None, 2), self.fake.calls)
        self.assertTrue(all(device.present for device in discovery.usb_devices))

    def test_wmi_failure_is_reported_not_raised(self):
        self.fake.wmi_error = RuntimeError("WMI is broken")
        discovery = self.backend.discover()
        self.assertEqual(len(discovery.candidates), 2)
        self.assertEqual(discovery.usable, [])
        self.assertEqual(len(discovery.errors), 1)
        self.assertEqual(
            discovery.errors[0], "WMI USB query failed: RuntimeError: WMI is broken"
        )

    def test_ghost_device_is_not_usable(self):
        self.fake.usb_rows = [
            wmi_row("USB\\VID_0922&PID_0028\\5&1&0&1", "DYMO", present=False),
            wmi_row("USB\\VID_0922&PID_0028\\5&1&0&2", "DYMO", error=45),
        ]
        discovery = self.backend.discover()
        self.assertEqual(discovery.usable, [])
        dymo = discovery.candidates[0]
        self.assertEqual(len(dymo.usb_devices), 2)
        self.assertIn("not connected", " ".join(dymo.reasons()))

    def test_missing_present_property_is_unknown(self):
        self.fake.usb_rows = [
            wmi_row("USB\\VID_0922&PID_0028\\5&1&0&1", "DYMO", present=None)
        ]
        discovery = self.backend.discover()
        self.assertIsNone(discovery.usb_devices[0].present)
        self.assertEqual([c.name for c in discovery.usable], [PRINTER])

    def test_wmi_property_error_is_tolerated(self):
        class RaisingRow:  # pylint: disable=too-few-public-methods
            """wmi raises x_wmi, not AttributeError, for some lookups."""

            DeviceID = "USB\\VID_0922&PID_0028\\5&1&0&1"

            def __getattr__(self, name):
                raise COM_ERROR(-1, f"no {name}")

        self.fake.usb_rows = [RaisingRow()]
        discovery = self.backend.discover()
        self.assertEqual(discovery.errors, [])
        self.assertIsNone(discovery.usb_devices[0].present)
        self.assertEqual(discovery.usb_devices[0].vid_pid, "0922:0028")

    def test_com_already_initialized_differently(self):
        self.fake.coinit_error = COM_ERROR(windows.RPC_E_CHANGED_MODE, "changed")
        discovery = self.backend.discover()
        self.assertEqual(discovery.errors, [])
        self.assertEqual(len(discovery.usb_devices), 3)
        # Our CoInitializeEx failed, so we must not CoUninitialize.
        self.assertNotIn("CoUninitialize", self.fake.names())

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

    def test_abort_failure_is_uncertain(self):
        self.fake.dc_fail["EndPage"] = RuntimeError("EndPage failed")
        self.fake.dc_fail["AbortDoc"] = RuntimeError("AbortDoc failed")
        result = self.print()
        self.assertEqual(result.outcome, JobOutcome.UNCERTAIN)
        self.assertFalse(result.outcome.ok)
        history = "\n".join(result.history)
        self.assertIn("EndPage failed after StartDoc", history)
        self.assertIn("EndPage failed", history)
        self.assertIn("AbortDoc failed", history)
        self.assertIn("no job with this attempt ID", history)
        self.assertEqual(self.gdi_calls()[-1], "DeleteDC")
        self.assertEqual(self.fake.open_handles, 0)

    def test_end_doc_failure_is_uncertain_even_with_empty_queue(self):
        self.fake.dc_fail["EndDoc"] = RuntimeError("EndDoc failed (error code -1)")
        result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.UNCERTAIN)
        self.assertEqual(
            self.gdi_calls(),
            [
                "CreatePrinterDC",
                "StartDoc",
                "StartPage",
                "Dib.draw",
                "EndPage",
                "EndDoc",
                "AbortDoc",
                "DeleteDC",
            ],
        )
        self.assertIn("EndDoc failed after StartDoc", result.history[0])
        self.assertIn("AbortDoc returned without raising", result.history[1])

    def test_job_still_in_queue_after_abort_is_uncertain(self):
        self.fake.visible_after_start_doc = True
        self.fake.job_statuses = [0x0004]  # DELETING, and it never goes away
        self.fake.draw_error = OSError("GDI draw failed")
        with self.assertLogs("ibp_printing", level="ERROR") as logs:
            result = self.print()
        self.assertEqual(result.outcome, JobOutcome.UNCERTAIN)
        self.assertEqual(result.job_id, 99)
        self.assertIn("is still in the queue (DELETING)", result.history[-1])
        self.assertTrue(any("MAY still print" in line for line in logs.output))

    def test_aborted_job_that_leaves_the_queue_is_a_print_error(self):
        self.backend.abort_settle_s = 5.0
        self.fake.visible_after_start_doc = True
        self.fake.job_statuses = [0x0004, None]  # DELETING, then gone
        self.fake.draw_error = OSError("GDI draw failed")
        with self.assertRaises(PrintError):
            self.print()

    def test_queue_check_failure_is_uncertain(self):
        self.fake.draw_error = OSError("GDI draw failed")
        self.fake.open_error = RuntimeError("OpenPrinter: RPC server unavailable")
        result = self.print()
        self.assertEqual(result.outcome, JobOutcome.UNCERTAIN)
        self.assertIn("queue check failed", result.history[-1])
        self.assertIn("RPC server unavailable", result.history[-1])

    def test_print_error_path_with_events_does_not_crash(self):
        # Regression (C1): PrintService events carry "level"/"message" keys,
        # which used to collide with log_event's own parameters.
        self.fake.event_xml = {
            windows.PRINT_SERVICE_CHANNELS[0]: [event_xml("372", "2")],
        }
        self.fake.dc_fail["StartDoc"] = RuntimeError("StartDoc: access denied")
        with self.assertLogs("ibp_printing", level="INFO") as logs:
            with self.assertRaises(PrintError):
                self.print()
        self.assertTrue(any("PrintService event" in line for line in logs.output))

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

    def test_tracking_failure_is_tracking_failed_with_history(self):
        snapshot = JobSnapshot(job_id=99, document=JOB_NAME, status=PAPEROUT)
        with mock.patch.object(
            windows,
            "_find_job",
            side_effect=[snapshot, RuntimeError("EnumJobs failed")],
        ):
            result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.TRACKING_FAILED)
        self.assertFalse(result.outcome.ok)
        self.assertEqual(result.job_id, 99)
        self.assertIn("PAPEROUT", result.history[0])
        self.assertIn(
            "tracking failed: RuntimeError: EnumJobs failed", result.history[1]
        )
        self.assertEqual(self.fake.open_handles, 0)

    def test_never_seen_logs_queue_state(self):
        self.fake.job_statuses = [None]
        with self.assertLogs("ibp_printing", level="INFO") as logs:
            result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.VANISHED_UNSEEN)
        self.assertTrue(
            any("queue state (job never seen)" in line for line in logs.output)
        )

    def test_job_found_by_attempt_suffix(self):
        self.fake.job_document = f"Microsoft GDI - {JOB_NAME}"
        self.fake.other_jobs.append(
            {"JobId": 6, "pDocument": "Shipping Label [zzz999]", "Status": PRINTING}
        )
        self.fake.job_statuses = [PRINTING, PRINTED]
        result = self.print(track_timeout_s=30)
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual(result.job_id, 99)

    def test_job_matches(self):
        self.assertTrue(windows.job_matches("x Label [abc123] y", "Label [abc123]"))
        self.assertFalse(windows.job_matches("Label [abc124]", "Label [abc123]"))
        self.assertFalse(windows.job_matches(None, "Label [abc123]"))
        self.assertTrue(windows.job_matches("Plain", "Plain"))
        self.assertFalse(windows.job_matches("Plain 2", "Plain"))


class EventTests(WindowsBackendTestCase):
    """recent_print_events / log_recent_print_events against a fake win32evtlog."""

    ADMIN, OPERATIONAL = windows.PRINT_SERVICE_CHANNELS

    def test_no_win32evtlog(self):
        with mock.patch.dict(sys.modules, {"win32evtlog": None}):
            self.assertEqual(self.backend.recent_print_events(), [])

    def test_events_are_rendered_and_handles_closed(self):
        self.fake.event_xml = {
            self.ADMIN: [event_xml("372", "2"), event_xml("808", "2")],
            self.OPERATIONAL: [event_xml("307", "4", provider="NoMetadata")],
        }
        events = self.backend.recent_print_events()
        self.assertEqual([e["event_id"] for e in events], ["372", "808", "307"])
        first = events[0]
        self.assertEqual(first["channel"], self.ADMIN)
        self.assertEqual(first["level"], "2")
        self.assertEqual(first["time"], "2026-09-30T12:00:00Z")
        self.assertEqual(first["message"], f"formatted event:{self.ADMIN}:0")
        # No publisher metadata: fall back to the raw UserData.
        self.assertIn("Param2=Shipping Label", events[2]["data"])
        # Metadata opened once per provider, and every handle closed (C10).
        self.assertEqual(self.fake.evt_metadata_opened.count("PrintService"), 1)
        closed = self.fake.evt_closed
        for name in (
            f"query:{self.ADMIN}",
            f"query:{self.OPERATIONAL}",
            f"event:{self.ADMIN}:0",
            f"event:{self.ADMIN}:1",
            f"event:{self.OPERATIONAL}:0",
            "metadata:PrintService",
        ):
            self.assertIn(name, closed)

    def test_query_failure_is_reported_per_channel(self):
        self.fake.event_xml = {self.OPERATIONAL: [event_xml("307", "4")]}
        self.fake.evt_query_error = {self.ADMIN: RuntimeError("channel disabled")}
        events = self.backend.recent_print_events()
        self.assertEqual(events[0]["channel"], self.ADMIN)
        self.assertIn("channel disabled", events[0]["error"])
        self.assertEqual(events[1]["event_id"], "307")

    def test_log_recent_print_events_nests_event_data(self):
        self.fake.event_xml = {self.ADMIN: [event_xml("372", "2")]}
        with self.assertLogs("ibp_printing", level="INFO") as logs:
            self.backend.log_recent_print_events()
        (record,) = [r for r in logs.records if r.getMessage() == "PrintService event"]
        data = record.data  # type: ignore[attr-defined]
        self.assertEqual(data["event"]["event_id"], "372")
        self.assertEqual(data["event"]["level"], "2")

    def test_log_recent_print_events_never_raises(self):
        with mock.patch.object(
            self.backend, "recent_print_events", side_effect=RuntimeError("boom")
        ):
            self.backend.log_recent_print_events()


class UsbMonitorTests(WindowsBackendTestCase):
    """The WMI USB device monitor thread."""

    def setUp(self):
        super().setUp()
        for name, value in (
            ("USB_MONITOR_BACKOFF_S", 0.0),
            ("USB_MONITOR_BACKOFF_MAX_S", 0.0),
        ):
            patcher = mock.patch.object(windows, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stop = threading.Event()
        self.fake.stop = self.stop

    def run_monitor(self) -> list[str]:
        """Run the monitor loop on this thread until the script ends."""
        with self.assertLogs("ibp_printing", level="INFO") as logs:
            windows._watch_usb_events(self.stop)  # pylint: disable=protected-access
        return [record.getMessage() for record in logs.records]

    def test_one_operation_subscription_logs_each_kind(self):
        dymo_id = "USB\\VID_0922&PID_0028\\5&1&0&1"
        self.fake.watch_scripts = [
            [
                wmi_row(dymo_id, "DYMO", event_type="creation"),
                X_WMI_TIMED_OUT(),
                wmi_row("HID\\VID_046D\\1", "mouse", event_type="creation"),
                wmi_row(dymo_id, "DYMO", present=False, event_type="modification"),
                wmi_row(dymo_id, "DYMO", event_type="deletion"),
            ]
        ]
        messages = self.run_monitor()
        self.assertEqual(len(self.fake.watch_calls), 1)
        call = self.fake.watch_calls[0]
        self.assertEqual(call["notification_type"].lower(), "operation")
        self.assertEqual(call["wmi_class"], "Win32_PnPEntity")
        self.assertGreaterEqual(call["delay_secs"], 5)
        self.assertEqual(
            [m for m in messages if m.startswith("USB device")],
            ["USB device creation", "USB device modification", "USB device deletion"],
        )
        self.assertIn("USB monitor stopped", messages)
        self.assertIn("CoUninitialize", self.fake.names())

    def test_errors_reconnect_with_rate_limited_logging(self):
        broken = RuntimeError("RPC server unavailable")
        self.fake.watch_scripts = [
            [broken],
            broken,  # watch_for itself fails
            [broken],
            [wmi_row("USB\\VID_0922&PID_0028\\5&1", "DYMO", event_type="creation")],
        ]
        messages = self.run_monitor()
        # One subscription per scripted connection; the last one ends the test.
        self.assertEqual(len(self.fake.watch_calls), 4)
        errors = [m for m in messages if "reconnecting" in m or "failed to start" in m]
        self.assertEqual(len(errors), 1)  # the rest were rate limited
        self.assertIn("USB monitor restarted", messages)
        self.assertIn("USB device creation", messages)

    def test_start_device_monitor_starts_one_thread(self):
        self.fake.watch_scripts = [[]]
        threads = self.backend.start_device_monitor(self.stop)
        self.assertEqual(len(threads), 1)
        threads[0].join(5)
        self.assertFalse(threads[0].is_alive())
        self.assertEqual(len(self.fake.watch_calls), 1)


if __name__ == "__main__":
    unittest.main()
