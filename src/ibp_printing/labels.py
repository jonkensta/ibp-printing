"""A shared record of recent labels, to warn before buying a duplicate.

When a label can't print, shippy and shippy-gui save it into
``<Downloads>/to-print/`` and the label watcher prints it later. Volunteers
then often enter the same shipment again and buy a second label. This module
keeps one per-user **label journal** that every app and the watcher write to:

* the apps call :func:`record_purchase` right after buying postage, and
  :func:`update_status` when the label printed, was queued, may have printed
  (``check_printer``) or was refunded;
* before buying, they call :func:`find_duplicates` with the recipient's
  :func:`recipient_key` and warn the volunteer about any hit;
* :func:`pending_labels` lists what is still waiting in ``to-print/`` (or needs
  a human in ``check-printer/``), for a UI to show who those labels are for;
* the watcher marks a queued label ``printed`` / ``check_printer`` when it
  files it (matched by the tracking code in the label's sidecar, see
  :func:`ibp_printing.paths.save_for_retry`).

Storage: ``labels.jsonl`` in the per-user state folder
(``%LOCALAPPDATA%\\ibp-printing`` on Windows, ``$XDG_STATE_HOME/ibp-printing``
elsewhere): append-only JSON lines, where the last line for a tracking code
wins. Every read and write holds an OS file lock (``labels.jsonl.lock``,
``msvcrt.locking`` / ``fcntl.flock``) with a timeout, so the apps and the
watcher can use it at the same time. A corrupt line is logged and skipped.
About once a day a write compacts the file to the latest record per label,
dropping labels not touched for :data:`RETENTION_DAYS` days (a queued or
check-printer label whose file still exists is kept).

**The journal never blocks printing.** No function here raises for a journal
problem (lock timeout, disk full, corrupt file): it is logged at ERROR and the
call degrades (:func:`find_duplicates` then returns ``[]``).
"""

import dataclasses
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast, Iterator, Mapping, Optional

from ibp_printing.log import describe_exception, get_logger, log_event
from ibp_printing.paths import (
    PARTIAL_SUFFIX,
    default_state_dir,
    read_sidecar,
    to_print_dir,
)

logger = get_logger(__name__)

__all__ = [
    "CHECK_PRINTER",
    "LabelRecord",
    "LabelStatus",
    "PRINTED",
    "PURCHASED",
    "QUEUED",
    "REFUNDED",
    "find_duplicates",
    "journal_path",
    "pending_labels",
    "recipient_key",
    "record_purchase",
    "set_journal_path",
    "update_status",
    "update_status_from_meta",
]

JOURNAL_FILENAME = "labels.jsonl"
LOCK_SUFFIX = ".lock"
# Records not updated for this long are dropped when the journal is compacted.
RETENTION_DAYS = 30
# The journal is compacted at most this often (and only when a write happens).
PRUNE_INTERVAL_S = 24 * 3600.0
# How long a call waits for another process's journal lock before giving up.
LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.02
# At most this many corrupt line numbers are listed in one log record.
_MAX_CORRUPT_LISTED = 20


class LabelStatus(StrEnum):
    """Where a label stands. Members are plain strings (``"queued"`` etc.)."""

    PURCHASED = "purchased"  # postage bought; not printed (yet)
    PRINTED = "printed"  # a printer accepted it
    QUEUED = "queued"  # waiting in to-print/ for the watcher
    CHECK_PRINTER = "check_printer"  # may have printed; needs a human
    REFUNDED = "refunded"  # postage refunded; never print it


PURCHASED = LabelStatus.PURCHASED.value
PRINTED = LabelStatus.PRINTED.value
QUEUED = LabelStatus.QUEUED.value
CHECK_PRINTER = LabelStatus.CHECK_PRINTER.value
REFUNDED = LabelStatus.REFUNDED.value
STATUSES = frozenset(status.value for status in LabelStatus)
# Statuses that stay relevant whatever their age (while their file exists).
_WAITING = frozenset({QUEUED, CHECK_PRINTER})


