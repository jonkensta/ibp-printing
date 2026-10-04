"""Barcode fidelity of the direct path's rasterizer (no hardware).

EasyPost PNG labels are 4x6 in at 300 dpi (1200x1800) or 203 dpi (812x1218).
These tests draw realistic shipping barcodes into such labels - a Code 128
tracking number and a USPS IMpb (GS1-128, ``420`` + ZIP + ``92...``) - run
them through ``tspl.rasterize`` and check the 203 dpi raster:

* bar/space widths: every element stays within one dot of its ideal width,
  and integer scale ratios (1x, 1.5x, 2x) are reproduced exactly;
* decoding with zbar (``pyzbar``, a dev dependency; skipped when the zbar
  library is missing), including simulated thermal dot gain/loss.

The study behind the choice of nearest-neighbour at 100 % (see
``tspl.place_on_label``): over 1200x1800, 1218x1827, 1624x2436, 1000x1500,
1700x2550 and 812x1218 sources, 8 phases and 4 dot-gain levels, nearest at
100 % decoded 449/512, LANCZOS+0xAF at 95 % (the old rule) 299/512; every
method lost decodes at 95 %. ``test_nearest_beats_filtered_resampling``
keeps a small version of that comparison.
"""

import unittest
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter

from ibp_printing.direct import tspl

try:
    import barcode as python_barcode
except ImportError:  # pragma: no cover - dev dependency
    python_barcode = None

try:
    from pyzbar import pyzbar
except Exception:  # pylint: disable=broad-exception-caught
    # ImportError without the zbar shared library (Linux without libzbar0),
    # OSError/FileNotFoundError on Windows without the VC++ runtime.
    pyzbar = None

TRACKING = "1Z999AA10123456784"
IMPB = "420787019205590123456789012345"

needs_barcode = unittest.skipIf(python_barcode is None, "python-barcode missing")
needs_zbar = unittest.skipIf(pyzbar is None, "zbar (pyzbar) not available")


def modules(kind: str, data: str) -> str:
    """The symbol's modules as a ``"1101..."`` string (no quiet zone)."""
    assert python_barcode is not None
    return python_barcode.get(kind, data).build()[0]


def runs(bits: str) -> list[int]:
    """Lengths of the alternating bar/space runs of a module string."""
    out: list[int] = []
    previous = None
    for bit in bits:
        if bit == previous:
            out[-1] += 1
        else:
            out.append(1)
            previous = bit
    return out


