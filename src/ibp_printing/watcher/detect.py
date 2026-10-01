"""Decide whether a downloaded file is a 4x6 shipping label, and load it."""

import fnmatch
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PIL import Image

from ibp_printing.log import describe_exception, get_logger, log_event

logger = get_logger(__name__)

# Browsers write these while downloading, then rename to the final name.
TEMP_SUFFIXES = frozenset(
    {".crdownload", ".tmp", ".part", ".partial", ".download", ".opdownload", ".!ut"}
)
# Raw printer languages: we cannot send these through the GDI image path.
RAW_PRINTER_SUFFIXES = frozenset({".zpl", ".epl", ".epl2"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp"})
PDF_SUFFIXES = frozenset({".pdf"})


def is_temp_name(path: Path) -> bool:
    """True for in-progress browser downloads and hidden/lock files."""
    name = path.name
    if name.startswith((".", "~$")):
        return True
    return path.suffix.lower() in TEMP_SUFFIXES


def matches_globs(path: Path, globs: Iterable[str]) -> Optional[str]:
    """Return the first (case-insensitive) glob matching the file name."""
    name = path.name.lower()
    for pattern in globs:
        if fnmatch.fnmatchcase(name, pattern.lower()):
            return pattern
    return None


@dataclass
class Stability:
    """Outcome of waiting for a download to finish."""

    ok: bool
    size: int = -1
    waited_s: float = 0.0
    checks: int = 0
    reason: str = ""


def wait_until_stable(  # pylint: disable=too-many-locals
    path: Path,
    *,
    stable_s: float = 1.0,
    timeout_s: float = 60.0,
    interval_s: float = 0.25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Stability:
    """Wait until ``path`` keeps the same size for ``stable_s`` and can be opened.

    Browsers sometimes keep writing (or keep a lock) briefly after the final
    rename, and antivirus scanners open new files too.
    """
    started = clock()
    last_size: Optional[int] = None
    same_since = started
    checks = 0
    last_problem = ""
    while True:
        checks += 1
        now = clock()
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return Stability(False, -1, now - started, checks, "file disappeared")
        except OSError as exc:
            size = -1
            last_problem = f"stat failed: {exc!r}"

        if size != last_size:
            if last_size is not None:
                log_event(
                    logger,
                    logging.DEBUG,
                    "file still changing",
                    file=path.name,
                    size=size,
                    previous=last_size,
                    t=round(now - started, 2),
                )
            last_size = size
            same_since = now
        elif size > 0 and now - same_since >= stable_s:
            try:
                with path.open("rb") as handle:
                    handle.read(1)
            except OSError as exc:
                last_problem = f"not openable: {exc!r}"
            else:
                return Stability(True, size, now - started, checks)

        if now - started >= timeout_s:
            reason = last_problem or (
                "file is empty" if last_size == 0 else "size kept changing"
            )
            return Stability(False, last_size or -1, now - started, checks, reason)
        sleep(interval_s)


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


class UnsupportedFormat(Exception):
    """The file cannot be turned into an image we can print."""


@dataclass
class LoadedLabel:
    """A decoded image plus facts about how it was decoded."""

    image: Image.Image
    source_format: str
    info: dict[str, Any] = field(default_factory=dict)


def _load_pdf(path: Path, dpi: int) -> LoadedLabel:
    try:
        import pypdfium2  # pylint: disable=import-outside-toplevel
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log_event(
            logger,
            logging.ERROR,
            "pypdfium2 unavailable; cannot render PDFs",
            error=describe_exception(exc),
        )
        raise UnsupportedFormat(f"PDF support unavailable: {exc!r}") from exc

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        page_count = len(pdf)
        if page_count == 0:
            raise UnsupportedFormat("PDF has no pages")
        page = pdf[0]
        width_pt, height_pt = page.get_size()
        bitmap = page.render(scale=dpi / 72.0)
        image = bitmap.to_pil()
        image.load()
        info = {
            "pages": page_count,
            "page_size_pt": [round(width_pt, 1), round(height_pt, 1)],
            "page_size_in": [round(width_pt / 72, 2), round(height_pt / 72, 2)],
            "dpi": dpi,
        }
        log_event(
            logger,
            logging.WARNING if page_count > 1 else logging.INFO,
            "rendered PDF first page",
            file=path.name,
            size=list(image.size),
            **info,
        )
        return LoadedLabel(image.copy(), "PDF", info)
    finally:
        pdf.close()


def load_label(path: Path, *, pdf_dpi: int = 300) -> LoadedLabel:
    """Decode an image or the first page of a PDF.

    Raises:
        UnsupportedFormat: for raw printer languages, unknown types, or files
            that fail to decode.
    """
    suffix = path.suffix.lower()
    if suffix in RAW_PRINTER_SUFFIXES:
        raise UnsupportedFormat(f"raw printer language {suffix} is not supported")
    if suffix in PDF_SUFFIXES:
        try:
            return _load_pdf(path, pdf_dpi)
        except UnsupportedFormat:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise UnsupportedFormat(f"PDF failed to render: {exc!r}") from exc
    try:
        with Image.open(path) as opened:
            opened.load()
            fmt = opened.format or suffix.lstrip(".").upper()
            info = {
                "format": fmt,
                "mode": opened.mode,
                "frames": getattr(opened, "n_frames", 1),
                "dpi": list(opened.info["dpi"]) if "dpi" in opened.info else None,
            }
            image = opened.copy()
            image.format = fmt
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise UnsupportedFormat(f"image failed to decode: {exc!r}") from exc
    return LoadedLabel(image, fmt, info)


@dataclass
class ShapeDecision:
    """Whether an image looks like a 4x6 label, and why."""

    is_label: bool
    width: int
    height: int
    aspect: float
    reasons: list[str]

    def to_log(self) -> dict[str, Any]:
        """Flatten for structured logging."""
        return {
            "is_label": self.is_label,
            "size": [self.width, self.height],
            "aspect": round(self.aspect, 3),
            "reasons": self.reasons,
        }


def classify_shape(
    size: tuple[int, int],
    *,
    aspect_min: float = 1.4,
    aspect_max: float = 1.6,
    min_short_side_px: int = 400,
) -> ShapeDecision:
    """Check that the long/short side ratio is near 1.5 and it is big enough."""
    width, height = size
    short, long_ = sorted((width, height))
    aspect = long_ / short if short > 0 else 0.0
    reasons: list[str] = []
    ok = True
    if aspect_min <= aspect <= aspect_max:
        reasons.append(f"aspect {aspect:.3f} within [{aspect_min}, {aspect_max}]")
    else:
        ok = False
        reasons.append(f"aspect {aspect:.3f} outside [{aspect_min}, {aspect_max}]")
    if short >= min_short_side_px:
        reasons.append(f"short side {short}px >= {min_short_side_px}px")
    else:
        ok = False
        reasons.append(f"short side {short}px < {min_short_side_px}px")
    return ShapeDecision(ok, width, height, aspect, reasons)
