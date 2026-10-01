"""Windows printer backend using win32print/win32ui (spooler + GDI) and WMI."""

# pylint: disable=import-outside-toplevel,import-error,c-extension-no-member

import contextlib
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Iterator, Mapping, Optional

from PIL import Image

from ibp_printing.backends.base import PrinterBackend, PrintError
from ibp_printing.discovery import discovery_from, vid_pid_from_device_id
from ibp_printing.jobs import track_job
from ibp_printing.log import (
    describe_exception,
    exception_summary,
    get_logger,
    log_event,
    timed_step,
)
from ibp_printing.models import (
    DeviceCaps,
    Discovery,
    JobOutcome,
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

# The attempt ID that api._job_name appends to every document name.
_ATTEMPT_SUFFIX = re.compile(r"(\[[0-9A-Za-z]+\])\s*$")

# USB monitor: WMI polls for instance events every USB_EVENT_DELAY_S seconds.
USB_EVENT_DELAY_S = 5
USB_MONITOR_BACKOFF_S = 5.0
USB_MONITOR_BACKOFF_MAX_S = 300.0
USB_MONITOR_ERROR_LOG_INTERVAL_S = 600.0

# RPC_E_CHANGED_MODE: the thread already joined a different COM apartment.
RPC_E_CHANGED_MODE = -2147417850


@contextlib.contextmanager
def com_apartment() -> Iterator[None]:
    """Initialize COM (single-threaded apartment) for the calling thread.

    WMI fails without it off the main thread. ``CoUninitialize`` is only
    called when our ``CoInitializeEx`` succeeded: ``CoInitializeEx`` raises on
    RPC_E_CHANGED_MODE (the thread is already in another apartment, which
    still leaves COM usable), unlike ``CoInitialize``, which hides it. Callers
    must drop every COM object they created before the block ends.
    """
    import pythoncom

    initialized = False
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
    except pythoncom.com_error as exc:
        hresult = getattr(exc, "hresult", None)
        log_event(
            logger,
            logging.DEBUG if hresult == RPC_E_CHANGED_MODE else logging.WARNING,
            "CoInitializeEx failed; using the thread's existing COM state",
            error=describe_exception(exc),
        )
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


# GDI stages after StartDoc that run before any page data can reach the
# printer. A failure here may still be a definite non-submission (if the abort
# is confirmed). EndPage and EndDoc hand page data to the spooler, which may
# already be sending it to the printer (print-while-spooling, direct ports), so
# a failure in either is always UNCERTAIN.
PRE_TRANSMIT_STAGES = frozenset({"StartPage", "Dib.draw"})


class _FailedAfterStartDoc(Exception):
    """A GDI call failed after StartDoc, so a job may exist in the spooler."""

    def __init__(
        self, stage: str, error: Exception, abort_error: Optional[Exception]
    ) -> None:
        super().__init__(f"{stage} failed: {error}")
        self.stage = stage
        self.error = error
        self.abort_error = abort_error


class WindowsPrinterBackend(PrinterBackend):
    """Spooler/GDI printing with USB presence checks through WMI."""

    platform_name = "win32"

    # After AbortDoc the spooler may keep the job for a moment while deleting.
    abort_settle_s = 2.0
    abort_poll_interval_s = 0.5

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
            errors.append(f"EnumPrinters failed: {exception_summary(exc)}")

        try:
            with timed_step(logger, "WMI USB query"):
                usb_devices = self.list_usb_devices()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(f"WMI USB query failed: {exception_summary(exc)}")

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
            not_connected=[d.device_id for d in usb_devices if not d.connected],
            usable=[candidate.name for candidate in discovery.usable],
            errors=errors,
        )
        return discovery

    @staticmethod
    def list_queues() -> list[PrintQueue]:
        """Enumerate local print queues with full status.

        Only ``PRINTER_ENUM_LOCAL``: USB label printers are always local
        queues, and enumerating connections can stall on a dead print server.
        """
        import win32print

        try:
            default = win32print.GetDefaultPrinter()
        except Exception:  # pylint: disable=broad-exception-caught
            default = None

        infos = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL, None, 2)
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
        """Enumerate every USB Plug and Play entity WMI knows about.

        This includes ghost entries for devices that are not plugged in; see
        :attr:`UsbDevice.connected`.
        """
        with com_apartment():
            # wmi connects to COM when imported, so import inside the apartment.
            import wmi  # type: ignore[import-not-found]

            connection = wmi.WMI()
            rows = connection.query(
                "SELECT * FROM Win32_PnPEntity WHERE DeviceID LIKE 'USB%'"
            )
            devices = [_usb_device_from_wmi(row) for row in rows]
            # Release COM objects while COM is still initialized.
            del rows, connection
        return devices

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
        """Draw the image on a GDI printer DC and optionally follow the job.

        Raises:
            PrintError: only when the job definitely never reached the spooler.
                A failure after StartDoc that cannot be ruled out as a partial
                submission returns a result with ``JobOutcome.UNCERTAIN``.
        """
        started = time.monotonic()
        result = PrintResult(printer_name=printer_name, job_name=job_name)
        self.log_queue_state(printer_name, "before print")

        img = flatten_for_print(orient_portrait(img))
        try:
            self._spool(img, printer_name, job_name)
        except _FailedAfterStartDoc as failure:
            self._resolve_failure_after_start_doc(result, failure)
            result.elapsed_s = round(time.monotonic() - started, 3)
            return result
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "spooling failed before StartDoc completed; nothing was submitted",
                printer=printer_name,
                error=describe_exception(exc),
            )
            self.log_queue_state(printer_name, "after failure")
            self.log_recent_print_events()
            raise PrintError(f"Printing to {printer_name!r} failed: {exc}") from exc

        if track_timeout_s > 0:
            self._track(result, track_timeout_s)
        else:
            self.log_queue_state(printer_name, "after spool")

        result.elapsed_s = round(time.monotonic() - started, 3)
        return result

    def _track(self, result: PrintResult, track_timeout_s: float) -> None:
        """Follow the spooled job; a tracking error becomes TRACKING_FAILED."""
        printer_name, job_name = result.printer_name, result.job_name
        try:
            with open_printer(printer_name) as handle:
                track_job(
                    result,
                    lambda: _find_job(handle, job_name),
                    timeout_s=track_timeout_s,
                )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            result.outcome = JobOutcome.TRACKING_FAILED
            result.history.append(f"tracking failed: {exception_summary(exc)}")
            log_event(
                logger,
                logging.WARNING,
                "job tracking failed; the job was spooled but its outcome is unknown",
                printer=printer_name,
                job_id=result.job_id,
                history=result.history,
                error=describe_exception(exc),
            )
        if result.outcome == JobOutcome.VANISHED_UNSEEN:
            # Usually printed before the first poll, but log what the queue
            # holds in case the job was renamed or went somewhere unexpected.
            self.log_queue_state(printer_name, "job never seen")
        if not result.outcome.ok:
            self.log_queue_state(printer_name, "after job problem")
            self.log_recent_print_events()

    def _resolve_failure_after_start_doc(
        self, result: PrintResult, failure: _FailedAfterStartDoc
    ) -> None:
        """Decide whether a post-StartDoc failure definitely submitted nothing.

        Raises PrintError only when the failure was in StartPage or Dib.draw
        (before EndPage could hand page data to the spooler), AbortDoc returned
        normally, and the queue holds no job with our attempt ID. A failure in
        EndPage or EndDoc is always UNCERTAIN: with print-while-spooling the
        page may already be on its way to the printer, and an empty queue only
        means the job has gone, not that it never printed. Anything uncertain
        leaves ``result.outcome`` as UNCERTAIN, with the reasons in
        ``result.history``.
        """
        printer_name, job_name = result.printer_name, result.job_name
        history = result.history
        history.append(
            f"{failure.stage} failed after StartDoc: {exception_summary(failure.error)}"
        )
        if failure.abort_error is None:
            # pywin32's PyCDC.AbortDoc discards the native return value, so
            # "no exception" does not prove the job was cancelled.
            history.append("AbortDoc returned without raising")
        else:
            history.append(f"AbortDoc failed: {exception_summary(failure.abort_error)}")

        job, check_error = self._look_for_job(printer_name, job_name)
        if check_error is not None:
            history.append(f"queue check failed: {exception_summary(check_error)}")
        elif job is not None:
            result.job_id = job.job_id
            flags = ",".join(decode_bits(job.status, JOB_STATUS_BITS)) or "QUEUED"
            history.append(
                f"job {job.job_id} {job.document!r} is still in the queue ({flags})"
            )
        else:
            history.append("no job with this attempt ID is in the queue")

        self.log_queue_state(printer_name, f"after {failure.stage} failure")
        self.log_recent_print_events()

        before_transmit = failure.stage in PRE_TRANSMIT_STAGES
        if not before_transmit:
            history.append(
                f"{failure.stage} may already have sent page data to the printer"
            )
        definite = (
            before_transmit
            and failure.abort_error is None
            and check_error is None
            and job is None
        )
        details: dict[str, Any] = {
            "printer": printer_name,
            "job_name": job_name,
            "stage": failure.stage,
            "error": describe_exception(failure.error),
            "abort_error": (
                None
                if failure.abort_error is None
                else describe_exception(failure.abort_error)
            ),
            "queue_check_error": (
                None if check_error is None else describe_exception(check_error)
            ),
            "job_in_queue": None if job is None else job.__dict__,
            "history": history,
        }
        if definite:
            log_event(
                logger,
                logging.ERROR,
                "print failed after StartDoc, job aborted and not in the queue: "
                "nothing was submitted",
                **details,
            )
            raise PrintError(
                f"Printing to {printer_name!r} failed: {failure.error}"
            ) from failure.error

        result.outcome = JobOutcome.UNCERTAIN
        log_event(
            logger,
            logging.ERROR,
            "print failed after StartDoc; the label MAY still print "
            "(outcome uncertain, do not resend automatically)",
            **details,
        )

    def _look_for_job(
        self, printer_name: str, job_name: str
    ) -> tuple[Optional[JobSnapshot], Optional[Exception]]:
        """Return ``(job, error)`` for our job in the queue after an abort.

        An aborted job can linger briefly while the spooler deletes it, so the
        queue is re-checked for up to ``abort_settle_s`` while it is present.
        """
        deadline = time.monotonic() + self.abort_settle_s
        try:
            with open_printer(printer_name) as handle:
                while True:
                    job = _find_job(handle, job_name)
                    if job is None or time.monotonic() >= deadline:
                        return job, None
                    time.sleep(self.abort_poll_interval_s)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.WARNING,
                "could not check the queue for the failed job",
                printer=printer_name,
                error=describe_exception(exc),
            )
            return None, exc

    @staticmethod
    def _spool(img: Image.Image, printer_name: str, job_name: str) -> None:
        """Render one page to the printer.

        ``PyCDC.StartDoc`` does not return the spooler job ID, so the job is
        found afterwards by its document name, which is unique per attempt.

        Raises:
            _FailedAfterStartDoc: if anything failed after StartDoc succeeded
                (AbortDoc has already been attempted).
            Exception: anything raised up to and including StartDoc, when no
                job can exist yet.
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

            stage = "StartPage"
            try:
                with timed_step(logger, "StartPage"):
                    context.StartPage()
                stage = "Dib.draw"
                with timed_step(logger, "Dib.draw"):
                    ImageWin.Dib(img).draw(context.GetHandleOutput(), rect)
                stage = "EndPage"
                with timed_step(logger, "EndPage"):
                    context.EndPage()
                stage = "EndDoc"
                with timed_step(logger, "EndDoc"):
                    context.EndDoc()
            except Exception as exc:
                raise _FailedAfterStartDoc(stage, exc, _abort_doc(context)) from exc
        finally:
            with contextlib.suppress(Exception):
                context.DeleteDC()

    # -- diagnostics ---------------------------------------------------------

    def log_queue_state(self, printer_name: str, when: str) -> None:
        """Log the queue's live status and every job currently in it. Never raises."""
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
            with contextlib.suppress(Exception):
                log_event(
                    logger,
                    logging.WARNING,
                    f"queue state ({when}) unavailable",
                    printer=printer_name,
                    error=describe_exception(exc),
                )

    def log_recent_print_events(self, minutes: int = 15) -> None:
        """Copy recent PrintService event-log entries into our log. Never raises."""
        try:
            events = self.recent_print_events(minutes=minutes)
            log_event(
                logger,
                logging.INFO,
                f"PrintService events, last {minutes} min",
                count=len(events),
            )
            for event in events:
                log_event(logger, logging.INFO, "PrintService event", event=event)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            with contextlib.suppress(Exception):
                log_event(
                    logger,
                    logging.WARNING,
                    "PrintService events unavailable",
                    error=describe_exception(exc),
                )

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
        metadata_cache: dict[Optional[str], Any] = {}
        try:
            for channel in PRINT_SERVICE_CHANNELS:
                events.extend(
                    _read_channel(
                        win32evtlog, channel, query, max_events, metadata_cache
                    )
                )
        finally:
            for metadata in metadata_cache.values():
                _close_evt_handle(metadata)
        return events

    def start_device_monitor(self, stop: threading.Event) -> list[threading.Thread]:
        """Log every USB device arrival, removal and status change."""
        thread = threading.Thread(
            target=_watch_usb_events, args=(stop,), name="usb-monitor", daemon=True
        )
        thread.start()
        return [thread]


