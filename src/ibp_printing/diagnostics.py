# pylint: disable=too-many-lines
"""``ibp-print-diag``: explain which printers are usable and why, and test them.

The default action runs a full discovery pass and prints a report showing
whether direct USB printing is on, every direct USB printer (path, serial,
1284 ID, and a label-safe status probe: cover and paper), then for every print
queue each detection gate and its result, every USB device that has a
VID:PID, the final verdict, recent PrintService events, and where the logs
live. ``--test-print`` sends a 4x6 test label (directly over USB when a
supported printer is plugged in; with ``--direct-only`` only directly, never
through a print queue), ``--direct-status`` runs only the direct probe and
names each direct printer as ``--test-print`` takes it, ``--listen SECONDS``
shows every line the direct printer pushes (cover open/close, realign), and
``--no-direct`` turns the direct path off for this run.
"""

import argparse
import contextlib
import json
import logging
import signal
import socket
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from collections import Counter
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from ibp_printing.api import (
    discover,
    get_backend,
    print_image,
    print_to_first_available,
)
from ibp_printing.backends import PrintError
from ibp_printing.direct import tspl
from ibp_printing.direct.backend import DirectFirstBackend
from ibp_printing.direct.config import set_direct_enabled
from ibp_printing.direct.session import StatusProbe
from ibp_printing.direct.transport import DirectDevice, Transport, TransportError
from ibp_printing.discovery import (
    PM2411BT_VID_PID,
    direct_candidates,
    is_direct_name,
)
from ibp_printing.log import (
    configure_logging,
    default_log_dir,
    describe_exception,
    get_logger,
    log_event,
    log_file_names,
)
from ibp_printing.models import (
    Discovery,
    JobOutcome,
    PrinterCandidate,
    PrintResult,
    UsbDevice,
    is_supported_direct_model,
)
from ibp_printing.winconst import (
    PRINTER_ATTRIBUTE_BITS,
    PRINTER_STATUS_BITS,
    describe_bits,
)

logger = get_logger(__name__)

RULE = "=" * 70
LABEL_SIZE = (1200, 1800)  # 4x6 inches at 300 DPI
LABEL_DPI = 300
TEST_PRINT_TIMEOUT_S = 60.0
NO_PRINTER_MESSAGE = "No label printer found plugged in."
LOG_APP = "diag"
# Exit status of ``--test-print --direct-only`` when nothing was printed
# (no usable direct printer, direct printing off, or the direct print was
# refused before sending). 1 stays "printed, but not confirmed".
EXIT_DIRECT_NOT_SENT = 3
# ``--listen``: longest single read, so Ctrl+C and the deadline are prompt.
LISTEN_POLL_S = 0.5
# ``--listen``: how long to wait for the reply to the closing SSSGETCAP.
LISTEN_FINAL_S = 2.0
PROBABLY_PM2411BT = "PM2411BT (probably)"


# -- report ------------------------------------------------------------------


def build_report(
    discovery: Discovery,
    events: Sequence[Mapping[str, Any]] = (),
    *,
    log_dir: Optional[Path] = None,
    probes: Optional[Mapping[str, StatusProbe]] = None,
) -> str:
    """Render a human-readable diagnostics report for one discovery pass.

    Args:
        discovery: The result of :func:`ibp_printing.discover`.
        events: Recent OS print-subsystem events (newest first).
        log_dir: Log directory to point the reader at; defaults to
            :func:`ibp_printing.default_log_dir`.
        probes: Status probe results by direct device path.
    """
    lines = [
        RULE,
        "ibp-printing printer diagnostics",
        f"host={socket.gethostname()} platform={sys.platform} "
        f"time={datetime.now().isoformat(timespec='seconds')}",
        RULE,
        "",
    ]
    if discovery.direct_enabled is not None:
        lines += direct_section(discovery, probes or {})
        lines.append("")
    lines += _queue_section(discovery)
    lines.append("")
    lines += _usb_section(discovery.usb_devices)
    lines.append("")
    if discovery.errors:
        lines.append("-- Discovery errors --")
        lines += [f"  {error}" for error in discovery.errors]
        lines.append("")
    lines += _verdict_section(discovery)
    lines.append("")
    lines += _events_section(events)
    lines.append("")
    lines.append("-- Logs --")
    lines.append(f"  {log_dir or default_log_dir()}")
    lines.append(
        "  (one pair per app: printer-<app>.log is human-readable, "
        "printer-<app>.jsonl has one JSON per line;"
    )
    lines.append(
        f"   this tool writes {' / '.join(log_file_names(LOG_APP))}, the label "
        "watcher printer-watcher.*, shippy printer-shippy.*, shippy-gui "
        "printer-shippy-gui.*)"
    )
    return "\n".join(lines)


