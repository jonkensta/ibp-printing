"""TSPL job building and status-line parsing for the PM2411BT.

The job layout is the one the vendor's Linux filter sends to model 100
(PM-2410/2411), verified on hardware; see ``docs/printers/pm2411bt.md``::

    \\r\\n SIZE 102 mm,152 mm\\r\\n REFERENCE 0,0\\r\\n DIRECTION 0,0\\r\\n
    GAP 3.0 mm,0.0 mm\\r\\n SET TEAR ON\\r\\n OFFSET 0 mm\\r\\n SPEED 4\\r\\n
    CLS\\r\\n BITMAP 0,0,102,1218,4,<payload>\\r\\nPRINT 1,1\\r\\n

(no spaces between the commands). ``<payload>`` is the packed raster in
4096-byte slices, each LZO1X-1 compressed and prefixed with its compressed
length as a little-endian uint32, terminated by a zero length.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from PIL import Image

from ibp_printing.direct import lzo
from ibp_printing.render import flatten_for_print, orient_portrait

# 4x6 in at 203 dpi.
LABEL_WIDTH_DOTS = 812
LABEL_HEIGHT_DOTS = 1218
BYTES_PER_ROW = (LABEL_WIDTH_DOTS + 7) // 8  # 102
RASTER_SIZE = BYTES_PER_ROW * LABEL_HEIGHT_DOTS  # 124236
# A dot is printed (black) when its 8-bit grey value is at most this.
DARK_THRESHOLD = 0xAF
# Raster slice size for BITMAP mode 4 (as the vendor filter).
SLICE_SIZE = 4096
BITMAP_MODE_LZO = 4

CRLF = b"\r\n"
JOB_HEADER_LINES = (
    b"SIZE 102 mm,152 mm",
    b"REFERENCE 0,0",
    b"DIRECTION 0,0",
    b"GAP 3.0 mm,0.0 mm",
    b"SET TEAR ON",
    b"OFFSET 0 mm",
    b"SPEED 4",
    b"CLS",
)
JOB_TRAILER = CRLF + b"PRINT 1,1" + CRLF

# Queries the printer answers over USB (one CR LF terminated line each).
QUERY_COVER = b"SSSGETCAP\r\n"
QUERY_PAPER = b"SSSGETPAPER\r\n"

# Grey value -> 1 if dark. Used with Image.point to threshold in C.
_DARK_LUT = [255 if value <= DARK_THRESHOLD else 0 for value in range(256)]


def label_rect(size: tuple[int, int]) -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` that fills the label with an image of ``size``.

    The image is scaled to fit 812x1218 at 100 % (keeping its aspect ratio,
    integer math so a 2:3 image fills the label exactly) and centered.
    """
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError(f"image has no area: {size}")
    if width * LABEL_HEIGHT_DOTS >= height * LABEL_WIDTH_DOTS:
        new_w = LABEL_WIDTH_DOTS
        new_h = min(LABEL_HEIGHT_DOTS, max(1, round(height * LABEL_WIDTH_DOTS / width)))
    else:
        new_h = LABEL_HEIGHT_DOTS
        new_w = min(LABEL_WIDTH_DOTS, max(1, round(width * LABEL_HEIGHT_DOTS / height)))
    left = (LABEL_WIDTH_DOTS - new_w) // 2
    top = (LABEL_HEIGHT_DOTS - new_h) // 2
    return left, top, new_w, new_h


def place_on_label(img: Image.Image) -> Image.Image:
    """Return an 812x1218 grey ("L") label image holding ``img``.

    Landscape images are rotated to portrait (never cropped). An image that is
    already 812x1218 (a 4x6 label at 203 dpi) is used dot for dot. Anything
    else is scaled to *fill* the label (100 %, not the GDI path's 95 %) with
    nearest-neighbour resampling and centered.

    Why not LANCZOS/BOX at 95 % like the GDI path: the barcode study in
    ``tests/test_direct_barcodes.py`` (Code 128 and USPS IMpb GS1-128 on
    1200x1800 300 dpi labels, decoded with zbar after simulated dot gain)
    found that nearest at 100 % keeps every bar's edges on whole dots in a
    consistent way, while filtered resampling plus a threshold, or any 95 %
    shrink, turns 3-pixel modules into 1-3 dot bars and loses decodes. The
    full 812x1218 raster printed on hardware with every edge inside the label,
    so the GDI path's 5 % backoff (for unknown driver margins) is not needed.
    """
    img = flatten_for_print(orient_portrait(img)).convert("L")
    if img.size == (LABEL_WIDTH_DOTS, LABEL_HEIGHT_DOTS):
        return img
    left, top, new_w, new_h = label_rect(img.size)
    img = img.resize((new_w, new_h), Image.Resampling.NEAREST)
    canvas = Image.new("L", (LABEL_WIDTH_DOTS, LABEL_HEIGHT_DOTS), 255)
    canvas.paste(img, (left, top))
    return canvas


