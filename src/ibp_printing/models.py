"""Typed records describing printers, USB devices, and print outcomes."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from ibp_printing.winconst import (
    PRINTER_ATTRIBUTE_BITS,
    PRINTER_ATTRIBUTE_WORK_OFFLINE,
    PRINTER_PROBLEM_MASK,
    PRINTER_STATUS_BITS,
    decode_bits,
)

if TYPE_CHECKING:
    from ibp_printing.direct.transport import DirectDevice

# Printers the direct-USB path can drive, by IEEE 1284 MDL (case-insensitive).
# Matching is by model, never by VID:PID alone: the PM2411BT's VID (Artery,
# 2E3C) is shared by many unrelated devices.
SUPPORTED_DIRECT_MODELS = frozenset({"PM2411BT"})

# Direct candidates are named "<model> (USB direct, serial <serial>)".
DIRECT_NAME_MARK = "(USB direct"

UDEV_RULE_HINT = (
    "install packaging/linux/60-ibp-label-printer.rules to /etc/udev/rules.d/ "
    "and replug the printer"
)


def is_supported_direct_model(model: str) -> bool:
    """True if ``model`` (a 1284 MDL) is a printer the direct path supports."""
    return model.strip().upper() in SUPPORTED_DIRECT_MODELS


@dataclass(frozen=True)
class PrintQueue:
    """A print queue as reported by the OS spooler."""

    name: str
    port: str = ""
    driver: str = ""
    status: int = 0
    attributes: int = 0
    jobs: int = 0
    is_default: bool = False

    @property
    def status_flags(self) -> list[str]:
        """Names of the set PRINTER_STATUS_* bits."""
        return decode_bits(self.status, PRINTER_STATUS_BITS)

    @property
    def attribute_flags(self) -> list[str]:
        """Names of the set PRINTER_ATTRIBUTE_* bits."""
        return decode_bits(self.attributes, PRINTER_ATTRIBUTE_BITS)

    @property
    def has_problem(self) -> bool:
        """True when the spooler reports a state that blocks printing."""
        return bool(self.status & PRINTER_PROBLEM_MASK) or bool(
            self.attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE
        )


# ConfigManagerErrorCode 45 (CM_PROB_PHANTOM): "Currently, this hardware device
# is not connected to the computer." Windows keeps such ghost device nodes for
# previously connected USB devices.
CM_PROB_PHANTOM = 45


@dataclass(frozen=True)
class UsbDevice:
    """A USB Plug and Play entity as reported by WMI."""

    device_id: str
    name: str = ""
    status: str = ""
    error_code: Optional[int] = None
    pnp_class: str = ""
    vid_pid: Optional[str] = None
    # Win32_PnPEntity.Present; None when WMI does not report it (pre-Windows 10).
    present: Optional[bool] = None

    @property
    def connected(self) -> bool:
        """False for ghost entries of devices that are not plugged in right now."""
        return self.present is not False and self.error_code != CM_PROB_PHANTOM

    @property
    def absence_reason(self) -> Optional[str]:
        """Why this entity does not count as plugged in, or None if it does."""
        if self.present is False:
            return "Present=False (Windows remembers it but it is not connected)"
        if self.error_code == CM_PROB_PHANTOM:
            return "error code 45 (device is not connected)"
        return None

    @property
    def healthy(self) -> bool:
        """True when Windows reports the device as working normally."""
        return (
            self.connected
            and self.status.lower() in {"", "ok"}
            and self.error_code in (None, 0)
        )


@dataclass(frozen=True)
class PrinterCandidate:
    """A printer that can be printed to, with the result of each check.

    Usually a print queue (``transport == "queue"``). A printer driven
    directly over USB without a queue or driver has ``direct_device`` set
    (``transport == "direct"``); its ``queue`` is synthetic (the name, the
    device path as ``port``), ``usb_devices`` is empty, and ``vid_pid`` is
    the device's.
    """

    queue: PrintQueue
    vid_pid: Optional[str]
    usb_devices: tuple[UsbDevice, ...] = ()
    # Platforms without USB matching (CUPS) treat every queue as usable.
    usb_matching: bool = True
    # The USB device a direct candidate prints to (None for a queue).
    direct_device: Optional["DirectDevice"] = field(default=None, compare=False)
    # Extra explanations shown by reasons() (e.g. "same printer as ...").
    notes: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The name used to print to this printer (spooler or direct name)."""
        return self.queue.name

    @property
    def transport(self) -> str:
        """``"direct"`` (USB, no queue or driver) or ``"queue"`` (OS spooler)."""
        return "direct" if self.direct_device is not None else "queue"

    @property
    def is_direct(self) -> bool:
        """True for a printer driven directly over USB."""
        return self.direct_device is not None

    @property
    def connected_devices(self) -> tuple[UsbDevice, ...]:
        """Matching USB entities that are actually plugged in (not ghosts)."""
        return tuple(device for device in self.usb_devices if device.connected)

    @property
    def usable(self) -> bool:
        """True when the printer is present and can be tried.

        A queue: a label printer whose USB device is present. A direct
        device: present and recognised by its 1284 model.
        """
        if self.direct_device is not None:
            return self.direct_device.present and is_supported_direct_model(
                self.direct_device.model
            )
        if not self.usb_matching:
            return True
        return self.vid_pid is not None and bool(self.connected_devices)

    @property
    def device_healthy(self) -> bool:
        """True when at least one connected matching USB device is healthy.

        For a direct device: present, not busy, and not known to be
        inaccessible (permissions).
        """
        if self.direct_device is not None:
            device = self.direct_device
            return device.present and not device.busy and device.accessible is not False
        if not self.usb_matching:
            return True
        return any(device.healthy for device in self.connected_devices)

    def reasons(self) -> list[str]:
        """Human-readable explanation of each check for this printer."""
        if self.direct_device is not None:
            return self._direct_reasons() + list(self.notes)
        return self._queue_reasons() + list(self.notes)

    def _direct_reasons(self) -> list[str]:
        device = self.direct_device
        assert device is not None
        model = device.model or "(unknown)"
        supported = is_supported_direct_model(device.model)
        reasons = [
            f"USB direct ({device.kind or 'usb'}) at {device.path}; 1284 model "
            f"{model} " + ("is supported" if supported else "is NOT supported")
        ]
        if not device.model:
            reasons.append(
                "could not read the printer's 1284 ID, so it cannot be identified"
            )
        if not device.present:
            reasons.append(f"device node {device.path} is missing")
        if device.busy:
            reasons.append("in use by another program or the Windows spooler right now")
        if device.accessible is False:
            reasons.append(f"no read/write permission: {UDEV_RULE_HINT}")
        reasons += [f"note: {error}" for error in device.errors]
        return reasons

    def _queue_reasons(self) -> list[str]:
        if not self.usb_matching:
            return ["no USB matching on this platform; every queue is usable"]
        if self.vid_pid is None:
            return ["name does not end in a VID:PID suffix like ' 0922:0028'"]
        if not self.usb_devices:
            return [f"no USB device with VID:PID {self.vid_pid} is present"]
        ghosts = [
            f"{device.device_id}: {device.absence_reason}"
            for device in self.usb_devices
            if not device.connected
        ]
        if not self.connected_devices:
            return [
                f"USB {self.vid_pid} is known to Windows but not connected "
                f"({len(ghosts)} ghost entities)"
            ] + [f"not connected: {ghost}" for ghost in ghosts]
        reasons = [
            f"USB {self.vid_pid} present ({len(self.connected_devices)} entities)"
        ]
        reasons += [f"ignored, not connected: {ghost}" for ghost in ghosts]
        if not self.device_healthy:
            reasons.append("USB device reports a non-OK status or error code")
        if self.queue.has_problem:
            flags = self.queue.status_flags + [
                flag for flag in self.queue.attribute_flags if flag == "WORK_OFFLINE"
            ]
            reasons.append(f"queue reports problem: {', '.join(flags)}")
        return reasons

    def rank_key(self) -> tuple[bool, bool, bool, bool, str]:
        """Sort key: healthy direct device first, then the queue rules.

        A direct USB printer that looks ready comes before every queue: it
        needs no driver and reports how the print ended. Among queues (and
        direct devices with a known problem, which rank with the unhealthy
        ones): healthy device, then problem-free queue, then default first.
        Device health ranks above the queue's problem flags: a queue flag
        such as OFFLINE often clears by itself once a job is sent to a
        working device, while a device Windows reports as broken will not
        print.
        """
        return (
            not (self.is_direct and self.device_healthy),
            not self.device_healthy,
            self.queue.has_problem,
            not self.queue.is_default,
            self.name,
        )

    def to_log(self) -> dict[str, Any]:
        """Flatten for structured logging."""
        return {
            "name": self.name,
            "transport": self.transport,
            "port": self.queue.port,
            "driver": self.queue.driver,
            "status": self.queue.status_flags,
            "attributes": self.queue.attribute_flags,
            "queued_jobs": self.queue.jobs,
            "is_default": self.queue.is_default,
            "vid_pid": self.vid_pid,
            "usable": self.usable,
            "reasons": self.reasons(),
            "usb_devices": [asdict(device) for device in self.usb_devices],
            "direct_device": (
                self.direct_device.to_log() if self.direct_device is not None else None
            ),
        }