def report_data(
    discovery: Discovery,
    events: Sequence[Mapping[str, Any]] = (),
    *,
    log_dir: Optional[Path] = None,
    probes: Optional[Mapping[str, StatusProbe]] = None,
) -> dict[str, Any]:
    """The same information as :func:`build_report`, as JSON-friendly data."""
    return {
        "host": socket.gethostname(),
        "platform": sys.platform,
        "time": datetime.now().isoformat(timespec="seconds"),
        "direct_enabled": discovery.direct_enabled,
        "direct_devices": [
            _direct_device_data(device, (probes or {}).get(device.path))
            for device in discovery.direct_devices
        ],
        "candidates": [candidate.to_log() for candidate in discovery.candidates],
        "usb_devices": [asdict(device) for device in discovery.usb_devices],
        "errors": list(discovery.errors),
        "usable": [candidate.name for candidate in discovery.usable],
        "events": [dict(event) for event in events],
        "log_dir": str(log_dir or default_log_dir()),
    }


def _direct_device_data(
    device: DirectDevice, probe: Optional[StatusProbe]
) -> dict[str, Any]:
    data = device.to_log()
    data["supported"] = is_supported_direct_model(device.model)
    data["identified_as"] = identify_device(device)
    data["open_problem"] = open_problem(device)
    data["probe"] = probe.to_log() if probe is not None else None
    return data


def direct_section(
    discovery: Discovery, probes: Mapping[str, StatusProbe]
) -> list[str]:
    """The "Direct USB printers" part of the report."""
    state = "ON" if discovery.direct_enabled else "OFF (queue-only)"
    lines = [
        "-- Direct USB printers (no driver, no print queue) --",
        f"  direct USB printing: {state}  (IBP_PRINTING_DIRECT=0 turns it off)",
    ]
    if not discovery.direct_enabled:
        return lines
    by_path = {
        candidate.direct_device.path: candidate
        for candidate in discovery.candidates
        if candidate.direct_device is not None
    }
    lines += describe_direct_devices(discovery.direct_devices, probes, by_path)
    return lines


def identify_device(device: DirectDevice) -> str:
    """The device's model, or ``"PM2411BT (probably)"`` by VID:PID.

    A device that could not be opened (busy in the Windows spooler, no
    permission) has no 1284 ID, so no model; the PM2411BT's VID:PID still
    identifies it.
    """
    if device.model:
        return device.model
    if device.vid_pid == PM2411BT_VID_PID:
        return PROBABLY_PM2411BT
    return "(unknown model)"


def _busy_text(device: DirectDevice) -> str:
    if device.kind == "usbprint":
        return "BUSY: another program or the Windows print queue has it open"
    return "BUSY: another program (e.g. CUPS) has it open"


def open_problem(device: DirectDevice) -> str:
    """Why discovery could not open the device ("" when it could, or unknown)."""
    if device.busy:
        return _busy_text(device)
    if device.accessible is False:
        if device.kind == "usblp":
            return "NO PERMISSION: this user cannot open it"
        return "CANNOT OPEN: see the error below"
    if not device.present:
        return "MISSING: the device node does not exist"
    return ""


def _supported_text(device: DirectDevice) -> str:
    if is_supported_direct_model(device.model):
        return "YES"
    if not device.model and device.vid_pid == PM2411BT_VID_PID:
        return (
            f"probably (VID:PID {PM2411BT_VID_PID} is the PM2411BT's; its "
            "1284 ID could not be read)"
        )
    return "NO"


def describe_direct_devices(
    devices: Sequence[DirectDevice],
    probes: Mapping[str, StatusProbe],
    candidates: Optional[Mapping[str, PrinterCandidate]] = None,
) -> list[str]:
    """One block per USB printer-class device, with its probe and verdict.

    ``candidates`` (by device path) adds each direct candidate's name, exactly
    as ``--test-print`` takes it, and its verdict.
    """
    if not devices:
        return ["  (no USB printer-class devices found)"]
    lines: list[str] = []
    for index, device in enumerate(devices, start=1):
        title = identify_device(device)
        problem = open_problem(device)
        if problem:
            lines += [f"  [{index}] {title} - {problem}", f"        at {device.path}"]
        else:
            lines.append(f"  [{index}] {title} at {device.path}")
        lines += [
            f"        kind={device.kind or '-'} vid_pid={device.vid_pid or '-'} "
            f"serial={device.serial or '-'} product={device.product or '-'}",
            f"        1284 ID: {device.ieee1284_raw or '(not read)'}",
            f"        supported model: {_supported_text(device)}  "
            f"present={device.present} accessible="
            f"{'-' if device.accessible is None else device.accessible} "
            f"busy={device.busy}",
        ]
        lines += [f"        error: {error}" for error in device.errors]
        probe = probes.get(device.path)
        if probe is not None:
            lines.append(f"        status probe: {probe.summary()}")
            if probe.lines:
                lines.append(f"        printer said: {probe.lines}")
            if probe.cover == "OPEN":
                lines.append("        -> close the printer cover")
        candidate = (candidates or {}).get(device.path)
        if candidate is not None:
            for reason in candidate.reasons():
                lines.append(f"        reason: {reason}")
            lines.append(f"        printer name: {candidate.name}")
            lines.append(
                f"        => usable as {candidate.name!r}: "
                f"{'YES' if candidate.usable else 'NO'}"
            )
    return lines


