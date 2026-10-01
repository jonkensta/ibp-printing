"""Null printer backend for unsupported platforms."""

import platform
from typing import Optional

from PIL import Image

from ibp_printing.backends.base import PrinterBackend, PrintError
from ibp_printing.models import Discovery, PrintResult


class NullPrinterBackend(PrinterBackend):
    """Backend that reports no printers and rejects print requests."""

    platform_name = "unsupported"

    def discover(self) -> Discovery:
        """Report no printers."""
        return Discovery(errors=[f"printing not supported on {platform.system()}"])

    def get_default_printer(self) -> Optional[str]:
        """Report no default printer."""
        return None

    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Refuse to print."""
        raise PrintError(f"Printing not supported on {platform.system()}")
