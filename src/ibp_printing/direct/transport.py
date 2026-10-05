"""Raw USB transport to a label printer, without a driver, CUPS or a print queue.

The printer is talked to through the operating system's USB printer-class
driver, which hands us a byte pipe to the bulk OUT / bulk IN endpoints:

* Linux: ``usblp`` creates ``/dev/usb/lpN`` (:class:`LinuxUsblpTransport`).
* Windows: ``usbprint.sys`` exposes a device interface of class
  ``GUID_DEVINTERFACE_USBPRINT`` (:class:`WindowsUsbprintTransport`).

Every transport has the same small contract (:class:`Transport`):

* ``write_all(data, timeout_s) -> int`` writes as much as the printer accepts
  before the timeout and returns the count. A printer that stops accepting
  data (paper out, jam) is *not* an exception: the caller compares the count
  with ``len(data)``. A dead or closed device raises; so does a Windows
  write whose count Windows cannot vouch for (cancelled after it started:
  ``bytes_accepted=None``, assume data was sent).
* ``read_lines(timeout_s) -> list[str]`` returns complete lines the printer
  sent (decoded latin-1, terminators removed). It keeps polling through empty
  reads until at least one line is complete or the timeout passes; partial
  lines are buffered for the next call.
* ``close()`` is idempotent; transports are context managers.

:class:`FakeTransport` is a scriptable stand-in for tests. See
``docs/printers/pm2411bt.md`` for the verified hardware behaviour.
"""

# The Win32 section mirrors C names (GUID fields, OVERLAPPED members).
# pylint: disable=too-many-lines,invalid-name,too-few-public-methods
# pylint: disable=attribute-defined-outside-init,too-many-instance-attributes

from __future__ import annotations

import abc
import ctypes
import errno
import logging
import os
import re
import select
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Iterable, Optional, Self, Sequence, Union

from ibp_printing.log import describe_exception, get_logger, log_event

logger = get_logger(__name__)

PM2411BT_VID_PID = "2E3C:5760"
"""USB VID:PID of the PM2411BT. The VID (Artery) is shared by many OEM devices:
identify the printer by its 1284 ``MDL`` instead."""

UDEV_RULE_PATH = "packaging/linux/60-ibp-label-printer.rules"

# Lines longer than this without a line feed are flushed as a line anyway, so
# a printer sending binary junk cannot grow the buffer without bound.
MAX_LINE_BYTES = 64 * 1024


# -- errors ------------------------------------------------------------------


class TransportError(Exception):
    """Base class for transport failures."""


class DeviceUnavailable(TransportError):
    """The device could not be opened; nothing was sent to it.

    ``reason`` is one of ``"busy"``, ``"permission"``, ``"not_found"`` or
    ``"error"``. ``errno`` / ``winerror`` carry the OS error code, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        path: str = "",
        os_errno: Optional[int] = None,
        winerror: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.path = path
        self.errno = os_errno
        self.winerror = winerror


class DeviceGone(TransportError):
    """The device stopped working (unplugged, I/O error) during I/O.

    ``bytes_accepted`` is how much of the current ``write_all`` call the device
    had accepted before the failure (0 for reads). ``None`` means *unknown*:
    a write request was in flight when it failed and its byte count cannot be
    trusted, so part of the data may have reached the printer.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str = "",
        bytes_accepted: Optional[int] = 0,
        os_errno: Optional[int] = None,
        winerror: Optional[int] = None,
        request_started: bool = True,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.bytes_accepted = bytes_accepted
        # False when the failing request was refused before it started (so
        # it transferred nothing).
        self.request_started = request_started
        self.errno = os_errno
        self.winerror = winerror


class IndeterminateIO(DeviceGone):
    """An overlapped request whose completion could not be confirmed.

    The request was cancelled (or its wait failed) and Windows never
    reported it finished, so it may still be transferring data. Its buffers
    are kept alive for the rest of the process (see :data:`ABANDONED`), the
    transport refuses further I/O, and for a write ``bytes_accepted`` is
    ``None``: the caller must assume the data may have reached the printer.
    """


class WriteCountUnknown(DeviceGone):
    """A write request was cancelled (aborted) after it had started.

    Windows confirmed the request is finished, but the byte count of a
    failed request is not trustworthy (``GetOverlappedResult`` returning
    FALSE), so part of the data may have reached the printer even when the
    count says 0: ``bytes_accepted`` is None. The transport stays usable.
    """


class TransportClosed(TransportError):
    """The transport was used after ``close()``."""


# -- device description ------------------------------------------------------

_IEEE1284_KEY_ALIASES = {
    "MANUFACTURER": "MFG",
    "MODEL": "MDL",
    "COMMAND SET": "CMD",
    "COMMANDSET": "CMD",
    "CLASS": "CLS",
    "DESCRIPTION": "DES",
}


def parse_ieee1284_id(raw: str) -> dict[str, str]:
    """Parse an IEEE 1284 device ID (``KEY:value;KEY:value;``) into a dict.

    Keys are upper-cased and long forms are mapped to the short ones
    (``MODEL`` -> ``MDL``, ``MANUFACTURER`` -> ``MFG``, ``COMMAND SET`` ->
    ``CMD``, ``CLASS`` -> ``CLS``, ``DESCRIPTION`` -> ``DES``); values are
    stripped. The first occurrence of a key wins. Fields without a colon are
    ignored.
    """
    result: dict[str, str] = {}
    for part in raw.replace("\x00", "").split(";"):
        key, sep, value = part.partition(":")
        if not sep:
            continue
        key = key.strip().upper()
        if not key:
            continue
        key = _IEEE1284_KEY_ALIASES.get(key, key)
        result.setdefault(key, value.strip())
    return result


@dataclass
class DirectDevice:
    """A USB printer-class device reachable without a driver or queue.

    ``path`` is what :func:`open_transport` opens (``/dev/usb/lp0`` or a
    ``\\\\?\\usb#vid_...`` interface path). ``vid_pid`` is upper-case
    ``"2E3C:5760"`` (empty if unknown). ``model`` is the 1284 ``MDL`` unless
    given explicitly. ``kind`` is ``"usblp"``, ``"usbprint"`` or ``"fake"``.
    ``present`` is False when the device node is missing (Linux sysfs lists
    the device but ``/dev/usb/lpN`` does not exist). ``accessible`` is whether
    this user can open it read-write: True/False when known (Linux: file
    permissions; Windows: the 1284 ID query's open succeeded), None if unknown.
    ``busy`` is True when discovery's open failed because something else had
    the device open (Windows spooler or another program).
    """

    path: str
    vid_pid: str = ""
    serial: Optional[str] = None
    ieee1284: dict[str, str] = field(default_factory=dict)
    model: str = ""
    ieee1284_raw: str = ""
    kind: str = ""
    manufacturer: str = ""
    product: str = ""
    errors: list[str] = field(default_factory=list)
    present: bool = True
    accessible: Optional[bool] = None
    busy: bool = False

    def __post_init__(self) -> None:
        if self.ieee1284_raw and not self.ieee1284:
            self.ieee1284 = parse_ieee1284_id(self.ieee1284_raw)
        if not self.model:
            self.model = self.ieee1284.get("MDL", "")
        self.vid_pid = self.vid_pid.upper()

    def to_log(self) -> dict[str, Any]:
        """Everything about the device, for a structured log record."""
        return {
            "path": self.path,
            "kind": self.kind,
            "vid_pid": self.vid_pid,
            "serial": self.serial,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "product": self.product,
            "ieee1284": self.ieee1284_raw or self.ieee1284,
            "present": self.present,
            "accessible": self.accessible,
            "busy": self.busy,
            "errors": self.errors,
        }


# -- shared line buffering ---------------------------------------------------