@dataclass
class LabelRecord:  # pylint: disable=too-many-instance-attributes
    """The latest known state of one label (one tracking code)."""

    tracking_code: str
    shipment_id: str
    recipient_key: str
    recipient_label: str
    app: str
    status: str
    created: float
    updated: float
    file: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        """The journal line's fields."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> "LabelRecord":
        """Parse one journal entry; raises ValueError if it is malformed."""
        if not isinstance(data, dict):
            raise ValueError("entry is not an object")
        tracking = data.get("tracking_code")
        if not isinstance(tracking, str) or not tracking:
            raise ValueError("tracking_code is missing")
        status = data.get("status")
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        created = data.get("created")
        updated = data.get("updated", created)
        if not _is_number(created) or not _is_number(updated):
            raise ValueError(f"created/updated not numbers: {created!r}, {updated!r}")
        file = data.get("file")
        if file is not None and not isinstance(file, str):
            raise ValueError(f"file is not a string: {file!r}")

        def text(name: str) -> str:
            value = data.get(name)
            return value if isinstance(value, str) else ""

        return cls(
            tracking_code=tracking,
            shipment_id=text("shipment_id"),
            recipient_key=text("recipient_key"),
            recipient_label=text("recipient_label"),
            app=text("app"),
            status=status,
            created=float(cast(float, created)),
            updated=float(cast(float, updated)),
            file=file or None,
        )

    def file_exists(self) -> bool:
        """True when ``file`` names an existing file (False for None)."""
        if not self.file:
            return False
        try:
            return Path(self.file).is_file()
        except OSError:
            return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------- recipients

_STREET_WORDS = {
    "street": "st",
    "str": "st",
    "avenue": "ave",
    "av": "ave",
    "aven": "ave",
    "road": "rd",
    "drive": "dr",
    "boulevard": "blvd",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "highway": "hwy",
    "parkway": "pkwy",
    "circle": "cir",
    "terrace": "ter",
    "trail": "trl",
    "square": "sq",
    "freeway": "fwy",
    "expressway": "expy",
    "route": "rte",
    "apartment": "apt",
    "unit": "apt",
    "suite": "apt",
    "ste": "apt",
    "#": "apt",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
}
_CITY_WORDS = {
    "saint": "st",
    "fort": "ft",
    "mount": "mt",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
}
_STATES = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "district of columbia": "dc",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
    "puerto rico": "pr",
}
_NON_WORD = re.compile(r"[^\w#]+|_+")
_HASH = re.compile(r"#")


def _words(text: str) -> list[str]:
    """Casefolded, accent-free words; punctuation (except ``#``) is a space."""
    folded = unicodedata.normalize("NFKD", text or "").casefold()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = _HASH.sub(" # ", folded)
    return [word for word in _NON_WORD.sub(" ", folded).split() if word]


def _join_po_box(words: list[str]) -> list[str]:
    """``P.O. Box`` / ``Post Office Box`` / ``POB`` -> ``po box``."""
    out: list[str] = []
    index = 0
    while index < len(words):
        rest = words[index:]
        if rest[:3] == ["p", "o", "box"]:
            out += ["po", "box"]
            index += 3
        elif rest[:3] == ["post", "office", "box"]:
            out += ["po", "box"]
            index += 3
        elif rest[:1] == ["pob"]:
            out += ["po", "box"]
            index += 1
        else:
            out.append(words[index])
            index += 1
    return out


def _normalize_street(street: str) -> str:
    words = [_STREET_WORDS.get(word, word) for word in _join_po_box(_words(street))]
    collapsed: list[str] = []
    for word in words:
        if word == "apt" and collapsed and collapsed[-1] == "apt":
            continue  # "Apt #5" -> "apt 5"
        collapsed.append(word)
    return " ".join(collapsed)


def _normalize_city(city: str) -> str:
    return " ".join(_CITY_WORDS.get(word, word) for word in _words(city))


def _normalize_state(state: str) -> str:
    joined = " ".join(_words(state))
    return _STATES.get(joined, joined)


def _normalize_zip(zip_code: str) -> str:
    return "".join(ch for ch in zip_code or "" if ch.isdigit())[:5]


def recipient_key(name: str, street: str, city: str, state: str, zip_code: str) -> str:
    """A normalized key for a recipient: equal for the same person and address.

    Case, accents, punctuation and spacing are ignored; common street words
    are abbreviated (``Street`` -> ``st``, ``Avenue`` -> ``ave``; ``Apartment``,
    ``Unit``, ``Suite`` and ``#`` -> ``apt``; ``P.O. Box`` -> ``po box``;
    ``North`` -> ``n``), state names become their two-letter code, and only
    the first five digits of the ZIP code count. The result is plain text,
    the same in every process and on every run.
    """
    return "|".join(
        (
            " ".join(_words(name)),
            _normalize_street(street),
            _normalize_city(city),
            _normalize_state(state),
            _normalize_zip(zip_code),
        )
    )


# ------------------------------------------------------------------ storage

_override_path: Optional[Path] = None  # pylint: disable=invalid-name
_thread_lock = threading.Lock()


def journal_path() -> Path:
    """The journal file (``<state dir>/labels.jsonl`` unless overridden)."""
    return _override_path or default_state_dir() / JOURNAL_FILENAME


def set_journal_path(path: Optional[Path]) -> None:
    """Use another journal file (tests, tools); ``None`` restores the default."""
    global _override_path  # pylint: disable=global-statement
    _override_path = Path(path) if path is not None else None


class JournalLockTimeout(OSError):
    """Another process held the journal lock for longer than the timeout."""


def _try_lock(fd: int) -> bool:
    # pylint: disable=import-outside-toplevel,import-error
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    # pylint: disable=import-outside-toplevel,import-error
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class _Locked:
    """Holds the journal's thread and file locks (a context manager)."""

    def __init__(self, path: Path, timeout_s: float) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self._fd: Optional[int] = None

    def __enter__(self) -> Path:
        deadline = time.monotonic() + self.timeout_s
        if not _thread_lock.acquire(timeout=self.timeout_s):
            raise JournalLockTimeout("label journal is busy in this process")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self.path.with_name(self.path.name + LOCK_SUFFIX)
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                while not _try_lock(fd):
                    if time.monotonic() >= deadline:
                        raise JournalLockTimeout(
                            f"another process held {lock_file} for more than "
                            f"{self.timeout_s:g} s"
                        )
                    time.sleep(_LOCK_POLL_S)
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        except BaseException:
            _thread_lock.release()
            raise
        return self.path

    def __exit__(self, *exc_info: Any) -> None:
        fd, self._fd = self._fd, None
        try:
            if fd is not None:
                try:
                    _unlock(fd)
                except OSError:
                    pass  # closing the descriptor releases it anyway
                os.close(fd)
        finally:
            _thread_lock.release()


def _locked() -> _Locked:
    return _Locked(journal_path(), LOCK_TIMEOUT_S)


@dataclass
class _Contents:
    """What one read of the journal found."""

    latest: dict[str, LabelRecord]
    lines: int = 0
    corrupt: int = 0
    pruned_at: Optional[float] = None


def _parse_lines(lines: Iterator[str], path: Path) -> _Contents:
    contents = _Contents(latest={})
    bad: list[int] = []
    for number, line in enumerate(lines, 1):
        contents.lines += 1
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict) and "_meta" in data:
                at = data.get("pruned_at")
                if isinstance(at, (int, float)) and not isinstance(at, bool):
                    contents.pruned_at = float(at)
                continue
            record = LabelRecord.from_json(data)
        except ValueError:
            bad.append(number)
            continue
        contents.latest[record.tracking_code] = record
    contents.corrupt = len(bad)
    if bad:
        log_event(
            logger,
            logging.WARNING,
            "label journal has corrupt lines; skipping them",
            path=str(path),
            count=len(bad),
            lines=bad[:_MAX_CORRUPT_LISTED],
        )
    return contents


