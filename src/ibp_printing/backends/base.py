"""Base class for platform-specific printer backends."""

import threading
from abc import ABC, abstractmethod
from typing import Any, Optional

from PIL import Image

from ibp_printing.models import Discovery, PrinterCandidate, PrintResult


class PrintError(RuntimeError):
    """Raised only when the job definitely never reached the spooler.

    It is safe to retry (or send to another printer) after a PrintError. A
    failure that may have left a job behind is reported as a PrintResult with
    ``JobOutcome.UNCERTAIN`` instead.

    ``reason`` is a short machine-readable code when one is known (e.g. the
    direct USB path's ``cover_open``, ``realign_busy``, ``busy``), else None.
    """

    def __init__(self, *args: Any, reason: Optional[str] = None) -> None:
        super().__init__(*args)
        self.reason = reason


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
            PrintError: Only if the job definitely never reached the spooler;
                a possible partial submission returns ``JobOutcome.UNCERTAIN``.
        """

    def print_to_candidate(
        self,
        img: Image.Image,
        candidate: PrinterCandidate,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Print to a candidate from :meth:`discover` (same contract as print_image).

        The default prints to ``candidate.name``; the direct-USB layer
        overrides it to use the candidate's device without rediscovering.
        """
        return self.print_image(
            img, candidate.name, job_name=job_name, track_timeout_s=track_timeout_s
        )

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
