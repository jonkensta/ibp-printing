"""Tests for structured logging."""

import contextlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from printing_helpers import reset_logging

from ibp_printing.log import (
    HumanFormatter,
    JsonFormatter,
    add_context,
    attempt,
    configure_logging,
    current_attempt_id,
    describe_exception,
    get_logger,
    log_event,
)


class CaptureMixin(unittest.TestCase):
    """Capture records from one test logger with a given formatter."""

    formatter: logging.Formatter

    def setUp(self):
        self.stream = io.StringIO()
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(self.formatter)
        handler.addFilter(add_context)
        self.logger = get_logger(f"test.{type(self).__name__}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(handler)
        self.addCleanup(self.logger.removeHandler, handler)

    def lines(self) -> list[str]:
        """Logged lines so far."""
        return self.stream.getvalue().splitlines()


class JsonFormatterTests(CaptureMixin):
    """JsonFormatter + add_context."""

    formatter = JsonFormatter()

    def test_line_shape_with_attempt_and_data(self):
        with attempt("unit test") as attempt_id:
            self.assertEqual(current_attempt_id(), attempt_id)
            log_event(
                self.logger, logging.INFO, "hello", printer="DYMO 0922:0028", n=[1, 2]
            )
        self.assertIsNone(current_attempt_id())

        (line,) = self.lines()
        record = json.loads(line)
        self.assertEqual(
            set(record),
            {
                "ts",
                "level",
                "logger",
                "host",
                "pid",
                "thread",
                "attempt_id",
                "msg",
                "data",
            },
        )
        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["logger"], "ibp_printing.test.JsonFormatterTests")
        self.assertEqual(record["attempt_id"], attempt_id)
        self.assertEqual(record["msg"], "hello")
        self.assertEqual(record["data"], {"printer": "DYMO 0922:0028", "n": [1, 2]})
        self.assertRegex(record["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}$")

    def test_no_attempt_no_data_and_unserializable_values(self):
        self.logger.info("plain")
        log_event(self.logger, logging.WARNING, "odd", obj=object())
        plain, odd = (json.loads(line) for line in self.lines())
        self.assertEqual(plain["attempt_id"], "-")
        self.assertNotIn("data", plain)
        self.assertIn("object object", odd["data"]["obj"])

    def test_exception_is_included(self):
        try:
            raise ValueError("bad")
        except ValueError:
            self.logger.exception("failed")
        record = json.loads(self.lines()[0])
        self.assertIn("ValueError: bad", record["exc"])


class HumanFormatterTests(CaptureMixin):
    """HumanFormatter."""

    formatter = HumanFormatter()

    def test_key_value_suffix(self):
        log_event(
            self.logger, logging.INFO, "queue", name="DYMO 0922:0028", jobs=0, ok=True
        )
        (line,) = self.lines()
        self.assertIn(" INFO    [-] MainThread ibp_printing.test.", line)
        self.assertTrue(
            line.endswith('queue | name="DYMO 0922:0028" jobs=0 ok=true'), line
        )

    def test_no_data_no_suffix(self):
        self.logger.info("bare")
        self.assertTrue(self.lines()[0].endswith(": bare"))


class ConfigureLoggingTests(unittest.TestCase):
    """configure_logging."""

    def setUp(self):
        reset_logging()
        self.addCleanup(reset_logging)
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.log_dir = Path(tmp.name) / "nested" / "logs"

    def test_creates_files_and_is_idempotent(self):
        package_logger = logging.getLogger("ibp_printing")
        returned = configure_logging(self.log_dir, console=False)
        self.assertEqual(returned, self.log_dir)
        count = len(package_logger.handlers)
        self.assertEqual(count, 2)

        configure_logging(self.log_dir, console=False)
        configure_logging(self.log_dir, console=True)
        self.assertEqual(len(package_logger.handlers), count)

        log_event(get_logger("x"), logging.DEBUG, "detail", k="v")
        for handler in package_logger.handlers:
            handler.flush()
        text = (self.log_dir / "printer.log").read_text(encoding="utf-8")
        self.assertIn("logging configured", text)
        self.assertIn("detail | k=v", text)
        records = [
            json.loads(line)
            for line in (self.log_dir / "printer.jsonl").read_text("utf-8").splitlines()
        ]
        self.assertEqual(records[-1]["data"], {"k": "v"})
        # Repeat calls return early without re-logging.
        self.assertEqual(
            sum(record["msg"] == "logging configured" for record in records), 1
        )

    def test_console_handler(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            configure_logging(self.log_dir, console=True)
        handlers = logging.getLogger("ibp_printing").handlers
        self.assertEqual(len(handlers), 3)
        self.assertIn("logging configured", stderr.getvalue())


class DescribeExceptionTests(unittest.TestCase):
    """describe_exception."""

    def test_pywin32_style_error(self):
        class FakeWinError(Exception):
            """Mimics pywintypes.error."""

            winerror = 1801
            funcname = "OpenPrinter"
            strerror = "The printer name is invalid."
            excepinfo = (0, "src", "desc", None, 0, -2147352567)
            hresult = None

        details = describe_exception(FakeWinError(1801, "OpenPrinter", "bad"))
        self.assertEqual(details["type"], "FakeWinError")
        self.assertEqual(details["winerror"], 1801)
        self.assertEqual(details["funcname"], "OpenPrinter")
        self.assertEqual(details["strerror"], "The printer name is invalid.")
        self.assertEqual(details["excepinfo"], repr(FakeWinError.excepinfo))
        self.assertNotIn("hresult", details)
        self.assertIn("OpenPrinter", details["repr"])

    def test_plain_exception(self):
        details = describe_exception(RuntimeError("x"))
        self.assertEqual(details, {"type": "RuntimeError", "repr": "RuntimeError('x')"})

    def test_os_error_strerror(self):
        details = describe_exception(OSError(2, "No such file"))
        self.assertEqual(details["strerror"], "No such file")


if __name__ == "__main__":
    unittest.main()