def _read(path: Path) -> _Contents:
    """Read the whole journal (the caller holds the lock)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return _parse_lines(iter(handle), path)
    except FileNotFoundError:
        return _Contents(latest={})


def _append(path: Path, record: LabelRecord) -> None:
    """Append one record durably (the caller holds the lock)."""
    line = json.dumps(record.to_json(), separators=(",", ":")) + "\n"
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        prefix = b""
        if size:
            # A crash mid-write can leave a line without its newline; never
            # glue the next record onto it.
            handle.seek(size - 1)
            if handle.read(1) != b"\n":
                prefix = b"\n"
            handle.seek(0, os.SEEK_END)
        handle.write(prefix + line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _first_line(path: Path) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.readline()
    except FileNotFoundError:
        return None


def _prune_due(path: Path, now: float) -> bool:
    """Cheap check (first line only): has the journal not been compacted lately?"""
    line = _first_line(path)
    if not line:
        return False
    try:
        data = json.loads(line)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return True
    if "_meta" in data:
        # Compacted before: again once a day.
        stamp, wait = data.get("pruned_at"), PRUNE_INTERVAL_S
    else:
        # Never compacted: the first line is the oldest record.
        stamp = data.get("updated")
        wait = RETENTION_DAYS * 86400.0 + PRUNE_INTERVAL_S
    return not _is_number(stamp) or now - float(cast(float, stamp)) >= wait


def _keep(record: LabelRecord, now: float) -> bool:
    if now - record.updated < RETENTION_DAYS * 86400.0:
        return True
    return record.status in _WAITING and record.file_exists()


def _compact(path: Path, contents: _Contents, now: float) -> None:
    """Rewrite the journal: one line per kept label, oldest first."""
    kept = sorted(
        (record for record in contents.latest.values() if _keep(record, now)),
        key=lambda record: record.updated,
    )
    tmp = path.with_name(path.name + ".tmp")
    header = {"_meta": "labels journal", "pruned_at": now}
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(header) + "\n")
        for record in kept:
            handle.write(json.dumps(record.to_json(), separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    log_event(
        logger,
        logging.INFO,
        "label journal compacted",
        path=str(path),
        lines_before=contents.lines,
        kept=len(kept),
        dropped=len(contents.latest) - len(kept),
        corrupt_dropped=contents.corrupt,
    )


def _maybe_prune(path: Path, now: float, contents: Optional[_Contents]) -> None:
    """Compact after a write when it is due (or corrupt lines were seen)."""
    try:
        if contents is not None and contents.corrupt:
            due = True
        else:
            due = _prune_due(path, now)
        if not due:
            return
        if contents is None:
            contents = _read(path)
        _compact(path, contents, now)
    except OSError as exc:
        # The append already happened; a failed compaction only costs space.
        log_event(
            logger,
            logging.WARNING,
            "could not compact the label journal; will try again later",
            path=str(path),
            error=describe_exception(exc),
        )


def _journal_failed(action: str, exc: BaseException, **data: Any) -> None:
    log_event(
        logger,
        logging.ERROR,
        f"label journal: {action} failed (printing is not affected)",
        path=str(journal_path()),
        error=describe_exception(exc),
        **data,
    )


def _now() -> float:
    return time.time()


# --------------------------------------------------------------- public API


def record_purchase(
    *,
    recipient_key: str,  # pylint: disable=redefined-outer-name
    recipient_label: str,
    tracking_code: str,
    shipment_id: str,
    app: str,
) -> LabelRecord:
    """Record a label just bought (status ``purchased``) and return it.

    Never raises for a journal problem: it is logged and the record is still
    returned.
    """
    now = _now()
    record = LabelRecord(
        tracking_code=tracking_code,
        shipment_id=shipment_id,
        recipient_key=recipient_key,
        recipient_label=recipient_label,
        app=app,
        status=PURCHASED,
        created=now,
        updated=now,
    )
    if not tracking_code:
        log_event(
            logger,
            logging.ERROR,
            "label journal: purchase without a tracking code not recorded",
            shipment_id=shipment_id,
            app=app,
        )
        return record
    try:
        with _locked() as path:
            _append(path, record)
            _maybe_prune(path, now, None)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _journal_failed("recording a purchase", exc, tracking_code=tracking_code)
        return record
    log_event(
        logger,
        logging.INFO,
        "label purchase recorded",
        tracking_code=tracking_code,
        shipment_id=shipment_id,
        recipient=recipient_label,
        app=app,
    )
    return record


def _record_from_meta(
    meta: Mapping[str, Any], status: str, file: Optional[str], now: float
) -> LabelRecord:
    def text(name: str) -> str:
        value = meta.get(name)
        return str(value) if value is not None else ""

    created = meta.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        created = now
    return LabelRecord(
        tracking_code=text("tracking_code"),
        shipment_id=text("shipment_id"),
        recipient_key=text("recipient_key"),
        recipient_label=text("recipient_label"),
        app=text("app"),
        status=status,
        created=float(created),
        updated=now,
        file=file,
    )


def _update(  # pylint: disable=too-many-return-statements
    tracking_code: str,
    status: str,
    file: Optional[str],
    meta: Optional[Mapping[str, Any]],
) -> None:
    if status not in STATUSES:
        log_event(
            logger,
            logging.ERROR,
            "label journal: unknown status ignored",
            tracking_code=tracking_code,
            status=status,
        )
        return
    if not tracking_code:
        log_event(
            logger,
            logging.ERROR,
            "label journal: status update without a tracking code ignored",
            status=status,
            file=file,
        )
        return
    now = _now()
    try:
        with _locked() as path:
            contents = _read(path)
            current = contents.latest.get(tracking_code)
            if current is None:
                new = _record_from_meta(
                    {**(meta or {}), "tracking_code": tracking_code},
                    status,
                    file,
                    now,
                )
                previous = None
            else:
                if current.status == status and (file is None or file == current.file):
                    return  # nothing new; keep the journal small
                new = dataclasses.replace(
                    current,
                    status=status,
                    updated=now,
                    file=file if file is not None else current.file,
                )
                if meta:  # fill in what the earlier record did not know
                    fill = _record_from_meta(meta, status, file, now)
                    for name in ("shipment_id", "recipient_key", "recipient_label"):
                        if not getattr(new, name):
                            setattr(new, name, getattr(fill, name))
                    if not new.app:
                        new.app = fill.app
                previous = current.status
            _append(path, new)
            contents.latest[tracking_code] = new
            _maybe_prune(path, now, contents)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _journal_failed(
            "status update", exc, tracking_code=tracking_code, status=status
        )
        return
    log_event(
        logger,
        logging.INFO,
        "label status recorded",
        tracking_code=tracking_code,
        status=status,
        previous=previous,
        file=new.file,
        recipient=new.recipient_label,
    )


def update_status(
    tracking_code: str, status: str, *, file: Optional[str] = None
) -> None:
    """Record a label's new status (and, if given, the file that holds it).

    ``file`` None keeps the file already recorded. A label with no record yet
    gets a minimal one. An unchanged status and file writes nothing. Never
    raises for a journal problem or an unknown status (both are logged).
    """
    _update(tracking_code, str(status), file, None)


def update_status_from_meta(
    meta: Mapping[str, Any], status: str, *, file: Optional[str] = None
) -> None:
    """Like :func:`update_status` for a label described by sidecar metadata.

    Used by :func:`~ibp_printing.paths.save_for_retry` and the watcher: when
    the journal has no record of ``meta["tracking_code"]`` (its purchase was
    never recorded), the record is created from ``meta``.
    """
    tracking = meta.get("tracking_code")
    _update(str(tracking) if tracking else "", str(status), file, meta)


def _read_all() -> dict[str, LabelRecord]:
    """Every label's latest record. Raises on journal problems."""
    with _locked() as path:
        return _read(path).latest


