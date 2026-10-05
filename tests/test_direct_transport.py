"""Tests for ibp_printing.direct.transport (no printer needed)."""

# pylint: disable=protected-access,missing-function-docstring

import ctypes
import errno
import gc
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from printing_helpers import quiet_logging

from ibp_printing.direct import transport as tr
from ibp_printing.direct.transport import (
    DeviceGone,
    DeviceUnavailable,
    DirectDevice,
    FakeTransport,
    LinuxUsblpTransport,
    TransportClosed,
    WindowsUsbprintTransport,
    fake_device,
    parse_ieee1284_id,
)

PM_1284 = "MFG: ;CMD:XPP,XL;MDL:PM2411BT;CLS:PRINTER;DES:PM2411BT;"
ON_WINDOWS = sys.platform == "win32"


class Ieee1284Tests(unittest.TestCase):
    def test_parses_printer_id(self):
        parsed = parse_ieee1284_id(PM_1284)
        self.assertEqual(
            parsed,
            {
                "MFG": "",
                "CMD": "XPP,XL",
                "MDL": "PM2411BT",
                "CLS": "PRINTER",
                "DES": "PM2411BT",
            },
        )

    def test_long_keys_case_and_junk(self):
        parsed = parse_ieee1284_id(
            " manufacturer:Acme ; Model: X 1 ;COMMAND SET:PCL;junk;:x;MDL:dup\x00"
        )
        self.assertEqual(parsed["MFG"], "Acme")
        self.assertEqual(parsed["MDL"], "X 1")  # first occurrence wins
        self.assertEqual(parsed["CMD"], "PCL")
        self.assertNotIn("JUNK", parsed)

    def test_empty(self):
        self.assertEqual(parse_ieee1284_id(""), {})

    def test_device_model_from_1284(self):
        device = DirectDevice(
            path="/dev/usb/lp0", vid_pid="2e3c:5760", ieee1284_raw=PM_1284
        )
        self.assertEqual(device.model, "PM2411BT")
        self.assertEqual(device.vid_pid, "2E3C:5760")
        self.assertEqual(device.ieee1284["CMD"], "XPP,XL")
        self.assertIn("ieee1284", device.to_log())

    def test_explicit_model_kept(self):
        device = DirectDevice(path="x", ieee1284={"MDL": "A"}, model="B")
        self.assertEqual(device.model, "B")


class LineBufferTests(unittest.TestCase):
    def test_partial_lines_and_lone_lf(self):
        buf = tr._LineBuffer()
        buf.feed(b"SSSGET")
        self.assertEqual(buf.take_lines(), [])
        buf.feed(b"CAP:OPEN\r\nA\nB\r")
        self.assertEqual(buf.take_lines(), ["SSSGETCAP:OPEN", "A"])
        self.assertEqual(buf.pending(), b"B\r")
        buf.feed(b"\n\xe9\r\n")
        self.assertEqual(buf.take_lines(), ["B", "\xe9"])

    def test_overlong_flushed(self):
        quiet_logging(self)
        buf = tr._LineBuffer()
        buf.feed(b"x" * (tr.MAX_LINE_BYTES + 1))
        lines = buf.take_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(buf), 0)


