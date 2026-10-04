"""The direct-USB kill switch.

Direct USB printing is on by default. It is turned off, restoring the old
queue-only behaviour exactly (no direct discovery, no direct printing), by

* the environment variable ``IBP_PRINTING_DIRECT=0`` (also ``false``, ``no``,
  ``off``; ``1``/``true``/``yes``/``on`` force it on), or
* ``ibp_printing.set_direct_enabled(False)`` (``None`` returns to the
  environment / default), or
* ``ibp-print-diag --no-direct``.

The setting is read on every discovery and print, so it can be flipped at
run time. :func:`log_direct_mode` records which mode is active and why; it
runs from ``configure_logging`` and when the backend is created.
"""

import logging
import os
import threading
from typing import Optional

from ibp_printing.log import add_configure_hook, get_logger, log_event

logger = get_logger(__name__)

ENV_VAR = "IBP_PRINTING_DIRECT"
_OFF = frozenset({"0", "false", "no", "off", "disable", "disabled"})
_ON = frozenset({"1", "true", "yes", "on", "enable", "enabled"})

_LOCK = threading.Lock()
_OVERRIDE: Optional[bool] = None


def direct_mode() -> tuple[bool, str]:
    """``(enabled, why)``: whether direct USB printing is on and what decided it."""
    with _LOCK:
        override = _OVERRIDE
    if override is not None:
        return override, f"set_direct_enabled({override})"
    raw = os.environ.get(ENV_VAR)
    if raw is None or not raw.strip():
        return True, "default (on)"
    value = raw.strip().lower()
    if value in _OFF:
        return False, f"{ENV_VAR}={raw}"
    if value in _ON:
        return True, f"{ENV_VAR}={raw}"
    return True, f"{ENV_VAR}={raw!r} not understood; default (on)"


def direct_enabled() -> bool:
    """True when direct USB discovery and printing are active."""
    return direct_mode()[0]


def set_direct_enabled(enabled: Optional[bool]) -> None:
    """Force direct USB printing on/off; ``None`` goes back to the environment."""
    global _OVERRIDE  # pylint: disable=global-statement
    with _LOCK:
        _OVERRIDE = enabled
    log_direct_mode("set_direct_enabled")


def log_direct_mode(context: str) -> bool:
    """Log the active mode (and why) at INFO; returns whether it is enabled."""
    enabled, why = direct_mode()
    log_event(
        logger,
        logging.INFO,
        "direct USB printing "
        + ("ENABLED" if enabled else "DISABLED")
        + (" (queue-only, the old behaviour)" if not enabled else ""),
        direct_enabled=enabled,
        decided_by=why,
        context=context,
        env_var=ENV_VAR,
    )
    return enabled


# configure_logging() records the active mode in every app's log.
add_configure_hook(log_direct_mode)