def _same_file(first: Optional[str], second: Optional[str]) -> bool:
    if not first or not second:
        return False
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second)
    )


_STAMPED = re.compile(r"^\d{8}-\d{6}_(?P<name>.+?)(?:_\d+)?$")


def _queued_files() -> list[Path]:
    folder = to_print_dir()
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        log_event(
            logger,
            logging.WARNING,
            "could not list the to-print folder",
            folder=str(folder),
            error=describe_exception(exc),
        )
        return []
    return [entry for entry in entries if _is_queued_png(entry)]


def _is_queued_png(entry: Path) -> bool:
    name = entry.name
    if not name.lower().endswith(".png") or name.startswith("."):
        return False
    if name.endswith(PARTIAL_SUFFIX):
        return False
    try:
        return entry.is_file()
    except OSError:
        return False


def _orphan_record(
    path: Path, latest: Mapping[str, LabelRecord], now: float
) -> LabelRecord:
    """A record for a PNG in to-print/ that the journal does not point at."""
    meta = read_sidecar(path)
    tracking = str(meta.get("tracking_code") or "") if meta else ""
    known = latest.get(tracking) if tracking else None
    if known is not None:
        status = REFUNDED if known.status == REFUNDED else QUEUED
        if status == REFUNDED:
            log_event(
                logger,
                logging.WARNING,
                "a REFUNDED label is waiting in to-print; delete it, its postage "
                "is not valid",
                file=str(path),
                tracking_code=tracking,
            )
        return dataclasses.replace(known, status=status, file=str(path))
    if meta and tracking:
        return _record_from_meta(meta, QUEUED, str(path), now)
    try:
        created = path.stat().st_mtime
    except OSError:
        created = now
    match = _STAMPED.match(path.stem)
    return LabelRecord(
        tracking_code=match.group("name") if match else path.stem,
        shipment_id="",
        recipient_key="",
        recipient_label=path.name,
        app="",
        status=QUEUED,
        created=created,
        updated=created,
        file=str(path),
    )


