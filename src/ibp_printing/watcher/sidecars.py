"""Label metadata sidecars in the watcher: carried along, reported to the journal.

A label saved by :func:`ibp_printing.paths.save_for_retry` with metadata has a
sidecar ``<label>.png.json``. The watcher never prints it; whenever it moves a
label, the sidecar goes along, and the label journal
(:mod:`ibp_printing.labels`) records where the label went, matched by the
sidecar's tracking code.
"""

import logging
import os
from pathlib import Path

from ibp_printing import labels
from ibp_printing.log import describe_exception, get_logger, log_event
from ibp_printing.paths import (
    CHECK_PRINTER_DIR,
    PRINTED_DIR,
    TO_PRINT_DIR,
    read_sidecar,
    sidecar_path,
)

logger = get_logger(__name__)

# Journal status of a label moved into each folder.
JOURNAL_STATUS = {
    PRINTED_DIR: labels.PRINTED,
    CHECK_PRINTER_DIR: labels.CHECK_PRINTER,
    TO_PRINT_DIR: labels.QUEUED,
}


def carry_sidecar(path: Path, dest: Path, subdir: str) -> None:
    """After the label ``path`` moved to ``dest`` (in ``subdir``): move its
    sidecar along and update the label journal. Never raises."""
    old = sidecar_path(path)
    try:
        if not old.exists():
            return
    except OSError:
        return
    meta = read_sidecar(path)
    new = sidecar_path(dest)
    try:
        os.replace(old, new)
    except OSError as exc:
        log_event(
            logger,
            logging.WARNING,
            "could not move the label's sidecar with it (it is never printed)",
            sidecar=str(old),
            dest=str(new),
            error=describe_exception(exc),
        )
    else:
        log_event(logger, logging.DEBUG, "sidecar moved with its label", dest=str(new))
    status = JOURNAL_STATUS.get(subdir)
    if meta and meta.get("tracking_code") and status:
        try:
            labels.update_status_from_meta(meta, status, file=str(dest))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("label journal update failed (ignored)")