def _queue_section(discovery: Discovery) -> list[str]:
    lines = ["-- Print queues --"]
    queues = [
        candidate for candidate in discovery.candidates if not candidate.is_direct
    ]
    if not queues:
        lines.append("  (no print queues found)")
        return lines
    for index, candidate in enumerate(queues, start=1):
        lines += _describe_candidate(index, candidate)
    return lines


def _describe_candidate(index: int, candidate: PrinterCandidate) -> list[str]:
    queue = candidate.queue
    lines = [
        f"  [{index}] name={queue.name!r}{'  (default)' if queue.is_default else ''}",
        f"        port={queue.port!r} driver={queue.driver!r} queued_jobs={queue.jobs}",
        f"        status={describe_bits(queue.status, PRINTER_STATUS_BITS)}",
        f"        attributes={describe_bits(queue.attributes, PRINTER_ATTRIBUTE_BITS)}",
    ]
    if not candidate.usb_matching:
        lines.append("        gates: not used on this platform (every queue is usable)")
    elif candidate.vid_pid is None:
        lines.append(
            "        gate 1 (name ends in VID:PID): NO MATCH "
            "(rename the queue to end in e.g. ' 0922:0028')"
        )
    else:
        lines.append(f"        gate 1 (name ends in VID:PID): {candidate.vid_pid}")
        connected = candidate.connected_devices
        present = "YES" if connected else "NO"
        ghosts = len(candidate.usb_devices) - len(connected)
        lines.append(
            f"        gate 2 (USB {candidate.vid_pid} present): {present}"
            f" ({len(connected)} matching devices"
            + (f", {ghosts} not connected)" if ghosts else ")")
        )
        for device in candidate.usb_devices:
            lines.append(f"          - {_describe_device(device)}")
        lines.append(
            "        health (ranking only): "
            f"device {'OK' if candidate.device_healthy else 'NOT OK'}, "
            f"queue {'has a problem' if queue.has_problem else 'OK'}"
        )
    for reason in candidate.reasons():
        lines.append(f"        reason: {reason}")
    lines.append(f"        => usable: {'YES' if candidate.usable else 'NO'}")
    return lines


def _describe_device(device: UsbDevice) -> str:
    error_code = "-" if device.error_code is None else device.error_code
    present = "-" if device.present is None else device.present
    text = (
        f"{device.name!r} status={device.status!r} error_code={error_code} "
        f"present={present} class={device.pnp_class!r} id={device.device_id}"
    )
    reason = device.absence_reason
    return f"{text}  NOT CONNECTED: {reason}" if reason else text


def _usb_section(devices: Sequence[UsbDevice]) -> list[str]:
    lines = ["-- USB devices with a VID:PID --"]
    with_ids = sorted(
        (device for device in devices if device.vid_pid),
        key=lambda device: (device.vid_pid or "", device.device_id),
    )
    if not with_ids:
        lines.append("  (none reported)")
        return lines
    for device in with_ids:
        lines.append(f"  {device.vid_pid}  {_describe_device(device)}")
    without = len(devices) - len(with_ids)
    if without:
        lines.append(f"  ({without} more USB entities without a VID:PID not shown)")
    return lines


def _verdict_section(discovery: Discovery) -> list[str]:
    lines = ["-- Verdict --"]
    usable = discovery.usable
    lines.append(f"usable label printers: {len(usable)}")
    if not usable:
        lines.append(f"  (printing would fail with: {NO_PRINTER_MESSAGE!r})")
    for rank, candidate in enumerate(usable, start=1):
        how = "  [direct USB]" if candidate.is_direct else ""
        lines.append(f"  {rank}. {candidate.name}{how}")
    return lines