def _pending(latest: Mapping[str, LabelRecord]) -> list[LabelRecord]:
    now = _now()
    found = [
        record
        for record in latest.values()
        if record.status in _WAITING and record.file_exists()
    ]
    for path in _queued_files():
        if any(_same_file(str(path), record.file) for record in found):
            continue
        orphan = _orphan_record(path, latest, now)
        # The journal's record of that label points elsewhere: show the file here.
        found = [r for r in found if r.tracking_code != orphan.tracking_code]
        found.append(orphan)
    return sorted(found, key=lambda record: (record.created, record.tracking_code))


def find_duplicates(
    recipient_key: str,  # pylint: disable=redefined-outer-name
    *,
    within_hours: float = 12,
) -> list[LabelRecord]:
    """Labels for this recipient that make buying another one suspicious.

    Returns, newest first:

    * ``queued`` and ``check_printer`` labels of any age, if their file still
      exists (or they have no file), including PNGs waiting in ``to-print/``
      the journal does not know about (from their sidecar);
    * ``purchased`` and ``printed`` labels updated within ``within_hours``.

    Refunded labels never count. On a journal problem this logs and returns
    ``[]``: a duplicate check must never stop a volunteer from shipping.
    """
    if not recipient_key:
        return []
    try:
        latest = _read_all()
        now = _now()
        window = within_hours * 3600.0
        hits: dict[str, LabelRecord] = {}
        for record in latest.values():
            if record.recipient_key != recipient_key:
                continue
            if record.status in _WAITING:
                if record.file is None or record.file_exists():
                    hits[record.tracking_code] = record
            elif record.status in (PURCHASED, PRINTED):
                if now - record.updated <= window:
                    hits[record.tracking_code] = record
        for record in _pending(latest):
            if record.recipient_key == recipient_key and record.status in _WAITING:
                hits[record.tracking_code] = record
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _journal_failed("duplicate check", exc)
        log_event(
            logger,
            logging.WARNING,
            "duplicate-purchase check unavailable; reporting no duplicates",
        )
        return []
    found = sorted(
        hits.values(),
        key=lambda record: (record.updated, record.created),
        reverse=True,
    )
    log_event(
        logger,
        logging.WARNING if found else logging.DEBUG,
        "duplicate-purchase check",
        recipient_key=recipient_key,
        within_hours=within_hours,
        found=[(record.tracking_code, record.status) for record in found],
    )
    return found


def pending_labels() -> list[LabelRecord]:
    """Labels waiting for the printer or for a human, oldest first.

    ``queued`` and ``check_printer`` labels whose file still exists, plus
    every PNG in ``to-print/`` the journal does not point at: described by its
    journal record (matched by the sidecar's tracking code), else by its
    sidecar, else by a minimal record whose ``tracking_code`` is guessed from
    the file name and whose ``recipient_label`` is the file name. (A refunded
    label left in ``to-print/`` is listed with status ``refunded``.) On a
    journal problem this logs and lists only what is in ``to-print/``.
    """
    try:
        latest = _read_all()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _journal_failed("listing pending labels", exc)
        latest = {}
    try:
        return _pending(latest)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _journal_failed("listing pending labels", exc)
        return []
