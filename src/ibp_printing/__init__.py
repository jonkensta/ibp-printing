"""Shared USB label-printer discovery, printing, and diagnostics for IBP."""

from ibp_printing.api import (
    direct_enabled,
    discover,
    find_label_printers,
    get_backend,
    get_default_printer,
    print_image,
    print_to_first_available,
    set_backend,
    set_direct_enabled,
)
from ibp_printing.backends import PrinterBackend, PrintError
from ibp_printing.labels import (
    LabelRecord,
    LabelStatus,
    find_duplicates,
    pending_labels,
    recipient_key,
    record_purchase,
    update_status,
)
from ibp_printing.log import configure_logging, default_log_dir, log_file_names
from ibp_printing.paths import downloads_dir, save_for_retry, to_print_dir
from ibp_printing.models import (
    Discovery,
    JobOutcome,
    PrinterCandidate,
    PrintQueue,
    PrintResult,
    UsbDevice,
)

__all__ = [
    "Discovery",
    "JobOutcome",
    "LabelRecord",
    "LabelStatus",
    "PrintError",
    "PrintQueue",
    "PrintResult",
    "PrinterBackend",
    "PrinterCandidate",
    "UsbDevice",
    "configure_logging",
    "default_log_dir",
    "direct_enabled",
    "discover",
    "downloads_dir",
    "find_duplicates",
    "find_label_printers",
    "get_backend",
    "get_default_printer",
    "log_file_names",
    "pending_labels",
    "print_image",
    "print_to_first_available",
    "recipient_key",
    "record_purchase",
    "save_for_retry",
    "set_backend",
    "set_direct_enabled",
    "to_print_dir",
    "update_status",
]