class FakeTransportTests(unittest.TestCase):
    def setUp(self):
        quiet_logging(self)

    def test_queued_lines_and_replies(self):
        fake = FakeTransport(lines=["SSSGETPRINTING:DONE"])
        fake.add_reply("SSSGETCAP", "SSSGETCAP:CLOSE")
        self.assertEqual(fake.read_lines(0.5), ["SSSGETPRINTING:DONE"])
        self.assertEqual(fake.read_lines(0.5), [])
        self.assertEqual(fake.write_all(b"SSSGETCAP\r\n", 1.0), 11)
        self.assertEqual(fake.read_lines(0.5), ["SSSGETCAP:CLOSE"])
        self.assertEqual(fake.written_lines, ["SSSGETCAP"])
        self.assertEqual(fake.read_timeouts, [0.5, 0.5, 0.5])

    def test_reply_only_on_complete_line_and_callable_wildcard(self):
        fake = FakeTransport(replies={"*": lambda line: [f"Cmd error:{line}"]})
        fake.write_all(b"FOO", 1.0)
        self.assertEqual(fake.read_lines(0), [])
        fake.write_all(b"\r\nBAR\r\n", 1.0)
        self.assertEqual(fake.read_lines(0), ["Cmd error:FOO", "Cmd error:BAR"])

    def test_raw_partial_lines(self):
        fake = FakeTransport()
        fake.queue_raw(b"SSSGETPRINT")
        self.assertEqual(fake.read_lines(0.1), [])
        fake.queue_raw(b"ING:DOING\r\n")
        self.assertEqual(fake.read_lines(0.1), ["SSSGETPRINTING:DOING"])

    def test_scheduled_lines(self):
        fake = FakeTransport()
        fake.queue_after_reads(2, "SSSGETPRINTING:DONE")
        self.assertEqual(fake.read_lines(0.1), [])
        self.assertEqual(fake.read_lines(0.1), [])
        self.assertEqual(fake.read_lines(0.1), ["SSSGETPRINTING:DONE"])

    def test_stall_after_bytes(self):
        fake = FakeTransport(stall_after_bytes=10)
        self.assertEqual(fake.write_all(b"12345678", 1.0), 8)
        self.assertEqual(fake.write_all(b"abcdef", 1.0), 2)
        self.assertEqual(fake.write_all(b"more", 1.0), 0)
        self.assertEqual(bytes(fake.written), b"12345678ab")
        self.assertEqual(fake.write_calls[1], (6, 1.0, 2))

    def test_errors_and_close(self):
        fake = FakeTransport(write_error=DeviceGone("gone"), read_error=OSError("x"))
        with self.assertRaises(DeviceGone):
            fake.write_all(b"a", 1.0)
        self.assertEqual(fake.write_all(b"a", 1.0), 1)  # one-shot
        with self.assertRaises(OSError):
            fake.read_lines(0.1)
        with fake:
            pass
        self.assertTrue(fake.closed)
        fake.close()  # idempotent
        with self.assertRaises(TransportClosed):
            fake.write_all(b"a", 1.0)
        with self.assertRaises(TransportClosed):
            fake.read_lines(0)

    def test_opener(self):
        fake = FakeTransport()
        device = fake_device(path="fake:lp9")
        self.assertIs(fake.opener()(device), fake)
        self.assertEqual(fake.path, "fake:lp9")
        failing = FakeTransport(raise_on_open=DeviceUnavailable("busy", reason="busy"))
        with self.assertRaises(DeviceUnavailable):
            failing.opener()(device)
        self.assertEqual(failing.open_count, 1)

    def test_fake_device(self):
        device = fake_device()
        self.assertEqual(device.model, "PM2411BT")
        self.assertEqual(device.vid_pid, "2E3C:5760")


@unittest.skipIf(ON_WINDOWS, "usblp transport is POSIX-only")
class LinuxTransportTests(unittest.TestCase):
    """LinuxUsblpTransport against a non-blocking socketpair stand-in."""

    def setUp(self):
        quiet_logging(self)
        self.ours, self.peer = socket.socketpair()
        self.ours.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.ours.setblocking(False)
        self.transport = LinuxUsblpTransport("/dev/usb/lpTEST", fd=self.ours.detach())
        self.addCleanup(self.transport.close)
        self.addCleanup(self.peer.close)

    def test_query_round_trip(self):
        self.assertEqual(self.transport.write_all(b"SSSGETCAP\r\n", 1.0), 11)
        self.assertEqual(self.peer.recv(100), b"SSSGETCAP\r\n")
        self.peer.sendall(b"SSSGETCAP:CLOSE\r\n")
        self.assertEqual(self.transport.read_lines(1.0), ["SSSGETCAP:CLOSE"])
        self.assertEqual(self.transport.bytes_written, 11)

    def test_read_keeps_polling_through_empty_reads(self):
        def later():
            time.sleep(0.3)
            self.peer.sendall(b"SSSGETPRINTING:DONE\r\n")

        thread = threading.Thread(target=later)
        thread.start()
        started = time.monotonic()
        lines = self.transport.read_lines(3.0)
        elapsed = time.monotonic() - started
        thread.join()
        self.assertEqual(lines, ["SSSGETPRINTING:DONE"])
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 2.5)  # returned once the line arrived

    def test_partial_line_buffered_across_calls(self):
        self.peer.sendall(b"SSSGETPA")
        self.assertEqual(self.transport.read_lines(0.2), [])
        self.peer.sendall(b"PER:YES\r\nSSSGETCAP:OP")
        self.assertEqual(self.transport.read_lines(0.5), ["SSSGETPAPER:YES"])
        self.peer.sendall(b"EN\r\n")
        self.assertEqual(self.transport.read_lines(0.5), ["SSSGETCAP:OPEN"])

    def test_read_timeout(self):
        started = time.monotonic()
        self.assertEqual(self.transport.read_lines(0.3), [])
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2.0)

    def test_zero_timeout_returns_available(self):
        self.peer.sendall(b"A\r\nB\r\n")
        time.sleep(0.05)
        self.assertEqual(self.transport.read_lines(0), ["A", "B"])
        self.assertEqual(self.transport.read_lines(0), [])

    def test_write_stall_returns_partial_count(self):
        data = b"x" * (4 * 1024 * 1024)
        started = time.monotonic()
        accepted = self.transport.write_all(data, 0.4)
        elapsed = time.monotonic() - started
        self.assertGreater(accepted, 0)
        self.assertLess(accepted, len(data))
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 3.0)

    def test_write_drains_when_peer_reads(self):
        data = bytes(range(256)) * 2000  # 512 000 bytes, far over the buffers
        received = bytearray()

        def reader():
            self.peer.settimeout(5)
            while len(received) < len(data):
                chunk = self.peer.recv(65536)
                if not chunk:
                    break
                received.extend(chunk)
                time.sleep(0.001)

        thread = threading.Thread(target=reader)
        thread.start()
        accepted = self.transport.write_all(data, 20.0)
        thread.join(10)
        self.assertEqual(accepted, len(data))
        self.assertEqual(bytes(received), data)

    def test_write_to_dead_device_raises(self):
        self.peer.close()
        with self.assertRaises(DeviceGone) as caught:
            for _ in range(5):
                self.transport.write_all(b"x" * 1000, 0.5)
        self.assertIn(caught.exception.errno, (errno.EPIPE, errno.ECONNRESET))

    def test_read_from_dead_device_raises(self):
        with mock.patch.object(tr.os, "read", side_effect=OSError(errno.ENODEV, "x")):
            with self.assertRaises(DeviceGone):
                self.transport.read_lines(0.2)

    def test_close_is_idempotent_and_blocks_io(self):
        with self.transport as transport:
            self.assertIs(transport, self.transport)
        self.transport.close()
        with self.assertRaises(TransportClosed):
            self.transport.write_all(b"a", 0.1)
        with self.assertRaises(TransportClosed):
            self.transport.read_lines(0.1)

    def test_eagain_on_write_is_waited(self):
        calls = {"n": 0}
        real_write = os.write

        def flaky(fd, data):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise BlockingIOError(errno.EAGAIN, "busy")
            return real_write(fd, data)

        with mock.patch.object(tr.os, "write", side_effect=flaky):
            self.assertEqual(self.transport.write_all(b"SSSGETCAP\r\n", 1.0), 11)
        self.assertEqual(self.peer.recv(100), b"SSSGETCAP\r\n")


