"""``ibp-print-diag``: explain which printers are usable and why, and test them.

The default action runs a full discovery pass and prints a report showing,
for every print queue, each detection gate and its result, followed by every
USB device that has a VID:PID, the final verdict, recent PrintService events,
and where the logs live. ``--test-print`` sends a 4x6 test label.
"""

import argparse
import json
import logging
import socket
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from ibp_printing.api import (
    discover,
    get_backend,
    print_image,
    print_to_first_available,
)
from ibp_printing.backends import PrintError
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


# -- report ------------------------------------------------------------------


def build_report(
    discovery: Discovery,
    events: Sequence[Mapping[str, Any]] = (),
    *,
    log_dir: Optional[Path] = None,
) -> str:
    """Render a human-readable diagnostics report for one discovery pass.

    Args:
        discovery: The result of :func:`ibp_printing.discover`.
        events: Recent OS print-subsystem events (newest first).
        log_dir: Log directory to point the reader at; defaults to
            :func:`ibp_printing.default_log_dir`.
    """
    lines = [
        RULE,
        "ibp-printing printer diagnostics",
        f"host={socket.gethostname()} platform={sys.platform} "
        f"time={datetime.now().isoformat(timespec='seconds')}",
        RULE,
        "",
    ]
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
) -> dict[str, Any]:
    """The same information as :func:`build_report`, as JSON-friendly data."""
    return {
        "host": socket.gethostname(),
        "platform": sys.platform,
        "time": datetime.now().isoformat(timespec="seconds"),
        "candidates": [candidate.to_log() for candidate in discovery.candidates],
        "usb_devices": [asdict(device) for device in discovery.usb_devices],
        "errors": list(discovery.errors),
        "usable": [candidate.name for candidate in discovery.usable],
        "events": [dict(event) for event in events],
        "log_dir": str(log_dir or default_log_dir()),
    }


def _queue_section(discovery: Discovery) -> list[str]:
    lines = ["-- Print queues --"]
    if not discovery.candidates:
        lines.append("  (no print queues found)")
        return lines
    for index, candidate in enumerate(discovery.candidates, start=1):
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
        lines.append(f"  {rank}. {candidate.name}")
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


def run_test_print(printer_name: Optional[str]) -> tuple[Optional[PrintResult], str]:
    """Print a test label and return ``(result, error)``; error is "" on success.

    Args:
        printer_name: The queue to print to, or None/"" for the best usable one.
    """
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
    return "PROBLEM"


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
        "-v", "--verbose", action="store_true", help="also echo the log to stderr"
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``ibp-print-diag``. Returns the process exit code."""
    args = _parse_args(argv)
    log_dir = configure_logging(args.log_dir, app=LOG_APP, console=args.verbose)

    discovery = discover()
    events = (
        get_backend().recent_print_events(minutes=args.events)
        if args.events > 0
        else []
    )
    report = build_report(discovery, events, log_dir=log_dir)
    log_event(logger, logging.INFO, "diagnostics report\n" + report)

    exit_code = 0
    test_result: Optional[PrintResult] = None
    test_error = ""
    if args.test_print is not None:
        test_result, test_error = run_test_print(args.test_print or None)
        exit_code = 0 if test_result is not None and test_result.outcome.ok else 1
    elif not discovery.usable:
        exit_code = 1

    if args.json:
        data = report_data(discovery, events, log_dir=log_dir)
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