def pack_raster(img: Image.Image) -> bytes:
    """Pack an exactly 812x1218 image into the printer's raster bytes.

    Dark if grey <= 0xAF, 8 dots per byte MSB-first, then inverted so that a
    0 bit is a black dot (the vendor filter's model-100 encoding).
    """
    if img.size != (LABEL_WIDTH_DOTS, LABEL_HEIGHT_DOTS):
        raise ValueError(
            f"raster must be {LABEL_WIDTH_DOTS}x{LABEL_HEIGHT_DOTS}, got {img.size}"
        )
    grey = flatten_for_print(img).convert("L")
    # point(): dark -> 255; mode "1" packs MSB-first with 255 -> bit 1.
    bits = grey.point(_DARK_LUT).convert("1", dither=Image.Dither.NONE)
    packed = bits.tobytes()
    if len(packed) != RASTER_SIZE:
        raise AssertionError(f"packed raster is {len(packed)} bytes")
    return bytes(byte ^ 0xFF for byte in packed)


def rasterize(img: Image.Image) -> bytes:
    """Place ``img`` on a 4x6 label and pack it (102 bytes x 1218 rows)."""
    return pack_raster(place_on_label(img))


def compress_payload(raster: bytes) -> bytes:
    """BITMAP mode-4 payload: length-prefixed LZO1X-1 slices + zero terminator."""
    out = bytearray()
    for offset in range(0, len(raster), SLICE_SIZE):
        chunk = lzo.compress(raster[offset : offset + SLICE_SIZE])
        out += struct.pack("<I", len(chunk))
        out += chunk
    out += struct.pack("<I", 0)
    return bytes(out)


def decompress_payload(payload: bytes) -> tuple[bytes, int]:
    """Inverse of :func:`compress_payload`; returns ``(raster, bytes_consumed)``."""
    raster = bytearray()
    pos = 0
    while True:
        if pos + 4 > len(payload):
            raise lzo.LzoError("payload ends without a zero-length terminator")
        (length,) = struct.unpack_from("<I", payload, pos)
        pos += 4
        if length == 0:
            return bytes(raster), pos
        if pos + length > len(payload):
            raise lzo.LzoError(f"slice of {length} bytes overruns the payload")
        raster += lzo.decompress(payload[pos : pos + length], max_out=SLICE_SIZE)
        pos += length


def job_header() -> bytes:
    """Everything before the BITMAP payload."""
    lines = CRLF + b"".join(line + CRLF for line in JOB_HEADER_LINES)
    bitmap = (
        f"BITMAP 0,0,{BYTES_PER_ROW},{LABEL_HEIGHT_DOTS},{BITMAP_MODE_LZO},".encode()
    )
    return lines + bitmap


def build_job(raster: bytes) -> bytes:
    """The complete one-label job for a packed raster (see module docstring)."""
    if len(raster) != RASTER_SIZE:
        raise ValueError(f"raster must be {RASTER_SIZE} bytes, got {len(raster)}")
    return job_header() + compress_payload(raster) + JOB_TRAILER


def parse_job(job: bytes) -> bytes:
    """Check a job's framing and return its decompressed raster (tests, tools)."""
    header = job_header()
    if not job.startswith(header):
        raise ValueError("job does not start with the expected header")
    raster, used = decompress_payload(job[len(header) :])
    if job[len(header) + used :] != JOB_TRAILER:
        raise ValueError("job does not end with the expected trailer")
    return raster


class EventKind(str, Enum):
    """What a line received from the printer means."""

    COVER = "cover"  # SSSGETCAP:<OPEN|CLOSE>
    PAPER = "paper"  # SSSGETPAPER:<YES|...>  (unreliable; log only)
    PRINTING = "printing"  # SSSGETPRINTING:<DOING|DONE>
    COMMAND_ERROR = "command_error"  # Cmd error:<echoed line>
    OTHER = "other"


@dataclass(frozen=True)
class PrinterEvent:
    """One parsed line from the printer."""

    kind: EventKind
    value: str
    raw: str

    @property
    def cover_closed(self) -> Optional[bool]:
        """True/False for a cover event, None otherwise."""
        if self.kind is not EventKind.COVER:
            return None
        return self.value == "CLOSE"

    def to_log(self) -> dict[str, str]:
        """Flatten for structured logging."""
        return {"kind": self.kind.value, "value": self.value, "raw": self.raw}


_PREFIXES = (
    ("SSSGETCAP:", EventKind.COVER),
    ("SSSGETPAPER:", EventKind.PAPER),
    ("SSSGETPRINTING:", EventKind.PRINTING),
    ("Cmd error:", EventKind.COMMAND_ERROR),
)


def parse_line(line: str) -> PrinterEvent:
    """Classify one line (terminator already stripped) from the printer."""
    raw = line
    text = line.strip("\r\n\x00 ")
    for prefix, kind in _PREFIXES:
        if text.startswith(prefix):
            value = text[len(prefix) :]
            if kind is not EventKind.COMMAND_ERROR:
                value = value.strip().upper()
            return PrinterEvent(kind, value, raw)
    return PrinterEvent(EventKind.OTHER, text, raw)