@unittest.skipIf(ON_WINDOWS, "usblp transport is POSIX-only")
class LinuxOpenErrorTests(unittest.TestCase):
    def setUp(self):
        quiet_logging(self)

    def _open_with(self, code: int) -> DeviceUnavailable:
        with mock.patch.object(tr.os, "open", side_effect=OSError(code, "nope")):
            with self.assertRaises(DeviceUnavailable) as caught:
                LinuxUsblpTransport("/dev/usb/lp0")
        return caught.exception

    def test_busy(self):
        error = self._open_with(errno.EBUSY)
        self.assertEqual(error.reason, "busy")
        self.assertEqual(error.errno, errno.EBUSY)

    def test_permission_mentions_udev_rule(self):
        error = self._open_with(errno.EACCES)
        self.assertEqual(error.reason, "permission")
        self.assertIn("60-ibp-label-printer.rules", str(error))

    def test_not_found(self):
        self.assertEqual(self._open_with(errno.ENOENT).reason, "not_found")

    def test_missing_node_for_real(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DeviceUnavailable) as caught:
                LinuxUsblpTransport(os.path.join(tmp, "lp0"))
        self.assertEqual(caught.exception.reason, "not_found")


def make_fake_sysfs(root: Path, dev_root: Path, *, serial: Optional[str]) -> None:
    """``/sys/class/usbmisc/lp0`` -> interface 3-9:1.0 under USB device 3-9."""
    usb_device = root / "devices" / "pci0000:00" / "usb3" / "3-9"
    interface = usb_device / "3-9:1.0"
    interface.mkdir(parents=True)
    (interface / "ieee1284_id").write_text(PM_1284, encoding="latin-1")
    (usb_device / "idVendor").write_text("2e3c\n")
    (usb_device / "idProduct").write_text("5760\n")
    if serial is not None:
        (usb_device / "serial").write_text(serial + "\n")
    (usb_device / "manufacturer").write_text("PM\n")
    (usb_device / "product").write_text("PM2411BT\n")
    class_dir = root / "class" / "usbmisc"
    (class_dir / "lp0").mkdir(parents=True)
    (class_dir / "lp0" / "device").symlink_to(interface)
    (class_dir / "hiddev0").mkdir()  # not a printer
    (dev_root / "usb").mkdir(parents=True)
    (dev_root / "usb" / "lp0").write_bytes(b"")


