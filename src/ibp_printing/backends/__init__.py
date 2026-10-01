"""Platform-specific printer backends."""

import sys

from ibp_printing.backends.base import PrinterBackend, PrintError


def create_backend() -> PrinterBackend:
    """Return the backend for the running platform."""
    # pylint: disable=import-outside-toplevel
    if sys.platform == "win32":
        from ibp_printing.backends.windows import WindowsPrinterBackend

        return WindowsPrinterBackend()
    if sys.platform.startswith("linux"):
        from ibp_printing.backends.linux import LinuxPrinterBackend

        return LinuxPrinterBackend()

    from ibp_printing.backends.null import NullPrinterBackend

    return NullPrinterBackend()


__all__ = ["PrinterBackend", "PrintError", "create_backend"]