class _LineBuffer:
    """Accumulates received bytes and splits them into lines.

    Lines end with CR LF; a bare LF is also accepted (a trailing CR is
    stripped), so a stray LF-only reply is not lost.
    """

    def __init__(self) -> None:
        self._data = bytearray()

    def __len__(self) -> int:
        return len(self._data)

    def feed(self, data: bytes) -> None:
        """Append received bytes."""
        self._data.extend(data)

    def take_lines(self) -> list[str]:
        """Remove and return every complete line."""
        lines: list[str] = []
        while True:
            index = self._data.find(b"\n")
            if index < 0:
                break
            raw = bytes(self._data[:index])
            del self._data[: index + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            lines.append(raw.decode("latin-1"))
        if len(self._data) > MAX_LINE_BYTES:
            log_event(
                logger,
                logging.WARNING,
                "received data without a line feed; flushing it as a line",
                partial_bytes=len(self._data),
            )
            lines.append(bytes(self._data).decode("latin-1"))
            self._data.clear()
        return lines

    def pending(self) -> bytes:
        """The buffered partial line (for logging)."""
        return bytes(self._data)


# -- the contract ------------------------------------------------------------


class Transport(abc.ABC):
    """A byte pipe to one printer. See the module docstring for the contract."""

    path: str = ""

    def __init__(self) -> None:
        self._closed = False
        self._lines = _LineBuffer()
        self._opened_at = time.monotonic()
        self.bytes_written = 0
        self.bytes_read = 0

    @property
    def closed(self) -> bool:
        """True once ``close()`` was called."""
        return self._closed

    @abc.abstractmethod
    def write_all(self, data: bytes, timeout_s: float) -> int:
        """Write ``data``; return how many bytes the device accepted.

        Returns early (with fewer than ``len(data)`` bytes) when the device
        stops accepting data for ``timeout_s`` seconds overall. Raises
        :class:`DeviceGone` for a dead device, :class:`TransportClosed` after
        ``close()``, and on Windows :class:`WriteCountUnknown` /
        :class:`IndeterminateIO` (``bytes_accepted=None``) when a timed-out
        request was cancelled after it started, because its count cannot be
        trusted: the caller must assume part of the data was sent.
        """

    @abc.abstractmethod
    def read_lines(self, timeout_s: float) -> list[str]:
        """Return complete lines received, waiting up to ``timeout_s``.

        Lines are decoded latin-1 without their CR LF. Returns as soon as at
        least one line is complete (after draining what is immediately
        available); keeps polling through empty reads otherwise, and returns
        ``[]`` at the timeout. A partial line stays buffered for the next call.
        """

    @abc.abstractmethod
    def _close_impl(self) -> None:
        """Release the OS handle (called once)."""

    def close(self) -> None:
        """Release the device. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        error: Optional[BaseException] = None
        try:
            self._close_impl()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            error = exc
        log_event(
            logger,
            logging.WARNING if error else logging.INFO,
            "transport closed",
            transport=type(self).__name__,
            path=self.path,
            bytes_written=self.bytes_written,
            bytes_read=self.bytes_read,
            unread_partial=self._lines.pending().decode("latin-1"),
            open_s=round(time.monotonic() - self._opened_at, 3),
            error=describe_exception(error) if error else None,
        )

    def _check_open(self) -> None:
        if self._closed:
            raise TransportClosed(f"transport for {self.path} is closed")

    def _log_lines(self, lines: list[str], started: float) -> list[str]:
        for line in lines:
            log_event(logger, logging.DEBUG, "read line", path=self.path, line=line)
        log_event(
            logger,
            logging.DEBUG,
            "read_lines returned",
            path=self.path,
            lines=lines,
            partial=self._lines.pending().decode("latin-1"),
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return lines

    def _log_read_timeout(self, timeout_s: float, started: float, reads: int) -> None:
        log_event(
            logger,
            logging.DEBUG,
            "read_lines timed out with no complete line",
            path=self.path,
            timeout_s=timeout_s,
            read_attempts=reads,
            partial=self._lines.pending().decode("latin-1"),
            elapsed_s=round(time.monotonic() - started, 3),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


# -- Linux: usblp ------------------------------------------------------------

_O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
_O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)

# errno values that mean the device is gone or broken, not merely busy.
_DEAD_ERRNOS = frozenset(
    {
        errno.ENODEV,
        errno.ENXIO,
        errno.EPIPE,
        errno.EBADF,
        errno.EIO,
        errno.ESHUTDOWN,
        errno.ECONNRESET,
    }
)


def _linux_open_error(path: str, exc: OSError) -> DeviceUnavailable:
    code = exc.errno
    if code == errno.EBUSY:
        return DeviceUnavailable(
            f"{path} is busy: another program (CUPS, another app) has it open",
            reason="busy",
            path=path,
            os_errno=code,
        )
    if code in (errno.EACCES, errno.EPERM):
        return DeviceUnavailable(
            f"permission denied opening {path}: install the udev rule "
            f"{UDEV_RULE_PATH} (to /etc/udev/rules.d/, then replug the printer) "
            "or add the user to the 'lp' group",
            reason="permission",
            path=path,
            os_errno=code,
        )
    if code in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
        return DeviceUnavailable(
            f"{path} not found (printer unplugged or off?)",
            reason="not_found",
            path=path,
            os_errno=code,
        )
    return DeviceUnavailable(
        f"cannot open {path}: {exc}", reason="error", path=path, os_errno=code
    )


class LinuxUsblpTransport(Transport):
    """``/dev/usb/lpN`` opened read-write and non-blocking.

    usblp allows a single open (a second one gets EBUSY). Writes that the
    printer is not draining fail with EAGAIN; reads with nothing pending fail
    with EAGAIN or return 0 bytes. Both are waited out with ``select`` in
    short slices until the caller's timeout.
    """

    chunk_size = 4096
    poll_slice_s = 0.05
    read_size = 4096

    def __init__(self, path: str, *, fd: Optional[int] = None) -> None:
        super().__init__()
        self.path = path
        if fd is None:
            started = time.monotonic()
            try:
                fd = os.open(path, os.O_RDWR | _O_NONBLOCK | _O_CLOEXEC)
            except OSError as exc:
                error = _linux_open_error(path, exc)
                log_event(
                    logger,
                    logging.WARNING,
                    "open failed",
                    path=path,
                    reason=error.reason,
                    error=describe_exception(exc),
                    elapsed_s=round(time.monotonic() - started, 3),
                )
                raise error from exc
            log_event(
                logger,
                logging.INFO,
                "opened usblp device",
                path=path,
                fd=fd,
                elapsed_s=round(time.monotonic() - started, 3),
            )
        else:
            log_event(logger, logging.DEBUG, "using supplied fd", path=path, fd=fd)
        self._fd = fd

    def _close_impl(self) -> None:
        os.close(self._fd)

    def _dead(self, action: str, exc: OSError, accepted: int = 0) -> DeviceGone:
        log_event(
            logger,
            logging.ERROR,
            f"{action} failed: device gone or broken",
            path=self.path,
            bytes_accepted=accepted,
            error=describe_exception(exc),
        )
        return DeviceGone(
            f"{action} on {self.path} failed: {exc}",
            path=self.path,
            bytes_accepted=accepted,
            os_errno=exc.errno,
        )

    def write_all(  # pylint: disable=too-many-locals
        self, data: bytes, timeout_s: float
    ) -> int:
        self._check_open()
        view = memoryview(data)
        total = len(view)
        started = time.monotonic()
        deadline = started + max(0.0, timeout_s)
        sent = 0
        chunks = 0
        eagain_total = 0
        stall_started: Optional[float] = None
        stall_eagains = 0
        log_event(
            logger,
            logging.DEBUG,
            "write_all start",
            path=self.path,
            bytes=total,
            timeout_s=timeout_s,
        )
        while sent < total:
            chunk = view[sent : sent + self.chunk_size]
            try:
                written: Optional[int] = os.write(self._fd, chunk)
            except BlockingIOError:
                written = None
            except InterruptedError:
                continue
            except OSError as exc:
                self.bytes_written += sent
                raise self._dead("write", exc, sent) from exc
            if written:
                now = time.monotonic()
                if stall_started is not None:
                    log_event(
                        logger,
                        logging.DEBUG,
                        "write resumed after EAGAIN",
                        path=self.path,
                        waited_s=round(now - stall_started, 3),
                        eagain_count=stall_eagains,
                        sent=sent,
                    )
                    stall_started = None
                    stall_eagains = 0
                sent += written
                chunks += 1
                log_event(
                    logger,
                    logging.DEBUG,
                    "write chunk",
                    path=self.path,
                    chunk=chunks,
                    bytes=written,
                    sent=sent,
                    total=total,
                    elapsed_s=round(now - started, 3),
                )
                continue
            # EAGAIN (or a zero-byte write): the printer is not draining.
            eagain_total += 1
            stall_eagains += 1
            now = time.monotonic()
            if stall_started is None:
                stall_started = now
                log_event(
                    logger,
                    logging.DEBUG,
                    "write got EAGAIN; waiting for the printer to drain",
                    path=self.path,
                    sent=sent,
                    total=total,
                    remaining_s=round(deadline - now, 3),
                )
            remaining = deadline - now
            if remaining <= 0:
                break
            select.select([], [self._fd], [], min(remaining, self.poll_slice_s))
        self.bytes_written += sent
        elapsed = round(time.monotonic() - started, 3)
        if sent < total:
            log_event(
                logger,
                logging.WARNING,
                "write_all timed out: printer stopped accepting data",
                path=self.path,
                sent=sent,
                total=total,
                chunks=chunks,
                eagain_count=eagain_total,
                stalled_s=(
                    round(time.monotonic() - stall_started, 3)
                    if stall_started is not None
                    else None
                ),
                timeout_s=timeout_s,
                elapsed_s=elapsed,
            )
        else:
            log_event(
                logger,
                logging.DEBUG,
                "write_all done",
                path=self.path,
                bytes=total,
                chunks=chunks,
                eagain_count=eagain_total,
                elapsed_s=elapsed,
            )
        return sent

    def _read_available(self) -> tuple[int, int]:
        """Read everything available without blocking: ``(bytes, empty_reads)``."""
        got = 0
        empty = 0
        while True:
            try:
                data = os.read(self._fd, self.read_size)
            except BlockingIOError:
                return got, empty + 1
            except InterruptedError:
                continue
            except OSError as exc:
                raise self._dead("read", exc) from exc
            if not data:
                # usblp can complete a read with 0 bytes; it is not EOF.
                return got, empty + 1
            got += len(data)
            self.bytes_read += len(data)
            log_event(
                logger,
                logging.DEBUG,
                "read bytes",
                path=self.path,
                bytes=len(data),
                data=data.decode("latin-1"),
            )
            self._lines.feed(data)

    def read_lines(self, timeout_s: float) -> list[str]:
        self._check_open()
        started = time.monotonic()
        deadline = started + max(0.0, timeout_s)
        reads = 0
        while True:
            _, empty = self._read_available()
            reads += 1
            lines = self._lines.take_lines()
            if lines:
                return self._log_lines(lines, started)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._log_read_timeout(timeout_s, started, reads)
                return []
            ready, _, _ = select.select(
                [self._fd], [], [], min(remaining, self.poll_slice_s)
            )
            if ready and empty:
                # Readable but nothing came (a zero-length transfer, or EOF on
                # a test pipe): do not spin.
                time.sleep(min(max(0.0, deadline - time.monotonic()), 0.01))


def discover_linux_usblp(
    sys_root: Union[str, Path] = "/sys", dev_root: Union[str, Path] = "/dev"
) -> list[DirectDevice]:
    """List usblp devices from ``<sys_root>/class/usbmisc/lp*``.

    The interface directory (``.../device``) holds ``ieee1284_id``; its parent
    is the USB device with ``idVendor``, ``idProduct``, ``serial``,
    ``manufacturer`` and ``product``. The node is ``<dev_root>/usb/lpN``.
    """
    class_dir = Path(sys_root) / "class" / "usbmisc"
    devices: list[DirectDevice] = []
    if not class_dir.is_dir():
        log_event(logger, logging.DEBUG, "no usbmisc class directory", dir=class_dir)
        return devices
    for entry in sorted(class_dir.iterdir()):
        if not re.fullmatch(r"lp\d+", entry.name):
            continue
        devices.append(_linux_device(entry, Path(dev_root)))
    return devices


def _read_sysfs(path: Path, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="latin-1").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        errors.append(f"reading {path}: {exc}")
        return ""


def _linux_device(entry: Path, dev_root: Path) -> DirectDevice:
    errors: list[str] = []
    interface = entry / "device"
    usb_device = interface / ".."
    try:
        usb_device = interface.resolve().parent
    except OSError as exc:
        errors.append(f"resolving {interface}: {exc}")
    raw_1284 = _read_sysfs(interface / "ieee1284_id", errors)
    vid = _read_sysfs(usb_device / "idVendor", errors)
    pid = _read_sysfs(usb_device / "idProduct", errors)
    node = dev_root / "usb" / entry.name
    device = DirectDevice(
        path=str(node),
        vid_pid=f"{vid}:{pid}" if vid and pid else "",
        serial=_read_sysfs(usb_device / "serial", errors) or None,
        ieee1284_raw=raw_1284,
        kind="usblp",
        manufacturer=_read_sysfs(usb_device / "manufacturer", errors),
        product=_read_sysfs(usb_device / "product", errors),
        errors=errors,
    )
    access: Optional[bool] = None
    if node.exists():
        access = os.access(node, os.R_OK | os.W_OK)
    else:
        errors.append(f"{node} does not exist")
        device.present = False
    device.accessible = access
    log_event(
        logger,
        logging.INFO,
        "usblp device",
        sysfs=str(entry),
        usb_device=str(usb_device),
        node_exists=node.exists(),
        node_read_write=access,
        **device.to_log(),
    )
    if access is False:
        log_event(
            logger,
            logging.WARNING,
            "no read/write access to the printer device node; "
            f"install the udev rule {UDEV_RULE_PATH}",
            path=str(node),
        )
    return device


# -- Windows: usbprint.sys ---------------------------------------------------
#
# Sources for every signature and constant below:
# * Microsoft Learn: SetupDiGetClassDevsW, SetupDiEnumDeviceInterfaces,
#   SetupDiGetDeviceInterfaceDetailW, SP_DEVICE_INTERFACE_DATA,
#   SP_DEVICE_INTERFACE_DETAIL_DATA_W, CreateFileW, ReadFile, WriteFile,
#   DeviceIoControl, GetOverlappedResult, CancelIoEx, CreateEventW,
#   WaitForSingleObject, OVERLAPPED, System Error Codes.
# * IOCTL_USBPRINT_GET_1284_ID: Microsoft Learn "IOCTL_USBPRINT_GET_1284_ID
#   (usbprint.h)" (output = 2-byte length prefix + ID + NUL; keep the output
#   buffer <= 4094 bytes or some devices fail with error 23); usbprint.h
#   (ReactOS sdk/include/ddk/usbprint.h, mingw-w64):
#   CTL_CODE(FILE_DEVICE_UNKNOWN, USBPRINT_IOCTL_INDEX+13, METHOD_BUFFERED,
#   FILE_ANY_ACCESS) with USBPRINT_IOCTL_INDEX 0 = 0x220034, matching
#   FreeRDP winpr/include/winpr/comm.h and IRPMon ioctl.txt.
# * GUID_DEVINTERFACE_USBPRINT and the SetupDi enumeration sequence:
#   minlux/dymon src/usbprint_win32.c (adapted from Peter Skarpetis, "Getting
#   a handle on usbprint.sys").

_FILE_DEVICE_UNKNOWN = 0x22
_METHOD_BUFFERED = 0
_FILE_ANY_ACCESS = 0


def _ctl_code(device_type: int, function: int, method: int, access: int) -> int:
    """The CTL_CODE macro from winioctl.h."""
    return (device_type << 16) | (access << 14) | (function << 2) | method


IOCTL_USBPRINT_GET_1284_ID = _ctl_code(
    _FILE_DEVICE_UNKNOWN, 13, _METHOD_BUFFERED, _FILE_ANY_ACCESS
)
assert IOCTL_USBPRINT_GET_1284_ID == 0x220034

GUID_DEVINTERFACE_USBPRINT = "{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

# Share mode for opening the printer: 0 = exclusive. While we hold the handle
# nobody else - in particular the Windows spooler's USB port monitor, which
# opens the same usbprint.sys interface for each job it sends - can open the
# device, so no other program's bytes can be interleaved into our job (the
# printer would read them as bitmap data). Sharing (FILE_SHARE_READ |
# FILE_SHARE_WRITE) would buy nothing: our reads use our own handle, and the
# share mode only governs *other* opens. The cost is that our open fails with
# ERROR_SHARING_VIOLATION (reason "busy") while anything else has the device
# open, e.g. the spooler is printing to it; the caller treats that as "not
# sent" and falls back. A spooler job that arrives while we print waits in
# the queue (port error / retry) and prints after we close.
USBPRINT_SHARE_MODE = 0
FILE_FLAG_OVERLAPPED = 0x40000000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_BAD_COMMAND = 22
ERROR_GEN_FAILURE = 31
ERROR_SHARING_VIOLATION = 32
ERROR_INVALID_PARAMETER = 87
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_INVALID_NAME = 123
ERROR_BUSY = 170
ERROR_NO_MORE_ITEMS = 259
ERROR_OPERATION_ABORTED = 995
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
ERROR_DEVICE_NOT_CONNECTED = 1167
ERROR_NO_SUCH_DEVICE = 433

_WIN_NOT_FOUND = frozenset(
    {
        ERROR_FILE_NOT_FOUND,
        ERROR_PATH_NOT_FOUND,
        ERROR_INVALID_NAME,
        ERROR_DEVICE_NOT_CONNECTED,
        ERROR_NO_SUCH_DEVICE,
    }
)
_WIN_DEAD = frozenset(
    {
        ERROR_INVALID_HANDLE,
        ERROR_BAD_COMMAND,
        ERROR_GEN_FAILURE,
        ERROR_DEVICE_NOT_CONNECTED,
        ERROR_NO_SUCH_DEVICE,
        ERROR_FILE_NOT_FOUND,
    }
)

# 1284 ID output buffer: Microsoft recommends <= 4094 bytes.
IEEE1284_BUFFER_SIZE = 4094

# How long to wait for a cancelled request to complete before giving up on it
# (and deliberately leaking its buffers so the kernel never writes into freed
# memory).
CANCEL_GRACE_S = 2.0

_DWORD = ctypes.c_uint32
_ULONG_PTR = ctypes.c_size_t


class GUID(ctypes.Structure):
    """``GUID`` (guiddef.h)."""

    _fields_ = [
        ("Data1", _DWORD),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, text: str) -> "GUID":
        """Build from ``{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}``."""
        hex_text = text.strip("{}").replace("-", "")
        raw = bytes.fromhex(hex_text)
        if len(raw) != 16:
            raise ValueError(f"not a GUID: {text!r}")
        guid = cls()
        guid.Data1 = int.from_bytes(raw[0:4], "big")
        guid.Data2 = int.from_bytes(raw[4:6], "big")
        guid.Data3 = int.from_bytes(raw[6:8], "big")
        for index in range(8):
            guid.Data4[index] = raw[8 + index]
        return guid


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    """``SP_DEVICE_INTERFACE_DATA`` (setupapi.h); 32 bytes on x64, 28 on x86."""

    _fields_ = [
        ("cbSize", _DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", _DWORD),
        ("Reserved", _ULONG_PTR),
    ]


class OVERLAPPED(ctypes.Structure):
    """``OVERLAPPED`` (minwinbase.h); the Offset/Pointer union is unused here."""

    _fields_ = [
        ("Internal", _ULONG_PTR),
        ("InternalHigh", _ULONG_PTR),
        ("Offset", _DWORD),
        ("OffsetHigh", _DWORD),
        ("hEvent", ctypes.c_void_p),
    ]


def interface_detail_cb_size(pointer_size: Optional[int] = None) -> int:
    """``sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)`` for ``cbSize``.

    setupapi.h packs the struct to 8 on 64-bit (DWORD + WCHAR[1] padded = 8)
    and to 1 on 32-bit (4 + 2 = 6). The caller's buffer is bigger; cbSize must
    still be the fixed-part size or the call fails with ERROR_INVALID_USER_BUFFER.
    """
    size = pointer_size if pointer_size is not None else ctypes.sizeof(ctypes.c_void_p)
    return 8 if size == 8 else 6


# DevicePath follows the DWORD cbSize in SP_DEVICE_INTERFACE_DETAIL_DATA_W.
_DETAIL_PATH_OFFSET = 4


class Win32Api(abc.ABC):
    """The handful of Win32 calls the Windows transport needs.

    Handles are ints (``None`` for an invalid handle). Methods return the raw
    BOOL / DWORD results; ``last_error()`` is the thread's last error after
    the most recent call. Tests substitute a fake implementation.
    """

    @abc.abstractmethod
    def last_error(self) -> int:
        """``GetLastError`` captured right after the previous call."""

    @abc.abstractmethod
    def list_interface_paths(self, guid: str) -> list[str]:
        """Device interface paths of present devices for ``guid``."""

    @abc.abstractmethod
    def create_file(self, path: str) -> Optional[int]:
        """``CreateFileW`` read/write, exclusive, overlapped; None on failure.

        See :data:`USBPRINT_SHARE_MODE` for why the handle is exclusive.
        """

    @abc.abstractmethod
    def create_event(self) -> Optional[int]:
        """A manual-reset, initially non-signalled event; None on failure."""

    @abc.abstractmethod
    def close_handle(self, handle: int) -> bool:
        """``CloseHandle``."""

    @abc.abstractmethod
    def write_file(self, handle: int, buffer: Any, size: int, ov: OVERLAPPED) -> bool:
        """``WriteFile`` with an OVERLAPPED."""

    @abc.abstractmethod
    def read_file(self, handle: int, buffer: Any, size: int, ov: OVERLAPPED) -> bool:
        """``ReadFile`` with an OVERLAPPED."""

    @abc.abstractmethod
    def device_io_control(
        self, handle: int, code: int, out_buffer: Any, out_size: int, ov: OVERLAPPED
    ) -> bool:
        """``DeviceIoControl`` with no input buffer and an OVERLAPPED."""

    @abc.abstractmethod
    def wait(self, handle: int, timeout_ms: int) -> int:
        """``WaitForSingleObject``."""

    @abc.abstractmethod
    def cancel_io(self, handle: int, ov: OVERLAPPED) -> bool:
        """``CancelIoEx`` for one request."""

    @abc.abstractmethod
    def get_overlapped_result(self, handle: int, ov: OVERLAPPED) -> tuple[bool, int]:
        """``GetOverlappedResult(bWait=FALSE)``: ``(ok, bytes_transferred)``."""


class CtypesWin32Api(Win32Api):
    """The real Win32 API through ctypes (Windows only)."""

    # Declared here so type checkers on other platforms know them.
    _get_last_error: Callable[[], int]
    _get_class_devs: Any
    _enum_interfaces: Any
    _get_detail: Any
    _destroy_list: Any
    _create_file: Any
    _create_event: Any
    _close_handle: Any
    _write_file: Any
    _read_file: Any
    _ioctl: Any
    _wait: Any
    _cancel: Any
    _overlapped_result: Any
    _invalid: Optional[int]

    def __init__(self) -> None:  # pylint: disable=too-many-statements
        if sys.platform != "win32":
            raise TransportError("the Win32 API is only available on Windows")
        # pylint: disable=import-outside-toplevel
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        self._get_last_error = ctypes.get_last_error
        HANDLE = wintypes.HANDLE
        BOOL = wintypes.BOOL
        DWORD = wintypes.DWORD
        LPVOID = wintypes.LPVOID
        POV = ctypes.POINTER(OVERLAPPED)
        PDWORD = ctypes.POINTER(DWORD)

        # HDEVINFO SetupDiGetClassDevsW(const GUID*, PCWSTR, HWND, DWORD)
        self._get_class_devs = setupapi.SetupDiGetClassDevsW
        self._get_class_devs.argtypes = [
            ctypes.POINTER(GUID),
            wintypes.LPCWSTR,
            wintypes.HWND,
            DWORD,
        ]
        self._get_class_devs.restype = HANDLE
        # BOOL SetupDiEnumDeviceInterfaces(HDEVINFO, PSP_DEVINFO_DATA,
        #     const GUID*, DWORD MemberIndex, PSP_DEVICE_INTERFACE_DATA)
        self._enum_interfaces = setupapi.SetupDiEnumDeviceInterfaces
        self._enum_interfaces.argtypes = [
            HANDLE,
            LPVOID,
            ctypes.POINTER(GUID),
            DWORD,
            ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ]
        self._enum_interfaces.restype = BOOL
        # BOOL SetupDiGetDeviceInterfaceDetailW(HDEVINFO,
        #     PSP_DEVICE_INTERFACE_DATA, PSP_DEVICE_INTERFACE_DETAIL_DATA_W,
        #     DWORD DetailSize, PDWORD RequiredSize, PSP_DEVINFO_DATA)
        self._get_detail = setupapi.SetupDiGetDeviceInterfaceDetailW
        self._get_detail.argtypes = [
            HANDLE,
            ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
            LPVOID,
            DWORD,
            PDWORD,
            LPVOID,
        ]
        self._get_detail.restype = BOOL
        # BOOL SetupDiDestroyDeviceInfoList(HDEVINFO)
        self._destroy_list = setupapi.SetupDiDestroyDeviceInfoList
        self._destroy_list.argtypes = [HANDLE]
        self._destroy_list.restype = BOOL
        # HANDLE CreateFileW(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES,
        #     DWORD, DWORD, HANDLE)
        self._create_file = kernel32.CreateFileW
        self._create_file.argtypes = [
            wintypes.LPCWSTR,
            DWORD,
            DWORD,
            LPVOID,
            DWORD,
            DWORD,
            HANDLE,
        ]
        self._create_file.restype = HANDLE
        # HANDLE CreateEventW(LPSECURITY_ATTRIBUTES, BOOL bManualReset,
        #     BOOL bInitialState, LPCWSTR)
        self._create_event = kernel32.CreateEventW
        self._create_event.argtypes = [LPVOID, BOOL, BOOL, wintypes.LPCWSTR]
        self._create_event.restype = HANDLE
        # BOOL CloseHandle(HANDLE)
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [HANDLE]
        self._close_handle.restype = BOOL
        # BOOL WriteFile(HANDLE, LPCVOID, DWORD, LPDWORD, LPOVERLAPPED)
        self._write_file = kernel32.WriteFile
        self._write_file.argtypes = [HANDLE, LPVOID, DWORD, PDWORD, POV]
        self._write_file.restype = BOOL
        # BOOL ReadFile(HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED)
        self._read_file = kernel32.ReadFile
        self._read_file.argtypes = [HANDLE, LPVOID, DWORD, PDWORD, POV]
        self._read_file.restype = BOOL
        # BOOL DeviceIoControl(HANDLE, DWORD, LPVOID, DWORD, LPVOID, DWORD,
        #     LPDWORD, LPOVERLAPPED)
        self._ioctl = kernel32.DeviceIoControl
        self._ioctl.argtypes = [
            HANDLE,
            DWORD,
            LPVOID,
            DWORD,
            LPVOID,
            DWORD,
            PDWORD,
            POV,
        ]
        self._ioctl.restype = BOOL
        # DWORD WaitForSingleObject(HANDLE, DWORD)
        self._wait = kernel32.WaitForSingleObject
        self._wait.argtypes = [HANDLE, DWORD]
        self._wait.restype = DWORD
        # BOOL CancelIoEx(HANDLE, LPOVERLAPPED)
        self._cancel = kernel32.CancelIoEx
        self._cancel.argtypes = [HANDLE, POV]
        self._cancel.restype = BOOL
        # BOOL GetOverlappedResult(HANDLE, LPOVERLAPPED, LPDWORD, BOOL bWait)
        self._overlapped_result = kernel32.GetOverlappedResult
        self._overlapped_result.argtypes = [HANDLE, POV, PDWORD, BOOL]
        self._overlapped_result.restype = BOOL
        self._invalid = ctypes.c_void_p(-1).value

    def _handle(self, value: Optional[int]) -> Optional[int]:
        if value is None or value == 0 or value == self._invalid:
            return None
        return int(value)

    def last_error(self) -> int:
        return int(self._get_last_error())

    def list_interface_paths(self, guid: str) -> list[str]:
        guid_struct = GUID.from_string(guid)
        devs = self._get_class_devs(
            ctypes.byref(guid_struct),
            None,
            None,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
        )
        if self._handle(devs) is None:
            code = self.last_error()
            raise OSError(0, f"SetupDiGetClassDevsW failed ({code})", None, code)
        paths: list[str] = []
        try:
            index = 0
            while True:
                iface = SP_DEVICE_INTERFACE_DATA()
                iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
                if not self._enum_interfaces(
                    devs, None, ctypes.byref(guid_struct), index, ctypes.byref(iface)
                ):
                    code = self.last_error()
                    if code != ERROR_NO_MORE_ITEMS:
                        log_event(
                            logger,
                            logging.WARNING,
                            "SetupDiEnumDeviceInterfaces failed",
                            index=index,
                            winerror=code,
                        )
                    break
                index += 1
                path = self._interface_path(devs, iface)
                if path:
                    paths.append(path)
                if index > 256:  # never loop forever on a broken list
                    break
        finally:
            self._destroy_list(devs)
        return paths

    def _interface_path(self, devs: Any, iface: SP_DEVICE_INTERFACE_DATA) -> str:
        required = ctypes.c_uint32(0)
        self._get_detail(
            devs, ctypes.byref(iface), None, 0, ctypes.byref(required), None
        )
        code = self.last_error()
        if code != ERROR_INSUFFICIENT_BUFFER or required.value < _DETAIL_PATH_OFFSET:
            log_event(
                logger,
                logging.WARNING,
                "SetupDiGetDeviceInterfaceDetailW size query failed",
                winerror=code,
                required=required.value,
            )
            return ""
        buffer = ctypes.create_string_buffer(required.value + 2)
        ctypes.c_uint32.from_buffer(buffer).value = interface_detail_cb_size()
        if not self._get_detail(
            devs, ctypes.byref(iface), buffer, required.value, None, None
        ):
            log_event(
                logger,
                logging.WARNING,
                "SetupDiGetDeviceInterfaceDetailW failed",
                winerror=self.last_error(),
            )
            return ""
        return ctypes.wstring_at(ctypes.addressof(buffer) + _DETAIL_PATH_OFFSET)

    def create_file(self, path: str) -> Optional[int]:
        return self._handle(
            self._create_file(
                path,
                GENERIC_READ | GENERIC_WRITE,
                USBPRINT_SHARE_MODE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED,
                None,
            )
        )

    def create_event(self) -> Optional[int]:
        return self._handle(self._create_event(None, True, False, None))

    def close_handle(self, handle: int) -> bool:
        return bool(self._close_handle(handle))

    def write_file(self, handle: int, buffer: Any, size: int, ov: OVERLAPPED) -> bool:
        return bool(self._write_file(handle, buffer, size, None, ctypes.byref(ov)))

    def read_file(self, handle: int, buffer: Any, size: int, ov: OVERLAPPED) -> bool:
        return bool(self._read_file(handle, buffer, size, None, ctypes.byref(ov)))

    def device_io_control(
        self, handle: int, code: int, out_buffer: Any, out_size: int, ov: OVERLAPPED
    ) -> bool:
        return bool(
            self._ioctl(
                handle, code, None, 0, out_buffer, out_size, None, ctypes.byref(ov)
            )
        )

    def wait(self, handle: int, timeout_ms: int) -> int:
        return int(self._wait(handle, timeout_ms))

    def cancel_io(self, handle: int, ov: OVERLAPPED) -> bool:
        return bool(self._cancel(handle, ctypes.byref(ov)))

    def get_overlapped_result(self, handle: int, ov: OVERLAPPED) -> tuple[bool, int]:
        transferred = ctypes.c_uint32(0)
        ok = self._overlapped_result(
            handle, ctypes.byref(ov), ctypes.byref(transferred), False
        )
        return bool(ok), int(transferred.value)


_WIN_PATH_IDS = re.compile(
    r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})[^#]*#([^#]*)#", re.IGNORECASE
)


def parse_usbprint_path(path: str) -> tuple[str, Optional[str]]:
    """``(vid_pid, serial)`` from ``\\\\?\\usb#vid_2e3c&pid_5760#SERIAL#{guid}``.

    The third segment is the USB serial number when the device has one;
    otherwise Windows puts a generated instance ID with ``&`` in it there
    (``5&2a3b4c&0&1``), which is not a serial.
    """
    match = _WIN_PATH_IDS.search(path)
    if not match:
        return "", None
    vid_pid = f"{match.group(1)}:{match.group(2)}".upper()
    instance = match.group(3)
    serial = instance if instance and "&" not in instance else None
    return vid_pid, serial


def parse_1284_reply(data: bytes) -> str:
    """Decode an IOCTL_USBPRINT_GET_1284_ID reply.

    The first two bytes are a big-endian length (per the USB printer class
    GET_DEVICE_ID, the length includes those two bytes, but devices disagree),
    followed by the ID and a NUL. The ID is taken as everything after the
    prefix up to the first NUL, bounded by the declared length when that is
    plausible.
    """
    if len(data) < 2:
        return ""
    declared = int.from_bytes(data[:2], "big")
    body = data[2:]
    if 2 <= declared <= len(data):
        body = data[2:declared]
    return body.split(b"\x00", 1)[0].decode("latin-1").strip()


def _clamp_ms(seconds: float) -> int:
    return max(0, min(int(seconds * 1000), 0x7FFFFFFF))


class AbandonedRequests:
    """Overlapped requests whose completion Windows never confirmed.

    The kernel may still write into (or read from) such a request's buffer
    and ``OVERLAPPED``, and will signal its event when it finishes, so all
    three must outlive the transport that started it: closing the device
    handle is not a completion barrier. This registry lives for the whole
    process (:data:`ABANDONED`). :meth:`reap` frees an entry only once its
    event is signalled, i.e. the I/O manager has finished with it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[tuple[Win32Api, OVERLAPPED, Any, int, str]] = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def add(
        self, api: "Win32Api", ov: OVERLAPPED, buffer: Any, event: int, op: str
    ) -> None:
        """Keep ``ov``, ``buffer`` and ``event`` alive until confirmed complete."""
        with self._lock:
            self._items.append((api, ov, buffer, event, op))

    def reap(self) -> int:
        """Release requests that have completed since; returns how many."""
        with self._lock:
            keep = []
            freed = 0
            for item in self._items:
                api, _, _, event, op = item
                try:
                    done = api.wait(event, 0) == WAIT_OBJECT_0
                except Exception:  # pylint: disable=broad-exception-caught
                    done = False
                if done:
                    api.close_handle(event)
                    freed += 1
                    log_event(
                        logger,
                        logging.INFO,
                        "abandoned request completed; buffers released",
                        op=op,
                    )
                else:
                    keep.append(item)
            self._items = keep
            return freed