@unittest.skipIf(ON_WINDOWS, "sysfs symlinks need POSIX")
class LinuxDiscoveryTests(unittest.TestCase):
    def setUp(self):
        quiet_logging(self)
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_discovers_printer(self):
        make_fake_sysfs(self.root / "sys", self.root / "dev", serial="Q529E56G9290059")
        devices = tr.discover_linux_usblp(self.root / "sys", self.root / "dev")
        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device.path, str(self.root / "dev" / "usb" / "lp0"))
        self.assertEqual(device.vid_pid, "2E3C:5760")
        self.assertEqual(device.serial, "Q529E56G9290059")
        self.assertEqual(device.model, "PM2411BT")
        self.assertEqual(device.ieee1284["CMD"], "XPP,XL")
        self.assertEqual(device.product, "PM2411BT")
        self.assertEqual(device.kind, "usblp")
        self.assertEqual(device.errors, [])
        self.assertTrue(device.present)
        self.assertIsNotNone(device.accessible)
        self.assertFalse(device.busy)

    def test_no_serial_and_missing_node(self):
        make_fake_sysfs(self.root / "sys", self.root / "dev", serial=None)
        (self.root / "dev" / "usb" / "lp0").unlink()
        device = tr.discover_linux_usblp(self.root / "sys", self.root / "dev")[0]
        self.assertIsNone(device.serial)
        self.assertTrue(any("does not exist" in error for error in device.errors))
        self.assertFalse(device.present)
        self.assertIsNone(device.accessible)

    def test_no_usbmisc(self):
        self.assertEqual(tr.discover_linux_usblp(self.root, self.root), [])

    def test_open_transport_dispatch(self):
        device = DirectDevice(path=str(self.root / "missing"), kind="usblp")
        with self.assertRaises(DeviceUnavailable):
            tr.open_transport(device)
        with self.assertRaises(DeviceUnavailable):
            tr.open_transport(DirectDevice(path="x", kind="carrier-pigeon"))

    def test_discover_direct_devices_never_raises(self):
        with (
            mock.patch.object(
                tr, "discover_linux_usblp", side_effect=RuntimeError("boom")
            ),
            mock.patch.object(tr.sys, "platform", "linux"),
        ):
            self.assertEqual(tr.discover_direct_devices(), [])


class Win32StructTests(unittest.TestCase):
    def test_ioctl_code(self):
        self.assertEqual(tr.IOCTL_USBPRINT_GET_1284_ID, 0x220034)

    def test_guid_from_string(self):
        guid = tr.GUID.from_string(tr.GUID_DEVINTERFACE_USBPRINT)
        self.assertEqual(guid.Data1, 0x28D78FAD)
        self.assertEqual(guid.Data2, 0x5A12)
        self.assertEqual(guid.Data3, 0x11D1)
        self.assertEqual(
            bytes(guid.Data4), bytes([0xAE, 0x5B, 0x00, 0x00, 0xF8, 0x03, 0xA8, 0xC2])
        )
        self.assertEqual(ctypes.sizeof(guid), 16)

    def test_struct_sizes_match_windows(self):
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(
            ctypes.sizeof(tr.SP_DEVICE_INTERFACE_DATA), 32 if pointer == 8 else 28
        )
        self.assertEqual(ctypes.sizeof(tr.OVERLAPPED), 32 if pointer == 8 else 20)
        self.assertEqual(tr.interface_detail_cb_size(8), 8)
        self.assertEqual(tr.interface_detail_cb_size(4), 6)

    def test_parse_usbprint_path(self):
        path = (
            "\\\\?\\usb#vid_2e3c&pid_5760#q529e56g9290059#"
            "{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"
        )
        self.assertEqual(tr.parse_usbprint_path(path), ("2E3C:5760", "q529e56g9290059"))
        composite = "\\\\?\\usb#vid_0922&pid_0028&mi_00#7&1a2b&0&0000#{guid}"
        self.assertEqual(tr.parse_usbprint_path(composite), ("0922:0028", None))
        self.assertEqual(tr.parse_usbprint_path("nonsense"), ("", None))

    def test_parse_1284_reply(self):
        body = PM_1284.encode()
        including = (len(body) + 2).to_bytes(2, "big") + body + b"\x00junk"
        self.assertEqual(tr.parse_1284_reply(including), PM_1284)
        excluding = len(body).to_bytes(2, "big") + body + b"\x00"
        # A device that excludes the prefix from the length loses 2 bytes at
        # most; the NUL still ends the string.
        self.assertTrue(tr.parse_1284_reply(excluding).startswith("MFG: ;CMD"))
        bogus = b"\xff\xff" + body + b"\x00"
        self.assertEqual(tr.parse_1284_reply(bogus), PM_1284)
        self.assertEqual(tr.parse_1284_reply(b"\x00"), "")


