"""Platform-specific printer backends."""

import sys

from ibp_printing.backends.base import PrinterBackend, PrintError


def create_platform_backend() -> PrinterBackend:
    """Return the queue backend (GDI / CUPS) for the running platform."""
    # pylint: disable=import-outside-toplevel
    if sys.platform == "win32":
        from ibp_printing.backends.windows import WindowsPrinterBackend

        return WindowsPrinterBackend()
    if sys.platform.startswith("linux"):
        from ibp_printing.backends.linux import LinuxPrinterBackend

        return LinuxPrinterBackend()

    from ibp_printing.backends.null import NullPrinterBackend

    return NullPrinterBackend()


def create_backend() -> PrinterBackend:
    """The default backend: direct USB printers first, then the platform queues.

    The direct layer follows the kill switch (``IBP_PRINTING_DIRECT=0`` or
    ``ibp_printing.set_direct_enabled(False)``) on every call; when it is off
    the result behaves exactly like the platform backend.
    """
    # pylint: disable=import-outside-toplevel
    from ibp_printing.direct.backend import DirectFirstBackend

    return DirectFirstBackend(create_platform_backend())


__all__ = ["PrinterBackend", "PrintError", "create_backend", "create_platform_backend"]
