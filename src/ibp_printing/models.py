"""Typed records describing printers, USB devices, and print outcomes."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from ibp_printing.winconst import (
    PRINTER_ATTRIBUTE_BITS,
    PRINTER_ATTRIBUTE_WORK_OFFLINE,
    PRINTER_PROBLEM_MASK,
    PRINTER_STATUS_BITS,
    decode_bits,
)


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


@dataclass(frozen=True)
class UsbDevice:
    """A USB Plug and Play entity as reported by WMI."""

    device_id: str
    name: str = ""
    status: str = ""
    error_code: Optional[int] = None
    pnp_class: str = ""
    vid_pid: Optional[str] = None

    @property
    def healthy(self) -> bool:
        """True when Windows reports the device as working normally."""
        return self.status.lower() in {"", "ok"} and self.error_code in (None, 0)


@dataclass(frozen=True)
class PrinterCandidate:
    """A print queue together with the result of each detection check."""

    queue: PrintQueue
    vid_pid: Optional[str]
    usb_devices: tuple[UsbDevice, ...] = ()
    # Platforms without USB matching (CUPS) treat every queue as usable.
    usb_matching: bool = True

    @property
    def name(self) -> str:
        """The spooler name used to print to this queue."""
        return self.queue.name

    @property
    def usable(self) -> bool:
        """True when the queue is a label printer whose USB device is present."""
        if not self.usb_matching:
            return True
        return self.vid_pid is not None and bool(self.usb_devices)

    @property
    def device_healthy(self) -> bool:
        """True when at least one matching USB device reports a healthy state."""
        if not self.usb_matching:
            return True
        return any(device.healthy for device in self.usb_devices)

    def reasons(self) -> list[str]:
        """Human-readable explanation of each check for this queue."""
        if not self.usb_matching:
            return ["no USB matching on this platform; every queue is usable"]
        if self.vid_pid is None:
            return ["name does not end in a VID:PID suffix like ' 0922:0028'"]
        if not self.usb_devices:
            return [f"no USB device with VID:PID {self.vid_pid} is present"]
        reasons = [f"USB {self.vid_pid} present ({len(self.usb_devices)} entities)"]
        if not self.device_healthy:
            reasons.append("USB device reports a non-OK status or error code")
        if self.queue.has_problem:
            flags = self.queue.status_flags + [
                flag for flag in self.queue.attribute_flags if flag == "WORK_OFFLINE"
            ]
            reasons.append(f"queue reports problem: {', '.join(flags)}")
        return reasons

    def rank_key(self) -> tuple[bool, bool, bool, str]:
        """Sort key: healthy, problem-free, default queues first."""
        return (
            self.queue.has_problem,
            not self.device_healthy,
            not self.queue.is_default,
            self.name,
        )

    def to_log(self) -> dict[str, Any]:
        """Flatten for structured logging."""
        return {
            "name": self.name,
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
        }


@dataclass
class Discovery:
    """Everything learned during one printer discovery pass."""

    queues: list[PrintQueue] = field(default_factory=list)
    usb_devices: list[UsbDevice] = field(default_factory=list)
    candidates: list[PrinterCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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

    @property
    def ok(self) -> bool:
        """True for outcomes that most likely produced a label."""
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
