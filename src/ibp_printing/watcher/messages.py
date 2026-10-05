"""The words volunteers see in the watcher's message boxes.

Each function returns the full box text. They are kept apart from the
processing logic so the wording can be reviewed (and tested) in one place.
"""

from pathlib import Path
from typing import Optional

_CHECK_PRINTER = "Check that the label printer is plugged in, turned on and has labels."

# PrintError reasons (direct USB printer) that need a specific action. The
# label is NOT sent to the same printer's print queue for these; it waits in
# to-print and prints automatically once the printer is ready.
_ACTIONS = {
    "cover_open": "Close the label printer's cover.",
    "realign_busy": (
        "Close the label printer's cover and leave it closed for a few seconds."
    ),
    "no_cover_reply": (
        "Turn the label printer off, wait a few seconds and turn it on again."
    ),
    "job_not_accepted": (
        "Turn the label printer off, wait a few seconds and turn it on again."
    ),
}


def _action(reason: Optional[str]) -> str:
    return _ACTIONS.get(reason or "", _CHECK_PRINTER)


_REPRINT = (
    "If you are sure it did NOT print, rename the file so its name starts "
    "with REPRINT (for example REPRINT-label.png) and move it into this "
    "folder; it will then print automatically:"
)


def _where(path: Path, moved_to: Optional[Path]) -> str:
    return f"It was moved to:\n{moved_to}" if moved_to else f"It is still at:\n{path}"


def did_not_print(
    path: Path,
    moved_to: Optional[Path],
    error: str,
    to_print: Path,
    reason: Optional[str] = None,
) -> str:
    """A downloaded label definitely never reached a printer.

    ``reason`` is the PrintError's reason; a cover / not-answering reason
    replaces the generic advice with what to do.
    """
    if moved_to is not None:
        when = (
            "once the cover is closed"
            if reason in ("cover_open", "realign_busy")
            else "as soon as a label printer is working"
        )
        next_step = (
            f"It will print automatically {when}, while the label watcher is "
            "running. You do not need to download it again."
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
        f"{_action(reason)} {next_step}"
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
        "Check the printer and its print queue BEFORE printing it again. "
        f"{_REPRINT}\n{to_print}"
    )


def interrupted(
    path: Path, moved_to: Optional[Path], since: str, to_print: Path
) -> str:
    """The watcher stopped while this label was being printed."""
    return (
        "The label watcher stopped while this shipping label was being printed "
        f"(at {since}), so it may or may not have printed.\n\n"
        f"File: {path.name}\n{_where(path, moved_to)}\n\n"
        "Check the printer before printing it again. It was NOT printed again "
        f"automatically. {_REPRINT}\n{to_print}"
    )


def retry_failed(path: Path, error: str, reason: Optional[str] = None) -> str:
    """A label waiting in to-print still could not be printed."""
    return (
        "A shipping label waiting to be printed still could not be printed.\n\n"
        f"File: {path}\n\n"
        f"Problem: {error}\n\n"
        f"{_action(reason)} The watcher keeps trying automatically; this message "
        "is shown only once for this label."
    )


def already_printed(
    path: Path, moved_to: Optional[Path], printed_at: str, to_print: Path
) -> str:
    """A copy of a label that already printed was found (and not printed)."""
    return (
        "This shipping label was NOT printed again: the same label already "
        f"printed at {printed_at}.\n\n"
        f"File: {path.name}\n{_where(path, moved_to)}\n\n"
        "Nothing needs to be done. If you really need a second copy, rename the "
        "file so its name starts with REPRINT (for example REPRINT-label.png) "
        f"and move it into this folder:\n{to_print}"
    )


def uncertain_copy(
    path: Path, moved_to: Optional[Path], since: str, to_print: Path
) -> str:
    """A copy of a label whose earlier print may or may not have worked."""
    return (
        "This shipping label was NOT printed: the same label was sent to a "
        f"printer at {since}, and the watcher could not tell whether it "
        "printed.\n\n"
        f"File: {path.name}\n{_where(path, moved_to)}\n\n"
        f"Check the printer and its print queue first. {_REPRINT}\n{to_print}"
    )


def state_unsaved(state_path: Path, error: str) -> str:
    """The watcher cannot write its state file (it keeps printing)."""
    return (
        "The label watcher cannot save its state file, so its protection "
        "against printing a label twice after a crash or restart is "
        "weakened. It keeps printing labels.\n\n"
        f"File: {state_path}\n\nProblem: {error}\n\n"
        "Please tell the shipping coordinator (the disk may be full or the "
        "folder read-only). This message is shown only once."
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


def queued_unreadable(path: Path, error: str) -> str:
    """A file in to-print stayed locked or kept changing."""
    return (
        "A shipping label in the to-print folder could not be read (another "
        "program may have it open, or it is still being written).\n\n"
        f"File: {path}\n\nProblem: {error}\n\n"
        "Close any program using it, then rename the file; it will then be "
        "printed automatically."
    )


def queued_unprintable(path: Path, reason: str) -> str:
    """A file in to-print can never be printed (wrong type, corrupt)."""
    return (
        "A file in the to-print folder cannot be printed.\n\n"
        f"File: {path}\n\n"
        f"Problem: {reason}\n\n"
        "Remove it, or replace it with the PNG or PDF label."
    )


def queued_printed(labels: list[tuple[str, str]]) -> str:
    """Labels that waited in to-print printed (``(recipient, tracking)`` pairs).

    One box for everything that printed in one pass through to-print.
    """
    lines = [
        f"Label for {recipient or 'an unknown recipient'} "
        f"(tracking {tracking or '?'}) printed."
        for recipient, tracking in labels
    ]
    if len(lines) == 1:
        return (
            f"{lines[0]}\n\n"
            "It had been waiting in the to-print folder. Do not buy postage for "
            "this shipment again."
        )
    listing = "\n".join(f"- {line}" for line in lines)
    return (
        f"{len(lines)} shipping labels that were waiting in the to-print folder "
        f"printed:\n\n{listing}\n\n"
        "Do not buy postage for these shipments again."
    )