def draw_label(
    size: tuple[int, int], bits: str, module_px: int, phase: int = 0
) -> tuple[Image.Image, tuple[int, int, int]]:
    """A white label with one barcode; returns it and ``(x0, y0, height)``."""
    width, height = size
    img = Image.new("L", size, 255)
    draw = ImageDraw.Draw(img)
    # Some label furniture: border, text-like blocks.
    draw.rectangle([0, 0, width - 1, height - 1], outline=0, width=max(2, width // 300))
    for row in range(8):
        draw.rectangle(
            [40, 60 + row * 30, 60 + (row * 53) % 500, 72 + row * 30], fill=0
        )
    x0 = (width - len(bits) * module_px) // 2 + phase
    y0 = int(height * 0.4)
    bar_h = height // 10
    x = x0
    for bit in bits:
        if bit == "1":
            draw.rectangle([x, y0, x + module_px - 1, y0 + bar_h - 1], fill=0)
        x += module_px
    return img, (x0, y0, bar_h)


def raster_image(raster: bytes) -> Image.Image:
    """The packed raster as an 812x1218 "L" image (black = 0)."""
    packed = bytes(byte ^ 0xFF for byte in raster)  # 1 = black
    bits = Image.frombytes(
        "1", (tspl.LABEL_WIDTH_DOTS, tspl.LABEL_HEIGHT_DOTS), packed
    ).convert("L")
    return bits.point(lambda value: 255 - value)


def scanline_runs(img: Image.Image, y: int) -> list[int]:
    """Bar/space runs of row ``y``, from the first black dot to the last."""
    row = [img.getpixel((x, y)) < 128 for x in range(img.width)]
    first = row.index(True)
    last = len(row) - 1 - row[::-1].index(True)
    bits = "".join("1" if dark else "0" for dark in row[first : last + 1])
    return runs(bits)


def band(
    img: Image.Image, src_size: tuple[int, int], where: tuple[int, int, int]
) -> Image.Image:
    """Crop the rasterized barcode: rows inside the bars, columns inside the border."""
    _, y0, bar_h = where
    left, top, new_w, _ = tspl.label_rect(src_size)
    scale = new_w / src_size[0]
    upper = int(top + y0 * scale) + 3
    lower = int(top + (y0 + bar_h) * scale) - 3
    return img.crop((left + 10, upper, left + new_w - 10, lower))


def simulate_print(crop: Image.Image, gain_quarter_dots: int) -> Image.Image:
    """Upscale 4x horizontally and grow (or shrink) black by quarter dots."""
    big = crop.resize((crop.width * 4, crop.height), Image.Resampling.NEAREST)
    if gain_quarter_dots > 0:
        big = big.filter(ImageFilter.MinFilter(2 * gain_quarter_dots + 1))
    elif gain_quarter_dots < 0:
        big = big.filter(ImageFilter.MaxFilter(-2 * gain_quarter_dots + 1))
    big = big.crop((0, 2, big.width, big.height - 2))
    padded = Image.new("L", (big.width + 80, big.height + 40), 255)
    padded.paste(big, (40, 20))
    return padded


def decodes(img: Image.Image, want: str) -> bool:
    """True if zbar finds a Code 128 symbol whose data ends in ``want``."""
    assert pyzbar is not None
    found = pyzbar.decode(img, symbols=[pyzbar.ZBarSymbol.CODE128])
    return any(result.data.decode("latin-1").endswith(want) for result in found)


def old_rasterize(img: Image.Image) -> bytes:
    """The previous rule (95 % fit, LANCZOS, then the 0xAF threshold)."""
    img = img.convert("L")
    scale = 0.95 * min(812 / img.width, 1218 / img.height)
    size = (int(scale * img.width), int(scale * img.height))
    canvas = Image.new("L", (812, 1218), 255)
    canvas.paste(
        img.resize(size, Image.Resampling.LANCZOS),
        ((812 - size[0]) // 2, (1218 - size[1]) // 2),
    )
    return tspl.pack_raster(canvas)


@needs_barcode
class BarWidthTests(unittest.TestCase):
    """Deterministic bar/space width checks (no decoder needed)."""

    def check_widths(
        self,
        src_size: tuple[int, int],
        kind: str,
        data: str,
        module_px: int,
        exact: bool,
        phases: range = range(4),
    ) -> None:
        bits = modules(kind, data)
        ideal_runs = runs(bits)
        for phase in phases:
            with self.subTest(size=src_size, kind=kind, x=module_px, phase=phase):
                label, where = draw_label(src_size, bits, module_px, phase)
                img = raster_image(tspl.rasterize(label))
                crop = band(img, src_size, where)
                got = scanline_runs(crop, crop.height // 2)
                self.assertEqual(len(got), len(ideal_runs))
                scale = tspl.label_rect(src_size)[2] / src_size[0]
                errors = [
                    dots - modules_count * module_px * scale
                    for dots, modules_count in zip(got, ideal_runs)
                ]
                if exact:
                    self.assertEqual(max(abs(error) for error in errors), 0.0)
                else:
                    self.assertLess(max(abs(error) for error in errors), 1.0)
                    # Total symbol width is preserved within a dot.
                    self.assertLess(abs(sum(errors)), 1.0 + 1e-9)

    def test_203_dpi_label_is_dot_for_dot(self):
        self.check_widths((812, 1218), "code128", TRACKING, 2, exact=True)
        self.check_widths((812, 1218), "gs1_128", IMPB, 3, exact=True)

    def test_exact_2x_and_1_5x_sources_keep_integer_widths(self):
        self.check_widths((1624, 2436), "code128", TRACKING, 4, exact=True)
        self.check_widths((1218, 1827), "gs1_128", IMPB, 3, exact=True)

    def test_300_dpi_easypost_label_widths_within_one_dot(self):
        self.check_widths((1200, 1800), "code128", TRACKING, 3, exact=False)
        self.check_widths((1200, 1800), "gs1_128", IMPB, 4, exact=False)


@needs_barcode
@needs_zbar
class ZbarDecodeTests(unittest.TestCase):
    """The rasterized barcodes decode, with simulated dot gain and loss."""

    CASES = (
        ((1200, 1800), "code128", TRACKING, 3),
        ((1200, 1800), "code128", TRACKING, 4),
        ((1200, 1800), "gs1_128", IMPB, 3),
        ((1200, 1800), "gs1_128", IMPB, 4),
        ((812, 1218), "code128", TRACKING, 2),
        ((812, 1218), "gs1_128", IMPB, 3),
    )

    def rate(
        self,
        rasterize,
        cases=CASES,
        phases=range(4),
        gains=(-1, 0, 1, 2),
        failures: Optional[list[str]] = None,
    ) -> tuple[int, int]:
        """``(decoded, tried)`` over cases x phases x gains."""
        good = tried = 0
        for src_size, kind, data, module_px in cases:
            bits = modules(kind, data)
            for phase in phases:
                label, where = draw_label(src_size, bits, module_px, phase)
                crop = band(raster_image(rasterize(label)), src_size, where)
                for gain in gains:
                    tried += 1
                    want = data
                    if decodes(simulate_print(crop, gain), want):
                        good += 1
                    elif failures is not None:
                        failures.append(
                            f"{src_size} {kind} X={module_px} {phase=} {gain=}"
                        )
        return good, tried

    def test_source_labels_decode(self):
        for src_size, kind, data, module_px in self.CASES:
            label, _ = draw_label(src_size, modules(kind, data), module_px)
            self.assertTrue(decodes(label, data), (src_size, kind, module_px))

    def test_every_case_decodes(self):
        failures: list[str] = []
        good, tried = self.rate(tspl.rasterize, failures=failures)
        self.assertEqual(good, tried, failures)

    def test_nearest_beats_filtered_resampling(self):
        cases = tuple(case for case in self.CASES if case[0] == (1200, 1800))
        new, tried = self.rate(tspl.rasterize, cases=cases, gains=(0, 1))
        old, _ = self.rate(old_rasterize, cases=cases, gains=(0, 1))
        self.assertEqual(new, tried)
        self.assertLess(old, new)


if __name__ == "__main__":
    unittest.main()