def _events_section(events: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["-- Recent PrintService events (newest first) --"]
    if not events:
        lines.append("  (none, or not available on this platform)")
        return lines
    for event in events:
        if "error" in event:
            lines.append(f"  {event.get('channel')}: unavailable: {event['error']}")
            continue
        head = (
            f"  {event.get('time')} {event.get('channel')} "
            f"id={event.get('event_id')} level={event.get('level')}"
        )
        detail = event.get("message") or event.get("data") or ""
        lines.append(f"{head}: {' '.join(str(detail).split())}")
    return lines


# -- test label --------------------------------------------------------------


def make_test_label(printer_name: str, when: Optional[datetime] = None) -> Image.Image:
    """Draw a 4x6 inch (1200x1800 at 300 DPI) test label.

    A border, a half-inch ruler grid and corner marks make scaling, cropping
    and rotation visible on the printed label.
    """
    when = when or datetime.now()
    width, height = LABEL_SIZE
    img = Image.new("L", LABEL_SIZE, 255)
    draw = ImageDraw.Draw(img)
    _draw_ruler(draw)
    _draw_frame(draw)

    lines = [
        ("IBP TEST PRINT", 110),
        (socket.gethostname(), 56),
        (when.isoformat(sep=" ", timespec="seconds"), 48),
        (printer_name or "(first available printer)", 44),
        (f'{width}x{height} px = 4x6" @ {LABEL_DPI} DPI', 40),
    ]
    box_top, box_bottom = height // 2 - 300, height // 2 + 260
    draw.rectangle(
        [(80, box_top), (width - 80, box_bottom)], fill=255, outline=0, width=6
    )
    y = box_top + 60
    for text, size in lines:
        draw.text((width // 2, y), text, fill=0, font=_font(size), anchor="ma")
        y += int(size * 1.6)

    img.info["dpi"] = (LABEL_DPI, LABEL_DPI)
    return img


def _draw_ruler(draw: ImageDraw.ImageDraw) -> None:
    """Half-inch grid with inch labels along the top and left edges."""
    width, height = LABEL_SIZE
    font = _font(28)
    step = LABEL_DPI // 2
    for x in range(step, width, step):
        major = x % LABEL_DPI == 0
        draw.line(
            [(x, 0), (x, height)], fill=120 if major else 200, width=3 if major else 1
        )
        draw.text((x + 4, 20), f'{x / LABEL_DPI:g}"', fill=0, font=font)
    for y in range(step, height, step):
        major = y % LABEL_DPI == 0
        draw.line(
            [(0, y), (width, y)], fill=120 if major else 200, width=3 if major else 1
        )
        draw.text((20, y + 4), f'{y / LABEL_DPI:g}"', fill=0, font=font)


def _draw_frame(draw: ImageDraw.ImageDraw) -> None:
    """Thick border plus a label in each corner, to reveal cropping and rotation."""
    width, height = LABEL_SIZE
    font = _font(36)
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=0, width=12)
    for text, anchor, pos in (
        ("TOP LEFT", "la", (30, 60)),
        ("TOP RIGHT", "ra", (width - 30, 60)),
        ("BOTTOM LEFT", "ld", (30, height - 30)),
        ("BOTTOM RIGHT", "rd", (width - 30, height - 30)),
    ):
        draw.text(pos, text, fill=0, font=font, anchor=anchor)


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (OSError, TypeError, ImportError):  # Pillow without FreeType
        return ImageFont.load_default()


def run_test_print(
    printer_name: Optional[str], *, direct_only: bool = False
) -> tuple[Optional[PrintResult], str]:
    """Print a test label and return ``(result, error)``; error is "" on success.

    Args:
        printer_name: The queue to print to, or None/"" for the best usable one.
        direct_only: Print only to a direct USB printer; never to a print
            queue (see :func:`run_direct_test_print`).
    """
    if direct_only:
        return run_direct_test_print(printer_name)
    img = make_test_label(printer_name or "")
    try:
        if printer_name:
            result = print_image(
                img,
                printer_name,
                job_name="IBP Test Print",
                track_timeout_s=TEST_PRINT_TIMEOUT_S,
            )
        else:
            result = print_to_first_available(
                img, job_name="IBP Test Print", track_timeout_s=TEST_PRINT_TIMEOUT_S
            )
    except PrintError as exc:
        log_event(
            logger, logging.ERROR, "test print failed", error=describe_exception(exc)
        )
        return None, str(exc)
    return result, ""


def run_direct_test_print(
    printer_name: Optional[str],
) -> tuple[Optional[PrintResult], str]:
    """``--test-print --direct-only``: a direct USB print or nothing.

    The label goes to the named direct printer (or the best usable one) over
    USB only. When there is none, direct printing is off, or the print is
    refused before sending (``PrintError``), the error says so and nothing is
    sent anywhere: no Windows / CUPS queue is ever tried.
    """
    nothing = "nothing was printed and no print queue was tried"
    layer = _direct_layer()
    if layer is None:
        return _direct_only_failed(
            f"the active backend has no direct USB layer; {nothing}"
        )
    enabled, why = layer.direct_mode()
    if not enabled:
        return _direct_only_failed(f"direct USB printing is OFF ({why}); {nothing}")
    if printer_name and not is_direct_name(printer_name):
        return _direct_only_failed(
            f"{printer_name!r} is not a direct USB printer name (it would print "
            "through a print queue); use the 'printer name:' from --direct-status, "
            f"e.g. 'PM2411BT (USB direct, serial ...)'; {nothing}"
        )
    try:
        if printer_name:
            img = make_test_label(printer_name)
            result = layer.print_image(
                img,
                printer_name,
                job_name="IBP Test Print",
                track_timeout_s=TEST_PRINT_TIMEOUT_S,
            )
        else:
            devices, _ = layer.list_devices()
            usable = sorted(
                (c for c in direct_candidates(devices) if c.usable),
                key=PrinterCandidate.rank_key,
            )
            if not usable:
                return _direct_only_failed(
                    "no usable direct USB printer found (run --direct-status to "
                    f"see why); {nothing}"
                )
            img = make_test_label(usable[0].name)
            result = layer.print_to_candidate(
                img,
                usable[0],
                job_name="IBP Test Print",
                track_timeout_s=TEST_PRINT_TIMEOUT_S,
            )
    except PrintError as exc:
        log_event(
            logger,
            logging.ERROR,
            "direct-only test print not sent",
            error=describe_exception(exc),
        )
        reason = getattr(exc, "reason", "")
        return _direct_only_failed(
            f"direct USB print not sent{f' ({reason})' if reason else ''}: "
            f"{str(exc).rstrip('.')}; {nothing}"
        )
    return result, ""


def _direct_only_failed(message: str) -> tuple[None, str]:
    text = f"--direct-only: {message}"
    log_event(logger, logging.ERROR, "direct-only test print failed", error=text)
    return None, text


def describe_result(result: Optional[PrintResult], error: str) -> str:
    """Human-readable summary of a test print."""
    if result is None:
        return f"-- Test print --\n  FAILED: {error}"
    lines = [
        "-- Test print --",
        f"  printer={result.printer_name!r}",
        f"  job_name={result.job_name!r} job_id={result.job_id}",
        f"  outcome={result.outcome.value} ({_outcome_text(result)})",
        f"  elapsed={result.elapsed_s}s",
    ]
    lines += [f"  history: {entry}" for entry in result.history]
    return "\n".join(lines)


def _outcome_text(result: PrintResult) -> str:
    if result.outcome.ok:
        return "label most likely printed"
    if result.outcome is JobOutcome.UNCERTAIN:
        return "PROBLEM: the label may or may not have printed; check the printer"
    if result.outcome is JobOutcome.TRACKING_FAILED:
        return "PROBLEM: spooled, but following the job failed; check the printer"
    if result.outcome is JobOutcome.TIMEOUT:
        return "PROBLEM: the printer did not finish in time; check the printer"
    return "PROBLEM: check the printer"


# -- CLI -----------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ibp-print-diag",
        description="Explain which label printers are usable and why.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="where to write printer-diag.log/.jsonl",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=60,
        metavar="MINUTES",
        help="include PrintService events from the last MINUTES (0 to skip)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead"
    )
    parser.add_argument(
        "--test-print",
        nargs="?",
        const="",
        default=None,
        metavar="PRINTER",
        help="print a 4x6 test label to PRINTER (default: best usable printer)",
    )
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="with --test-print: print only over direct USB; if no direct printer "
        "is usable or it refuses, fail (exit status 3) instead of using a print "
        "queue",
    )
    parser.add_argument(
        "--direct-status",
        action="store_true",
        help="only list direct USB printers and probe their cover/paper state "
        "(sends nothing that prints)",
    )
    parser.add_argument(
        "--listen",
        type=float,
        default=None,
        metavar="SECONDS",
        help="open the direct USB printer and show every line it sends for "
        "SECONDS (sends only SSSGETCAP, at the start and the end; no label); "
        "open and close the cover meanwhile. Ctrl+C stops early",
    )
    parser.add_argument(
        "--no-direct",
        action="store_true",
        help="turn direct USB printing off for this run (like IBP_PRINTING_DIRECT=0)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="also echo the log to stderr"
    )
    args = parser.parse_args(argv)
    if args.direct_only and args.test_print is None:
        parser.error("--direct-only only applies to --test-print")
    if args.listen is not None and args.listen <= 0:
        parser.error("--listen needs a number of seconds > 0")
    if args.listen is not None and (args.direct_status or args.test_print is not None):
        parser.error("--listen cannot be combined with --direct-status/--test-print")
    return args


