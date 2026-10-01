"""Platform-independent USB label printer matching.

A print queue is a label printer when its name ends in a ``VID:PID`` suffix
(for example ``"DYMO LabelWriter 450 0922:0020"``), separated from the rest of
the name by whitespace, ``_`` or ``-``. It is usable when a USB device with that
VID:PID is currently present: ghost entries Windows keeps for unplugged devices
(``Present`` false, or ConfigManagerErrorCode 45) do not count, though they are
still listed in diagnostics. Device health and queue status only affect the
ranking, never visibility, so a misbehaving printer still shows up in the logs.
"""

import re
from typing import Iterable, Optional

from ibp_printing.models import Discovery, PrinterCandidate, PrintQueue, UsbDevice

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
