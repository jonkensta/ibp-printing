"""Locate the user's real Downloads folder (it can be redirected on Windows)."""

import logging
import sys
from pathlib import Path
from typing import Optional

from ibp_printing.log import describe_exception, get_logger, log_event

logger = get_logger(__name__)

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