ABANDONED = AbandonedRequests()
"""Process-wide home of overlapped requests that never confirmed completion."""


class WindowsUsbprintTransport(Transport):
    """A usbprint.sys device interface opened overlapped.

    Every ReadFile / WriteFile / DeviceIoControl is overlapped and waited with
    a timeout; on timeout the request is cancelled (CancelIoEx) and its result
    collected, so no call can block forever. A request whose cancellation does
    not complete within ``CANCEL_GRACE_S`` raises :class:`IndeterminateIO`
    (for a write: ``bytes_accepted=None``, the data may have been sent), its
    buffers move to the process-wide :data:`ABANDONED` registry (freed only
    once Windows signals completion) and the transport refuses further I/O.
    """

    chunk_size = 4096
    read_size = 4096
    empty_read_sleep_s = 0.05

    def __init__(self, path: str, *, api: Optional[Win32Api] = None) -> None:
        super().__init__()
        self.path = path
        self._api: Win32Api = api if api is not None else CtypesWin32Api()
        # Set once a request could not be confirmed finished: no more I/O.
        self._abandoned: Optional[str] = None
        started = time.monotonic()
        handle = self._api.create_file(path)
        if handle is None:
            code = self._api.last_error()
            error = _windows_open_error(path, code)
            log_event(
                logger,
                logging.WARNING,
                "open failed",
                path=path,
                reason=error.reason,
                winerror=code,
                elapsed_s=round(time.monotonic() - started, 3),
            )
            raise error
        self._handle = handle
        log_event(
            logger,
            logging.INFO,
            "opened usbprint device",
            path=path,
            handle=handle,
            elapsed_s=round(time.monotonic() - started, 3),
        )

    def _close_impl(self) -> None:
        self._api.close_handle(self._handle)
        ABANDONED.reap()

    def _check_usable(self) -> None:
        self._check_open()
        if self._abandoned is not None:
            raise DeviceGone(
                f"{self.path}: an earlier {self._abandoned} never finished "
                "cancelling",
                path=self.path,
            )

    def _overlapped(  # pylint: disable=too-many-locals,too-many-branches
        self,
        op: str,
        start: Callable[[OVERLAPPED], bool],
        buffer: Any,
        timeout_s: float,
        *,
        trust_aborted_count: bool = True,
    ) -> tuple[int, bool]:
        """Run one overlapped request: ``(bytes_transferred, timed_out)``.

        With ``trust_aborted_count=False`` (writes) a request that ended
        aborted raises :class:`WriteCountUnknown` instead of returning its
        (untrustworthy) count; only a successful completion's count is used.

        Returns only when Windows confirmed the request finished (completed,
        or cancelled with its final byte count). Raises:

        * :class:`DeviceGone` when the request could not be started
          (``request_started=False`` on the exception) or finished with an
          error;
        * :class:`IndeterminateIO` when it was started but could not be
          confirmed finished (cancel not completed, wait failed): its
          buffers go to :data:`ABANDONED` and this transport is unusable.
        """
        api = self._api
        ABANDONED.reap()
        event = api.create_event()
        if event is None:
            code = api.last_error()
            raise DeviceGone(
                f"CreateEventW failed ({code})",
                path=self.path,
                winerror=code,
                request_started=False,
            )
        ov = OVERLAPPED()
        ov.hEvent = event
        release = True
        try:
            if not start(ov):
                code = api.last_error()
                if code != ERROR_IO_PENDING:
                    error = self._win_dead(op, code)
                    error.request_started = False
                    raise error
            waited = api.wait(event, _clamp_ms(timeout_s))
            wait_error = 0
            if waited == WAIT_OBJECT_0:
                ok, transferred = api.get_overlapped_result(self._handle, ov)
                if ok:
                    return transferred, False
                code = api.last_error()
                if code == ERROR_OPERATION_ABORTED:
                    if not trust_aborted_count:
                        raise self._count_unknown(op, transferred)
                    return transferred, True
                if code != ERROR_IO_INCOMPLETE:
                    raise self._win_dead(op, code)
                # Signalled but not complete: should not happen; treat it
                # like a timeout below (cancel, then confirm).
            elif waited != WAIT_TIMEOUT:
                wait_error = api.last_error()
            # Timed out (or the wait failed): cancel and collect the result.
            cancelled = api.cancel_io(self._handle, ov)
            cancel_error = 0 if cancelled else api.last_error()
            api.wait(event, _clamp_ms(CANCEL_GRACE_S))
            ok, transferred = api.get_overlapped_result(self._handle, ov)
            code = 0 if ok else api.last_error()
            log_event(
                logger,
                logging.DEBUG if not wait_error else logging.ERROR,
                f"{op} {'wait failed' if wait_error else 'timed out'}; cancelled",
                path=self.path,
                timeout_s=round(timeout_s, 3),
                wait_result=waited,
                wait_winerror=wait_error,
                cancel_ok=cancelled,
                cancel_winerror=cancel_error,
                completed=ok,
                transferred=transferred,
                winerror=code,
            )
            if not ok and code == ERROR_IO_INCOMPLETE:
                release = False
                self._abandon(op, ov, buffer, event)
                raise IndeterminateIO(
                    f"{op} on {self.path} did not finish cancelling within "
                    f"{CANCEL_GRACE_S:g} s; it may still complete",
                    path=self.path,
                    bytes_accepted=None,
                    winerror=code,
                )
            if not ok and code not in (ERROR_OPERATION_ABORTED, 0):
                raise self._win_dead(op, code)
            if wait_error:
                raise self._win_dead(f"{op} wait", wait_error)
            if not ok and not trust_aborted_count:
                raise self._count_unknown(op, transferred)
            return transferred, True
        finally:
            if release:
                api.close_handle(event)

    def _count_unknown(self, op: str, reported: int) -> WriteCountUnknown:
        log_event(
            logger,
            logging.ERROR,
            f"{op} was aborted after it started; its byte count is not trusted",
            path=self.path,
            reported_bytes=reported,
        )
        return WriteCountUnknown(
            f"{op} on {self.path} was cancelled after it started (reported "
            f"{reported} bytes); part of the data may have been sent",
            path=self.path,
            bytes_accepted=None,
            winerror=ERROR_OPERATION_ABORTED,
        )

    def _abandon(self, op: str, ov: OVERLAPPED, buffer: Any, event: int) -> None:
        """Hand an unconfirmed request's memory to :data:`ABANDONED`."""
        self._abandoned = op
        ABANDONED.add(self._api, ov, buffer, event, op)
        log_event(
            logger,
            logging.ERROR,
            f"{op} did not finish cancelling; abandoning the device (its "
            "buffers are kept for the life of the process)",
            path=self.path,
            grace_s=CANCEL_GRACE_S,
            abandoned_requests=len(ABANDONED),
        )

    def _win_dead(self, op: str, code: int, accepted: int = 0) -> DeviceGone:
        log_event(
            logger,
            logging.ERROR,
            f"{op} failed",
            path=self.path,
            winerror=code,
            bytes_accepted=accepted,
        )
        return DeviceGone(
            f"{op} on {self.path} failed (winerror {code})",
            path=self.path,
            bytes_accepted=accepted,
            winerror=code,
        )

    def write_all(  # pylint: disable=too-many-locals
        self, data: bytes, timeout_s: float
    ) -> int:
        self._check_usable()
        total = len(data)
        started = time.monotonic()
        deadline = started + max(0.0, timeout_s)
        sent = 0
        chunks = 0
        timed_out = False
        log_event(
            logger,
            logging.DEBUG,
            "write_all start",
            path=self.path,
            bytes=total,
            timeout_s=timeout_s,
        )
        while sent < total:
            chunk = data[sent : sent + self.chunk_size]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            def start(
                ov: OVERLAPPED, buffer: Any = buffer, size: int = len(chunk)
            ) -> bool:
                return self._api.write_file(self._handle, buffer, size, ov)

            try:
                written, timed_out = self._overlapped(
                    "WriteFile", start, buffer, remaining, trust_aborted_count=False
                )
            except DeviceGone as exc:
                # A request that never started sent nothing; one that was in
                # flight (failed or unconfirmed) may have sent part of its
                # chunk, and Windows' count for it cannot be trusted.
                started_io = exc.request_started
                exc.bytes_accepted = None if started_io else sent
                self.bytes_written += sent
                log_event(
                    logger,
                    logging.ERROR,
                    "write_all failed",
                    path=self.path,
                    sent_confirmed=sent,
                    bytes_accepted=exc.bytes_accepted,
                    total=total,
                    request_started=started_io,
                )
                raise
            sent += written
            chunks += 1
            log_event(
                logger,
                logging.DEBUG,
                "write chunk",
                path=self.path,
                chunk=chunks,
                bytes=written,
                requested=len(chunk),
                sent=sent,
                total=total,
                timed_out=timed_out,
                elapsed_s=round(time.monotonic() - started, 3),
            )
            if timed_out:
                break
            if written == 0:
                # A zero-byte completion: give the device a moment.
                time.sleep(
                    min(self.empty_read_sleep_s, max(0.0, deadline - time.monotonic()))
                )
        self.bytes_written += sent
        log_event(
            logger,
            logging.WARNING if sent < total else logging.DEBUG,
            (
                "write_all timed out: printer stopped accepting data"
                if sent < total
                else "write_all done"
            ),
            path=self.path,
            sent=sent,
            total=total,
            chunks=chunks,
            timed_out=timed_out,
            timeout_s=timeout_s,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return sent

    def _read_once(self, timeout_s: float) -> int:
        buffer = ctypes.create_string_buffer(self.read_size)

        def start(ov: OVERLAPPED) -> bool:
            return self._api.read_file(self._handle, buffer, self.read_size, ov)

        got, _ = self._overlapped("ReadFile", start, buffer, timeout_s)
        if got:
            data = buffer.raw[:got]
            self.bytes_read += got
            log_event(
                logger,
                logging.DEBUG,
                "read bytes",
                path=self.path,
                bytes=got,
                data=data.decode("latin-1"),
            )
            self._lines.feed(data)
        return got

    def read_lines(self, timeout_s: float) -> list[str]:
        self._check_usable()
        started = time.monotonic()
        deadline = started + max(0.0, timeout_s)
        reads = 0
        lines = self._lines.take_lines()
        while not lines:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and reads:
                self._log_read_timeout(timeout_s, started, reads)
                return []
            got = self._read_once(max(0.0, remaining))
            reads += 1
            lines = self._lines.take_lines()
            if self._abandoned is not None and not lines:
                self._log_read_timeout(timeout_s, started, reads)
                return []
            if not got and not lines:
                left = deadline - time.monotonic()
                if left > 0:
                    time.sleep(min(self.empty_read_sleep_s, left))
        return self._log_lines(lines, started)

    def get_1284_id(self, timeout_s: float = 2.0) -> str:
        """Ask usbprint.sys for the 1284 device ID string ("" on failure)."""
        self._check_usable()
        buffer = ctypes.create_string_buffer(IEEE1284_BUFFER_SIZE)

        def start(ov: OVERLAPPED) -> bool:
            return self._api.device_io_control(
                self._handle,
                IOCTL_USBPRINT_GET_1284_ID,
                buffer,
                IEEE1284_BUFFER_SIZE,
                ov,
            )

        try:
            got, timed_out = self._overlapped(
                "IOCTL_USBPRINT_GET_1284_ID", start, buffer, timeout_s
            )
        except DeviceGone as exc:
            log_event(
                logger,
                logging.WARNING,
                "1284 ID query failed",
                path=self.path,
                error=describe_exception(exc),
            )
            return ""
        raw = parse_1284_reply(buffer.raw[:got]) if got else ""
        log_event(
            logger,
            logging.INFO if raw else logging.WARNING,
            "1284 ID",
            path=self.path,
            bytes=got,
            declared_length=(
                int.from_bytes(buffer.raw[:2], "big") if got >= 2 else None
            ),
            timed_out=timed_out,
            ieee1284=raw,
        )
        return raw


def _windows_open_error(path: str, code: int) -> DeviceUnavailable:
    if code in (ERROR_SHARING_VIOLATION, ERROR_BUSY):
        return DeviceUnavailable(
            f"{path} is in use by another program or the Windows spooler "
            f"(winerror {code})",
            reason="busy",
            path=path,
            winerror=code,
        )
    if code == ERROR_ACCESS_DENIED:
        return DeviceUnavailable(
            f"access denied opening {path}; it may be in use by another program "
            "or the Windows spooler (winerror 5)",
            reason="busy",
            path=path,
            winerror=code,
        )
    if code in _WIN_NOT_FOUND:
        return DeviceUnavailable(
            f"{path} not found (printer unplugged or off?) (winerror {code})",
            reason="not_found",
            path=path,
            winerror=code,
        )
    return DeviceUnavailable(
        f"cannot open {path} (winerror {code})",
        reason="error",
        path=path,
        winerror=code,
    )


def discover_windows_usbprint(api: Optional[Win32Api] = None) -> list[DirectDevice]:
    """List present usbprint.sys devices, reading each one's 1284 ID.

    A device that cannot be opened (busy in the spooler) is still listed,
    without a 1284 ID and with the reason in ``errors``.
    """
    api = api if api is not None else CtypesWin32Api()
    started = time.monotonic()
    paths = api.list_interface_paths(GUID_DEVINTERFACE_USBPRINT)
    log_event(
        logger,
        logging.DEBUG,
        "usbprint interfaces",
        paths=paths,
        elapsed_s=round(time.monotonic() - started, 3),
    )
    devices: list[DirectDevice] = []
    for path in paths:
        vid_pid, serial = parse_usbprint_path(path)
        errors: list[str] = []
        raw = ""
        accessible: Optional[bool] = None
        busy = False
        try:
            with WindowsUsbprintTransport(path, api=api) as transport:
                accessible = True
                raw = transport.get_1284_id()
        except DeviceUnavailable as exc:
            errors.append(str(exc))
            busy = exc.reason == "busy"
            accessible = None if busy else False
        except TransportError as exc:
            errors.append(str(exc))
        device = DirectDevice(
            path=path,
            vid_pid=vid_pid,
            serial=serial,
            ieee1284_raw=raw,
            kind="usbprint",
            errors=errors,
            accessible=accessible,
            busy=busy,
        )
        log_event(logger, logging.INFO, "usbprint device", **device.to_log())
        devices.append(device)
    return devices


# -- platform entry points ---------------------------------------------------


def discover_direct_devices() -> list[DirectDevice]:
    """Every USB printer-class device on this machine (any vendor).

    Callers pick the printer by ``device.model`` (1284 MDL). Never raises:
    errors are logged and give an empty list.
    """
    started = time.monotonic()
    devices: list[DirectDevice] = []
    try:
        if sys.platform == "win32":
            devices = discover_windows_usbprint()
        elif sys.platform.startswith("linux"):
            devices = discover_linux_usblp()
        else:
            log_event(
                logger,
                logging.INFO,
                "direct USB printing is not supported on this platform",
                platform=sys.platform,
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log_event(
            logger,
            logging.ERROR,
            "direct device discovery failed",
            error=describe_exception(exc),
        )
        return []
    log_event(
        logger,
        logging.INFO,
        "direct device discovery",
        count=len(devices),
        devices=[device.to_log() for device in devices],
        elapsed_s=round(time.monotonic() - started, 3),
    )
    return devices


def open_transport(device: DirectDevice) -> Transport:
    """Open ``device`` with the transport for its kind (or this platform).

    Raises :class:`DeviceUnavailable` when it cannot be opened.
    """
    kind = device.kind or ("usbprint" if sys.platform == "win32" else "usblp")
    log_event(logger, logging.DEBUG, "open_transport", using=kind, **device.to_log())
    if kind == "usbprint":
        return WindowsUsbprintTransport(device.path)
    if kind == "usblp":
        return LinuxUsblpTransport(device.path)
    raise DeviceUnavailable(
        f"no transport for device kind {kind!r}", reason="error", path=device.path
    )


# -- test double -------------------------------------------------------------

ReplySpec = Union[Sequence[str], Callable[[str], Optional[Iterable[str]]]]


class FakeTransport(Transport):
    """A scriptable transport for tests (no I/O, no sleeping).

    * ``queue_lines(...)`` / ``queue_raw(...)``: data the "printer" has sent,
      returned by the next ``read_lines``.
    * ``queue_after_reads(n, ...)``: lines that appear after ``n`` more
      ``read_lines`` calls (e.g. ``SSSGETPRINTING:DONE`` a while later).
    * ``add_reply(line, *replies)``: when a complete written line (without
      CR LF) equals ``line``, queue ``replies``. ``replies`` may instead be a
      callable taking the written line and returning lines (or None). The
      key ``"*"`` matches any written line without its own entry.
    * ``stall_after_bytes``: accept at most this many bytes in total, then
      accept nothing more (``write_all`` returns the short count).
    * ``write_error`` / ``read_error``: raised by the next write/read (use a
      :class:`DeviceGone` for a dead device).
    * ``raise_on_open``: raised by the callable from :meth:`opener`.

    ``written`` holds every accepted byte; ``written_lines`` the complete
    lines; ``read_timeouts`` / ``write_calls`` record each call.
    """

    def __init__(
        self,
        *,
        lines: Iterable[str] = (),
        replies: Optional[dict[str, ReplySpec]] = None,
        stall_after_bytes: Optional[int] = None,
        raise_on_open: Optional[BaseException] = None,
        write_error: Optional[BaseException] = None,
        read_error: Optional[BaseException] = None,
        device: Optional[DirectDevice] = None,
    ) -> None:
        super().__init__()
        self.device = device or fake_device()
        self.path = self.device.path
        self.written = bytearray()
        self.stall_after_bytes = stall_after_bytes
        self.raise_on_open = raise_on_open
        self.write_error = write_error
        self.read_error = read_error
        self.replies: dict[str, ReplySpec] = dict(replies or {})
        self.read_timeouts: list[float] = []
        self.write_calls: list[tuple[int, float, int]] = []
        self.open_count = 0
        self._incoming: deque[bytes] = deque()
        self._scheduled: list[list[Any]] = []
        self._written_lines = _LineBuffer()
        self.written_lines: list[str] = []
        self.queue_lines(*lines)

    # scripting

    def queue_lines(self, *lines: str) -> None:
        """Lines (without CR LF) the printer has sent."""
        for line in lines:
            self._incoming.append(line.encode("latin-1") + b"\r\n")

    def queue_raw(self, data: bytes) -> None:
        """Raw bytes the printer has sent (may contain partial lines)."""
        self._incoming.append(bytes(data))

    def queue_after_reads(self, reads: int, *lines: str) -> None:
        """Lines that arrive once ``reads`` more ``read_lines`` calls have run."""
        self._scheduled.append([reads, list(lines)])

    def add_reply(self, line: str, *replies: str) -> None:
        """Reply with ``replies`` whenever ``line`` is written."""
        self.replies[line] = list(replies)

    def opener(self) -> Callable[[DirectDevice], "FakeTransport"]:
        """A drop-in for :func:`open_transport` that returns this transport."""

        def _open(device: DirectDevice) -> FakeTransport:
            self.open_count += 1
            log_event(logger, logging.DEBUG, "fake open", **device.to_log())
            if self.raise_on_open is not None:
                raise self.raise_on_open
            self.device = device
            self.path = device.path
            return self

        return _open

    # the contract

    def write_all(self, data: bytes, timeout_s: float) -> int:
        self._check_open()
        if self.write_error is not None:
            error, self.write_error = self.write_error, None
            raise error
        accept = len(data)
        if self.stall_after_bytes is not None:
            accept = max(0, min(accept, self.stall_after_bytes - len(self.written)))
        chunk = bytes(data[:accept])
        self.written.extend(chunk)
        self.bytes_written += accept
        self.write_calls.append((len(data), timeout_s, accept))
        log_event(
            logger,
            logging.DEBUG,
            "fake write_all",
            requested=len(data),
            accepted=accept,
            timeout_s=timeout_s,
        )
        self._written_lines.feed(chunk)
        for line in self._written_lines.take_lines():
            self.written_lines.append(line)
            self._reply(line)
        return accept

    def _reply(self, line: str) -> None:
        spec = self.replies.get(line, self.replies.get("*"))
        if spec is None:
            return
        produced = spec(line) if callable(spec) else spec
        if produced:
            self.queue_lines(*produced)

    def read_lines(self, timeout_s: float) -> list[str]:
        self._check_open()
        self.read_timeouts.append(timeout_s)
        if self.read_error is not None:
            error, self.read_error = self.read_error, None
            raise error
        for item in self._scheduled:
            item[0] -= 1
        due = [item for item in self._scheduled if item[0] < 0]
        self._scheduled = [item for item in self._scheduled if item[0] >= 0]
        for _, lines in due:
            self.queue_lines(*lines)
        while self._incoming:
            data = self._incoming.popleft()
            self.bytes_read += len(data)
            self._lines.feed(data)
        lines = self._lines.take_lines()
        log_event(logger, logging.DEBUG, "fake read_lines", lines=lines)
        return lines

    def _close_impl(self) -> None:
        return None


def fake_device(
    model: str = "PM2411BT",
    *,
    path: str = "fake:lp0",
    serial: Optional[str] = "TESTSERIAL",
    vid_pid: str = PM2411BT_VID_PID,
) -> DirectDevice:
    """A :class:`DirectDevice` like the PM2411BT, for tests."""
    raw = f"MFG: ;CMD:XPP,XL;MDL:{model};CLS:PRINTER;DES:{model};"
    return DirectDevice(
        path=path,
        vid_pid=vid_pid,
        serial=serial,
        ieee1284_raw=raw,
        kind="fake",
        manufacturer="PM",
        product=model,
    )