def _direct_layer() -> Optional[DirectFirstBackend]:
    backend = get_backend()
    return backend if isinstance(backend, DirectFirstBackend) else None


def probe_direct_devices(
    devices: Sequence[DirectDevice],
    layer: Optional[DirectFirstBackend] = None,
) -> dict[str, StatusProbe]:
    """Probe every supported device (cover/paper queries only), by path."""
    layer = layer or _direct_layer()
    if layer is None:
        return {}
    return {
        device.path: layer.probe(device)
        for device in devices
        if is_supported_direct_model(device.model)
    }


def _candidates_by_path(
    devices: Sequence[DirectDevice],
) -> dict[str, PrinterCandidate]:
    return {
        candidate.direct_device.path: candidate
        for candidate in direct_candidates(devices)
        if candidate.direct_device is not None
    }


def _direct_off(
    layer: Optional[DirectFirstBackend], as_json: bool, what: str
) -> Optional[int]:
    """Print why the direct layer cannot be used; None when it can."""
    if layer is None:
        message = "the active backend has no direct USB layer"
        print(json.dumps({"error": message}) if as_json else message)
        return 1
    enabled, why = layer.direct_mode()
    if enabled:
        return None
    log_event(logger, logging.INFO, f"{what}: direct USB printing is off", why=why)
    if as_json:
        data = {"direct_enabled": False, "decided_by": why, "devices": []}
        print(json.dumps(data, indent=2))
    else:
        print(
            f"-- Direct USB printers --\n  direct USB printing: OFF ({why})\n"
            f"  {what}: no USB device was opened (direct printing is turned off)"
        )
    return 1


