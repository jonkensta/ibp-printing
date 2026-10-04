"""Platform-independent USB label printer matching.

A print queue is a label printer when its name ends in a ``VID:PID`` suffix
(for example ``"DYMO LabelWriter 450 0922:0020"``), separated from the rest of
the name by whitespace, ``_`` or ``-``. It is usable when a USB device with that
VID:PID is currently present: ghost entries Windows keeps for unplugged devices
(``Present`` false, or ConfigManagerErrorCode 45) do not count, though they are
still listed in diagnostics. Device health and queue status only affect the
ranking, never visibility, so a misbehaving printer still shows up in the logs.

Direct USB devices (``ibp_printing.direct``) become candidates of their own
(:func:`direct_candidates`, merged by :func:`merge_direct`): a supported model
(by IEEE 1284 MDL) or anything with the PM2411BT's VID:PID, so diagnostics can
explain an unrecognised one. They rank ahead of queues. A queue whose VID:PID
matches a usable direct device is kept (as the fallback) with a note.
"""

import dataclasses
import re
from typing import TYPE_CHECKING, Iterable, Optional

from ibp_printing.models import (
    DIRECT_NAME_MARK,
    Discovery,
    PrinterCandidate,
    PrintQueue,
    UsbDevice,
    is_supported_direct_model,
)

if TYPE_CHECKING:
    from ibp_printing.direct.transport import DirectDevice

# The PM2411BT's USB VID:PID (shared by other Artery-based devices, so only
# used to decide which unrecognised devices are worth explaining).
PM2411BT_VID_PID = "2E3C:5760"
DIRECT_DRIVER = "(none: direct USB)"

NAME_VID_PID_PATTERN = re.compile(r"(?:^|[\s_\-])([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})$")
DEVICE_VID_PID_PATTERN = re.compile(
    r"VID_([0-9A-Fa-f]{4}).*?PID_([0-9A-Fa-f]{4})", re.IGNORECASE
)


def vid_pid_from_printer_name(name: str) -> Optional[str]:
    """Return the uppercase ``VID:PID`` suffix of a queue name, if any."""
    match = NAME_VID_PID_PATTERN.search(name.rstrip())
    if match is None:
        return None
    return f"{match.group(1)}:{match.group(2)}".upper()


def vid_pid_from_device_id(device_id: str) -> Optional[str]:
    """Return the uppercase ``VID:PID`` of a Windows PnP device ID, if any."""
    match = DEVICE_VID_PID_PATTERN.search(device_id)
    if match is None:
        return None
    return f"{match.group(1)}:{match.group(2)}".upper()


def build_candidates(
    queues: Iterable[PrintQueue], usb_devices: Iterable[UsbDevice]
) -> list[PrinterCandidate]:
    """Pair every queue with the USB devices that match its name suffix."""
    by_vid_pid: dict[str, list[UsbDevice]] = {}
    for device in usb_devices:
        if device.vid_pid:
            by_vid_pid.setdefault(device.vid_pid, []).append(device)

    candidates = []
    for queue in queues:
        vid_pid = vid_pid_from_printer_name(queue.name)
        devices = tuple(by_vid_pid.get(vid_pid, ())) if vid_pid else ()
        candidates.append(PrinterCandidate(queue, vid_pid, devices))
    return candidates


def discovery_from(
    queues: list[PrintQueue],
    usb_devices: list[UsbDevice],
    errors: Optional[list[str]] = None,
) -> Discovery:
    """Assemble a Discovery from raw queue and device listings."""
    return Discovery(
        queues=queues,
        usb_devices=usb_devices,
        candidates=build_candidates(queues, usb_devices),
        errors=list(errors or []),
    )


def direct_candidate_name(device: "DirectDevice") -> str:
    """``"PM2411BT (USB direct, serial Q529...)"`` (the path if no serial)."""
    label = device.model or device.product or f"USB printer {device.vid_pid or '?'}"
    where = f"serial {device.serial}" if device.serial else device.path
    return f"{label} {DIRECT_NAME_MARK}, {where})"


def is_direct_name(name: str) -> bool:
    """True if ``name`` looks like a direct candidate's name."""
    return DIRECT_NAME_MARK in name


def direct_candidates(devices: Iterable["DirectDevice"]) -> list[PrinterCandidate]:
    """Candidates for supported models and PM2411BT-VID:PID devices."""
    candidates = []
    for device in devices:
        if not (
            is_supported_direct_model(device.model)
            or device.vid_pid == PM2411BT_VID_PID
        ):
            continue
        queue = PrintQueue(
            name=direct_candidate_name(device), port=device.path, driver=DIRECT_DRIVER
        )
        candidates.append(
            PrinterCandidate(
                queue,
                device.vid_pid or None,
                (),
                usb_matching=True,
                direct_device=device,
            )
        )
    return candidates


def merge_direct(discovery: Discovery, devices: list["DirectDevice"]) -> Discovery:
    """Add direct candidates (first) to ``discovery`` and note shadowed queues.

    A queue with the VID:PID of a usable direct candidate is probably the same
    physical printer: it stays a candidate (ranked after the direct one, so it
    is the fallback when the direct path definitely did not print) and says so.
    """
    discovery.direct_devices = list(devices)
    direct = direct_candidates(devices)
    by_vid_pid = {
        candidate.vid_pid: candidate.name
        for candidate in direct
        if candidate.usable and candidate.vid_pid
    }
    queues = []
    for candidate in discovery.candidates:
        twin = by_vid_pid.get(candidate.vid_pid) if candidate.vid_pid else None
        if twin and not candidate.is_direct:
            note = (
                f"probably the same printer as {twin!r}; used only if direct "
                "USB printing definitely did not print"
            )
            candidate = dataclasses.replace(candidate, notes=candidate.notes + (note,))
        queues.append(candidate)
    discovery.candidates = direct + queues
    return discovery