class FakeWin32(tr.Win32Api):
    """Simulates usbprint.sys overlapped I/O.

    ``write_capacity`` bytes are accepted (then writes pend forever);
    ``partial_pending``: a write that hits the capacity transfers what fits
    and stays pending (instead of completing short);
    ``incoming`` chunks are returned by reads (reads pend when empty);
    ``cancel_stuck`` makes cancellation never complete;
    ``wait_fails``: the next request's first wait returns WAIT_FAILED;
    ``async_error``: the next write is queued, then completes with this error;
    ``on_write(data)``: called with every accepted write (to script replies).
    """

    def __init__(self) -> None:
        self.error = 0
        self.open_error: Optional[int] = None
        self.paths: list[str] = []
        self.written = bytearray()
        self.write_capacity = 1 << 30
        self.incoming: list[bytes] = []
        self.reply_1284 = b""
        self.cancel_stuck = False
        self.fail_io: Optional[int] = None
        self.handles: set[int] = set()
        self.closed: list[int] = []
        self.pending: dict[int, Any] = {}  # event -> (ov, done?)
        self._next = 100
        self.cancels = 0
        self.waits: list[int] = []
        self.partial_pending = False
        self.cancel_stuck_writes = False  # like cancel_stuck, writes only
        self.abort_reports_zero = False  # an aborted request reports 0 bytes
        self.cancel_completes = False  # the request finishes OK during cancel
        self.wait_fails = False
        self.async_error: Optional[int] = None
        self.on_write: Optional[Any] = None

    def _new(self) -> int:
        self._next += 1
        self.handles.add(self._next)
        return self._next

    def last_error(self) -> int:
        return self.error

    def list_interface_paths(self, guid: str) -> list[str]:
        assert guid == tr.GUID_DEVINTERFACE_USBPRINT
        return list(self.paths)

    def create_file(self, path: str) -> Optional[int]:
        if self.open_error is not None:
            self.error = self.open_error
            return None
        return self._new()

    def create_event(self) -> Optional[int]:
        return self._new()

    def close_handle(self, handle: int) -> bool:
        self.closed.append(handle)
        self.handles.discard(handle)
        return True

    def _complete(self, ov: Any, transferred: int, ok: bool = True) -> bool:
        self.pending[ov.hEvent] = {"ov": ov, "done": True, "n": transferred, "ok": ok}
        self.error = tr.ERROR_IO_PENDING
        return False

    def _pend(self, ov: Any) -> bool:
        self.pending[ov.hEvent] = {"ov": ov, "done": False, "n": 0, "ok": False}
        self.error = tr.ERROR_IO_PENDING
        return False

    def write_file(self, handle: int, buffer: Any, size: int, ov: Any) -> bool:
        if self.fail_io is not None:
            self.error = self.fail_io
            return False
        if self.async_error is not None:
            code, self.async_error = self.async_error, None
            self.written.extend(bytes(buffer)[: size // 2])
            self.pending[ov.hEvent] = {
                "ov": ov,
                "done": True,
                "n": size // 2,
                "ok": False,
                "code": code,
            }
            self.error = tr.ERROR_IO_PENDING
            return False
        room = self.write_capacity - len(self.written)
        if room <= 0:
            self._pend(ov)
            self.pending[ov.hEvent]["write"] = True
            return False
        take = min(room, size)
        self.written.extend(bytes(buffer)[:take])
        if self.on_write is not None:
            self.on_write(bytes(buffer)[:take])
        if take < size and self.partial_pending:
            self._pend(ov)
            self.pending[ov.hEvent].update(n=take, write=True)
            return False
        return self._complete(ov, take)

    def read_file(self, handle: int, buffer: Any, size: int, ov: Any) -> bool:
        if self.fail_io is not None:
            self.error = self.fail_io
            return False
        if not self.incoming:
            return self._pend(ov)
        data = self.incoming.pop(0)[:size]
        ctypes.memmove(buffer, data, len(data))
        return self._complete(ov, len(data))

    def device_io_control(self, handle, code, out_buffer, out_size, ov) -> bool:
        assert code == 0x220034 and out_size <= 4094
        if not self.reply_1284:
            self.error = tr.ERROR_GEN_FAILURE
            return False
        ctypes.memmove(out_buffer, self.reply_1284, len(self.reply_1284))
        self.pending[ov.hEvent] = {
            "ov": ov,
            "done": True,
            "n": len(self.reply_1284),
            "ok": True,
        }
        return True  # completed synchronously

    def wait(self, handle: int, timeout_ms: int) -> int:
        self.waits.append(timeout_ms)
        if self.wait_fails and timeout_ms != tr._clamp_ms(tr.CANCEL_GRACE_S):
            self.wait_fails = False
            self.error = tr.ERROR_INVALID_HANDLE
            return tr.WAIT_FAILED
        entry = self.pending.get(handle)
        if entry and entry["done"]:
            return tr.WAIT_OBJECT_0
        return tr.WAIT_TIMEOUT

    def cancel_io(self, handle: int, ov: Any) -> bool:
        self.cancels += 1
        entry = self.pending.get(ov.hEvent)
        stuck = self.cancel_stuck or (
            self.cancel_stuck_writes and entry is not None and entry.get("write")
        )
        if entry and not entry["done"] and not stuck:
            entry.update(done=True, ok=self.cancel_completes)
            if self.abort_reports_zero:
                entry["n"] = 0
        return True

    def finish(self, event: int) -> None:
        """Let a stuck request complete (the kernel finally finishes it)."""
        self.pending[event].update(done=True, ok=False)

    def get_overlapped_result(self, handle: int, ov: Any) -> tuple[bool, int]:
        entry = self.pending[ov.hEvent]
        if not entry["done"]:
            self.error = tr.ERROR_IO_INCOMPLETE
            return False, 0
        if not entry["ok"]:
            self.error = entry.get("code", tr.ERROR_OPERATION_ABORTED)
            return False, entry["n"]
        return True, entry["n"]


PRINTER_PATH = (
    "\\\\?\\usb#vid_2e3c&pid_5760#Q529E56G9290059#"
    "{28d78fad-5a12-11d1-ae5b-0000f803a8c2}"
)


class WindowsTransportLogicTests(unittest.TestCase):
    """WindowsUsbprintTransport against FakeWin32 (runs on every platform)."""

    def setUp(self):
        quiet_logging(self)
        self.api = FakeWin32()
        self.abandoned = tr.AbandonedRequests()
        patcher = mock.patch.object(tr, "ABANDONED", self.abandoned)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.transport = WindowsUsbprintTransport(PRINTER_PATH, api=self.api)
        self.addCleanup(self._close)

    def _close(self):
        transport = getattr(self, "transport", None)
        if transport is not None:
            transport.close()

    def test_write_all_in_chunks(self):
        data = bytes(range(256)) * 40  # 10240 bytes -> 3 chunks of <= 4096
        self.assertEqual(self.transport.write_all(data, 5.0), len(data))
        self.assertEqual(bytes(self.api.written), data)
        self.assertEqual(self.api.cancels, 0)

    def test_write_stall_cancelled_count_is_unknown(self):
        """An aborted write's count is never trusted, even after full chunks."""
        self.api.write_capacity = 5000
        with self.assertRaises(tr.WriteCountUnknown) as caught:
            self.transport.write_all(b"x" * 10000, 0.2)
        self.assertIsNone(caught.exception.bytes_accepted)
        self.assertEqual(self.api.cancels, 1)
        # Not abandoned: Windows confirmed the request finished.
        self.assertEqual(len(self.abandoned), 0)
        self.api.incoming = [b"OK\r\n"]
        self.assertEqual(self.transport.read_lines(1.0), ["OK"])

    def test_aborted_first_chunk_reporting_zero_is_unknown(self):
        """Codex: 100 bytes went out, the abort reports 0 -> not 'nothing sent'."""
        self.api.write_capacity = 100
        self.api.partial_pending = True
        self.api.abort_reports_zero = True
        with self.assertRaises(tr.WriteCountUnknown) as caught:
            self.transport.write_all(b"x" * 1000, 0.05)
        self.assertIsNone(caught.exception.bytes_accepted)
        self.assertEqual(len(self.api.written), 100)

    def test_request_completing_during_cancel_is_counted(self):
        self.api.write_capacity = 100
        self.api.partial_pending = True
        self.api.cancel_completes = True
        self.assertEqual(self.transport.write_all(b"x" * 1000, 0.05), 100)

    def test_read_lines_and_timeout(self):
        self.api.incoming = [b"SSSGETCAP:OP", b"EN\r\nSSSGETPRINTING:DO"]
        self.assertEqual(self.transport.read_lines(1.0), ["SSSGETCAP:OPEN"])
        self.assertEqual(self.transport.read_lines(0.1), [])  # pends, cancelled
        self.assertGreaterEqual(self.api.cancels, 1)
        self.api.incoming = [b"ING\r\n"]
        self.assertEqual(self.transport.read_lines(1.0), ["SSSGETPRINTING:DOING"])

    def test_events_closed(self):
        self.api.incoming = [b"A\r\n"]
        self.transport.read_lines(1.0)
        self.transport.write_all(b"abc", 1.0)
        self.transport.read_lines(0.05)
        self.transport.close()
        self.assertEqual(self.api.handles, set())

    def test_stuck_cancel_abandons_device(self):
        self.api.cancel_stuck = True
        with self.assertRaises(tr.IndeterminateIO):
            self.transport.read_lines(0.05)
        self.assertEqual(len(self.abandoned), 1)
        with self.assertRaises(DeviceGone):
            self.transport.read_lines(0.05)
        with self.assertRaises(DeviceGone) as caught:
            self.transport.write_all(b"x", 0.05)
        self.assertEqual(caught.exception.bytes_accepted, 0)
        self.assertEqual(self.api.written, b"")

    def test_unconfirmed_write_is_unknown_not_zero(self):
        """A chunk still pending after a failed cancel may have been sent."""
        self.api.write_capacity = 100
        self.api.partial_pending = True
        self.api.cancel_stuck = True
        with self.assertRaises(tr.IndeterminateIO) as caught:
            self.transport.write_all(b"x" * 1000, 0.05)
        self.assertIsNone(caught.exception.bytes_accepted)
        self.assertEqual(len(self.api.written), 100)  # the printer got some

    def test_confirmed_cancel_of_partial_chunk_is_unknown(self):
        self.api.write_capacity = 100
        self.api.partial_pending = True
        with self.assertRaises(tr.WriteCountUnknown):
            self.transport.write_all(b"x" * 1000, 0.05)
        self.assertEqual(len(self.abandoned), 0)

    def test_abandoned_buffers_outlive_the_transport(self):
        self.api.write_capacity = 0
        self.api.cancel_stuck = True
        with self.assertRaises(tr.IndeterminateIO):
            self.transport.write_all(b"x" * 10, 0.05)
        ((_, ov, buffer, event, op),) = self.abandoned._items
        self.assertEqual(op, "WriteFile")
        self.transport.close()
        del self.transport
        gc.collect()
        # The kernel may still use them: still referenced, event still open.
        self.assertEqual(len(self.abandoned), 1)
        self.assertIn(event, self.api.handles)
        self.assertEqual(ov.hEvent, event)
        self.assertEqual(bytes(buffer), b"x" * 10)
        self.assertEqual(self.abandoned.reap(), 0)
        self.api.finish(event)
        self.assertEqual(self.abandoned.reap(), 1)
        self.assertEqual(len(self.abandoned), 0)
        self.assertNotIn(event, self.api.handles)

    def test_wait_failure_is_cancelled_and_confirmed(self):
        self.api.write_capacity = 0
        self.api.wait_fails = True
        with self.assertRaises(DeviceGone) as caught:
            self.transport.write_all(b"x" * 10, 1.0)
        self.assertNotIsInstance(caught.exception, tr.IndeterminateIO)
        self.assertEqual(self.api.cancels, 1)
        self.assertIsNone(caught.exception.bytes_accepted)  # it was in flight
        self.assertEqual(len(self.abandoned), 0)

    def test_wait_failure_with_stuck_cancel_is_abandoned(self):
        self.api.write_capacity = 0
        self.api.wait_fails = True
        self.api.cancel_stuck = True
        with self.assertRaises(tr.IndeterminateIO) as caught:
            self.transport.write_all(b"x" * 10, 1.0)
        self.assertIsNone(caught.exception.bytes_accepted)
        self.assertEqual(len(self.abandoned), 1)

    def test_async_write_failure_is_unknown(self):
        """A queued write that fails: Windows' count is not trusted."""
        self.api.async_error = tr.ERROR_GEN_FAILURE
        with self.assertRaises(DeviceGone) as caught:
            self.transport.write_all(b"x" * 10, 1.0)
        self.assertIsNone(caught.exception.bytes_accepted)
        self.assertEqual(caught.exception.winerror, tr.ERROR_GEN_FAILURE)

    def test_refused_write_sent_nothing(self):
        self.api.fail_io = tr.ERROR_DEVICE_NOT_CONNECTED
        with self.assertRaises(DeviceGone) as caught:
            self.transport.write_all(b"x" * 10, 1.0)
        self.assertEqual(caught.exception.bytes_accepted, 0)
        self.assertFalse(caught.exception.request_started)

    def test_io_error_raises_device_gone(self):
        self.api.write_capacity = 4096
        self.transport.write_all(b"x" * 100, 1.0)
        self.api.fail_io = tr.ERROR_DEVICE_NOT_CONNECTED
        with self.assertRaises(DeviceGone) as caught:
            self.transport.write_all(b"y" * 10, 1.0)
        self.assertEqual(caught.exception.winerror, tr.ERROR_DEVICE_NOT_CONNECTED)
        with self.assertRaises(DeviceGone):
            self.transport.read_lines(0.1)

    def test_get_1284_id(self):
        body = PM_1284.encode()
        self.api.reply_1284 = (len(body) + 2).to_bytes(2, "big") + body + b"\x00"
        self.assertEqual(self.transport.get_1284_id(), PM_1284)
        self.api.reply_1284 = b""
        self.assertEqual(self.transport.get_1284_id(), "")

    def test_waits_never_infinite(self):
        self.transport.read_lines(0.05)
        self.assertTrue(all(ms < 0xFFFFFFFF for ms in self.api.waits))


class WindowsOpenAndDiscoveryTests(unittest.TestCase):
    def setUp(self):
        quiet_logging(self)
        self.api = FakeWin32()

    def _open_error(self, code: int) -> DeviceUnavailable:
        self.api.open_error = code
        with self.assertRaises(DeviceUnavailable) as caught:
            WindowsUsbprintTransport(PRINTER_PATH, api=self.api)
        return caught.exception

    def test_open_errors(self):
        busy = self._open_error(tr.ERROR_SHARING_VIOLATION)
        self.assertEqual(busy.reason, "busy")
        self.assertIn("spooler", str(busy))
        self.assertEqual(self._open_error(tr.ERROR_BUSY).reason, "busy")
        self.assertEqual(self._open_error(tr.ERROR_FILE_NOT_FOUND).reason, "not_found")
        self.assertEqual(self._open_error(1234).reason, "error")

    def test_discovery(self):
        body = PM_1284.encode()
        self.api.reply_1284 = (len(body) + 2).to_bytes(2, "big") + body + b"\x00"
        self.api.paths = [PRINTER_PATH]
        devices = tr.discover_windows_usbprint(self.api)
        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device.vid_pid, "2E3C:5760")
        self.assertEqual(device.serial, "Q529E56G9290059")
        self.assertEqual(device.model, "PM2411BT")
        self.assertEqual(device.kind, "usbprint")
        self.assertTrue(device.accessible)
        self.assertFalse(device.busy)
        self.assertEqual(self.api.handles, set())  # opened and closed again

    def test_discovery_lists_busy_device(self):
        self.api.paths = [PRINTER_PATH]
        self.api.open_error = tr.ERROR_SHARING_VIOLATION
        devices = tr.discover_windows_usbprint(self.api)
        self.assertEqual(devices[0].model, "")
        self.assertIn("spooler", devices[0].errors[0])
        self.assertTrue(devices[0].busy)
        self.assertIsNone(devices[0].accessible)

    def test_discovery_access_denied_is_busy(self):
        # usbprint answers ERROR_ACCESS_DENIED when another handle is open.
        self.api.paths = [PRINTER_PATH]
        self.api.open_error = tr.ERROR_ACCESS_DENIED
        self.assertTrue(tr.discover_windows_usbprint(self.api)[0].busy)

    def test_discovery_device_vanished(self):
        self.api.paths = [PRINTER_PATH]
        self.api.open_error = tr.ERROR_FILE_NOT_FOUND
        device = tr.discover_windows_usbprint(self.api)[0]
        self.assertFalse(device.busy)
        self.assertFalse(device.accessible)

    def test_printer_is_opened_exclusively(self):
        # Nobody (the spooler included) may interleave bytes into our job.
        self.assertEqual(tr.USBPRINT_SHARE_MODE, 0)

    def test_ctypes_api_refuses_off_windows(self):
        if ON_WINDOWS:
            self.skipTest("Windows has the real API")
        with self.assertRaises(tr.TransportError):
            tr.CtypesWin32Api()


@unittest.skipUnless(ON_WINDOWS, "real Win32 calls")
class RealWindowsTests(unittest.TestCase):
    """Smoke tests against the real Win32 API (CI windows-latest has no printer)."""

    def setUp(self):
        quiet_logging(self)

    def test_discovery_without_printer_does_not_hang(self):
        started = time.monotonic()
        devices = tr.discover_direct_devices()
        self.assertIsInstance(devices, list)
        self.assertLess(time.monotonic() - started, 30)
        # The raw enumeration must succeed (not just be swallowed).
        tr.CtypesWin32Api().list_interface_paths(tr.GUID_DEVINTERFACE_USBPRINT)

    def test_exclusive_open_is_exclusive(self):
        """Our handle locks others out, and an open file makes us 'busy'."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "device")
            with open(path, "wb"):
                pass
            transport = WindowsUsbprintTransport(path)
            try:
                with self.assertRaises(PermissionError):
                    with open(path, "rb"):
                        pass
            finally:
                transport.close()
            with open(path, "rb"):
                with self.assertRaises(DeviceUnavailable) as caught:
                    WindowsUsbprintTransport(path)
            self.assertEqual(caught.exception.reason, "busy")
            self.assertEqual(caught.exception.winerror, tr.ERROR_SHARING_VIOLATION)

    def test_open_missing_device(self):
        path = "\\\\?\\usb#vid_0000&pid_0000#none#" + tr.GUID_DEVINTERFACE_USBPRINT
        with self.assertRaises(DeviceUnavailable):
            WindowsUsbprintTransport(path)

    def test_overlapped_io_over_named_pipe(self):
        """Real ReadFile/WriteFile/CancelIoEx timeouts, with a pipe as the printer."""
        import _winapi  # pylint: disable=import-outside-toplevel,import-error

        name = f"\\\\.\\pipe\\ibp-transport-test-{os.getpid()}"
        server = _winapi.CreateNamedPipe(
            name,
            _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED,
            _winapi.PIPE_WAIT,  # byte mode
            1,
            4096,
            4096,
            0,
            _winapi.NULL,
        )
        self.addCleanup(_winapi.CloseHandle, server)
        transport = WindowsUsbprintTransport(name)
        self.addCleanup(transport.close)
        connect = _winapi.ConnectNamedPipe(server, overlapped=True)
        connect.GetOverlappedResult(True)

        # A read with nothing to read times out (cancelled), not hangs.
        started = time.monotonic()
        self.assertEqual(transport.read_lines(0.3), [])
        self.assertLess(time.monotonic() - started, 5)

        pending, _ = _winapi.WriteFile(server, b"SSSGETCAP:CLOSE\r\n", overlapped=True)
        pending.GetOverlappedResult(True)
        self.assertEqual(transport.read_lines(2.0), ["SSSGETCAP:CLOSE"])

        self.assertEqual(transport.write_all(b"SSSGETCAP\r\n", 2.0), 11)
        reading, _ = _winapi.ReadFile(server, 100, overlapped=True)
        reading.GetOverlappedResult(True)
        self.assertEqual(reading.getbuffer(), b"SSSGETCAP\r\n")

        # Nobody reads: the write stalls, is cancelled, and its count is
        # not trusted (it may have sent part of the data).
        started = time.monotonic()
        with self.assertRaises(tr.WriteCountUnknown) as caught:
            transport.write_all(b"x" * (1 << 20), 0.5)
        self.assertIsNone(caught.exception.bytes_accepted)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
