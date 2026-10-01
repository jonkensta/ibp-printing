"""Per-user watcher state that survives restarts: the lock and a small JSON file.

``watcher-state.json`` (schema version 2) holds two things:

* ``watch_dirs`` - per watched folder: ``seen_until`` (every file in the folder
  that arrived at or before this wall-clock time has been decided; files seen
  but not yet handled hold it back, so a crash or shutdown never loses them)
  and ``clean_shutdown`` (False while running, so the next start can tell a
  crash).
* ``reservations`` - content reservations, keyed by the label's SHA-256. A
  reservation is written *before* a label is handed to a printer and updated
  afterwards, so neither a second copy nor a restart can print it again:

  - ``filing``    - handed to a printer; the file has not been moved into its
    result folder yet (``dest``), or the outcome is not known yet (``dest`` is
    null: the watcher died mid-print). The watcher retries the *move*, never
    the print.
  - ``printed``   - a printer accepted it. Other copies are not printed for
    ``duplicate_window_hours`` after it finished.
  - ``uncertain`` - it may or may not have printed. Never expires: other
    copies always go to ``check-printer/``.

  A file whose name starts with ``REPRINT`` bypasses ``printed`` and
  ``uncertain`` reservations (a deliberate reprint).

Version 1 files (``in_flight`` per folder) are migrated: each in-flight entry
becomes a ``filing`` reservation with an unknown outcome. Unknown fields are
logged and ignored. A missing, unreadable or corrupt file is logged and treated
as "no history"; it never stops the watcher.
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
STATE_VERSION = 2

FILING = "filing"
PRINTED = "printed"
UNCERTAIN = "uncertain"
STATUSES = (FILING, PRINTED, UNCERTAIN)
# Reason recorded when the watcher stopped while a label was being printed.
INTERRUPTED = "interrupted"

_FOLDER_FIELDS = {
    "seen_until",
    "seen_until_local",
    "updated",
    "updated_local",
    "clean_shutdown",
}


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


def _number(data: dict[str, Any], name: str, default: Any = None) -> Optional[float]:
    value = data.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is not a number: {value!r}")
    return float(value)


def _text(data: dict[str, Any], name: str) -> Optional[str]:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a string: {value!r}")
    return value


@dataclass
class FolderState:
    """What the watcher remembers about one watched folder."""

    seen_until: float
    updated: float
    clean_shutdown: bool = False

    def to_json(self) -> dict[str, Any]:
        """The on-disk form."""
        return {
            "seen_until": self.seen_until,
            "seen_until_local": iso(self.seen_until),
            "updated": self.updated,
            "updated_local": iso(self.updated),
            "clean_shutdown": self.clean_shutdown,
        }

    @classmethod
    def from_json(cls, data: Any) -> "FolderState":
        """Parse one folder entry; raises ValueError if it is malformed."""
        if not isinstance(data, dict):
            raise ValueError("folder entry is not an object")
        seen_until = _number(data, "seen_until")
        if seen_until is None:
            raise ValueError("seen_until is missing")
        updated = _number(data, "updated", seen_until)
        return cls(
            seen_until=seen_until,
            updated=updated if updated is not None else seen_until,
            clean_shutdown=bool(data.get("clean_shutdown", False)),
        )


@dataclass
class Reservation:  # pylint: disable=too-many-instance-attributes
    """One label's content, reserved so it is never printed twice by accident."""

    status: str
    first_submitted: float
    updated: float
    # The file that was handed to the printer (the one a ``filing`` move is for).
    file: str = ""
    # Where this content was most recently seen (the submitted file or a copy).
    last_seen_path: str = ""
    # For ``filing``: the result folder the file must be moved to (None while
    # the outcome is unknown) and the status it gets once it is there.
    dest: Optional[str] = None
    final: Optional[str] = None
    # When it was filed as printed: the duplicate window starts here.
    completed: Optional[float] = None
    reason: str = ""
    printer: Optional[str] = None
    copies_seen: int = 0
    move_failures: int = 0

    @property
    def effective(self) -> str:
        """``printed`` or ``uncertain``: what this content counts as right now."""
        if self.status == FILING:
            return self.final or UNCERTAIN
        return self.status

    def to_json(self) -> dict[str, Any]:
        """The on-disk form."""
        return {
            "status": self.status,
            "first_submitted": self.first_submitted,
            "first_submitted_local": iso(self.first_submitted),
            "updated": self.updated,
            "file": self.file,
            "last_seen_path": self.last_seen_path,
            "dest": self.dest,
            "final": self.final,
            "completed": self.completed,
            "completed_local": iso(self.completed),
            "reason": self.reason,
            "printer": self.printer,
            "copies_seen": self.copies_seen,
            "move_failures": self.move_failures,
        }

    def to_log(self, digest: str) -> dict[str, Any]:
        """Short form for log lines."""
        return {
            "sha256": digest,
            "status": self.status,
            "dest": self.dest,
            "final": self.final,
            "file": self.file,
            "last_seen_path": self.last_seen_path,
            "first_submitted": iso(self.first_submitted),
            "completed": iso(self.completed),
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: Any) -> "Reservation":
        """Parse one reservation; raises ValueError if it is malformed."""
        if not isinstance(data, dict):
            raise ValueError("reservation is not an object")
        status = data.get("status")
        if status not in STATUSES:
            raise ValueError(f"unknown reservation status {status!r}")
        first = _number(data, "first_submitted")
        if first is None:
            raise ValueError("first_submitted is missing")
        final = _text(data, "final")
        if final not in (None, PRINTED, UNCERTAIN):
            raise ValueError(f"unknown final status {final!r}")
        return cls(
            status=status,
            first_submitted=first,
            updated=_number(data, "updated", first) or first,
            file=_text(data, "file") or "",
            last_seen_path=_text(data, "last_seen_path") or "",
            dest=_text(data, "dest"),
            final=final,
            completed=_number(data, "completed"),
            reason=_text(data, "reason") or "",
            printer=_text(data, "printer"),
            copies_seen=int(_number(data, "copies_seen", 0) or 0),
            move_failures=int(_number(data, "move_failures", 0) or 0),
        )


