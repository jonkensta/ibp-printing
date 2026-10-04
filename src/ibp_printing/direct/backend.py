"""Direct USB printing layered in front of the platform (GDI / CUPS) backend.

:class:`DirectFirstBackend` wraps the platform backend:

* ``discover()`` runs the platform discovery, then (unless the kill switch is
  off, see :mod:`ibp_printing.direct.config`) lists USB printer-class devices
  and adds a candidate for every supported printer, ranked ahead of the
  queues. Direct discovery never raises: a failure is logged and recorded in
  ``discovery.errors``, and the queues are still returned.
* ``print_to_candidate()`` / ``print_image()`` send a direct candidate's job
  over USB with :func:`ibp_printing.direct.session.print_label`, and anything
  else to the platform backend.

Outcome mapping (direct jobs):

* the device cannot be opened (busy in the spooler or another program, no
  permission, unplugged) or the session's pre-check fails -> ``PrintError``
  (a :class:`~ibp_printing.direct.session.DirectPrintError`): nothing was
  sent, so ``print_to_first_available`` may try the next candidate and the
  watcher may queue to to-print/. The same printer's Windows queue is
  skipped when the reason is in
  :data:`~ibp_printing.direct.session.NO_SAME_PRINTER_FALLBACK` (cover open,
  realigning, not answering, not taking data);
* otherwise a ``PrintResult`` with the session's outcome (COMPLETED,
  UNCERTAIN, TIMEOUT or ERROR) and history. Only COMPLETED is ``ok``; after
  any other outcome nothing is retried or sent elsewhere.

``track_timeout_s``: a direct job is always followed (the printer pushes
``SSSGETPRINTING:DOING`` / ``DONE``), so the outcome is never NOT_TRACKED,
even with ``track_timeout_s=0``. The wait for DONE after DOING is
``max(track_timeout_s, SessionTimeouts.done_s)`` (30 s by default); the other
waits (pre-check, write, 10 s for DOING) keep their defaults.
"""

import dataclasses
import logging
import time
from typing import Any, Callable, Optional

from PIL import Image

from ibp_printing.backends.base import PrinterBackend, PrintError
from ibp_printing.direct.config import direct_mode, log_direct_mode
from ibp_printing.direct.session import (
    DirectPrintError,
    SessionTimeouts,
    StatusProbe,
    print_label,
    probe_status,
)
from ibp_printing.direct.transport import (
    DirectDevice,
    Transport,
    TransportError,
    discover_direct_devices,
    open_transport,
)
from ibp_printing.discovery import direct_candidates, is_direct_name, merge_direct
from ibp_printing.log import describe_exception, get_logger, log_event
from ibp_printing.models import (
    Discovery,
    PrinterCandidate,
    PrintResult,
    is_supported_direct_model,
)

logger = get_logger(__name__)

Opener = Callable[[DirectDevice], Transport]


def session_timeouts(base: SessionTimeouts, track_timeout_s: float) -> SessionTimeouts:
    """The session's timeouts for a print that asked for ``track_timeout_s``."""
    done_s = max(base.done_s, float(track_timeout_s or 0.0))
    return dataclasses.replace(base, done_s=done_s)


