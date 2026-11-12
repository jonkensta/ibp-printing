"""``ibp-print-server``: the HTTP label print server on the intake stations.

The IBP frontend (``ibp-frontend/src/lib/printServer.ts``) runs in a browser
on the station and posts each request label PNG to this server, which prints
it silently on the station's USB label printer. The wire contract, kept from
the old standalone ibp-print-server:

* ``GET /health``: ``{"status": "ok", "service": "print-server",
  "printers": {"count": N, "names": [...]}}``, or ``"status": "degraded"``
  with an ``"error"`` if the printers could not be listed.
* ``POST /print-image``: the raw image (``Content-Type: image/png``, at most
  1 MB). ``200 {"status": "queued"}`` once queued; ``400`` (not an image, bad
  ``Content-Length``), ``413`` (too large) or ``503`` (no label printer) with
  ``{"status": "error", "error": "..."}``. The frontend falls back to the
  label PDF on any error.
* Every response, errors included, has ``Access-Control-Allow-Origin: *``;
  ``OPTIONS`` answers CORS preflights.

Jobs are printed one at a time, in order, by a single worker thread through
:func:`ibp_printing.print_to_first_available` (or, with ``--printer``,
:func:`ibp_printing.print_image` on that printer), so discovery, the CUPS
media fitting, direct USB printing and logging are the library's own. Each
job is followed for up to 60 s (the CUPS job state, or the direct printer's
own reports) and its outcome logged: "label printed (completed)", or an
ERROR "label may not have printed (...); check the printer". The
query string is ignored for now, so per-request options (e.g. a receipt roll
size) can be added to :class:`PrintJob` later without changing the routes.
"""

import argparse
import http.server
import io
import json
import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from PIL import Image

from ibp_printing import diagnostics
from ibp_printing.api import discover, print_image, print_to_first_available
from ibp_printing.backends import PrintError
from ibp_printing.log import (
    attempt,
    configure_logging,
    describe_exception,
    get_logger,
    install_exception_hooks,
    log_event,
    quiet,
)
from ibp_printing.models import PrintResult

logger = get_logger(__name__)

LOG_APP = "print-server"
SERVICE_NAME = "print-server"
DEFAULT_PORT = 40121
MAX_PAYLOAD_SIZE = 1024 * 1024  # 1 MB
JOB_NAME = "IBP Label"
# How long a job is followed in CUPS (or on a direct printer) before TIMEOUT.
TRACK_TIMEOUT_S = 60.0
# How long stop() waits for the label being printed before giving up on it.
STOP_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class PrintJob:
    """One queued label: the image bytes as received."""

    data: bytes
    received: float = field(default_factory=time.time)


class Printers:
    """Which printers the server may use, and printing to them.

    Without a preferred printer: every usable label printer, best first (see
    :func:`ibp_printing.discover`). With one: only that printer, and only if
    it exists (it need not be plugged in, as with the old print server).
    """

    def __init__(self, preferred: Optional[str] = None) -> None:
        self.preferred = preferred or None

    def available(self) -> list[str]:
        """Names of the printers a job would be sent to."""
        discovery = discover()
        if self.preferred is None:
            return [candidate.name for candidate in discovery.usable]
        if any(candidate.name == self.preferred for candidate in discovery.candidates):
            return [self.preferred]
        log_event(
            logger,
            logging.WARNING,
            "preferred printer not found",
            printer=self.preferred,
            printers=[candidate.name for candidate in discovery.candidates],
        )
        return []

    def print(self, img: Image.Image) -> PrintResult:
        """Print ``img``; raises PrintError if no printer took it."""
        if self.preferred is not None:
            return print_image(
                img, self.preferred, job_name=JOB_NAME, track_timeout_s=TRACK_TIMEOUT_S
            )
        return print_to_first_available(
            img, job_name=JOB_NAME, track_timeout_s=TRACK_TIMEOUT_S
        )


class _HttpServer(http.server.HTTPServer):
    """An HTTPServer that knows the PrintServer it belongs to."""

    app: "PrintServer"