def run_direct_status(as_json: bool) -> int:
    """``--direct-status``: list direct devices and probe them; exit code.

    With direct printing off (``IBP_PRINTING_DIRECT=0`` / ``--no-direct``)
    it says so and opens nothing.
    """
    layer = _direct_layer()
    off = _direct_off(layer, as_json, "--direct-status")
    if off is not None or layer is None:
        return off or 1
    enabled, why = layer.direct_mode()
    devices, errors = layer.list_devices()
    probes = probe_direct_devices(devices, layer)
    candidates = _candidates_by_path(devices)
    ready = [path for path, probe in probes.items() if probe.ready]
    device_data = []
    for device in devices:
        data = _direct_device_data(device, probes.get(device.path))
        candidate = candidates.get(device.path)
        data["printer_name"] = candidate.name if candidate else None
        data["usable"] = candidate.usable if candidate else False
        device_data.append(data)
    log_event(
        logger,
        logging.INFO,
        "direct status",
        enabled=enabled,
        decided_by=why,
        devices=device_data,
        errors=errors,
    )
    if as_json:
        data = {
            "direct_enabled": enabled,
            "decided_by": why,
            "devices": device_data,
            "errors": errors,
            "ready": ready,
        }
        print(json.dumps(data, indent=2, default=repr))
    else:
        lines = [
            "-- Direct USB printers (status probe: SSSGETCAP / SSSGETPAPER only) --",
            f"  direct USB printing: {'ON' if enabled else 'OFF'} ({why})",
        ]
        lines += describe_direct_devices(devices, probes, candidates)
        lines += [f"  error: {error}" for error in errors]
        lines.append(f"  ready: {len(ready)} of {len(probes)} supported printer(s)")
        print("\n".join(lines))
    return 0 if probes and len(ready) == len(probes) else 1


# -- listen ----------------------------------------------------------------------

# The clock --listen uses (tests replace it).
_now: Callable[[], float] = time.monotonic


@contextlib.contextmanager
def _ctrl_c_flag() -> Iterator[list[bool]]:
    """Turn Ctrl+C into a flag (``flag[0]``) while listening.

    A KeyboardInterrupt raised in the middle of a Windows overlapped read
    could leave a request pending; with the flag the current read finishes
    (at most ``LISTEN_POLL_S``) and the device is closed normally. Outside
    the main thread the handler cannot be installed and Ctrl+C raises as
    usual (still caught by :func:`run_listen`).
    """
    flag = [False]

    def handler(_signum: int, _frame: Any) -> None:
        flag[0] = True

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread
        yield flag
        return
    try:
        yield flag
    finally:
        signal.signal(signal.SIGINT, previous)


