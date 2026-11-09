"""LinuxPrinterBackend with ``lp``, CUPS and sysfs faked."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image
from printing_helpers import quiet_logging

from ibp_printing.backends import PrintError
from ibp_printing.backends import linux
from ibp_printing.models import JobOutcome


def completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    """A finished ``lp`` run."""
    return subprocess.CompletedProcess(["lp"], returncode, stdout="", stderr=stderr)


class LinuxPrintImageTests(unittest.TestCase):
    """Temp-file cleanup must never change what print_image reports."""

    def setUp(self):
        quiet_logging(self)
        self.backend = linux.LinuxPrinterBackend()
        self.img = Image.new("RGB", (60, 40), "white")

    def print(self):
        """Print the test image to a fake queue."""
        return self.backend.print_image(self.img, "Dymo", job_name="Label [abc]")

    def test_success(self):
        with mock.patch.object(linux.subprocess, "run", return_value=completed(0)):
            result = self.print()
        self.assertEqual(result.outcome, JobOutcome.NOT_TRACKED)

    def test_temp_file_removal_failure_after_lp_succeeds_is_logged_not_raised(self):
        with (
            mock.patch.object(linux.subprocess, "run", return_value=completed(0)),
            mock.patch.object(
                linux.os, "remove", side_effect=PermissionError("file in use")
            ) as remove,
        ):
            with self.assertLogs("ibp_printing", level="WARNING") as logs:
                result = self.print()
        remove.assert_called_once()
        self.assertEqual(result.outcome, JobOutcome.NOT_TRACKED)
        self.assertTrue(
            any("could not delete the temporary print file" in x for x in logs.output)
        )

    def test_temp_file_removal_failure_keeps_print_error(self):
        with (
            mock.patch.object(
                linux.subprocess, "run", return_value=completed(1, "no such queue")
            ),
            mock.patch.object(
                linux.os, "remove", side_effect=PermissionError("file in use")
            ),
        ):
            with self.assertRaises(PrintError) as ctx:
                self.print()
        self.assertIn("no such queue", str(ctx.exception))

    def test_temp_file_removal_failure_keeps_uncertain_timeout(self):
        with (
            mock.patch.object(
                linux.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["lp"], 30),
            ),
            mock.patch.object(linux.os, "remove", side_effect=OSError("gone")),
        ):
            result = self.print()
        self.assertEqual(result.outcome, JobOutcome.UNCERTAIN)


def fake_sysfs(root: Path, devices: dict[str, tuple[str, str, str]]) -> None:
    """Write ``<root>/bus/usb/devices/<name>/{idVendor,idProduct,product}``."""
    base = root / "bus" / "usb" / "devices"
    base.mkdir(parents=True)
    for name, (vid, pid, product) in devices.items():
        entry = base / name
        entry.mkdir()
        (entry / "idVendor").write_text(vid + "\n")
        (entry / "idProduct").write_text(pid + "\n")
        (entry / "product").write_text(product + "\n")
    # An interface entry: no idVendor, never a device.
    (base / "1-1:1.0").mkdir()


class LinuxDiscoveryTests(unittest.TestCase):
    """CUPS queues are matched to sysfs USB devices by their VID:PID suffix."""

    def setUp(self):
        quiet_logging(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sys_root = Path(tmp.name)
        self.backend = linux.LinuxPrinterBackend(sys_root=self.sys_root)

    def discover(self, queues: list[str]):
        """Discover with ``queues`` as the CUPS printers."""
        with (
            mock.patch.object(
                linux.LinuxPrinterBackend,
                "_cups_printers",
                return_value={name: {} for name in queues},
            ),
            mock.patch.object(
                linux.LinuxPrinterBackend, "get_default_printer", return_value=None
            ),
        ):
            return self.backend.discover()

    def test_lists_usb_devices_from_sysfs(self):
        fake_sysfs(self.sys_root, {"1-1": ("0a5f", "0001", "SP310")})
        devices = linux.list_usb_devices(self.sys_root)
        self.assertEqual(
            [(d.vid_pid, d.name, d.connected) for d in devices],
            [("0A5F:0001", "SP310", True)],
        )

    def test_no_sysfs_means_no_devices(self):
        self.assertEqual(linux.list_usb_devices(self.sys_root / "missing"), [])

    def test_queue_with_plugged_in_vid_pid_is_usable(self):
        fake_sysfs(self.sys_root, {"1-1": ("0a5f", "0001", "SP310")})
        discovery = self.discover(
            ["iDPRT_SP310_0a5f:0001", "Office_Laser", "Dymo_0922:0028"]
        )
        self.assertEqual(
            [candidate.name for candidate in discovery.usable],
            ["iDPRT_SP310_0a5f:0001"],
        )
        self.assertEqual(len(discovery.usb_devices), 1)

    def test_nothing_plugged_in_means_nothing_usable(self):
        fake_sysfs(self.sys_root, {})
        self.assertEqual(self.discover(["iDPRT_SP310_0a5f:0001"]).usable, [])


if __name__ == "__main__":
    unittest.main()
