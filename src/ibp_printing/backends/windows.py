"""Windows printer backend using win32print/win32ui (spooler + GDI) and WMI."""

# pylint: disable=import-outside-toplevel,import-error,c-extension-no-member

import contextlib
import logging
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Iterator, Mapping, Optional

from PIL import Image

from ibp_printing.backends.base import PrinterBackend, PrintError
from ibp_printing.discovery import discovery_from, vid_pid_from_device_id
from ibp_printing.jobs import track_job
from ibp_printing.log import describe_exception, get_logger, log_event, timed_step
from ibp_printing.models import (
    DeviceCaps,
    Discovery,
    JobSnapshot,
    PrintQueue,
    PrintResult,
    UsbDevice,
)
from ibp_printing.render import compute_draw_rect, flatten_for_print, orient_portrait
from ibp_printing.winconst import (
    DEVCAP_HORZRES,
    DEVCAP_LOGPIXELSX,
    DEVCAP_LOGPIXELSY,
    DEVCAP_PHYSICALHEIGHT,
    DEVCAP_PHYSICALOFFSETX,
    DEVCAP_PHYSICALOFFSETY,
    DEVCAP_PHYSICALWIDTH,
    DEVCAP_VERTRES,
    JOB_STATUS_BITS,
    PRINTER_ATTRIBUTE_BITS,
    PRINTER_STATUS_BITS,
    decode_bits,
)

logger = get_logger(__name__)

PRINT_SERVICE_CHANNELS = (
    "Microsoft-Windows-PrintService/Admin",
    "Microsoft-Windows-PrintService/Operational",
)
_EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


@contextlib.contextmanager
def com_apartment() -> Iterator[None]:
    """Initialize COM for the calling thread; WMI fails without it off-main."""
    import pythoncom

    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
    except pythoncom.com_error:
        pass  # Already initialized with a different threading model.
    try:
        yield
    finally:
        if initialized:
            pythoncom.CoUninitialize()


@contextlib.contextmanager
def open_printer(printer_name: str) -> Iterator[Any]:
    """Open a spooler handle and always close it."""
    import win32print

    handle = win32print.OpenPrinter(printer_name)
    try:
        yield handle
    finally:
        win32print.ClosePrinter(handle)


