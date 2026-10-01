"""Tests for the high-level printing API with a fake backend."""

import unittest

from PIL import Image
from printing_helpers import FakeBackend, dymo, quiet_logging

import ibp_printing
from ibp_printing import PrintError, PrintQueue
from ibp_printing.models import JobOutcome

FIRST = "A DYMO 0922:0028"
SECOND = "B DYMO 0922:0020"


class ApiTests(unittest.TestCase):
    """print_to_first_available, print_image and discovery helpers."""

    def setUp(self):
        self.img = Image.new("RGB", (1200, 1800), "white")
        self.addCleanup(ibp_printing.set_backend, None)
        quiet_logging(self)

    def use(self, backend: FakeBackend) -> FakeBackend:
        """Install ``backend`` as the process-wide backend."""
        ibp_printing.set_backend(backend)
        return backend

    def two_printers(self, **kwargs) -> FakeBackend:
        """A backend with two usable printers, FIRST ranked first."""
        return self.use(
            FakeBackend(
                [PrintQueue(SECOND), PrintQueue(FIRST), PrintQueue("Office laser")],
                [dymo("0922:0028"), dymo("0922:0020")],
                **kwargs,
            )
        )

    def test_prints_once_to_best_printer(self):
        backend = self.two_printers()
        result = ibp_printing.print_to_first_available(self.img, track_timeout_s=5)
        self.assertEqual(result.printer_name, FIRST)
        self.assertEqual(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual([call[0] for call in backend.printed], [FIRST])

    def test_falls_back_on_print_error(self):
        backend = self.two_printers(fail={FIRST: PrintError("StartDoc failed")})
        result = ibp_printing.print_to_first_available(self.img)
        self.assertEqual(result.printer_name, SECOND)
        self.assertEqual([call[0] for call in backend.printed], [FIRST, SECOND])

    def test_does_not_fall_back_on_unexpected_errors(self):
        backend = self.two_printers(fail={FIRST: ValueError("bug")})
        with self.assertRaises(ValueError):
            ibp_printing.print_to_first_available(self.img)
        self.assertEqual(len(backend.printed), 1)

    def test_every_printer_failing(self):
        backend = self.two_printers(
            fail={FIRST: PrintError("one"), SECOND: PrintError("two")}
        )
        with self.assertRaises(PrintError) as ctx:
            ibp_printing.print_to_first_available(self.img)
        self.assertIn("Every label printer failed", str(ctx.exception))
        self.assertIn(f"{FIRST}: one", str(ctx.exception))
        self.assertEqual(len(backend.printed), 2)

    def test_no_usable_printer(self):
        backend = self.use(
            FakeBackend([PrintQueue("DYMO 0922:0028"), PrintQueue("Laser")], [])
        )
        with self.assertRaises(PrintError) as ctx:
            ibp_printing.print_to_first_available(self.img)
        self.assertEqual(str(ctx.exception), "No label printer found plugged in.")
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertEqual(backend.printed, [])

    def test_job_name_carries_attempt_id(self):
        backend = self.two_printers()
        ibp_printing.print_to_first_available(self.img, job_name="Order 12")
        job_name = backend.printed[0][1]
        self.assertRegex(job_name, r"^Order 12 \[[0-9a-f]{10}\]$")

    def test_print_image_to_named_printer(self):
        backend = self.two_printers()
        result = ibp_printing.print_image(self.img, SECOND, track_timeout_s=2.5)
        self.assertEqual(result.printer_name, SECOND)
        self.assertEqual(backend.printed[0][0], SECOND)
        self.assertEqual(backend.printed[0][2], 2.5)
        self.assertRegex(backend.printed[0][1], r"^Shipping Label \[[0-9a-f]{10}\]$")

    def test_print_image_propagates_print_error(self):
        self.two_printers(fail={FIRST: PrintError("nope")})
        with self.assertRaises(PrintError):
            ibp_printing.print_image(self.img, FIRST)

    def test_find_label_printers_and_default(self):
        self.use(
            FakeBackend(
                [PrintQueue(FIRST), PrintQueue(SECOND, is_default=True)],
                [dymo("0922:0028"), dymo("0922:0020")],
            )
        )
        names = [candidate.name for candidate in ibp_printing.find_label_printers()]
        self.assertEqual(names, [SECOND, FIRST])
        self.assertEqual(ibp_printing.get_default_printer(), SECOND)
        self.assertEqual(len(ibp_printing.discover().candidates), 2)


if __name__ == "__main__":
    unittest.main()
