"""One direct-USB print on a PM2411BT, from pre-check to outcome.

``print_label`` runs one print over an already-open ``Transport``:

1. drain whatever the printer queued (cover pushes, realign DOING/DONE, ...);
2. ask ``SSSGETCAP`` (must be ``CLOSE``) and ``SSSGETPAPER`` (logged, never
   trusted: it says YES with the roll removed);
3. if a realign (``SSSGETPRINTING:DOING`` without ``DONE``) is running, wait
   briefly for it to finish;
4. write the job once, with a timeout;
5. follow the printer's pushed ``SSSGETPRINTING:DOING`` / ``DONE`` lines.

Outcome mapping (``docs/printers/pm2411bt.md``):

* pre-check fails (no reply, cover open, realign never finishes, query or
  transport failure, or not a single job byte accepted) -> raises
  ``DirectPrintError`` (a ``PrintError``): nothing was sent, safe to fall back;
* job not fully accepted within the timeout -> ``UNCERTAIN`` (paper out / jam:
  a partial job is in the printer, power-cycle it before reloading paper);
* DOING then DONE -> ``COMPLETED``;
* DOING but no DONE -> ``TIMEOUT``;
* no DOING within ``doing_s`` -> ``UNCERTAIN``;
* a ``Cmd error:`` reply to part of the job -> ``ERROR`` (the printer misread
  the job; a label may or may not have come out).

The job is never resent. Everything (bytes, lines, timings) is logged.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

from PIL import Image

from ibp_printing.backends.base import PrintError
from ibp_printing.direct import tspl
from ibp_printing.direct.tspl import EventKind, PrinterEvent
from ibp_printing.log import (
    describe_exception,
    exception_summary,
    get_logger,
    log_event,
)
from ibp_printing.models import JobOutcome

if TYPE_CHECKING:
    from ibp_printing.direct.transport import Transport

logger = get_logger(__name__)

PAPER_OUT_HINT = (
    "printer stopped accepting data (paper out? jam?) - a partial job is in "
    "the printer: power-cycle it before reloading paper"
)


class DirectPrintError(PrintError):
    """The pre-check failed: no job byte reached the printer.

    ``reason`` is a short machine-readable code (``cover_open``,
    ``no_cover_reply``, ``realign_busy``, ``query_not_accepted``,
    ``transport_error``, ``job_not_accepted``, ``bad_image``); ``history`` holds
    the human-readable steps taken so far.
    """

    def __init__(self, message: str, reason: str, history: list[str]) -> None:
        super().__init__(message)
        self.reason = reason
        self.history = list(history)


class SessionOutcome(str, Enum):
    """How a direct print ended (maps 1:1 onto ``JobOutcome``)."""

    COMPLETED = "completed"
    UNCERTAIN = "uncertain"
    TIMEOUT = "timeout"
    ERROR = "error"

    @property
    def job_outcome(self) -> JobOutcome:
        """The equivalent ``JobOutcome`` for a ``PrintResult``."""
        return JobOutcome(self.value)

    @property
    def ok(self) -> bool:
        """True only when the printer reported the label printed."""
        return self is SessionOutcome.COMPLETED


@dataclass(frozen=True)
class SessionTimeouts:
    """Every wait in a print session, in seconds.

    Defaults come from the hardware test: query replies arrive within ~1 s,
    a cover-close realign takes ~1-6 s, and a ~10 KB job is accepted at once
    and printed (DOING -> DONE) in a few seconds.
    """

    # Read queued lines before the first query.
    drain_s: float = 0.5
    # Accepting a query write (EAGAIN while the printer is busy).
    query_write_s: float = 1.0
    # Waiting for a query's reply line.
    query_reply_s: float = 1.5
    # How many times SSSGETCAP is asked before giving up.
    cover_query_attempts: int = 2
    # Waiting for a running realign (DOING) to report DONE before sending.
    realign_wait_s: float = 10.0
    # Accepting the whole job; a stall here means paper out / jam.
    job_write_s: float = 20.0
    # After the job is written: waiting for SSSGETPRINTING:DOING.
    doing_s: float = 10.0
    # After DOING: waiting for SSSGETPRINTING:DONE.
    done_s: float = 30.0
    # After a stalled write: keep listening this long, for the log only.
    stall_listen_s: float = 2.0
    # Longest single read_lines() call.
    poll_s: float = 0.25

    def to_log(self) -> dict[str, Any]:
        """Flatten for structured logging."""
        return dataclasses.asdict(self)


@dataclass
class SessionResult:  # pylint: disable=too-many-instance-attributes
    """Everything one print session learned."""

    outcome: SessionOutcome
    job_name: str
    job_bytes: int = 0
    bytes_written: int = 0
    cover: Optional[str] = None
    paper: Optional[str] = None
    saw_doing: bool = False
    saw_done: bool = False
    command_errors: list[str] = field(default_factory=list)
    # (seconds since the session started, event) for every line received.
    events: list[tuple[float, PrinterEvent]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def job_outcome(self) -> JobOutcome:
        """The equivalent ``JobOutcome``."""
        return self.outcome.job_outcome

    @property
    def ok(self) -> bool:
        """True only when the printer reported the label printed."""
        return self.outcome.ok

    def to_log(self) -> dict[str, Any]:
        """Flatten for structured logging."""
        return {
            "outcome": self.outcome.value,
            "job_name": self.job_name,
            "job_bytes": self.job_bytes,
            "bytes_written": self.bytes_written,
            "cover": self.cover,
            "paper": self.paper,
            "saw_doing": self.saw_doing,
            "saw_done": self.saw_done,
            "command_errors": self.command_errors,
            "events": [[round(at, 3), event.raw] for at, event in self.events],
            "timings": self.timings,
            "history": self.history,
            "elapsed_s": self.elapsed_s,
        }


ImageOrRaster = Union[Image.Image, bytes, bytearray]


class PrintSession:
    """Runs one print over ``transport``; not reusable across prints."""

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        transport: "Transport",
        *,
        job_name: str,
        timeouts: Optional[SessionTimeouts] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.job_name = job_name
        self.timeouts = timeouts or SessionTimeouts()
        self.clock = clock
        self.started = clock()
        self.result = SessionResult(outcome=SessionOutcome.UNCERTAIN, job_name=job_name)
        # Last SSSGETPRINTING value seen, for realign detection.
        self.printing: Optional[str] = None
        # Query bytes that may sit unterminated in the printer's line buffer.
        self._partial_queries: list[bytes] = []
        # True while such a fragment may still be unterminated.
        self._line_dirty = False
        # Set once the job write starts: Cmd errors then count against the job.
        self._job_started = False

    # ------------------------------------------------------------- helpers

    def _now(self) -> float:
        return self.clock() - self.started

    def _note(self, text: str, level: int = logging.INFO, **data: Any) -> None:
        """Add a history line and log it with the session clock."""
        stamp = self._now()
        self.result.history.append(f"+{stamp:.2f}s {text}")
        log_event(
            logger, level, text, t_s=round(stamp, 3), job_name=self.job_name, **data
        )

    def _fail(self, reason: str, message: str, **data: Any) -> DirectPrintError:
        self._note(f"not sent ({reason}): {message}", logging.ERROR, **data)
        self._finish_timing()
        return DirectPrintError(message, reason, self.result.history)

    def _finish_timing(self) -> None:
        self.result.elapsed_s = round(self._now(), 3)

    def _handle_line(self, line: str) -> PrinterEvent:
        event = tspl.parse_line(line)
        at = self._now()
        self.result.events.append((at, event))
        log_event(
            logger,
            logging.DEBUG,
            "printer line",
            t_s=round(at, 3),
            phase="job" if self._job_started else "pre-check",
            **event.to_log(),
        )
        if event.kind is EventKind.COVER:
            if self.result.cover is not None and self.result.cover != event.value:
                self._note(f"cover changed: {self.result.cover} -> {event.value}")
            self.result.cover = event.value
        elif event.kind is EventKind.PAPER:
            self.result.paper = event.value
        elif event.kind is EventKind.PRINTING:
            self.printing = event.value
            if self._job_started:
                if event.value == "DOING":
                    self.result.saw_doing = True
                elif event.value == "DONE" and self.result.saw_doing:
                    self.result.saw_done = True
                elif event.value == "DONE":
                    self._note("DONE without DOING (ignored)", logging.WARNING)
        elif event.kind is EventKind.COMMAND_ERROR:
            self._handle_command_error(event)
        else:
            log_event(
                logger, logging.WARNING, "unrecognised printer line", **event.to_log()
            )
        return event

    def _handle_command_error(self, event: PrinterEvent) -> None:
        echoed = event.value.encode("latin-1", errors="replace")
        stray = not echoed or any(
            query.startswith(echoed) for query in self._partial_queries
        )
        if not self._job_started or stray:
            # A stale line, an empty line, or the echo of a query fragment
            # that a later CR LF terminated: harmless for this job.
            self._note(
                f"printer rejected a line (ignored): {event.raw!r}", logging.WARNING
            )
            return
        self.result.command_errors.append(event.raw)
        self._note(f"printer rejected part of the job: {event.raw!r}", logging.ERROR)

    def _read(self, timeout_s: float) -> list[PrinterEvent]:
        lines = self.transport.read_lines(max(0.0, timeout_s))
        return [self._handle_line(line) for line in lines]

    def _listen(
        self, duration_s: float, until: Optional[Callable[[], bool]] = None
    ) -> bool:
        """Read lines for up to ``duration_s``; stop early once ``until()`` holds.

        Returns ``until()`` (or True when there is no condition).
        """
        deadline = self.clock() + duration_s
        while True:
            if until is not None and until():
                return True
            remaining = deadline - self.clock()
            if remaining <= 0:
                return until() if until is not None else True
            self._read(min(self.timeouts.poll_s, remaining))

    def _write_query(self, query: bytes) -> bool:
        """Send one query; never raises. False if the printer did not take it."""
        name = query.strip().decode("ascii")
        if self._line_dirty:
            # A fragment may sit unterminated in the printer's line buffer:
            # end it first so this query is read as a line of its own.
            query = tspl.CRLF + query
        started = self.clock()
        try:
            accepted = self.transport.write_all(query, self.timeouts.query_write_s)
        except OSError as exc:  # EAGAIN and friends, if the transport lets them out
            accepted = 0
            self._note(
                f"{name}: write failed, printer busy? ({exception_summary(exc)})",
                logging.WARNING,
                error=describe_exception(exc),
            )
        log_event(
            logger,
            logging.DEBUG,
            "query written",
            query=name,
            accepted=accepted,
            size=len(query),
            write_s=round(self.clock() - started, 3),
        )
        if accepted >= len(query):
            self._line_dirty = False
            return True
        if accepted > 0:
            # The fragment waits in the printer's line buffer; the next query
            # (or the job's leading CR LF) ends it, and the printer answers
            # it with a harmless "Cmd error:<fragment>".
            self._line_dirty = True
            fragment = query[:accepted].lstrip(b"\r\n")
            if fragment:
                self._partial_queries.append(fragment)
        self._note(
            f"{name}: printer busy, accepted {accepted}/{len(query)} bytes",
            logging.WARNING,
        )
        return False

    def _ask(self, query: bytes, kind: EventKind) -> Optional[str]:
        """Send ``query`` and wait for its reply; None if busy or no reply."""
        before = len(self.result.events)
        if not self._write_query(query):
            return None
        started = self.clock()

        def answered() -> bool:
            return any(event.kind is kind for _, event in self.result.events[before:])

        self._listen(self.timeouts.query_reply_s, answered)
        replies = [e for _, e in self.result.events[before:] if e.kind is kind]
        log_event(
            logger,
            logging.DEBUG,
            "query reply",
            query=query.strip().decode("ascii"),
            reply=replies[-1].value if replies else None,
            reply_s=round(self.clock() - started, 3),
        )
        return replies[-1].value if replies else None

    # ------------------------------------------------------------ the steps

    def precheck(self) -> None:
        """Drain, check the cover, log paper, wait out a realign.

        Raises:
            DirectPrintError: if printing must not start (nothing was sent).
        """
        step = self.clock()
        try:
            self._listen(self.timeouts.drain_s)
            queued = len(self.result.events)
            self._note(
                f"drained {queued} queued line(s)",
                lines=[event.raw for _, event in self.result.events],
            )

            cover: Optional[str] = None
            for attempt_no in range(1, self.timeouts.cover_query_attempts + 1):
                cover = self._ask(tspl.QUERY_COVER, EventKind.COVER)
                if cover is not None:
                    break
                self._note(f"no cover reply (attempt {attempt_no})", logging.WARNING)
            if cover is None:
                raise self._fail(
                    "no_cover_reply",
                    "printer did not answer the cover query (busy, or holding a "
                    "partial job? power-cycle it)",
                )
            self._note(f"cover: {cover}")
            if cover != "CLOSE":
                raise self._fail("cover_open", f"printer cover is {cover}; close it")

            paper = self._ask(tspl.QUERY_PAPER, EventKind.PAPER)
            self._note(f"paper sensor: {paper} (unreliable, not used)")

            if self.printing == "DOING":
                self._note("realign in progress (DOING without DONE); waiting")
                if not self._listen(
                    self.timeouts.realign_wait_s, lambda: self.printing != "DOING"
                ):
                    raise self._fail(
                        "realign_busy",
                        f"printer still busy after {self.timeouts.realign_wait_s:g} s "
                        "(DOING without DONE)",
                    )
                self._note(f"realign finished ({self.printing})")
                if self.result.cover != "CLOSE":
                    raise self._fail(
                        "cover_open", f"printer cover is {self.result.cover}; close it"
                    )
        except DirectPrintError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise self._fail(
                "transport_error",
                f"printer I/O failed before sending: {exception_summary(exc)}",
                error=describe_exception(exc),
            ) from exc
        finally:
            self.result.timings["precheck_s"] = round(self.clock() - step, 3)

    def send(self, job: bytes) -> bool:
        """Write the job once; True if every byte was accepted.

        Raises:
            DirectPrintError: if the printer accepted none of it.
        """
        self.result.job_bytes = len(job)
        self._note(f"writing job ({len(job)} bytes)")
        self._job_started = True
        started = self.clock()
        try:
            written = self.transport.write_all(job, self.timeouts.job_write_s)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return self._send_raised(exc, round(self.clock() - started, 3))
        write_s = round(self.clock() - started, 3)
        self.result.timings["write_s"] = write_s
        self.result.bytes_written = written
        log_event(
            logger,
            logging.INFO,
            "job write returned",
            written=written,
            size=len(job),
            write_s=write_s,
            bytes_per_s=round(written / write_s) if write_s > 0 else None,
        )
        if written <= 0:
            self._job_started = False
            raise self._fail(
                "job_not_accepted",
                f"printer accepted none of the job within "
                f"{self.timeouts.job_write_s:g} s (stuck? power-cycle it)",
            )
        if written < len(job):
            self._note(
                f"job write stalled at {written}/{len(job)} bytes after {write_s} s",
                logging.ERROR,
            )
            self._note(PAPER_OUT_HINT, logging.ERROR)
            return False
        self._note(f"job written ({written} bytes in {write_s} s)")
        return True

    def _send_raised(self, exc: Exception, write_s: float) -> bool:
        """The job write raised (device gone?): decide sent / maybe sent."""
        self.result.timings["write_s"] = write_s
        accepted = getattr(exc, "bytes_accepted", None)
        if accepted == 0:
            # The transport knows nothing went out: still a clean failure.
            self._job_started = False
            raise self._fail(
                "transport_error",
                f"job write failed before any byte was accepted: "
                f"{exception_summary(exc)}",
                error=describe_exception(exc),
            ) from exc
        self.result.bytes_written = accepted if isinstance(accepted, int) else -1
        sent = f"{accepted} bytes" if isinstance(accepted, int) else "an unknown part"
        self._note(
            f"job write failed after {write_s} s with {sent} of the job sent: "
            f"{exception_summary(exc)}; the label may still print - check the "
            "printer (power-cycle it before printing again)",
            logging.ERROR,
            error=describe_exception(exc),
        )
        return False

    def watch(self) -> SessionOutcome:
        """Follow DOING/DONE after a complete write."""
        started = self.clock()
        if not self._listen(self.timeouts.doing_s, lambda: self.result.saw_doing):
            self.result.timings["doing_wait_s"] = round(self.clock() - started, 3)
            self._note(
                f"printer never reported printing within {self.timeouts.doing_s:g} s "
                "(paper out? the label may still print)",
                logging.ERROR,
            )
            return self._outcome_with_errors(SessionOutcome.UNCERTAIN)
        doing_at = self.clock()
        self.result.timings["doing_after_s"] = round(doing_at - started, 3)
        self._note("printer started printing (DOING)")
        if not self._listen(self.timeouts.done_s, lambda: self.result.saw_done):
            self.result.timings["done_wait_s"] = round(self.clock() - doing_at, 3)
            self._note(
                f"printer never reported DONE within {self.timeouts.done_s:g} s "
                "(jam? paper ran out mid-label?)",
                logging.ERROR,
            )
            return self._outcome_with_errors(SessionOutcome.TIMEOUT)
        self.result.timings["print_s"] = round(self.clock() - doing_at, 3)
        self._note(f"printer finished (DONE after {self.result.timings['print_s']} s)")
        return self._outcome_with_errors(SessionOutcome.COMPLETED)

    def _outcome_with_errors(self, outcome: SessionOutcome) -> SessionOutcome:
        if self.result.command_errors and outcome is SessionOutcome.COMPLETED:
            self._note(
                "printer rejected part of the job, so the label may be wrong",
                logging.ERROR,
            )
            return SessionOutcome.ERROR
        if self.result.command_errors and outcome is SessionOutcome.UNCERTAIN:
            return SessionOutcome.ERROR
        return outcome

    def run(self, job: bytes) -> SessionResult:
        """Pre-check, send, watch. Raises ``DirectPrintError`` before sending."""
        log_event(
            logger,
            logging.INFO,
            "direct print session start",
            job_name=self.job_name,
            job_bytes=len(job),
            timeouts=self.timeouts.to_log(),
        )
        self.precheck()
        if not self.send(job):
            try:
                self._listen(self.timeouts.stall_listen_s)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._note(f"read failed: {exception_summary(exc)}", logging.WARNING)
            self.result.outcome = SessionOutcome.UNCERTAIN
        else:
            try:
                self.result.outcome = self.watch()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._note(
                    f"lost the printer while waiting for it: {exception_summary(exc)}",
                    logging.ERROR,
                    error=describe_exception(exc),
                )
                self.result.outcome = self._outcome_with_errors(
                    SessionOutcome.UNCERTAIN
                )
        self._finish_timing()
        log_event(
            logger,
            logging.INFO if self.result.ok else logging.ERROR,
            "direct print session end",
            **self.result.to_log(),
        )
        return self.result


def prepare_job(img_or_raster: ImageOrRaster) -> bytes:
    """Rasterize (if needed) and build the job, logging sizes and timings.

    Raises:
        DirectPrintError: (``bad_image``) if the input cannot be printed.
    """
    started = time.monotonic()
    try:
        if isinstance(img_or_raster, Image.Image):
            raster = tspl.rasterize(img_or_raster)
        else:
            raster = bytes(img_or_raster)
        rasterized = time.monotonic()
        job = tspl.build_job(raster)
    except (ValueError, OSError) as exc:
        log_event(
            logger, logging.ERROR, "cannot build job", error=describe_exception(exc)
        )
        raise DirectPrintError(
            f"cannot build the label job: {exception_summary(exc)}", "bad_image", []
        ) from exc
    done = time.monotonic()
    log_event(
        logger,
        logging.DEBUG,
        "job built",
        raster_bytes=len(raster),
        job_bytes=len(job),
        dark_dots=tspl.RASTER_SIZE * 8 - int.from_bytes(raster).bit_count(),
        rasterize_s=round(rasterized - started, 3),
        compress_s=round(done - rasterized, 3),
    )
    return job


def print_label(
    transport: "Transport",
    img_or_raster: ImageOrRaster,
    *,
    job_name: str,
    timeouts: Optional[SessionTimeouts] = None,
    clock: Callable[[], float] = time.monotonic,
    **timeout_overrides: Any,
) -> SessionResult:
    """Print one label over an open transport and report how it ended.

    Args:
        img_or_raster: A PIL image (placed like the other backends) or an
            already packed 124236-byte raster.
        timeouts: Every wait; individual fields can also be passed as keyword
            arguments (``doing_s=5.0``).

    Returns:
        The session result; ``result.outcome`` is COMPLETED, UNCERTAIN,
        TIMEOUT or ERROR, and ``result.history`` explains it.

    Raises:
        DirectPrintError: (a ``PrintError``) when nothing was sent.
    """
    settings = timeouts or SessionTimeouts()
    if timeout_overrides:
        settings = dataclasses.replace(settings, **timeout_overrides)
    job = prepare_job(img_or_raster)
    session = PrintSession(transport, job_name=job_name, timeouts=settings, clock=clock)
    return session.run(job)
