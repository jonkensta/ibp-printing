"""Linux printer backend using CUPS (pycups with an lpstat/lp fallback)."""

import logging
import os
import subprocess
import tempfile
from typing import Any, Optional

from PIL import Image

from ibp_printing.backends.base import PrinterBackend, PrintError
from ibp_printing.log import describe_exception, get_logger, log_event, timed_step
from ibp_printing.models import (
    Discovery,
    JobOutcome,
    PrinterCandidate,
    PrintQueue,
    PrintResult,
)
from ibp_printing.render import PRINT_SCALE_FACTOR, flatten_for_print, orient_portrait

logger = get_logger(__name__)

# Letter page at 300 DPI with quarter-inch margins.
DEFAULT_PRINT_DPI = 300
POINTS_PER_INCH = 72
PAGE_WIDTH_POINTS = 612
PAGE_HEIGHT_POINTS = 792
PAGE_MARGIN_POINTS = 18


class LinuxPrinterBackend(PrinterBackend):
    """CUPS backend. Every queue is usable; there is no USB VID:PID matching."""

    platform_name = "linux"

    def discover(self) -> Discovery:
        """List CUPS queues."""
        errors: list[str] = []
        queues: list[PrintQueue] = []
        default = self.get_default_printer()
        try:
            details = self._cups_printers()
            if details is None:
                names = self._lpstat_printers()
                details = {name: {} for name in names}
            for name, info in details.items():
                queues.append(
                    PrintQueue(
                        name=name,
                        port=str(info.get("device-uri", "")),
                        driver=str(info.get("printer-make-and-model", "")),
                        is_default=name == default,
                    )
                )
                log_event(logger, logging.DEBUG, "cups queue", name=name, info=info)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(f"queue enumeration failed: {exc!r}")
            logger.exception("CUPS queue enumeration failed")

        discovery = Discovery(
            queues=queues,
            candidates=[
                PrinterCandidate(queue, None, (), usb_matching=False)
                for queue in queues
            ],
            errors=errors,
        )
        log_event(
            logger,
            logging.INFO,
            "discovery",
            queues=[queue.name for queue in queues],
            errors=errors,
        )
        return discovery

    def get_default_printer(self) -> Optional[str]:
        """Return the CUPS default destination."""
        try:
            import cups  # type: ignore[import-not-found] # pylint: disable=import-outside-toplevel,import-error

            return (
                cups.Connection().getDefault()
            )  # pylint: disable=c-extension-no-member
        except ImportError:
            pass
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("CUPS default printer lookup failed", exc_info=True)

        try:
            result = subprocess.run(
                ["lpstat", "-d"], capture_output=True, text=True, timeout=5, check=False
            )
            if result.returncode == 0 and ":" in result.stdout:
                return result.stdout.strip().split(":")[-1].strip()
        except (OSError, subprocess.SubprocessError):
            logger.debug("lpstat -d failed", exc_info=True)
        return None

    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Print with ``lp`` after scaling onto a letter-size canvas."""
        result = PrintResult(printer_name=printer_name, job_name=job_name)
        img = self._scale_onto_page(flatten_for_print(orient_portrait(img)))

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmpfile:
            path = tmpfile.name
        try:
            img.save(path)
            with timed_step(logger, "lp", printer=printer_name):
                proc = subprocess.run(
                    [
                        "lp",
                        "-d",
                        printer_name,
                        "-t",
                        job_name,
                        "-o",
                        "fit-to-page",
                        path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            log_event(
                logger,
                logging.INFO if proc.returncode == 0 else logging.ERROR,
                "lp finished",
                returncode=proc.returncode,
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
            )
            if proc.returncode != 0:
                raise PrintError(f"Print command failed: {proc.stderr.strip()}")
        except subprocess.TimeoutExpired as exc:
            # lp may have queued the job before hanging: not a definite failure.
            result.outcome = JobOutcome.UNCERTAIN
            result.history.append(f"lp timed out after {exc.timeout}s")
            log_event(
                logger, logging.ERROR, "lp timed out", error=describe_exception(exc)
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log_event(logger, logging.ERROR, "lp failed", error=describe_exception(exc))
            raise PrintError(f"Print command failed: {exc}") from exc
        finally:
            self._remove_temp_file(path)
        return result

    @staticmethod
    def _remove_temp_file(path: str) -> None:
        """Delete the temporary PNG. Never raises: once ``lp`` has run, an
        exception here would hide whether the job was submitted."""
        try:
            os.remove(path)
        except OSError as exc:
            log_event(
                logger,
                logging.WARNING,
                "could not delete the temporary print file",
                path=path,
                error=describe_exception(exc),
            )

    @staticmethod
    def _cups_printers() -> Optional[dict[str, dict[str, Any]]]:
        try:
            import cups  # type: ignore[import-not-found] # pylint: disable=import-outside-toplevel,import-error
        except ImportError:
            return None
        return dict(
            cups.Connection().getPrinters()
        )  # pylint: disable=c-extension-no-member

    @staticmethod
    def _lpstat_printers() -> list[str]:
        proc = subprocess.run(
            ["lpstat", "-p"], capture_output=True, text=True, timeout=5, check=False
        )
        return [
            line.split()[1]
            for line in proc.stdout.splitlines()
            if line.startswith("printer ") and len(line.split()) >= 2
        ]

    @staticmethod
    def _scale_onto_page(img: Image.Image) -> Image.Image:
        """Scale to the printable area and center on a white letter page."""

        def to_px(points: float) -> int:
            return int(points * DEFAULT_PRINT_DPI / POINTS_PER_INCH)

        printable_w = to_px(PAGE_WIDTH_POINTS - 2 * PAGE_MARGIN_POINTS)
        printable_h = to_px(PAGE_HEIGHT_POINTS - 2 * PAGE_MARGIN_POINTS)
        scale = PRINT_SCALE_FACTOR * min(
            printable_w / img.size[0], printable_h / img.size[1]
        )
        scaled = img.resize(
            (int(img.size[0] * scale), int(img.size[1] * scale)),
            Image.Resampling.LANCZOS,
        )
        page_w, page_h = to_px(PAGE_WIDTH_POINTS), to_px(PAGE_HEIGHT_POINTS)
        canvas = Image.new("RGB", (page_w, page_h), "white")
        canvas.paste(
            scaled, ((page_w - scaled.size[0]) // 2, (page_h - scaled.size[1]) // 2)
        )
        return canvas