def describe_line(line: str) -> str:
    """What one line from the printer means, for ``--listen``."""
    event = tspl.parse_line(line)
    if event.kind is tspl.EventKind.COVER:
        return f"cover {event.value}"
    if event.kind is tspl.EventKind.PRINTING:
        meaning = {"DOING": "paper moving (realign or label)", "DONE": "finished"}
        return f"printing {event.value}: {meaning.get(event.value, '?')}"
    if event.kind is tspl.EventKind.PAPER:
        return f"paper sensor {event.value} (unreliable)"
    if event.kind is tspl.EventKind.COMMAND_ERROR:
        return "printer rejected a line"
    return "(not understood)"


class _Listener:
    """Reads (and logs) everything one open printer sends; see run_listen."""

    def __init__(self, transport: Transport, *, write_s: float, echo: bool) -> None:
        self.transport = transport
        self.write_s = write_s
        self.echo = echo
        self.started = _now()
        self.heard: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.failed = ""
        self._line_dirty = False

    def note(self, text: str, level: int = logging.INFO) -> None:
        """Record (and print) one event of the listen."""
        stamp = f"+{_now() - self.started:5.1f} s"
        self.notes.append(f"{stamp}  {text}")
        log_event(logger, level, "listen: " + text)
        if self.echo:
            print(f"  {stamp}  {text}", flush=True)

    def ask_cover(self) -> None:
        """Send SSSGETCAP once (the only thing --listen ever writes)."""
        query = tspl.QUERY_COVER
        if self._line_dirty:
            query = tspl.CRLF + query  # end a fragment left by a short write
        try:
            accepted = self.transport.write_all(query, self.write_s)
        except (TransportError, OSError) as exc:
            self._line_dirty = True
            self.note(f"SSSGETCAP not sent: {exc}", logging.WARNING)
            return
        self._line_dirty = 0 < accepted < len(query)
        if accepted < len(query):
            self.note(
                f"SSSGETCAP: printer took {accepted}/{len(query)} bytes (busy?)",
                logging.WARNING,
            )
        else:
            self.note("sent SSSGETCAP")

    def listen(self, seconds: float, stop: list[bool]) -> None:
        """Read for ``seconds``, or until ``stop[0]`` or a read error."""
        deadline = _now() + seconds
        while not stop[0] and not self.failed:
            remaining = deadline - _now()
            if remaining <= 0:
                return
            try:
                lines = self.transport.read_lines(min(LISTEN_POLL_S, remaining))
            except (TransportError, OSError) as exc:
                self.failed = f"reading from the printer failed: {exc}"
                self.note(self.failed, logging.ERROR)
                return
            for line in lines:
                at = round(_now() - self.started, 3)
                self.heard.append(
                    {"t_s": at, "line": line, "meaning": describe_line(line)}
                )
                self.note(f"{line!r:28} {describe_line(line)}")

    def summary(self) -> str:
        """Counts of the cover / printing lines heard."""
        counts = Counter(
            (event.kind.value, event.value)
            for event in (tspl.parse_line(item["line"]) for item in self.heard)
        )
        parts = [f"{kind} {value} x{n}" for (kind, value), n in sorted(counts.items())]
        return ", ".join(parts) or "nothing"


def _open_listen_printer(
    layer: DirectFirstBackend, as_json: bool
) -> Optional[tuple[PrinterCandidate, Transport]]:
    """Open the best usable direct printer for --listen; None (reported) if not."""
    devices, errors = layer.list_devices()
    candidates = _candidates_by_path(devices)
    usable = sorted(
        (c for c in candidates.values() if c.usable), key=PrinterCandidate.rank_key
    )
    if not usable:
        lines = ["--listen: no usable direct USB printer to listen to"]
        lines += describe_direct_devices(devices, {}, candidates)
        lines += [f"  error: {error}" for error in errors]
        log_event(logger, logging.ERROR, "\n".join(lines))
        print(
            json.dumps({"error": lines[0], "devices": len(devices)})
            if as_json
            else "\n".join(lines)
        )
        return None
    candidate = usable[0]
    device = candidate.direct_device
    assert device is not None
    try:
        return candidate, layer.open_device(device)
    except (TransportError, OSError) as exc:
        reason = getattr(exc, "reason", "")
        problem = _busy_text(device) if reason == "busy" else "CANNOT OPEN"
        message = f"--listen: cannot open {candidate.name}: {problem} ({exc})"
        log_event(logger, logging.ERROR, message)
        print(json.dumps({"error": message}) if as_json else message)
        return None


