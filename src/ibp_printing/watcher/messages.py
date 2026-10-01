"""The words volunteers see in the watcher's message boxes.

Each function returns the full box text. They are kept apart from the
processing logic so the wording can be reviewed (and tested) in one place.
"""

from pathlib import Path
from typing import Optional

_CHECK_PRINTER = "Check that the label printer is plugged in, turned on and has labels."


def _where(path: Path, moved_to: Optional[Path]) -> str:
    return f"It was moved to:\n{moved_to}" if moved_to else f"It is still at:\n{path}"


def did_not_print(
    path: Path, moved_to: Optional[Path], error: str, to_print: Path
) -> str:
    """A downloaded label definitely never reached a printer."""
    if moved_to is not None:
        next_step = (
            "It will print automatically as soon as a label printer is working, "
            "while the label watcher is running. You do not need to download it "
            "again."
        )
    else:
        next_step = (
            "The file could not be moved. Drag it into this folder and it will "
            f"print automatically once a printer works:\n{to_print}"
        )
    return (
        "The shipping label did NOT print.\n\n"
        f"File: {path.name}\n{_where(path, moved_to)}\n\n"
        f"Problem: {error}\n\n"
        f"{_CHECK_PRINTER} {next_step}"
    )


def check_printer(
    path: Path, moved_to: Optional[Path], error: str, to_print: Path
) -> str:
    """The label reached a printer queue, but the queue reported a problem."""
    return (
        "The shipping label may NOT have printed. It was sent to the printer, "
        "but the print queue reported a problem (or the watcher could not tell "
        "what happened). If it is still waiting in the queue it may print once "
        "the printer is fixed.\n\n"
        f"File: {path.name}\n{_where(path, moved_to)}\n\n"
        f"Problem: {error}\n\n"
        "Check the printer and its print queue BEFORE printing it again. If it "
        "really did not print, move the file into this folder and it will print "
        f"automatically:\n{to_print}"
    )


def interrupted(path: Path, moved_to: Optional[Path], since: str) -> str:
    """The watcher stopped while this label was being printed."""
    return (
        "The label watcher stopped while this shipping label was being printed "
        f"(at {since}), so it may or may not have printed.\n\n"
        f"File: {path.name}\n{_where(path, moved_to)}\n\n"
        "Check the printer before printing it again. It was NOT printed again "
        "automatically."
    )


def retry_failed(path: Path, error: str) -> str:
    """A label waiting in to-print still could not be printed."""
    return (
        "A shipping label waiting to be printed still could not be printed.\n\n"
        f"File: {path}\n\n"
        f"Problem: {error}\n\n"
        f"{_CHECK_PRINTER} The watcher keeps trying automatically; this message "
        "is shown only once for this label."
    )


def duplicate(path: Path, age_s: float, window_s: float) -> str:
    """Identical content was just sent to a printer."""
    return (
        "This label was NOT printed because the same label was sent to the "
        f"printer {age_s:.0f} seconds ago.\n\n"
        f"File: {path}\n\n"
        "If you really want a second copy, wait "
        f"{max(0.0, window_s - age_s):.0f} seconds, then rename the file."
    )


def queued_duplicate(path: Path, moved_to: Optional[Path], age_s: float) -> str:
    """A to-print file matches a label that was just sent to a printer."""
    return (
        "A label in the to-print folder was NOT printed because the same label "
        f"was sent to the printer {age_s:.0f} seconds ago.\n\n"
        f"File: {path.name}\n{_where(path, moved_to)}\n\n"
        "Check the printer before printing it again."
    )


def unreadable(path: Path, error: str) -> str:
    """A downloaded label stayed locked by another program."""
    return (
        "A downloaded file that may be a shipping label could not be read "
        "(another program may have it open).\n\n"
        f"File: {path}\n\n"
        f"Problem: {error}\n\n"
        "To print it, close any program using it and drag it into the to-print "
        "folder."
    )


def queued_unprintable(path: Path, reason: str) -> str:
    """A file in to-print can never be printed (wrong type, corrupt)."""
    return (
        "A file in the to-print folder cannot be printed.\n\n"
        f"File: {path}\n\n"
        f"Problem: {reason}\n\n"
        "Remove it, or replace it with the PNG or PDF label."
    )