@dataclass
class LoadedState:
    """What :meth:`StateFile.load` found."""

    folder: Optional[FolderState] = None
    reservations: dict[str, Reservation] = field(default_factory=dict)
    # Human-readable notes about migration and ignored data (already logged).
    notes: list[str] = field(default_factory=list)


def folder_key(watch_dir: Path) -> str:
    """Stable key for a folder (case-insensitive on Windows)."""
    return os.path.normcase(str(watch_dir))


def _migrate_v1_in_flight(
    entry: dict[str, Any], reservations: dict[str, Reservation], notes: list[str]
) -> None:
    """Turn a version-1 folder's ``in_flight`` map into ``filing`` reservations."""
    in_flight = entry.get("in_flight")
    if not in_flight:
        return
    if not isinstance(in_flight, dict):
        notes.append("version 1 in_flight is not an object; ignored")
        return
    for digest, info in in_flight.items():
        if not isinstance(digest, str) or not isinstance(info, dict):
            notes.append(f"version 1 in_flight entry {digest!r} malformed; ignored")
            continue
        since = info.get("since")
        if isinstance(since, bool) or not isinstance(since, (int, float)):
            since = time.time()
        file = info.get("file") if isinstance(info.get("file"), str) else ""
        reservations.setdefault(
            digest,
            Reservation(
                status=FILING,
                first_submitted=float(since),
                updated=float(since),
                file=file or "",
                last_seen_path=file or "",
                reason="migrated from version 1 in_flight",
            ),
        )
        notes.append(f"migrated version 1 in_flight entry {digest[:12]} ({file})")


def _parse_reservations(raw: Any, notes: list[str]) -> dict[str, Reservation]:
    reservations: dict[str, Reservation] = {}
    if raw is None:
        return reservations
    if not isinstance(raw, dict):
        notes.append("reservations is not an object; ignored")
        return reservations
    for digest, data in raw.items():
        try:
            reservations[str(digest)] = Reservation.from_json(data)
        except (ValueError, TypeError) as exc:
            # One bad entry must not cost the others their protection.
            notes.append(f"reservation {str(digest)[:12]} ignored: {exc}")
    return reservations


