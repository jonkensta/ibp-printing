"""Tests for structured logging."""

import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from printing_helpers import reset_logging

from ibp_printing.log import (
    HumanFormatter,
    JsonFormatter,
    SafeRotatingFileHandler,
    add_context,
    attempt,
    configure_logging,
    current_attempt_id,
    describe_exception,
    exception_summary,
    get_logger,
    log_event,
    log_file_names,
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

    def test_data_may_use_parameter_names(self):
        # PrintService events carry "level" and "message" keys (C1).
        event = {"level": "2", "message": "failed", "logger": "x"}
        log_event(self.logger, logging.INFO, "event", **event)
        log_event(self.logger, logging.INFO, "nested", event=event)
        flat, nested = (json.loads(line) for line in self.lines())
        self.assertEqual(flat["level"], "INFO")
        self.assertEqual(flat["data"], event)
        self.assertEqual(nested["data"], {"event": event})

    def test_nested_attempt_reuses_id(self):
        with attempt("outer") as outer_id:
            with attempt("inner", file="a.png") as inner_id:
                self.assertEqual(inner_id, outer_id)
                self.assertEqual(current_attempt_id(), outer_id)
            self.assertEqual(current_attempt_id(), outer_id)
        self.assertIsNone(current_attempt_id())
        with attempt("next") as next_id:
            self.assertNotEqual(next_id, outer_id)

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
        text = (self.log_dir / "printer-ibp-printing.log").read_text(encoding="utf-8")
        self.assertIn("logging configured", text)
        self.assertIn("detail | k=v", text)
        records = [
            json.loads(line)
            for line in (self.log_dir / "printer-ibp-printing.jsonl")
            .read_text("utf-8")
            .splitlines()
        ]
        self.assertEqual(records[-1]["data"], {"k": "v"})
        # Repeat calls return early without re-logging.
        self.assertEqual(
            sum(record["msg"] == "logging configured" for record in records), 1
        )

    def test_per_app_files(self):
        configure_logging(self.log_dir, app="watcher", console=False)
        configure_logging(self.log_dir, app="shippy-gui", console=False)
        configure_logging(self.log_dir, app="watcher", console=False)
        package_logger = logging.getLogger("ibp_printing")
        self.assertEqual(len(package_logger.handlers), 4)
        for handler in package_logger.handlers:
            handler.flush()
        names = sorted(path.name for path in self.log_dir.iterdir())
        self.assertEqual(
            names,
            [
                "printer-shippy-gui.jsonl",
                "printer-shippy-gui.log",
                "printer-watcher.jsonl",
                "printer-watcher.log",
            ],
        )

    def test_log_file_names(self):
        self.assertEqual(
            log_file_names("diag"), ("printer-diag.log", "printer-diag.jsonl")
        )
        self.assertEqual(log_file_names(), log_file_names("ibp-printing"))
        self.assertEqual(log_file_names("a/b c")[0], "printer-a-b-c.log")
        self.assertEqual(log_file_names("..")[0], "printer-ibp-printing.log")

    def test_console_handler(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            configure_logging(self.log_dir, console=True)
        handlers = logging.getLogger("ibp_printing").handlers
        self.assertEqual(len(handlers), 3)
        self.assertIn("logging configured", stderr.getvalue())


class SafeRotatingFileHandlerTests(unittest.TestCase):
    """Rotation that fails (a second process holds the file) loses nothing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.base = self.dir / "printer-shippy.log"
        self.base.write_text("current-0\n", encoding="utf-8")
        self.backups = {}
        for index in (1, 2, 3):
            path = Path(f"{self.base}.{index}")
            path.write_text(f"backup-{index}\n", encoding="utf-8")
            self.backups[index] = f"backup-{index}\n"
        self.now = 1000.0
        self.handler = SafeRotatingFileHandler(
            self.base, maxBytes=200, backupCount=3, encoding="utf-8"
        )
        self.handler.clock = lambda: self.now
        self.handler.setFormatter(HumanFormatter())
        self.handler.addFilter(add_context)
        self.addCleanup(self.handler.close)
        self.logger = logging.getLogger("ibp_printing.test.rotation")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.real_rename = os.rename
        self.renames: list[tuple[str, str]] = []

    def fail_rename(self, should_fail):
        """Patch os.rename in ibp_printing.log to fail when ``should_fail``."""

        def fake(source, target):
            self.renames.append((str(source), str(target)))
            if should_fail(str(source), str(target)):
                raise PermissionError(
                    13, "The process cannot access the file", str(source)
                )
            self.real_rename(source, target)

        patcher = mock.patch("ibp_printing.log.os.rename", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def log_lines(self, count, prefix="record"):
        """Log ``count`` long-ish records; return their messages."""
        messages = [f"{prefix}-{n:03d} " + "x" * 60 for n in range(count)]
        for message in messages:
            self.logger.info(message)
        return messages

    def all_text(self):
        """Every log file's content, current file first."""
        paths = [self.base] + sorted(self.dir.glob("printer-shippy.log.*"))
        return "".join(path.read_text("utf-8") for path in paths)

    def assert_backups_intact(self):
        """The pre-existing backups were not moved, changed, or deleted."""
        for index, content in self.backups.items():
            self.assertEqual(
                Path(f"{self.base}.{index}").read_text("utf-8"), content, index
            )

    def test_sharing_violation_keeps_writing_and_backs_off(self):
        self.fail_rename(lambda source, target: source == str(self.base))
        messages = self.log_lines(10)

        text = self.base.read_text("utf-8")
        self.assertTrue(text.startswith("current-0\n"))
        for message in messages:
            self.assertIn(message, text)
        self.assertIn("log rotation failed", text)
        self.assertEqual(text.count("log rotation failed"), 1)
        self.assertIsInstance(self.handler.last_rotation_error, PermissionError)
        # Only one rotation attempt during the back-off, touching only the base.
        self.assertEqual(len(self.renames), 1)
        self.assert_backups_intact()
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            [
                "printer-shippy.log",
                "printer-shippy.log.1",
                "printer-shippy.log.2",
                "printer-shippy.log.3",
            ],
        )

    def test_rotation_resumes_after_back_off(self):
        failing = [True]
        self.fail_rename(lambda source, target: failing[0] and source == str(self.base))
        first = self.log_lines(5, "early")
        before = self.base.read_text("utf-8")

        failing[0] = False
        self.now += SafeRotatingFileHandler.retry_interval_s + 1
        later = self.log_lines(1, "later")

        self.assertIsNone(self.handler.last_rotation_error)
        self.assertEqual(Path(f"{self.base}.1").read_text("utf-8"), before)
        self.assertEqual(Path(f"{self.base}.2").read_text("utf-8"), "backup-1\n")
        self.assertEqual(Path(f"{self.base}.3").read_text("utf-8"), "backup-2\n")
        self.assertIn(later[0], self.base.read_text("utf-8"))
        for message in first:
            self.assertIn(message, before)
        self.assertEqual(len(list(self.dir.iterdir())), 4)

    def test_failure_mid_cascade_rolls_back(self):
        self.fail_rename(
            lambda source, target: source == f"{self.base}.1"
            and target == f"{self.base}.2"
        )
        messages = self.log_lines(6)

        self.assert_backups_intact()
        text = self.base.read_text("utf-8")
        self.assertTrue(text.startswith("current-0\n"))
        for message in messages:
            self.assertIn(message, text)
        self.assertIn("log rotation failed", text)
        # No holding/dropping leftovers.
        self.assertEqual(len(list(self.dir.iterdir())), 4)

    def test_base_implementation_would_lose_history(self):
        # Documents why the subclass exists: the stdlib handler shifts backups
        # before renaming the open file, so repeated failures destroy history.
        plain = logging.handlers.RotatingFileHandler(
            self.dir / "plain.log", maxBytes=1, backupCount=2, encoding="utf-8"
        )
        self.addCleanup(plain.close)
        for index in (1, 2):
            Path(f"{self.dir / 'plain.log'}.{index}").write_text(f"b{index}")
        self.fail_rename(lambda source, target: source == str(self.dir / "plain.log"))
        with self.assertRaises(PermissionError):
            plain.doRollover()
        with self.assertRaises(PermissionError):
            plain.doRollover()
        # The oldest backup ("b2") was deleted and nothing replaced ".1".
        self.assertFalse(Path(f"{self.dir / 'plain.log'}.1").exists())
        self.assertEqual(Path(f"{self.dir / 'plain.log'}.2").read_text(), "b1")

    def test_configure_logging_uses_it(self):
        reset_logging()
        self.addCleanup(reset_logging)
        configure_logging(self.dir / "logs", console=False)
        handlers = logging.getLogger("ibp_printing").handlers
        self.assertTrue(handlers)
        self.assertTrue(
            all(isinstance(handler, SafeRotatingFileHandler) for handler in handlers)
        )


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
        self.assertEqual(
            details, {"type": "RuntimeError", "repr": "RuntimeError('x')", "str": "x"}
        )
        self.assertEqual(exception_summary(RuntimeError("x")), "RuntimeError: x")

    def test_wmi_error(self):
        class FakeXWmi(Exception):
            """Mimics wmi.x_wmi: info + com_error, custom __str__."""

            def __init__(self, info="", com_error=None):
                super().__init__()
                self.info = info
                self.com_error = com_error

            def __str__(self):
                return f"<x_wmi: {self.info} {self.com_error}>"

        com_error = RuntimeError(-2147217392, "Exception occurred.", None, None)
        details = describe_exception(FakeXWmi("Invalid class", com_error))
        self.assertEqual(details["info"], "Invalid class")
        self.assertEqual(details["com_error"], repr(com_error))
        self.assertIn("Invalid class", details["str"])
        summary = exception_summary(FakeXWmi("", com_error))
        self.assertTrue(summary.startswith("FakeXWmi: <x_wmi:"), summary)
        self.assertNotIn("info=", summary)

    def test_os_error_strerror(self):
        details = describe_exception(OSError(2, "No such file"))
        self.assertEqual(details["strerror"], "No such file")


if __name__ == "__main__":
    unittest.main()