def run_listen(seconds: float, as_json: bool) -> int:
    """``--listen SECONDS``: show what the direct printer pushes; exit code.

    Opens the best usable direct printer, sends ``SSSGETCAP`` (so the first
    reply shows the link works), reads and prints every line for
    ``seconds`` (cover pushes ``SSSGETCAP:OPEN`` / ``CLOSE``, the realign's
    ``SSSGETPRINTING:DOING`` / ``DONE``), sends ``SSSGETCAP`` again and
    releases the device. Nothing else is written, so no label moves except
    the printer's own realign. Ctrl+C stops early and still releases the
    device. Exit status 0 when at least one line was heard, 1 otherwise,
    130 after Ctrl+C.
    """
    layer = _direct_layer()
    off = _direct_off(layer, as_json, "--listen")
    if off is not None or layer is None:
        return off or 1
    opened = _open_listen_printer(layer, as_json)
    if opened is None:
        return 1
    candidate, transport = opened
    if not as_json:
        print(
            f"-- Listening to {candidate.name} for {seconds:g} s --\n"
            "  sends only SSSGETCAP (now and at the end); open and close the "
            "cover now. Ctrl+C stops early.",
            flush=True,
        )
    listener = _Listener(
        transport, write_s=layer.timeouts.query_write_s, echo=not as_json
    )
    with transport, _ctrl_c_flag() as stop:
        try:
            listener.ask_cover()
            listener.listen(seconds, stop)
            if not stop[0] and not listener.failed:
                listener.ask_cover()
                listener.listen(max(LISTEN_FINAL_S, layer.timeouts.query_reply_s), stop)
        except KeyboardInterrupt:
            stop[0] = True
        if stop[0]:
            listener.note("stopped early (Ctrl+C)")
    interrupted = stop[0]
    log_event(
        logger,
        logging.INFO,
        "listen finished",
        printer=candidate.name,
        seconds=seconds,
        heard=listener.heard,
        interrupted=interrupted,
        failed=listener.failed,
        device_released=transport.closed,
    )
    if as_json:
        data = {
            "printer": candidate.name,
            "seconds": seconds,
            "heard": listener.heard,
            "notes": listener.notes,
            "interrupted": interrupted,
            "error": listener.failed or None,
            "device_released": transport.closed,
        }
        print(json.dumps(data, indent=2))
    else:
        print(
            f"-- Heard {len(listener.heard)} line(s): {listener.summary()}; "
            "printer released --"
        )
    if interrupted:
        return 130
    return 0 if listener.heard and not listener.failed else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``ibp-print-diag``. Returns the process exit code."""
    args = _parse_args(argv)
    if args.no_direct:
        set_direct_enabled(False)
    log_dir = configure_logging(args.log_dir, app=LOG_APP, console=args.verbose)
    if args.direct_status:
        return run_direct_status(args.json)
    if args.listen is not None:
        return run_listen(args.listen, args.json)

    discovery = discover()
    probes = (
        probe_direct_devices(discovery.direct_devices)
        if discovery.direct_enabled
        else {}
    )
    events = (
        get_backend().recent_print_events(minutes=args.events)
        if args.events > 0
        else []
    )
    report = build_report(discovery, events, log_dir=log_dir, probes=probes)
    log_event(logger, logging.INFO, "diagnostics report\n" + report)

    exit_code = 0
    test_result: Optional[PrintResult] = None
    test_error = ""
    if args.test_print is not None:
        test_result, test_error = run_test_print(
            args.test_print or None, direct_only=args.direct_only
        )
        exit_code = 0 if test_result is not None and test_result.outcome.ok else 1
        if test_result is None and args.direct_only:
            exit_code = EXIT_DIRECT_NOT_SENT
    elif not discovery.usable:
        exit_code = 1

    if args.json:
        data = report_data(discovery, events, log_dir=log_dir, probes=probes)
        if args.test_print is not None:
            data["test_print"] = _result_data(test_result, test_error)
        print(json.dumps(data, indent=2, default=repr))
    else:
        print(report)
        if args.test_print is not None:
            print()
            print(describe_result(test_result, test_error))
    return exit_code


def _result_data(result: Optional[PrintResult], error: str) -> dict[str, Any]:
    if result is None:
        return {"ok": False, "error": error}
    return {
        "ok": result.outcome.ok,
        "printer": result.printer_name,
        "job_name": result.job_name,
        "job_id": result.job_id,
        "outcome": result.outcome.value,
        "history": result.history,
        "elapsed_s": result.elapsed_s,
    }


if __name__ == "__main__":
    sys.exit(main())
