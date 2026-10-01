"""Watcher settings: dataclass defaults, a TOML file, then command-line overrides."""

import dataclasses
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CONFIG_FILENAME = "watcher.toml"

DEFAULT_GLOBS = ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.pdf")


@dataclass
class WatcherConfig:  # pylint: disable=too-many-instance-attributes
    """Every tunable of the label watcher, with production defaults."""

    # Folder to watch; None means the user's real Downloads folder.
    watch_dir: Optional[Path] = None
    # Log directory; None means ibp_printing.default_log_dir().
    log_dir: Optional[Path] = None
    # Case-insensitive filename patterns that may be labels.
    globs: list[str] = field(default_factory=lambda: list(DEFAULT_GLOBS))
    # Long side / short side of a 4x6 label is 1.5.
    aspect_min: float = 1.4
    aspect_max: float = 1.6
    min_short_side_px: int = 400
    # A label that printed is not printed again from another copy (a manual
    # download of a label the app saved to to-print/, a "label (1).png") for
    # this many hours after it printed. Labels whose fate is uncertain are never
    # re-sent automatically. Name a file REPRINT... to print it anyway.
    duplicate_window_hours: float = 24.0
    # How often to retry labels waiting in <watch_dir>/to-print/ (and re-check
    # downloads that were temporarily locked).
    retry_seconds: float = 60.0
    # How long to follow the spooled job before giving up.
    track_timeout_s: float = 60.0
    notify_on_failure: bool = True
    heartbeat_minutes: float = 15.0
    # A download is complete once its size is unchanged this long.
    stable_seconds: float = 1.0
    stable_timeout_s: float = 60.0
    pdf_dpi: int = 300
    process_existing: bool = False
    dry_run: bool = False

    def to_log(self) -> dict[str, Any]:
        """Flatten for structured logging."""
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in dataclasses.asdict(self).items()
        }


_PATH_KEYS = {"watch_dir", "log_dir"}
# Keys older versions understood; reported with a specific warning, then ignored.
_RETIRED_KEYS = {
    "dedupe_seconds": (
        "dedupe_seconds is no longer used: duplicates are now remembered across "
        "restarts for duplicate_window_hours (default 24); ignored"
    ),
}


def default_config_path() -> Path:
    """``%LOCALAPPDATA%\\ibp-printing\\watcher.toml`` (XDG config dir elsewhere)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ibp-printing" / CONFIG_FILENAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ibp-printing" / CONFIG_FILENAME


def _coerce(key: str, value: Any, default: Any) -> Any:
    """Convert a TOML value to the field's type, or raise ValueError."""
    if key in _PATH_KEYS:
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string path")
        return Path(os.path.expandvars(os.path.expanduser(value))) if value else None
    if key == "globs":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("globs must be a list of strings")
        return list(value)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        return float(value)
    return value


def config_from_mapping(data: dict[str, Any]) -> tuple[WatcherConfig, list[str]]:
    """Build a config from parsed TOML, returning it with any warnings.

    Unknown keys and badly typed values are reported and otherwise ignored, so
    a typo never stops the watcher from starting.
    """
    config = WatcherConfig()
    warnings: list[str] = []
    known = {f.name for f in dataclasses.fields(WatcherConfig)}
    for key, value in data.items():
        if key in _RETIRED_KEYS:
            warnings.append(_RETIRED_KEYS[key])
            continue
        if key not in known:
            warnings.append(f"unknown config key {key!r} ignored")
            continue
        try:
            setattr(config, key, _coerce(key, value, getattr(config, key)))
        except ValueError as exc:
            warnings.append(f"bad value for {key!r} ({value!r}): {exc}; default kept")
    if config.aspect_min > config.aspect_max:
        warnings.append(
            f"aspect_min {config.aspect_min} > aspect_max {config.aspect_max}; "
            "defaults restored"
        )
        config.aspect_min, config.aspect_max = 1.4, 1.6
    return config, warnings


def load_config(path: Optional[Path] = None) -> tuple[WatcherConfig, Path, list[str]]:
    """Read the TOML config file (missing file means all defaults).

    Returns:
        ``(config, path_used, warnings)``. Warnings are returned rather than
        logged because logging is configured from the config itself.
    """
    path = path or default_config_path()
    if not path.exists():
        return WatcherConfig(), path, []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return WatcherConfig(), path, [f"could not read {path}: {exc!r}; defaults"]
    config, warnings = config_from_mapping(data)
    return config, path, warnings