class StateFile:
    """Reads and atomically rewrites ``watcher-state.json``; never raises."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._doc: dict[str, Any] = {"version": STATE_VERSION, "watch_dirs": {}}
        self._lock = threading.Lock()

    def load(self, watch_dir: Path) -> LoadedState:
        """Read the file: this folder's entry and every reservation."""
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
                return LoadedState()
            except OSError as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "could not read watcher state file; ignoring it",
                    path=str(self.path),
                    error=describe_exception(exc),
                )
                return LoadedState()
            try:
                loaded = self._parse(raw, watch_dir)
            except (ValueError, TypeError) as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "watcher state file is corrupt; ignoring it (it will be rewritten)",
                    path=str(self.path),
                    error=describe_exception(exc),
                    content_start=raw[:200],
                )
                self._doc = {"version": STATE_VERSION, "watch_dirs": {}}
                return LoadedState()
        for note in loaded.notes:
            log_event(logger, logging.WARNING, "watcher state file: " + note)
        log_event(
            logger,
            logging.INFO,
            "watcher state loaded",
            path=str(self.path),
            watch_dir=str(watch_dir),
            found_folder=loaded.folder is not None,
            reservations=len(loaded.reservations),
            **(loaded.folder.to_json() if loaded.folder else {}),
        )
        return loaded

    def _parse(self, raw: str, watch_dir: Path) -> LoadedState:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or not isinstance(doc.get("watch_dirs"), dict):
            raise ValueError("top level is not {'watch_dirs': {...}}")
        notes: list[str] = []
        version = doc.get("version", 1)
        if version != STATE_VERSION:
            if isinstance(version, int) and version < STATE_VERSION:
                notes.append(f"migrating from schema version {version}")
            else:
                notes.append(
                    f"schema version {version!r} is newer than this watcher's "
                    f"({STATE_VERSION}); reading the fields it understands"
                )
        unknown = sorted(set(doc) - {"version", "watch_dirs", "reservations"})
        if unknown:
            notes.append(f"unknown top-level fields ignored: {unknown}")
        reservations = _parse_reservations(doc.get("reservations"), notes)
        # Any folder's version-1 in-flight labels matter, whatever folder we watch.
        for key, entry in doc["watch_dirs"].items():
            if not isinstance(entry, dict):
                continue
            _migrate_v1_in_flight(entry, reservations, notes)
            entry.pop("in_flight", None)
            extra = sorted(set(entry) - _FOLDER_FIELDS)
            if extra:
                notes.append(f"unknown fields for folder {key} ignored: {extra}")
        entry = doc["watch_dirs"].get(folder_key(watch_dir))
        folder = None if entry is None else FolderState.from_json(entry)
        doc["version"] = STATE_VERSION
        doc["reservations"] = {
            digest: reservation.to_json()
            for digest, reservation in reservations.items()
        }
        self._doc = doc
        return LoadedState(folder=folder, reservations=reservations, notes=notes)

    def save(
        self,
        watch_dir: Path,
        folder: Optional[FolderState],
        reservations: dict[str, Reservation],
    ) -> bool:
        """Write this folder's entry (if given; other folders' are kept) and
        all reservations. Returns success; never raises."""
        with self._lock:
            if folder is not None:
                self._doc.setdefault("watch_dirs", {})[
                    folder_key(watch_dir)
                ] = folder.to_json()
            self._doc.setdefault("watch_dirs", {})
            self._doc["version"] = STATE_VERSION
            self._doc["reservations"] = {
                digest: reservation.to_json()
                for digest, reservation in reservations.items()
            }
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(self._doc, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                log_event(
                    logger,
                    logging.ERROR,
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
            seen_until=iso(folder.seen_until) if folder else "(unchanged)",
            clean_shutdown=folder.clean_shutdown if folder else None,
            reservations=len(reservations),
        )
        return True