class WindowsPrinterBackend(PrinterBackend):
    """Spooler/GDI printing with USB presence checks through WMI."""

    platform_name = "win32"

    # -- discovery -----------------------------------------------------------

    def discover(self) -> Discovery:
        """List queues and USB devices, then match them by VID:PID."""
        errors: list[str] = []
        queues: list[PrintQueue] = []
        usb_devices: list[UsbDevice] = []

        try:
            with timed_step(logger, "EnumPrinters"):
                queues = self.list_queues()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(f"EnumPrinters failed: {exc!r}")

        try:
            with timed_step(logger, "WMI USB query"):
                usb_devices = self.list_usb_devices()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(f"WMI USB query failed: {exc!r}")

        discovery = discovery_from(queues, usb_devices, errors)
        for candidate in discovery.candidates:
            log_event(logger, logging.DEBUG, "candidate", **candidate.to_log())
        log_event(
            logger,
            logging.INFO if discovery.usable else logging.WARNING,
            "discovery",
            queues=[queue.name for queue in queues],
            usb_device_count=len(usb_devices),
            usb_vid_pids=sorted({d.vid_pid for d in usb_devices if d.vid_pid}),
            usable=[candidate.name for candidate in discovery.usable],
            errors=errors,
        )
        return discovery

    @staticmethod
    def list_queues() -> list[PrintQueue]:
        """Enumerate local and connected print queues with full status."""
        import win32print

        try:
            default = win32print.GetDefaultPrinter()
        except Exception:  # pylint: disable=broad-exception-caught
            default = None

        infos = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS, None, 2
        )
        return [
            PrintQueue(
                name=info.get("pPrinterName") or "",
                port=info.get("pPortName") or "",
                driver=info.get("pDriverName") or "",
                status=int(info.get("Status") or 0),
                attributes=int(info.get("Attributes") or 0),
                jobs=int(info.get("cJobs") or 0),
                is_default=info.get("pPrinterName") == default,
            )
            for info in infos
        ]

    @staticmethod
    def list_usb_devices() -> list[UsbDevice]:
        """Enumerate every USB Plug and Play entity WMI knows about."""
        import wmi  # type: ignore[import-not-found]

        with com_apartment():
            rows = wmi.WMI().query(
                "SELECT * FROM Win32_PnPEntity WHERE DeviceID LIKE 'USB%'"
            )
            return [_usb_device_from_wmi(row) for row in rows]

    def get_default_printer(self) -> Optional[str]:
        """Return the Windows default printer."""
        try:
            import win32print

            return win32print.GetDefaultPrinter()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("GetDefaultPrinter failed", exc_info=True)
            return None

    # -- printing ------------------------------------------------------------

    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Draw the image on a GDI printer DC and optionally follow the job."""
        started = time.monotonic()
        result = PrintResult(printer_name=printer_name, job_name=job_name)
        self.log_queue_state(printer_name, "before print")

        img = flatten_for_print(orient_portrait(img))
        try:
            self._spool(img, printer_name, job_name)
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "spooling failed",
                printer=printer_name,
                error=describe_exception(exc),
            )
            self.log_queue_state(printer_name, "after failure")
            self.log_recent_print_events()
            raise PrintError(f"Printing to {printer_name!r} failed: {exc}") from exc

        if track_timeout_s > 0:
            try:
                with open_printer(printer_name) as handle:
                    track_job(
                        result,
                        lambda: _find_job(handle, job_name),
                        timeout_s=track_timeout_s,
                    )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log_event(
                    logger,
                    logging.WARNING,
                    "job tracking failed",
                    error=describe_exception(exc),
                )
            if not result.outcome.ok:
                self.log_queue_state(printer_name, "after job problem")
                self.log_recent_print_events()
        else:
            self.log_queue_state(printer_name, "after spool")

        result.elapsed_s = round(time.monotonic() - started, 3)
        return result

    @staticmethod
    def _spool(img: Image.Image, printer_name: str, job_name: str) -> None:
        """Render one page to the printer.

        ``PyCDC.StartDoc`` does not return the spooler job ID, so the job is
        found afterwards by its document name, which is unique per attempt.
        """
        import win32ui
        from PIL import ImageWin

        context = win32ui.CreateDC()
        try:
            with timed_step(logger, "CreatePrinterDC", printer=printer_name):
                context.CreatePrinterDC(printer_name)

            caps = _read_caps(context)
            rect = compute_draw_rect(img.size, caps)
            log_event(
                logger,
                logging.INFO,
                "page layout",
                printer=printer_name,
                caps=caps.__dict__,
                image_size=list(img.size),
                image_mode=img.mode,
                draw_rect=list(rect),
            )

            with timed_step(logger, "StartDoc", job_name=job_name):
                # Single-argument form, as shippy has always called it; the
                # stubs wrongly require outputFile.
                context.StartDoc(job_name)  # type: ignore[call-arg]
            log_event(logger, logging.INFO, "job started", job_name=job_name)

            try:
                with timed_step(logger, "StartPage"):
                    context.StartPage()
                with timed_step(logger, "Dib.draw"):
                    ImageWin.Dib(img).draw(context.GetHandleOutput(), rect)
                with timed_step(logger, "EndPage"):
                    context.EndPage()
            except Exception:
                with contextlib.suppress(Exception):
                    context.AbortDoc()
                    logger.warning("job aborted after drawing failure")
                raise

            with timed_step(logger, "EndDoc"):
                context.EndDoc()
        finally:
            with contextlib.suppress(Exception):
                context.DeleteDC()

    # -- diagnostics ---------------------------------------------------------

    def log_queue_state(self, printer_name: str, when: str) -> None:
        """Log the queue's live status and every job currently in it."""
        try:
            import win32print

            with open_printer(printer_name) as handle:
                info = win32print.GetPrinter(handle, 2)
                jobs = win32print.EnumJobs(handle, 0, 999, 1)
            log_event(
                logger,
                logging.INFO,
                f"queue state ({when})",
                printer=printer_name,
                status=decode_bits(int(info.get("Status") or 0), PRINTER_STATUS_BITS),
                attributes=decode_bits(
                    int(info.get("Attributes") or 0), PRINTER_ATTRIBUTE_BITS
                ),
                port=info.get("pPortName"),
                driver=info.get("pDriverName"),
                jobs=[_job_to_log(job) for job in jobs],
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.WARNING,
                f"queue state ({when}) unavailable",
                printer=printer_name,
                error=describe_exception(exc),
            )

    def log_recent_print_events(self, minutes: int = 15) -> None:
        """Copy recent PrintService event-log entries into our log."""
        events = self.recent_print_events(minutes=minutes)
        log_event(
            logger,
            logging.INFO,
            f"PrintService events, last {minutes} min",
            count=len(events),
        )
        for event in events:
            log_event(logger, logging.INFO, "PrintService event", **event)

    def recent_print_events(
        self, minutes: int = 15, max_events: int = 40
    ) -> list[dict[str, Any]]:
        """Read recent events from the PrintService Admin/Operational channels.

        The Operational channel is disabled by default; enable it once with
        ``wevtutil sl Microsoft-Windows-PrintService/Operational /e:true``.
        """
        try:
            import win32evtlog
        except ImportError:
            return []

        query = f"*[System[TimeCreated[timediff(@SystemTime) <= {minutes * 60_000}]]]"
        events: list[dict[str, Any]] = []
        for channel in PRINT_SERVICE_CHANNELS:
            try:
                handle = win32evtlog.EvtQuery(
                    channel,
                    win32evtlog.EvtQueryChannelPath
                    | win32evtlog.EvtQueryReverseDirection,
                    query,
                )
                for event in win32evtlog.EvtNext(handle, max_events):
                    events.append(_render_event(win32evtlog, channel, event))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                events.append({"channel": channel, "error": repr(exc)})
        return events

    def start_device_monitor(self, stop: threading.Event) -> list[threading.Thread]:
        """Log every USB device arrival, removal and status change."""
        threads = []
        for kind in ("Creation", "Deletion", "Modification"):
            thread = threading.Thread(
                target=_watch_usb_events,
                args=(kind, stop),
                name=f"usb-{kind.lower()}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        return threads


# -- helpers -------------------------------------------------------------------


def _usb_device_from_wmi(row: Any) -> UsbDevice:
    device_id = getattr(row, "DeviceID", "") or ""
    error_code = getattr(row, "ConfigManagerErrorCode", None)
    return UsbDevice(
        device_id=device_id,
        name=getattr(row, "Name", "") or "",
        status=getattr(row, "Status", "") or "",
        error_code=int(error_code) if error_code is not None else None,
        pnp_class=getattr(row, "PNPClass", "") or "",
        vid_pid=vid_pid_from_device_id(device_id),
    )


def _read_caps(context: Any) -> DeviceCaps:
    caps = context.GetDeviceCaps
    return DeviceCaps(
        horzres=caps(DEVCAP_HORZRES),
        vertres=caps(DEVCAP_VERTRES),
        physical_width=caps(DEVCAP_PHYSICALWIDTH),
        physical_height=caps(DEVCAP_PHYSICALHEIGHT),
        offset_x=caps(DEVCAP_PHYSICALOFFSETX),
        offset_y=caps(DEVCAP_PHYSICALOFFSETY),
        dpi_x=caps(DEVCAP_LOGPIXELSX),
        dpi_y=caps(DEVCAP_LOGPIXELSY),
    )


def _find_job(handle: Any, job_name: str) -> Optional[JobSnapshot]:
    """Find our job by its (attempt-unique) document name."""
    import win32print

    for job in win32print.EnumJobs(handle, 0, 999, 1):
        if job.get("pDocument") == job_name:
            return JobSnapshot(
                job_id=int(job.get("JobId") or 0),
                document=job.get("pDocument") or "",
                status=int(job.get("Status") or 0),
                status_text=job.get("pStatus") or "",
                pages_printed=int(job.get("PagesPrinted") or 0),
                total_pages=int(job.get("TotalPages") or 0),
            )
    return None


def _job_to_log(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("JobId"),
        "document": job.get("pDocument"),
        "status": decode_bits(int(job.get("Status") or 0), JOB_STATUS_BITS),
        "status_text": job.get("pStatus"),
        "pages": f"{job.get('PagesPrinted')}/{job.get('TotalPages')}",
        "submitted": repr(job.get("Submitted")),
    }


def _render_event(win32evtlog: Any, channel: str, event: Any) -> dict[str, Any]:
    xml = win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml)
    root = ET.fromstring(xml)
    system = root.find(f"{_EVENT_NS}System")
    record: dict[str, Any] = {"channel": channel}
    if system is not None:
        provider = system.find(f"{_EVENT_NS}Provider")
        created = system.find(f"{_EVENT_NS}TimeCreated")
        record["event_id"] = system.findtext(f"{_EVENT_NS}EventID")
        record["level"] = system.findtext(f"{_EVENT_NS}Level")
        record["time"] = created.get("SystemTime") if created is not None else None
        provider_name = provider.get("Name") if provider is not None else None
        try:
            metadata = win32evtlog.EvtOpenPublisherMetadata(provider_name)
            record["message"] = win32evtlog.EvtFormatMessage(
                metadata, event, win32evtlog.EvtFormatMessageEvent
            )
        except Exception:  # pylint: disable=broad-exception-caught
            payload = root.find(f"{_EVENT_NS}UserData")
            if payload is None:
                payload = root.find(f"{_EVENT_NS}EventData")
            if payload is not None:
                record["data"] = " ".join(
                    f"{child.tag.split('}')[-1]}={(child.text or '').strip()}"
                    for child in payload.iter()
                    if child.text and child.text.strip()
                )
    return record


