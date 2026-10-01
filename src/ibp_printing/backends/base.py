"""Base class for platform-specific printer backends."""

import threading
from abc import ABC, abstractmethod
from typing import Any, Optional

from PIL import Image

from ibp_printing.models import Discovery, PrintResult


class PrintError(RuntimeError):
    """Raised when an image could not be handed to the spooler."""


class PrinterBackend(ABC):
    """Platform-specific printer discovery, printing and diagnostics."""

    platform_name = "unknown"

    @abstractmethod
    def discover(self) -> Discovery:
        """List every print queue and decide which ones are usable."""

    @abstractmethod
    def get_default_printer(self) -> Optional[str]:
        """Return the system default printer name, if any."""

    @abstractmethod
    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Send one image to one printer.

        Args:
            track_timeout_s: When positive, follow the spooled job for up to this
                many seconds and record how it ended in the result.

        Raises:
            PrintError: If the job could not be spooled.
        """

    def recent_print_events(
        self, minutes: int = 15, max_events: int = 40
    ) -> list[dict[str, Any]]:
        """Return recent OS print-subsystem events, newest first."""
        del minutes, max_events
        return []

    def start_device_monitor(self, stop: threading.Event) -> list[threading.Thread]:
        """Start background threads that log device plug/unplug events."""
        del stop
        return []
