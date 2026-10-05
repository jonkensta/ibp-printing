"""Shared folder locations: Downloads and the label print queue.

The label watcher watches Downloads. Inside it, three subfolders record what
happened to each label:

* ``to-print/``      - labels that definitely did not print. shippy and
  shippy-gui save a label here when it could not be sent to any printer; the
  watcher retries everything in it whenever a usable printer is available.
* ``printed/``       - labels the printer confirmed (or at least accepted).
* ``check-printer/`` - labels sent to a printer whose queue then reported a
  problem. They may still come out once the printer is fixed, so they are
  never retried automatically.

A label saved with metadata has a JSON *sidecar* next to it,
``<label>.png.json`` (see :func:`save_for_retry`): who the label is for and its
tracking code. The watcher never prints a sidecar and moves it along with its
label.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from PIL import Image

from ibp_printing.log import describe_exception, get_logger, log_event

logger = get_logger(__name__)

TO_PRINT_DIR = "to-print"
PRINTED_DIR = "printed"
CHECK_PRINTER_DIR = "check-printer"

# Suffix for half-written files; the watcher ignores it.
PARTIAL_SUFFIX = ".partial"
# Suffix of a label's metadata sidecar (``<label>.png.json``); never printed.
SIDECAR_SUFFIX = ".json"
# Keys written to every sidecar (missing ones are filled with "" / now).
SIDECAR_KEYS = (
    "tracking_code",
    "shipment_id",
    "recipient_label",
    "recipient_key",
    "app",
    "created",
)

FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def _known_folder_path(folder_id: str) -> Optional[Path]:
    """Call SHGetKnownFolderPath via ctypes; None on any failure."""
    # pylint: disable=import-outside-toplevel
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):  # pylint: disable=too-few-public-methods
        """Win32 GUID layout."""

        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = GUID()
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    hr = ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid))
    if hr != 0:
        log_event(logger, logging.WARNING, "CLSIDFromString failed", hresult=hr)
        return None

    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    out = ctypes.c_wchar_p()
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    try:
        if hr != 0 or not out.value:
            log_event(
                logger,
                logging.WARNING,
                "SHGetKnownFolderPath failed",
                hresult=hr & 0xFFFFFFFF,
                folder_id=folder_id,
            )
            return None
        return Path(out.value)
    finally:
        # The shell allocates the string even on some failures; always free it.
        ole32.CoTaskMemFree(out)


def downloads_dir() -> Path:
    """The Downloads folder, honoring Windows folder redirection.

    Falls back to ``~/Downloads`` off Windows or when the shell API fails.
    """
    fallback = Path.home() / "Downloads"
    if sys.platform != "win32":
        log_event(
            logger, logging.DEBUG, "downloads folder (non-Windows)", path=str(fallback)
        )
        return fallback
    try:
        path = _known_folder_path(FOLDERID_DOWNLOADS)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log_event(
            logger,
            logging.WARNING,
            "known-folder lookup raised; using fallback",
            error=describe_exception(exc),
            fallback=str(fallback),
        )
        path = None
    if path is None:
        log_event(
            logger, logging.WARNING, "using fallback Downloads", path=str(fallback)
        )
        return fallback
    log_event(logger, logging.INFO, "downloads folder from shell", path=str(path))
    return path


def default_state_dir() -> Path:
    """Fixed per-user folder for the watcher's lock and state, and the label journal.

    ``%LOCALAPPDATA%\\ibp-printing`` on Windows, ``$XDG_STATE_HOME/ibp-printing``
    (default ``~/.local/state/ibp-printing``) elsewhere. Deliberately independent
    of ``--log-dir`` and ``--watch-dir``, so two watchers for one user always
    contend for the same lock, and every app shares one label journal.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ibp-printing"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ibp-printing"


def to_print_dir(watch_dir: Optional[Path] = None) -> Path:
    """The print-queue folder (``<Downloads>/to-print`` by default)."""
    return (watch_dir or downloads_dir()) / TO_PRINT_DIR


def sidecar_path(label: Path) -> Path:
    """The metadata sidecar of a label file: ``<label>.json``."""
    return label.with_name(label.name + SIDECAR_SUFFIX)


def is_sidecar(path: Path) -> bool:
    """True for a metadata sidecar (any ``*.json``): never a label to print."""
    return path.name.lower().endswith(SIDECAR_SUFFIX)


def write_sidecar(label: Path, meta: Mapping[str, Any]) -> Path:
    """Write ``label``'s sidecar atomically (temporary name, then rename).

    Every key in :data:`SIDECAR_KEYS` is written (missing ones as "", and
    ``created`` as now); other keys in ``meta`` are kept.

    Raises:
        OSError: if it could not be written.
    """
    doc: dict[str, Any] = {key: meta.get(key) or "" for key in SIDECAR_KEYS}
    doc["created"] = meta.get("created") or time.time()
    for key, value in meta.items():
        doc.setdefault(key, value)
    final = sidecar_path(label)
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    partial.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    os.replace(partial, final)
    return final


def read_sidecar(label: Path) -> Optional[dict[str, Any]]:
    """``label``'s sidecar as a dict, or None if it is missing or unreadable.

    A corrupt sidecar is logged at WARNING; this never raises.
    """
    path = sidecar_path(label)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log_event(
            logger,
            logging.WARNING,
            "could not read label sidecar",
            path=str(path),
            error=describe_exception(exc),
        )
        return None
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        doc = exc
    if not isinstance(doc, dict):
        log_event(
            logger,
            logging.WARNING,
            "label sidecar is corrupt; ignoring it",
            path=str(path),
            content_start=raw[:200],
        )
        return None
    return doc


def save_for_retry(
    img: Image.Image,
    name: str,
    *,
    watch_dir: Optional[Path] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Save a label that could not be printed into the print-queue folder.

    The file is written under a temporary name and renamed into place, so the
    watcher never sees a half-written image.

    Args:
        name: Something that identifies the shipment, e.g. its tracking code.
        meta: Optional label metadata (``tracking_code``, ``shipment_id``,
            ``recipient_label``, ``recipient_key``, ``app``, ``created``).
            It is written to the sidecar ``<png>.json`` *before* the label
            appears, and, when it has a ``tracking_code``, the label journal
            marks that label ``queued`` (see :mod:`ibp_printing.labels`). A
            sidecar or journal problem is logged and never stops the label
            from being saved.

    Returns:
        The saved file's path.
    """
    queue = to_print_dir(watch_dir)
    queue.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "label"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = queue / f"{stamp}_{safe}.png"
    counter = 1
    while final.exists() or sidecar_path(final).exists():
        final = queue / f"{stamp}_{safe}_{counter}.png"
        counter += 1
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    img.save(partial, format="PNG")
    if meta is not None:
        try:
            write_sidecar(final, meta)
        except OSError as exc:
            log_event(
                logger,
                logging.ERROR,
                "could not write label sidecar; saving the label without it",
                path=str(sidecar_path(final)),
                error=describe_exception(exc),
            )
    os.replace(partial, final)
    log_event(logger, logging.WARNING, "label saved to print queue", path=str(final))
    if meta is not None and meta.get("tracking_code"):
        # pylint: disable-next=import-outside-toplevel,cyclic-import
        from ibp_printing import labels

        labels.update_status_from_meta(meta, labels.QUEUED, file=str(final))
    return final