# -- helpers -------------------------------------------------------------------


def _abort_doc(context: Any) -> Optional[Exception]:
    """Call AbortDoc and return what it raised (None if it returned)."""
    try:
        context.AbortDoc()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log_event(
            logger, logging.ERROR, "AbortDoc failed", error=describe_exception(exc)
        )
        return exc
    log_event(logger, logging.WARNING, "AbortDoc called after failure")
    return None


def _wmi_attr(row: Any, name: str) -> Any:
    """Read a WMI property; missing properties (or COM errors) give None.

    ``wmi`` raises ``x_wmi`` rather than AttributeError for some lookups, so
    ``getattr(row, name, None)`` alone is not enough.
    """
    try:
        return getattr(row, name, None)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _usb_device_from_wmi(row: Any) -> UsbDevice:
    device_id = _wmi_attr(row, "DeviceID") or ""
    error_code = _wmi_attr(row, "ConfigManagerErrorCode")
    present = _wmi_attr(row, "Present")
    return UsbDevice(
        device_id=device_id,
        name=_wmi_attr(row, "Name") or "",
        status=_wmi_attr(row, "Status") or "",
        error_code=int(error_code) if error_code is not None else None,
        pnp_class=_wmi_attr(row, "PNPClass") or "",
        vid_pid=vid_pid_from_device_id(device_id),
        present=bool(present) if present is not None else None,
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


def job_matches(document: Optional[str], job_name: str) -> bool:
    """True when a spooler document is our job.

    Job names end in a unique ``[attempt-id]``; matching on that suffix
    tolerates drivers or the spooler decorating the document name. Names
    without one must match exactly.
    """
    document = document or ""
    match = _ATTEMPT_SUFFIX.search(job_name)
    if match is None:
        return document == job_name
    return match.group(1) in document


def _find_job(handle: Any, job_name: str) -> Optional[JobSnapshot]:
    """Find our job by the attempt ID in its document name."""
    import win32print

    for job in win32print.EnumJobs(handle, 0, 999, 1):
        if job_matches(job.get("pDocument"), job_name):
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


def _close_evt_handle(handle: Any) -> None:
    """Close a PyEVT_HANDLE now (its Close() calls EvtClose) instead of at GC."""
    close = getattr(handle, "Close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


def _read_channel(
    win32evtlog: Any,
    channel: str,
    query: str,
    max_events: int,
    metadata_cache: dict[Optional[str], Any],
) -> list[dict[str, Any]]:
    """Render up to ``max_events`` recent events from one channel."""
    events: list[dict[str, Any]] = []
    handle = None
    try:
        handle = win32evtlog.EvtQuery(
            channel,
            win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
            query,
        )
        for event in win32evtlog.EvtNext(handle, max_events):
            try:
                events.append(
                    _render_event(win32evtlog, channel, event, metadata_cache)
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                events.append({"channel": channel, "error": exception_summary(exc)})
            finally:
                _close_evt_handle(event)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        events.append({"channel": channel, "error": exception_summary(exc)})
    finally:
        if handle is not None:
            _close_evt_handle(handle)
    return events


def _render_event(
    win32evtlog: Any,
    channel: str,
    event: Any,
    metadata_cache: Optional[dict[Optional[str], Any]] = None,
) -> dict[str, Any]:
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
            if metadata_cache is not None and provider_name in metadata_cache:
                metadata = metadata_cache[provider_name]
            else:
                metadata = win32evtlog.EvtOpenPublisherMetadata(provider_name)
                if metadata_cache is not None:
                    metadata_cache[provider_name] = metadata
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


class _ErrorLogLimiter:  # pylint: disable=too-few-public-methods
    """Log a recurring error once, then at most once per interval with a count."""

    def __init__(
        self, interval_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.interval_s = interval_s
        self.clock = clock
        self.last_logged: Optional[float] = None
        self.suppressed = 0

    def error(self, message: str, exc: BaseException, **data: Any) -> None:
        """Log ``message`` now, or count it if one was logged recently."""
        now = self.clock()
        if self.last_logged is not None and now - self.last_logged < self.interval_s:
            self.suppressed += 1
            return
        log_event(
            logger,
            logging.ERROR,
            message,
            error=describe_exception(exc),
            suppressed_since_last=self.suppressed,
            **data,
        )
        self.last_logged = now
        self.suppressed = 0


def _watch_usb_events(stop: threading.Event) -> None:
    """Log WMI instance events for USB PnP entities until ``stop`` is set.

    One ``__InstanceOperationEvent`` subscription covers creation, deletion
    and modification. On errors other than a timeout the subscription is
    dropped and recreated with exponential backoff, and repeated errors are
    logged at most once per ``USB_MONITOR_ERROR_LOG_INTERVAL_S``.
    """
    with com_apartment():
        import wmi  # type: ignore[import-not-found]

        limiter = _ErrorLogLimiter(USB_MONITOR_ERROR_LOG_INTERVAL_S)
        backoff = USB_MONITOR_BACKOFF_S
        connects = 0
        while not stop.is_set():
            watcher: Optional[Callable[..., Any]] = None
            try:
                watcher = wmi.WMI().watch_for(
                    notification_type="Operation",
                    wmi_class="Win32_PnPEntity",
                    delay_secs=USB_EVENT_DELAY_S,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                limiter.error(
                    "USB monitor failed to start", exc, retry_in_s=round(backoff, 1)
                )
            else:
                connects += 1
                log_event(
                    logger,
                    logging.INFO,
                    "USB monitor started" if connects == 1 else "USB monitor restarted",
                    connects=connects,
                )
                backoff, error = _pump_usb_events(
                    watcher, wmi.x_wmi_timed_out, stop, backoff
                )
                if error is not None:
                    limiter.error(
                        "USB monitor error; reconnecting",
                        error,
                        retry_in_s=round(backoff, 1),
                    )
            # Drop the COM objects before waiting (and before leaving COM).
            watcher = None
            if stop.wait(backoff):
                break
            backoff = min(backoff * 2, USB_MONITOR_BACKOFF_MAX_S)
        log_event(logger, logging.INFO, "USB monitor stopped")


def _pump_usb_events(
    watcher: Callable[..., Any],
    timed_out: type[BaseException],
    stop: threading.Event,
    backoff: float,
) -> tuple[float, Optional[Exception]]:
    """Log events until ``stop`` or an error.

    Returns:
        The backoff to use before reconnecting, and the error (None on stop).
    """
    while not stop.is_set():
        try:
            row = watcher(timeout_ms=1000)
        except timed_out:
            backoff = USB_MONITOR_BACKOFF_S  # The subscription is healthy.
            continue
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return backoff, exc
        backoff = USB_MONITOR_BACKOFF_S
        _log_usb_event(row)
        row = None
    return backoff, None


def _log_usb_event(row: Any) -> None:
    device = _usb_device_from_wmi(row)
    if not device.device_id.upper().startswith("USB"):
        return
    kind = _wmi_attr(row, "event_type") or "event"
    log_event(
        logger,
        logging.INFO if kind == "modification" else logging.WARNING,
        f"USB device {kind}",
        name=device.name,
        device_id=device.device_id,
        vid_pid=device.vid_pid,
        status=device.status,
        error_code=device.error_code,
        present=device.present,
    )
