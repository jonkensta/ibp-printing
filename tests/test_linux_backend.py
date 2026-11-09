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


class MediaSizeTests(unittest.TestCase):
    """media_size_points / page_pixels / default_media."""

    def test_media_names(self):
        cases = {
            "custom_35.56x88.9mm_35.56x88.9mm": (35.56, 88.9, "mm"),
            "na_letter_8.5x11in": (8.5, 11, "in"),
            "oe_4x6-label_4x6in": (4, 6, "in"),
            "Custom.1.4x3.5in": (1.4, 3.5, "in"),
            "Custom.80x210mm": (80, 210, "mm"),
            "w288h432": (288, 432, "pt"),
        }
        per_unit = {"in": 72.0, "mm": 72 / 25.4, "pt": 1.0}
        for name, (width, height, unit) in cases.items():
            with self.subTest(name=name):
                got = linux.media_size_points(name)
                assert got is not None
                self.assertAlmostEqual(got[0], width * per_unit[unit])
                self.assertAlmostEqual(got[1], height * per_unit[unit])

    def test_names_without_a_size(self):
        for name in ("Letter", "A4", "Custom.WIDTHxHEIGHT", ""):
            with self.subTest(name=name):
                self.assertIsNone(linux.media_size_points(name))

    def test_intake_label_pixels(self):
        size = linux.media_size_points("custom_35.56x88.9mm_35.56x88.9mm")
        assert size is not None
        self.assertEqual(linux.page_pixels(size), (420, 1050))

    def test_default_media_from_lpoptions(self):
        stdout = (
            "PageSize/Media Size: w76h50 *w288h432 Custom.WIDTHxHEIGHT\n"
            "Resolution/Resolution: *203dpi\n"
        )
        with mock.patch.object(
            linux.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=stdout),
        ):
            self.assertEqual(linux.LinuxPrinterBackend.default_media("X"), "w288h432")

    def test_default_media_lpoptions_failure_is_none(self):
        with mock.patch.object(linux.subprocess, "run", side_effect=OSError("no")):
            self.assertIsNone(linux.LinuxPrinterBackend.default_media("X"))


class LinuxMediaPrintTests(unittest.TestCase):
    """What print_image hands to ``lp`` for a given default media."""

    def setUp(self):
        quiet_logging(self)
        self.backend = linux.LinuxPrinterBackend()

    def printed(self, img: Image.Image, media):
        """Print ``img`` with ``media`` as the default; return the PNG lp got."""
        sent: list[Image.Image] = []
        self.lp_args: list[str] = []

        def run(args, **_kwargs):
            self.lp_args = list(args)
            with Image.open(args[-1]) as png:
                png.load()
                sent.append(png)
            return completed(0)

        with (
            mock.patch.object(
                linux.LinuxPrinterBackend, "default_media", return_value=media
            ),
            mock.patch.object(linux.subprocess, "run", side_effect=run),
        ):
            self.backend.print_image(img, "Label_0a5f:0001", job_name="Label")
        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_landscape_label_fills_portrait_label_media(self):
        img = Image.new("L", (1050, 420), 255)
        img.putpixel((0, 0), 0)
        png = self.printed(img, "custom_35.56x88.9mm_35.56x88.9mm")
        self.assertEqual(png.size, (420, 1050))
        self.assertEqual(round(png.info["dpi"][0]), 300)
        self.assertEqual(png.tobytes(), img.rotate(90, expand=True).tobytes())
        self.assertNotIn("fit-to-page", self.lp_args)

    def test_backend_request_label_keeps_its_aspect_ratio(self):
        # ibp-backend's 85x32 mm label PNG on a 1.4x3.5 in label roll.
        png = self.printed(
            Image.new("L", (1004, 378), 0), "custom_35.56x88.9mm_35.56x88.9mm"
        )
        self.assertEqual(png.size, (420, 1050))
        self.assertEqual(png.point(lambda v: 255 - v).getbbox(), (12, 0, 407, 1050))

    def test_unknown_media_falls_back_to_a_letter_page(self):
        png = self.printed(Image.new("L", (1050, 420), 255), None)
        self.assertEqual(png.size, (2550, 3300))
        self.assertIn("fit-to-page", self.lp_args)


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
