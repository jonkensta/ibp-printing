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


class Notifier:
    """Shows at most one message box at a time, each from a daemon thread."""

    def __init__(
        self,
        enabled: bool = True,
        show: Optional[Callable[[str, str], object]] = None,
    ) -> None:
        self.enabled = enabled
        self._custom = show is not None
        self._show: Callable[[str, str], object] = show or message_box
        self._busy = threading.Lock()

    @property
    def available(self) -> bool:
        """True when a box would actually appear (enabled, and Windows or a hook)."""
        return self.enabled and (sys.platform == "win32" or self._custom)

    def notify(self, text: str, title: str = TITLE) -> bool:
        """Pop a box without blocking the caller.

        Returns:
            True if a box was started; False if disabled, unavailable, or a
            box is already open (boxes never stack up).
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
        # pylint: disable-next=consider-using-with
        if not self._busy.acquire(blocking=False):
            log_event(
                logger,
                logging.WARNING,
                "notification skipped: a message box is already open",
                text=text,
            )
            return False

        def _run() -> None:
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
                )
            finally:
                self._busy.release()

        # Copy the context so the box's log lines keep the print attempt ID.
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run, args=(_run,), name="notify", daemon=True
        ).start()
        return True