class _Handler(http.server.BaseHTTPRequestHandler):
    """One HTTP request. Every response carries the CORS header."""

    server: _HttpServer
    # A client that stops sending must not block the (serial) server.
    timeout = 10
    server_version = "ibp-print-server"

    # pylint: disable-next=redefined-builtin
    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), format % args)

    def _route(self) -> str:
        return urlsplit(self.path).path

    def _send_json(
        self, status: int, payload: dict[str, Any], headers: Optional[dict] = None
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, error: str) -> None:
        self._send_json(status, {"status": "error", "error": error})

    def do_OPTIONS(self) -> None:  # pylint: disable=invalid-name
        """CORS preflight (also Chrome's private network access preflight)."""
        self.send_response(200, "ok")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """``GET /health``."""
        if self._route() != "/health":
            self._send_error_json(404, "Not found")
            return
        self._send_json(200, self.server.app.health())

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """``POST /print-image``."""
        if self._route() != "/print-image":
            self._send_error_json(404, "Not found")
            return
        try:
            status, payload = self._print_image()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("unexpected error handling POST /print-image")
            status, payload = 500, {"status": "error", "error": "Internal error"}
        self._send_json(status, payload)

    def _print_image(self) -> tuple[int, dict[str, Any]]:
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return 400, _error("Invalid Content-Length header")
        if length < 0:
            return 400, _error("Invalid Content-Length header")
        if length > MAX_PAYLOAD_SIZE:
            log_event(logger, logging.WARNING, "image too large", bytes=length)
            return 413, _error(f"Payload too large: {length} bytes")
        data = self.rfile.read(length)
        return self.server.app.submit(data)


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