def _watch_usb_events(kind: str, stop: threading.Event) -> None:
    """Block on WMI instance events for USB PnP entities until ``stop`` is set."""
    import wmi  # type: ignore[import-not-found]

    level = logging.INFO if kind == "Modification" else logging.WARNING
    with com_apartment():
        try:
            watcher: Callable[..., Any] = wmi.WMI().watch_for(
                notification_type=kind, wmi_class="Win32_PnPEntity", delay_secs=2
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.ERROR,
                f"USB {kind.lower()} monitor failed to start",
                error=describe_exception(exc),
            )
            return

        log_event(logger, logging.INFO, f"USB {kind.lower()} monitor started")
        while not stop.is_set():
            try:
                row = watcher(timeout_ms=1000)
            except wmi.x_wmi_timed_out:
                continue
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log_event(
                    logger,
                    logging.ERROR,
                    f"USB {kind.lower()} monitor error",
                    error=describe_exception(exc),
                )
                stop.wait(5)
                continue

            device = _usb_device_from_wmi(row)
            if not device.device_id.upper().startswith("USB"):
                continue
            log_event(
                logger,
                level,
                f"USB device {kind.lower()}",
                name=device.name,
                device_id=device.device_id,
                vid_pid=device.vid_pid,
                status=device.status,
                error_code=device.error_code,
            )
