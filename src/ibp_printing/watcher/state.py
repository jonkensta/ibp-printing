"""Per-user watcher state that survives restarts: the lock and a small JSON file.

The state file records, for each watched folder:

* ``seen_until`` - every file in the folder whose mtime is at or before this
  wall-clock time has been decided (printed, skipped, ...). Files queued but not
  yet handled hold it back, so a crash or shutdown never loses them: on the next
  start the watcher processes every file modified after ``seen_until``.
* ``clean_shutdown`` - False while running, so the next start can tell a crash.
* ``in_flight`` - content hashes of labels handed to a printer whose outcome
  was not yet recorded. If the watcher dies mid-print, the next start files
  such a label under ``check-printer/`` instead of printing it a second time.

A missing, unreadable or corrupt file is logged and treated as "no history";
it never stops the watcher.
"""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ibp_printing.log import describe_exception, get_logger, log_event

logger = get_logger(__name__)

STATE_FILENAME = "watcher-state.json"
LOCK_FILENAME = "watcher.lock"
STATE_VERSION = 1
# Forget in-flight records this old (the label has long since been dealt with).
IN_FLIGHT_MAX_AGE_S = 7 * 24 * 3600.0


def default_state_dir() -> Path:
    """Fixed per-user folder for the lock and state file.

    ``%LOCALAPPDATA%\\ibp-printing`` on Windows, ``$XDG_STATE_HOME/ibp-printing``
    (default ``~/.local/state/ibp-printing``) elsewhere. Deliberately independent
    of ``--log-dir`` and ``--watch-dir``, so two watchers for one user always
    contend for the same lock.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ibp-printing"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ibp-printing"


def iso(timestamp: Optional[float]) -> Optional[str]:
    """Local ISO time for logs (None stays None)."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


@dataclass
class FolderState:
    """What the watcher remembers about one watched folder."""

    seen_until: float
    updated: float
    clean_shutdown: bool = False
    in_flight: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """The on-disk form."""
        return {
            "seen_until": self.seen_until,
            "seen_until_local": iso(self.seen_until),
            "updated": self.updated,
            "updated_local": iso(self.updated),
            "clean_shutdown": self.clean_shutdown,
            "in_flight": self.in_flight,
        }

    @classmethod
    def from_json(cls, data: Any) -> "FolderState":
        """Parse one folder entry; raises ValueError if it is malformed."""
        if not isinstance(data, dict):
            raise ValueError("folder entry is not an object")
        seen_until = data.get("seen_until")
        updated = data.get("updated", seen_until)
        for name, value in (("seen_until", seen_until), ("updated", updated)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} is not a number: {value!r}")
        in_flight = data.get("in_flight", {})
        if not isinstance(in_flight, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in in_flight.items()
        ):
            raise ValueError("in_flight is not an object of objects")
        return cls(
            seen_until=float(seen_until),  # type: ignore[arg-type]
            updated=float(updated),  # type: ignore[arg-type]
            clean_shutdown=bool(data.get("clean_shutdown", False)),
            in_flight=dict(in_flight),
        )


def folder_key(watch_dir: Path) -> str:
    """Stable key for a folder (case-insensitive on Windows)."""
    return os.path.normcase(str(watch_dir))


class StateFile:
    """Reads and atomically rewrites ``watcher-state.json``; never raises."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._doc: dict[str, Any] = {"version": STATE_VERSION, "watch_dirs": {}}
        self._lock = threading.Lock()

    def load(self, watch_dir: Path) -> Optional[FolderState]:
        """Read the file; return this folder's entry, or None if there is none."""
        with self._lock:
            self._doc = {"version": STATE_VERSION, "watch_dirs": {}}
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                log_event(
                    logger,
                    logging.INFO,
                    "no watcher state file yet (first run)",
                    path=str(self.path),
                )
                return None
            except OSError as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "could not read watcher state file; ignoring it",
                    path=str(self.path),
                    error=describe_exception(exc),
                )
                return None
            try:
                doc = json.loads(raw)
                if not isinstance(doc, dict) or not isinstance(
                    doc.get("watch_dirs"), dict
                ):
                    raise ValueError("top level is not {'watch_dirs': {...}}")
                entry = doc["watch_dirs"].get(folder_key(watch_dir))
                folder = None if entry is None else FolderState.from_json(entry)
            except (ValueError, TypeError) as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "watcher state file is corrupt; ignoring it (it will be rewritten)",
                    path=str(self.path),
                    error=describe_exception(exc),
                    content_start=raw[:200],
                )
                return None
            self._doc = doc
            self._doc["version"] = STATE_VERSION
            log_event(
                logger,
                logging.INFO,
                "watcher state loaded",
                path=str(self.path),
                watch_dir=str(watch_dir),
                found=folder is not None,
                **(folder.to_json() if folder else {}),
            )
            return folder

    def save(self, watch_dir: Path, folder: FolderState) -> bool:
        """Write this folder's entry (keeping other folders'). Returns success."""
        with self._lock:
            now = time.time()
            folder.in_flight = {
                key: value
                for key, value in folder.in_flight.items()
                if now - float(value.get("since", now)) < IN_FLIGHT_MAX_AGE_S
            }
            self._doc.setdefault("watch_dirs", {})[
                folder_key(watch_dir)
            ] = folder.to_json()
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(self._doc, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "could not write watcher state file",
                    path=str(self.path),
                    error=describe_exception(exc),
                )
                return False
        log_event(
            logger,
            logging.DEBUG,
            "watcher state saved",
            path=str(self.path),
            seen_until=iso(folder.seen_until),
            clean_shutdown=folder.clean_shutdown,
            in_flight=len(folder.in_flight),
        )
        return True
