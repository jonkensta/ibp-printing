"""Shared fakes for the ibp_printing core tests (not collected as a test module)."""

import logging
import unittest
from typing import Optional

from PIL import Image

from ibp_printing import log as ibp_log
from ibp_printing.backends import PrinterBackend, PrintError
from ibp_printing.discovery import discovery_from
from ibp_printing.models import (
    Discovery,
    JobOutcome,
    PrintQueue,
    PrintResult,
    UsbDevice,
)


def reset_logging() -> None:
    """Detach and close every handler configure_logging added."""
    package_logger = logging.getLogger(ibp_log.LOGGER_NAME)
    for handler in list(package_logger.handlers):
        package_logger.removeHandler(handler)
        handler.close()
    ibp_log._CONFIGURED_DIRS.clear()  # pylint: disable=protected-access


def quiet_logging(test: unittest.TestCase) -> None:
    """Keep package records off stderr (logging's lastResort) during ``test``."""
    package_logger = logging.getLogger(ibp_log.LOGGER_NAME)
    handler = logging.NullHandler()
    package_logger.addHandler(handler)
    test.addCleanup(package_logger.removeHandler, handler)


def dymo(vid_pid: str = "0922:0028", status: str = "OK") -> UsbDevice:
    """A present USB device with ``vid_pid``."""
    vid, pid = vid_pid.split(":")
    return UsbDevice(
        device_id=f"USB\\VID_{vid}&PID_{pid}\\5&1&0&1",
        name="DYMO LabelWriter 4XL",
        status=status,
        error_code=0,
        pnp_class="USB",
        vid_pid=vid_pid,
    )


class FakeBackend(PrinterBackend):
    """Backend whose queues, devices and per-printer failures are scripted."""

    platform_name = "fake"

    def __init__(
        self,
        queues: list[PrintQueue],
        devices: list[UsbDevice],
        fail: Optional[dict[str, BaseException]] = None,
        events: Optional[list[dict]] = None,
        outcomes: Optional[dict[str, JobOutcome]] = None,
    ) -> None:
        self.queues = queues
        self.devices = devices
        self.fail = fail or {}
        self.events = events or []
        self.outcomes = outcomes or {}
        self.printed: list[tuple[str, str, float]] = []

    def discover(self) -> Discovery:
        return discovery_from(self.queues, self.devices)

    def get_default_printer(self) -> Optional[str]:
        return next((q.name for q in self.queues if q.is_default), None)

    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        self.printed.append((printer_name, job_name, track_timeout_s))
        if printer_name in self.fail:
            raise self.fail[printer_name]
        default = JobOutcome.COMPLETED if track_timeout_s else JobOutcome.NOT_TRACKED
        return PrintResult(
            printer_name=printer_name,
            job_name=job_name,
            job_id=7,
            outcome=self.outcomes.get(printer_name, default),
        )

    def recent_print_events(self, minutes: int = 15, max_events: int = 40):
        return list(self.events)


__all__ = ["FakeBackend", "PrintError", "dymo", "quiet_logging", "reset_logging"]
