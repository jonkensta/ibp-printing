"""LinuxPrinterBackend.print_image with ``lp`` and the filesystem faked."""

import subprocess
import unittest
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


if __name__ == "__main__":
    unittest.main()
