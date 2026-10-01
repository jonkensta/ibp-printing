"""Make sure only one watcher runs per user (two would print every label twice)."""

import logging
import os
import sys
from pathlib import Path
from typing import IO, Optional

from ibp_printing.log import describe_exception, get_logger, log_event
from ibp_printing.watcher.state import LOCK_FILENAME

logger = get_logger(__name__)

__all__ = ["LOCK_FILENAME", "SingleInstance"]


class SingleInstance:
    """An exclusive, non-blocking lock on a file, held for the process lifetime.

    The OS drops the lock automatically if the process dies, so a crash never
    leaves a stale lock behind.
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._handle: Optional[IO[str]] = None

    def acquire(self) -> bool:
        """Try to take the lock. Returns False if another instance holds it."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # pylint: disable-next=consider-using-with
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            self._lock(handle)
        except OSError as exc:
            try:
                handle.seek(0)
                holder = handle.read().strip()
            except OSError:
                holder = ""  # Windows refuses reads of the locked byte range.
            handle.close()
            log_event(
                logger,
                logging.ERROR,
                "another watcher already holds the lock",
                lock=str(self.lock_path),
                holder=holder or "?",
                error=describe_exception(exc),
            )
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
        except OSError as exc:
            # Purely informational; the lock itself is what matters.
            log_event(
                logger,
                logging.DEBUG,
                "could not write pid to lock file",
                error=describe_exception(exc),
            )
        self._handle = handle
        log_event(
            logger, logging.INFO, "instance lock acquired", lock=str(self.lock_path)
        )
        return True

    @staticmethod
    def _lock(handle: IO[str]) -> None:
        # pylint: disable=import-outside-toplevel,import-error
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        """Drop the lock (also happens automatically at process exit)."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            # pylint: disable=import-outside-toplevel,import-error
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            log_event(
                logger,
                logging.DEBUG,
                "unlock failed",
                error=describe_exception(exc),
            )
        finally:
            handle.close()
        log_event(
            logger, logging.INFO, "instance lock released", lock=str(self.lock_path)
        )