class DirectFirstBackend(PrinterBackend):
    """Direct USB printers first, the platform backend for everything else."""

    def __init__(
        self,
        inner: PrinterBackend,
        *,
        discover_devices: Callable[[], list[DirectDevice]] = discover_direct_devices,
        opener: Opener = open_transport,
        timeouts: Optional[SessionTimeouts] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        """
        Args:
            inner: The platform backend (queues, spooler).
            discover_devices / opener: Direct discovery and transport factory
                (tests pass fakes).
            timeouts: Base session timeouts (default ``SessionTimeouts()``).
            enabled: Force direct printing on/off for this backend; None
                follows the kill switch (``IBP_PRINTING_DIRECT``,
                ``set_direct_enabled``).
        """
        self.inner = inner
        self.platform_name = inner.platform_name
        self._discover_devices = discover_devices
        self._opener = opener
        self.timeouts = timeouts or SessionTimeouts()
        self._enabled = enabled
        log_direct_mode(f"backend created ({type(inner).__name__})")

    # -- mode -----------------------------------------------------------------

    def direct_mode(self) -> tuple[bool, str]:
        """``(enabled, why)`` for this backend."""
        if self._enabled is not None:
            return self._enabled, f"DirectFirstBackend(enabled={self._enabled})"
        return direct_mode()

    @property
    def direct_enabled(self) -> bool:
        """True when direct USB discovery and printing are on."""
        return self.direct_mode()[0]

    # -- discovery --------------------------------------------------------------

    def discover(self) -> Discovery:
        """Platform discovery plus direct USB candidates (never raised by direct)."""
        discovery = self.inner.discover()
        enabled, why = self.direct_mode()
        discovery.direct_enabled = enabled
        if not enabled:
            log_event(
                logger,
                logging.INFO,
                "direct USB discovery skipped (disabled)",
                decided_by=why,
            )
            return discovery
        started = time.monotonic()
        try:
            devices = self._discover_devices()
            merge_direct(discovery, devices)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            discovery.errors.append(f"direct USB discovery failed: {exc!r}")
            log_event(
                logger,
                logging.ERROR,
                "direct USB discovery failed; using queues only",
                error=describe_exception(exc),
            )
            return discovery
        direct = [
            candidate for candidate in discovery.candidates if candidate.is_direct
        ]
        log_event(
            logger,
            logging.INFO,
            "direct USB discovery merged",
            devices=len(discovery.direct_devices),
            direct_candidates=[candidate.to_log() for candidate in direct],
            usable=[candidate.name for candidate in discovery.usable],
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return discovery

    def list_devices(self) -> tuple[list[DirectDevice], list[str]]:
        """Every USB printer-class device (any model) and errors; never raises."""
        try:
            return list(self._discover_devices()), []
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.ERROR,
                "direct USB discovery failed",
                error=describe_exception(exc),
            )
            return [], [f"direct USB discovery failed: {exc!r}"]

    def probe(self, device: DirectDevice) -> StatusProbe:
        """Label-safe status probe (SSSGETCAP / SSSGETPAPER only); never raises.

        Only supported models are probed: another printer could print the
        query text.
        """
        if not is_supported_direct_model(device.model):
            return StatusProbe(
                error=f"not probed: model {device.model or '(unknown)'} is not "
                "supported (another printer could print the query text)"
            )
        try:
            transport = self._opener(device)
        except (TransportError, OSError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "status probe: cannot open the printer",
                path=device.path,
                error=describe_exception(exc),
            )
            return StatusProbe(error=f"cannot open {device.path}: {exc}")
        with transport:
            return probe_status(transport, timeouts=self.timeouts)

    def get_default_printer(self) -> Optional[str]:
        return self.inner.get_default_printer()

    def recent_print_events(
        self, minutes: int = 15, max_events: int = 40
    ) -> list[dict[str, Any]]:
        return self.inner.recent_print_events(minutes, max_events)

    def start_device_monitor(self, stop: Any) -> list[Any]:
        return self.inner.start_device_monitor(stop)

    # -- printing -----------------------------------------------------------------

    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Print to a direct candidate by name, or to a queue."""
        if not is_direct_name(printer_name):
            return self.inner.print_image(
                img, printer_name, job_name=job_name, track_timeout_s=track_timeout_s
            )
        self._require_enabled(printer_name)
        candidate = self._find_direct(printer_name)
        return self.print_direct(
            img, candidate, job_name=job_name, track_timeout_s=track_timeout_s
        )

    def print_to_candidate(
        self,
        img: Image.Image,
        candidate: PrinterCandidate,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Direct candidates over USB; queue candidates via the platform backend."""
        if candidate.direct_device is None:
            return self.inner.print_to_candidate(
                img, candidate, job_name=job_name, track_timeout_s=track_timeout_s
            )
        self._require_enabled(candidate.name)
        return self.print_direct(
            img, candidate, job_name=job_name, track_timeout_s=track_timeout_s
        )

    def _require_enabled(self, name: str) -> None:
        enabled, why = self.direct_mode()
        if not enabled:
            raise DirectPrintError(
                f"direct USB printing is disabled ({why})",
                "disabled",
                [f"not sent to {name}: direct USB printing is disabled ({why})"],
            )

    def _find_direct(self, name: str) -> PrinterCandidate:
        """Rediscover direct devices and return the one called ``name``."""
        devices, _ = self.list_devices()
        for candidate in direct_candidates(devices):
            if candidate.name == name:
                return candidate
        log_event(
            logger,
            logging.ERROR,
            "direct printer not found",
            printer=name,
            found=[device.to_log() for device in devices],
        )
        raise DirectPrintError(
            "printer not found (unplugged or off?)",
            "not_found",
            [f"not sent: no USB device matches {name!r}"],
        )

    def print_direct(
        self,
        img: Image.Image,
        candidate: PrinterCandidate,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """One direct USB print; see the module docstring for the outcomes.

        Raises:
            DirectPrintError: (a PrintError) when nothing was sent.
        """
        device = candidate.direct_device
        if device is None:
            raise ValueError(f"{candidate.name} is not a direct USB printer")
        if not candidate.usable:
            raise DirectPrintError(
                "printer is not usable: " + "; ".join(candidate.reasons()),
                "not_usable",
                [f"not sent to {candidate.name}: not usable"],
            )
        timeouts = session_timeouts(self.timeouts, track_timeout_s)
        started = time.monotonic()
        log_event(
            logger,
            logging.INFO,
            "direct USB print",
            printer=candidate.name,
            job_name=job_name,
            track_timeout_s=track_timeout_s,
            timeouts=timeouts.to_log(),
            device=device.to_log(),
        )
        transport = self._open(candidate)
        opened_s = round(time.monotonic() - started, 3)
        try:
            with transport:
                session = print_label(
                    transport, img, job_name=job_name, timeouts=timeouts
                )
        except DirectPrintError as exc:
            log_event(
                logger,
                logging.WARNING,
                "direct USB print not sent",
                printer=candidate.name,
                reason=exc.reason,
                history=exc.history,
                error=describe_exception(exc),
            )
            raise
        history = [
            f"direct USB: {device.path} ({device.kind or 'usb'}, serial "
            f"{device.serial or '-'}), opened in {opened_s} s"
        ] + session.history
        result = PrintResult(
            printer_name=candidate.name,
            job_name=job_name,
            job_id=None,
            outcome=session.job_outcome,
            history=history,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        log_event(
            logger,
            logging.INFO if result.outcome.ok else logging.ERROR,
            "direct USB print result",
            printer=candidate.name,
            outcome=result.outcome.value,
            open_s=opened_s,
            session=session.to_log(),
        )
        return result

    def _open(self, candidate: PrinterCandidate) -> Transport:
        """Open the candidate's device; any failure is a definite not-sent."""
        device = candidate.direct_device
        assert device is not None
        try:
            return self._opener(device)
        except (TransportError, OSError) as exc:
            reason = getattr(exc, "reason", "open_failed")
            log_event(
                logger,
                logging.WARNING,
                "cannot open the direct USB printer; nothing was sent",
                printer=candidate.name,
                reason=reason,
                error=describe_exception(exc),
            )
            raise DirectPrintError(
                f"cannot open {device.path}: {exc}",
                reason,
                [f"not sent to {candidate.name}: open failed ({reason}): {exc}"],
            ) from exc


def wrap_backend(inner: PrinterBackend) -> PrinterBackend:
    """Return ``inner`` with the direct USB layer in front of it."""
    if isinstance(inner, DirectFirstBackend):
        return inner
    return DirectFirstBackend(inner)


__all__ = [
    "DirectFirstBackend",
    "PrintError",
    "session_timeouts",
    "wrap_backend",
]
