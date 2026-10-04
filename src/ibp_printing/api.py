"""High-level printing API used by shippy, shippy-gui and the label watcher."""

import logging
import threading
from typing import Optional

from PIL import Image

from ibp_printing.backends import PrinterBackend, PrintError, create_backend
from ibp_printing.direct.config import (  # pylint: disable=unused-import
    direct_enabled,
    set_direct_enabled,
)
from ibp_printing.direct.session import NO_SAME_PRINTER_FALLBACK
from ibp_printing.log import (
    attempt,
    current_attempt_id,
    describe_exception,
    get_logger,
    log_event,
)
from ibp_printing.models import Discovery, PrinterCandidate, PrintResult
from ibp_printing.render import describe_image

logger = get_logger(__name__)

_BACKEND: Optional[PrinterBackend] = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> PrinterBackend:
    """Return the process-wide backend, creating it on first use."""
    global _BACKEND  # pylint: disable=global-statement
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = create_backend()
        return _BACKEND


def set_backend(backend: Optional[PrinterBackend]) -> None:
    """Override the backend (tests, dry runs). ``None`` restores auto-detection.

    The backend given replaces everything, including the direct USB layer;
    wrap it in ``ibp_printing.direct.backend.DirectFirstBackend`` to keep it.
    """
    global _BACKEND  # pylint: disable=global-statement
    with _BACKEND_LOCK:
        _BACKEND = backend


def discover() -> Discovery:
    """Run a full discovery pass (every queue, every check, logged)."""
    return get_backend().discover()


def find_label_printers() -> list[PrinterCandidate]:
    """Usable USB label printers, best first."""
    return discover().usable


def get_default_printer() -> Optional[str]:
    """The OS default printer name, if any."""
    return get_backend().get_default_printer()


def _job_name(job_name: Optional[str]) -> str:
    # The attempt ID makes the job findable in the spooler and in the logs.
    suffix = current_attempt_id()
    base = job_name or "Shipping Label"
    return f"{base} [{suffix}]" if suffix else base


def print_image(
    img: Image.Image,
    printer_name: str,
    *,
    job_name: Optional[str] = None,
    track_timeout_s: float = 0.0,
) -> PrintResult:
    """Print to a specific printer (a queue name or a direct USB candidate name).

    Check ``result.outcome.ok``: ``JobOutcome.UNCERTAIN`` means a failure
    after StartDoc (or, for a direct USB printer, a job the printer may have
    partly taken) where the label may still print, and
    ``JobOutcome.TRACKING_FAILED`` means it was spooled but could not be
    followed. Neither should be refunded or resent automatically.

    Direct USB jobs are always followed until the printer reports DONE:
    ``track_timeout_s`` then only lengthens the wait for DONE beyond its
    30 s default.

    Raises:
        PrintError: (a RuntimeError) only if the job definitely never reached
            the spooler (or the direct USB printer).
    """
    with attempt("print_image", printer=printer_name, image=describe_image(img)):
        result = get_backend().print_image(
            img,
            printer_name,
            job_name=_job_name(job_name),
            track_timeout_s=track_timeout_s,
        )
        _log_result(result)
        return result


def print_to_first_available(
    img: Image.Image,
    *,
    job_name: Optional[str] = None,
    track_timeout_s: float = 0.0,
) -> PrintResult:
    """Print to the best usable label printer, falling back to the next one.

    Fallback only happens on PrintError, which the backends raise only when
    the job definitely never reached the spooler (or the direct USB printer,
    which is tried first when one is plugged in; its own Windows queue, if
    any, comes later in the list). A job that may have been submitted comes
    back as ``JobOutcome.UNCERTAIN`` (or TIMEOUT / ERROR) and is never
    re-sent to a second printer.

    When the direct printer refuses because its cover is open or it is still
    realigning (``reason`` ``cover_open`` / ``realign_busy``), or because it
    did not answer or take data (``no_cover_reply`` / ``job_not_accepted``:
    it may hold a partial job that would swallow a queued one), the same
    physical printer's Windows queue (same VID:PID) is skipped; other
    printers are still tried. The PrintError then carries that ``reason`` and
    the volunteer-facing message ("Close the printer cover..."), so callers
    keep the label (the watcher in ``to-print/``) and print it later.

    Raises:
        PrintError: if no printer is usable or every printer definitely failed
            to spool.
    """
    with attempt("print_to_first_available", image=describe_image(img)):
        discovery = discover()
        usable = discovery.usable
        if not usable:
            log_event(
                logger,
                logging.ERROR,
                "no usable label printer",
                candidates=[candidate.to_log() for candidate in discovery.candidates],
                errors=discovery.errors,
            )
            raise PrintError("No label printer found plugged in.")

        failures: list[str] = []
        # VID:PIDs of direct printers whose own queue must not be tried.
        blocked: dict[str, PrintError] = {}
        first_block: Optional[PrintError] = None
        for index, candidate in enumerate(usable, start=1):
            twin_of = blocked.get(candidate.vid_pid) if candidate.vid_pid else None
            if twin_of is not None and not candidate.is_direct:
                failures.append(f"{candidate.name}: skipped (same printer)")
                log_event(
                    logger,
                    logging.WARNING,
                    "skipping the direct printer's own queue: nothing is sent "
                    "to the same printer while it refuses",
                    printer=candidate.name,
                    vid_pid=candidate.vid_pid,
                    reason=twin_of.reason,
                )
                continue
            log_event(
                logger,
                logging.INFO,
                f"trying printer {index}/{len(usable)}",
                **candidate.to_log(),
            )
            try:
                result = get_backend().print_to_candidate(
                    img,
                    candidate,
                    job_name=_job_name(job_name),
                    track_timeout_s=track_timeout_s,
                )
            except PrintError as exc:
                failures.append(f"{candidate.name}: {exc}")
                log_event(
                    logger,
                    logging.WARNING,
                    "printer failed, trying next",
                    printer=candidate.name,
                    reason=exc.reason,
                    error=describe_exception(exc),
                )
                if candidate.is_direct and exc.reason in NO_SAME_PRINTER_FALLBACK:
                    first_block = first_block or exc
                    if candidate.vid_pid:
                        blocked[candidate.vid_pid] = exc
                continue
            _log_result(result)
            return result

        if first_block is not None:
            others = [
                line
                for line in failures
                if not line.endswith(("(same printer)", f": {first_block}"))
            ]
            message = str(first_block)
            if others:
                message += " Other printers also failed: " + "; ".join(others)
            raise PrintError(message, reason=first_block.reason)
        raise PrintError(
            "Every label printer failed: " + "; ".join(failures), reason="all_failed"
        )


def _log_result(result: PrintResult) -> None:
    log_event(
        logger,
        logging.INFO if result.outcome.ok else logging.ERROR,
        "print result",
        printer=result.printer_name,
        job_name=result.job_name,
        job_id=result.job_id,
        outcome=result.outcome.value,
        history=result.history,
        elapsed_s=result.elapsed_s,
    )