def is_valid_image(data: bytes) -> bool:
    """True if Pillow recognises ``data`` as an intact image."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except Exception:  # pylint: disable=broad-exception-caught
        # verify() raises a mix of OSError, ValueError, SyntaxError, ...
        return False
    return True


class PrintServer:
    """The HTTP server plus the single worker thread that prints jobs in order."""

    def __init__(
        self, address: tuple[str, int], printers: Optional[Printers] = None
    ) -> None:
        self.printers = printers or Printers()
        self.jobs: "queue.Queue[PrintJob]" = queue.Queue()
        self._stop = threading.Event()
        self._httpd = _HttpServer(address, _Handler)
        self._httpd.app = self
        self._serve_thread = threading.Thread(
            target=self._httpd.serve_forever, name="print-server-http", daemon=True
        )
        self._worker = threading.Thread(
            target=self._work, name="print-server-worker", daemon=True
        )

    @property
    def port(self) -> int:
        """The port the server listens on (useful when started on port 0)."""
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        """Start serving and printing."""
        log_event(
            logger,
            logging.INFO,
            "print server starting",
            address=list(self._httpd.server_address),
            preferred_printer=self.printers.preferred,
        )
        self._worker.start()
        self._serve_thread.start()

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        """Stop serving, let the label being printed finish, drop the rest."""
        log_event(logger, logging.INFO, "print server stopping")
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._serve_thread.join()
        self._worker.join(timeout_s)
        if self._worker.is_alive():
            log_event(
                logger,
                logging.ERROR,
                "the label being printed did not finish in time",
                timeout_s=timeout_s,
            )
        dropped = self.jobs.qsize()
        if dropped:
            log_event(
                logger, logging.WARNING, "queued labels not printed", count=dropped
            )
        log_event(logger, logging.INFO, "print server stopped")

    def health(self) -> dict[str, Any]:
        """The ``/health`` body. Polled often, so only errors are logged."""
        try:
            with quiet():
                names = self.printers.available()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.ERROR,
                "health check could not list printers",
                error=describe_exception(exc),
            )
            return {"status": "degraded", "service": SERVICE_NAME, "error": str(exc)}
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "printers": {"count": len(names), "names": names},
        }

    def submit(self, data: bytes) -> tuple[int, dict[str, Any]]:
        """Validate and queue one image; returns the HTTP status and body."""
        if not is_valid_image(data):
            log_event(
                logger,
                logging.WARNING,
                "POST /print-image: not an image",
                bytes=len(data),
            )
            return 400, _error("Body is not a valid image")
        try:
            printers = self.printers.available()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log_event(
                logger,
                logging.ERROR,
                "could not list printers",
                error=describe_exception(exc),
            )
            printers = []
        if not printers:
            return 503, _error("No label printers are available")
        self.jobs.put(PrintJob(data))
        log_event(
            logger,
            logging.INFO,
            "label queued",
            bytes=len(data),
            queued=self.jobs.qsize(),
            printers=printers,
        )
        return 200, {"status": "queued"}

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self.print_job(job)

    def print_job(self, job: PrintJob) -> Optional[PrintResult]:
        """Print one job. Never raises: every failure is logged."""
        return print_job(job, self.printers)


def print_job(job: PrintJob, printers: Printers) -> Optional[PrintResult]:
    """Print one job; None if it was not printed. Never raises: failures are logged."""
    with attempt(
        "print-server job",
        bytes=len(job.data),
        waited_s=round(time.time() - job.received, 3),
    ):
        try:
            with Image.open(io.BytesIO(job.data)) as img:
                img.load()
                result = printers.print(img)
            _log_outcome(result)
            return result
        except PrintError as exc:
            log_event(
                logger,
                logging.ERROR,
                "label not printed",
                error=describe_exception(exc),
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("unexpected error printing a label")
    return None


def _log_outcome(result: PrintResult) -> None:
    """One line per label saying whether it printed."""
    if result.outcome.ok:
        log_event(
            logger,
            logging.INFO,
            f"label printed ({result.outcome.value})",
            printer=result.printer_name,
            job_id=result.job_id,
            elapsed_s=result.elapsed_s,
        )
        return
    log_event(
        logger,
        logging.ERROR,
        f"label may not have printed ({result.outcome.value}); check the printer",
        printer=result.printer_name,
        job_id=result.job_id,
        history=result.history,
        elapsed_s=result.elapsed_s,
    )


def serve(port: int, printer: Optional[str]) -> int:
    """Run the server until SIGINT or SIGTERM."""
    stop = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        log_event(logger, logging.INFO, "signal received", signal=signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    server = PrintServer(("", port), Printers(printer))
    server.start()
    try:
        while not stop.wait(1.0):
            pass
    finally:
        server.stop()
    return 0


def print_file(path: Path, printer: Optional[str]) -> int:
    """Print one image file the way the server prints a posted label."""
    data = path.read_bytes()
    if not is_valid_image(data):
        print(f"{path}: not an image", file=sys.stderr)
        return 1
    result = print_job(PrintJob(data), Printers(printer))
    if result is None:
        print("Not printed; see the log.", file=sys.stderr)
        return 1
    print(diagnostics.describe_result(result, ""))
    return 0 if result.outcome.ok else 1


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ibp-print-server",
        description="HTTP print server for the IBP intake stations' label printers.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="log directory (default: ~/.local/state/ibp-printing/logs)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    server = commands.add_parser("server", help="run the HTTP print server")
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    server.add_argument(
        "--printer", help="only ever print to this printer (queue or direct name)"
    )

    print_cmd = commands.add_parser(
        "print", help="print an image file as the server would print a posted label"
    )
    print_cmd.add_argument("image", type=Path)
    print_cmd.add_argument("--printer", help="printer to use (default: best usable)")

    commands.add_parser("list", help="list printers (same as ibp-print-diag)")
    test = commands.add_parser(
        "test", help="print a test label (same as ibp-print-diag --test-print)"
    )
    test.add_argument("--printer", help="printer to use (default: best usable)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``ibp-print-server``. Returns the process exit code."""
    args = _parse_args(argv)
    diag_args = ["--events", "0"]
    if args.log_dir is not None:
        diag_args += ["--log-dir", str(args.log_dir)]
    if args.command == "list":
        return diagnostics.main(diag_args)
    if args.command == "test":
        return diagnostics.main(
            diag_args + ["--test-print"] + ([args.printer] if args.printer else [])
        )

    configure_logging(args.log_dir, app=LOG_APP)
    install_exception_hooks()
    if args.command == "print":
        return print_file(args.image, args.printer)
    return serve(args.port, args.printer)


if __name__ == "__main__":
    sys.exit(main())