@dataclass
class Discovery:
    """Everything learned during one printer discovery pass."""

    queues: list[PrintQueue] = field(default_factory=list)
    usb_devices: list[UsbDevice] = field(default_factory=list)
    candidates: list[PrinterCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Every USB printer-class device seen by direct discovery (any model).
    direct_devices: list["DirectDevice"] = field(default_factory=list)
    # Whether direct USB printing was on for this pass (None: not asked,
    # e.g. a custom backend without the direct layer).
    direct_enabled: Optional[bool] = None

    @property
    def usable(self) -> list[PrinterCandidate]:
        """Usable candidates, best first."""
        return sorted(
            (candidate for candidate in self.candidates if candidate.usable),
            key=PrinterCandidate.rank_key,
        )


@dataclass(frozen=True)
class DeviceCaps:
    """The GetDeviceCaps values that drive label placement."""

    horzres: int
    vertres: int
    physical_width: int
    physical_height: int
    offset_x: int = 0
    offset_y: int = 0
    dpi_x: int = 0
    dpi_y: int = 0


class JobOutcome(str, Enum):
    """How a spooled job ended (as far as the spooler tells us)."""

    COMPLETED = "completed"
    VANISHED_UNSEEN = "vanished_unseen"
    ERROR = "error"
    DELETED = "deleted"
    TIMEOUT = "timeout"
    NOT_TRACKED = "not_tracked"
    # A failure after StartDoc: the job may or may not have reached the printer.
    UNCERTAIN = "uncertain"
    # The job was spooled, but following it in the queue raised.
    TRACKING_FAILED = "tracking_failed"

    @property
    def ok(self) -> bool:
        """True for outcomes that most likely produced a label.

        Every other outcome means "the label may not have printed, and may
        still print later": check the printer before reprinting, and never
        refund or send it to a second printer automatically.
        """
        return self in {
            JobOutcome.COMPLETED,
            JobOutcome.VANISHED_UNSEEN,
            JobOutcome.NOT_TRACKED,
        }


@dataclass(frozen=True)
class JobSnapshot:
    """One observation of a job in the spooler queue."""

    job_id: int
    document: str
    status: int
    status_text: str = ""
    pages_printed: int = 0
    total_pages: int = 0


@dataclass
class PrintResult:
    """Result of sending one image to one printer."""

    printer_name: str
    job_name: str
    job_id: Optional[int] = None
    outcome: JobOutcome = JobOutcome.NOT_TRACKED
    history: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
