"""Win32 spooler and GDI constants, kept import-safe on every platform.

These mirror the ``win32print`` / ``wingdi.h`` values so that decoding and
ranking logic can be unit tested without pywin32 installed.
"""

# PRINTER_STATUS_* bits from PRINTER_INFO_2.Status.
PRINTER_STATUS_BITS: dict[int, str] = {
    0x00000001: "PAUSED",
    0x00000002: "ERROR",
    0x00000004: "PENDING_DELETION",
    0x00000008: "PAPER_JAM",
    0x00000010: "PAPER_OUT",
    0x00000020: "MANUAL_FEED",
    0x00000040: "PAPER_PROBLEM",
    0x00000080: "OFFLINE",
    0x00000100: "IO_ACTIVE",
    0x00000200: "BUSY",
    0x00000400: "PRINTING",
    0x00000800: "OUTPUT_BIN_FULL",
    0x00001000: "NOT_AVAILABLE",
    0x00002000: "WAITING",
    0x00004000: "PROCESSING",
    0x00008000: "INITIALIZING",
    0x00010000: "WARMING_UP",
    0x00020000: "TONER_LOW",
    0x00040000: "NO_TONER",
    0x00080000: "PAGE_PUNT",
    0x00100000: "USER_INTERVENTION",
    0x00200000: "OUT_OF_MEMORY",
    0x00400000: "DOOR_OPEN",
    0x00800000: "SERVER_UNKNOWN",
    0x01000000: "POWER_SAVE",
}

# Printer status bits that mean a job sent now will probably not come out.
PRINTER_PROBLEM_MASK = (
    0x00000001  # PAUSED
    | 0x00000002  # ERROR
    | 0x00000004  # PENDING_DELETION
    | 0x00000008  # PAPER_JAM
    | 0x00000010  # PAPER_OUT
    | 0x00000040  # PAPER_PROBLEM
    | 0x00000080  # OFFLINE
    | 0x00001000  # NOT_AVAILABLE
    | 0x00100000  # USER_INTERVENTION
    | 0x00400000  # DOOR_OPEN
)

# PRINTER_ATTRIBUTE_* bits from PRINTER_INFO_2.Attributes.
PRINTER_ATTRIBUTE_BITS: dict[int, str] = {
    0x00000001: "QUEUED",
    0x00000002: "DIRECT",
    0x00000004: "DEFAULT",
    0x00000008: "SHARED",
    0x00000010: "NETWORK",
    0x00000040: "LOCAL",
    0x00000100: "KEEPPRINTEDJOBS",
    0x00000400: "WORK_OFFLINE",
    0x00000800: "ENABLE_BIDI",
    0x00004000: "PUBLISHED",
}

PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400

# JOB_STATUS_* bits from JOB_INFO_1.Status.
JOB_STATUS_BITS: dict[int, str] = {
    0x00000001: "PAUSED",
    0x00000002: "ERROR",
    0x00000004: "DELETING",
    0x00000008: "SPOOLING",
    0x00000010: "PRINTING",
    0x00000020: "OFFLINE",
    0x00000040: "PAPEROUT",
    0x00000080: "PRINTED",
    0x00000100: "DELETED",
    0x00000200: "BLOCKED_DEVQ",
    0x00000400: "USER_INTERVENTION",
    0x00000800: "RESTART",
    0x00001000: "COMPLETE",
    0x00002000: "RETAINED",
    0x00004000: "RENDERING_LOCALLY",
}

JOB_STATUS_ERROR_MASK = (
    0x00000002  # ERROR
    | 0x00000020  # OFFLINE
    | 0x00000040  # PAPEROUT
    | 0x00000200  # BLOCKED_DEVQ
    | 0x00000400  # USER_INTERVENTION
)
JOB_STATUS_DELETED_MASK = 0x00000004 | 0x00000100  # DELETING | DELETED
JOB_STATUS_DONE_MASK = 0x00000080 | 0x00001000  # PRINTED | COMPLETE

# GetDeviceCaps indices.
DEVCAP_HORZRES = 8
DEVCAP_VERTRES = 10
DEVCAP_LOGPIXELSX = 88
DEVCAP_LOGPIXELSY = 90
DEVCAP_PHYSICALWIDTH = 110
DEVCAP_PHYSICALHEIGHT = 111
DEVCAP_PHYSICALOFFSETX = 112
DEVCAP_PHYSICALOFFSETY = 113


def decode_bits(value: int, table: dict[int, str]) -> list[str]:
    """Return the names of every flag set in ``value``."""
    return [name for bit, name in table.items() if value & bit]


def describe_bits(value: int, table: dict[int, str]) -> str:
    """Return a compact ``0x... (A, B)`` description of a bitfield."""
    names = decode_bits(value, table)
    return f"0x{value:08x} ({', '.join(names) if names else 'none'})"
