"""Non-blocking Windows message boxes to tell volunteers a label did not print."""

import contextvars
import logging
import sys
import threading
from typing import Callable, Optional

from ibp_printing.log import describe_exception, get_logger, log_event

logger = get_logger(__name__)

TITLE = "IBP Label Watcher"

MB_OK = 0x00000000
MB_ICONWARNING = 0x00000030
MB_ICONERROR = 0x00000010
MB_SYSTEMMODAL = 0x00001000
MB_SETFOREGROUND = 0x00010000
WARNING_FLAGS = MB_OK | MB_ICONWARNING | MB_SYSTEMMODAL | MB_SETFOREGROUND


def message_box(text: str, title: str = TITLE, flags: int = WARNING_FLAGS) -> int:
    """Show a blocking MessageBoxW; returns 0 without showing anything off Windows."""
    if sys.platform != "win32":
        return 0
    import ctypes  # pylint: disable=import-outside-toplevel

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    return int(user32.MessageBoxW(None, text, title, flags))


# At most this many messages wait while a box is open; older ones are dropped
# from the combined box (and logged) beyond that.
MAX_PENDING = 20


def combine(texts: list[str], dropped: int = 0) -> str:
    """One box's text for several messages that queued up while a box was open."""
    if len(texts) == 1 and not dropped:
        return texts[0]
    header = f"The label watcher has {len(texts) + dropped} more messages:"
    parts = [header]
    for number, text in enumerate(texts, 1):
        parts.append(f"--- {number} ---\n{text}")
    if dropped:
        parts.append(f"(and {dropped} older messages; see the watcher log)")
    return "\n\n".join(parts)


class Notifier:
    """Shows at most one message box at a time, each from a daemon thread.

    Messages that arrive while a box is open are queued and shown together in
    one combined box as soon as the open one is closed, so none is lost and
    boxes never stack up.
    """

    def __init__(
        self,
        enabled: bool = True,
        show: Optional[Callable[[str, str], object]] = None,
    ) -> None:
        self.enabled = enabled
        self._custom = show is not None
        self._show: Callable[[str, str], object] = show or message_box
        self._lock = threading.Lock()
        self._busy = False
        self._pending: list[str] = []
        self._dropped = 0

    @property
    def available(self) -> bool:
        """True when a box would actually appear (enabled, and Windows or a hook)."""
        return self.enabled and (sys.platform == "win32" or self._custom)

    @property
    def pending(self) -> int:
        """How many messages wait for the open box to close."""
        with self._lock:
            return len(self._pending)

    def notify(self, text: str, title: str = TITLE) -> bool:
        """Show a box without blocking the caller (or queue it behind the open one).

        Returns:
            True if the message was accepted (shown now or queued to be shown
            when the open box closes); False if boxes are disabled or
            unavailable on this platform.
        """
        if not self.available:
            log_event(
                logger,
                logging.DEBUG,
                "notification not shown (disabled or not Windows)",
                enabled=self.enabled,
                platform=sys.platform,
            )
            return False
        with self._lock:
            if self._busy:
                self._pending.append(text)
                if len(self._pending) > MAX_PENDING:
                    lost = self._pending.pop(0)
                    self._dropped += 1
                    log_event(
                        logger,
                        logging.WARNING,
                        "too many queued notifications; oldest left out of the box",
                        text=lost,
                    )
                log_event(
                    logger,
                    logging.INFO,
                    "notification queued: a message box is already open",
                    pending=len(self._pending),
                    text=text,
                )
                return True
            self._busy = True
        # Copy the context so the box's log lines keep the print attempt ID.
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run,
            args=(self._run, text, title),
            name="notify",
            daemon=True,
        ).start()
        return True

    def _run(self, text: str, title: str) -> None:
        while True:
            try:
                log_event(logger, logging.INFO, "message box shown", text=text)
                result = self._show(text, title)
                log_event(logger, logging.INFO, "message box closed", result=result)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                log_event(
                    logger,
                    logging.ERROR,
                    "message box failed",
                    error=describe_exception(exc),
                    text=text,
                )
            with self._lock:
                if not self._pending:
                    self._busy = False
                    return
                text = combine(self._pending, self._dropped)
                log_event(
                    logger,
                    logging.INFO,
                    "showing queued notifications",
                    count=len(self._pending),
                    dropped=self._dropped,
                )
                self._pending = []
                self._dropped = 0
