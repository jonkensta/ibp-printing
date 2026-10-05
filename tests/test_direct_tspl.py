"""Tests for PM2411BT TSPL rasterizing, job building and line parsing."""

import os
import struct
import unittest

from PIL import Image, ImageDraw
from test_direct_lzo import LibLzo2, realistic_raster

from ibp_printing.direct import lzo, tspl
from ibp_printing.direct.tspl import EventKind

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pm2411bt")
# The job that printed correctly on real hardware (2026-10-04), built by the
# vendor-equivalent reference builder with liblzo2, and the image it came from.
REFERENCE_JOB = os.path.join(DATA_DIR, "test_job.bin")
REFERENCE_LABEL = os.path.join(DATA_DIR, "test_label.png")

EXPECTED_HEADER = (
    b"\r\nSIZE 102 mm,152 mm\r\nREFERENCE 0,0\r\nDIRECTION 0,0\r\n"
    b"GAP 3.0 mm,0.0 mm\r\nSET TEAR ON\r\nOFFSET 0 mm\r\nSPEED 4\r\nCLS\r\n"
    b"BITMAP 0,0,102,1218,4,"
)
EXPECTED_TRAILER = b"\r\nPRINT 1,1\r\n"


def bit_is_black(raster: bytes, x: int, y: int) -> bool:
    """True if dot (x, y) prints (0 bits are black)."""
    byte = raster[y * tspl.BYTES_PER_ROW + x // 8]
    return not byte & (0x80 >> (x % 8))


def split_payload(job: bytes) -> list[bytes]:
    """The compressed slices of a job's BITMAP payload."""
    pos = len(EXPECTED_HEADER)
    chunks = []
    while True:
        (length,) = struct.unpack_from("<I", job, pos)
        pos += 4
        if length == 0:
            return chunks
        chunks.append(job[pos : pos + length])
        pos += length


class ReferenceJobTests(unittest.TestCase):
    """build_job reproduces the job verified on the printer."""

    def setUp(self):
        with open(REFERENCE_JOB, "rb") as handle:
            self.reference = handle.read()
        with Image.open(REFERENCE_LABEL) as img:
            img.load()
            self.label = img.copy()

    def test_reference_framing(self):
        self.assertTrue(self.reference.startswith(EXPECTED_HEADER))
        self.assertTrue(self.reference.endswith(b"\x00\x00\x00\x00" + EXPECTED_TRAILER))
        self.assertEqual(tspl.job_header(), EXPECTED_HEADER)
        self.assertEqual(tspl.JOB_TRAILER, EXPECTED_TRAILER)

    def test_pack_raster_matches_reference_raster(self):
        raster = tspl.pack_raster(self.label)
        self.assertEqual(len(raster), 124236)
        self.assertEqual(tspl.parse_job(self.reference), raster)

    def test_build_job_header_footer_and_raster(self):
        job = tspl.build_job(tspl.pack_raster(self.label))
        self.assertEqual(job[: len(EXPECTED_HEADER)], EXPECTED_HEADER)
        self.assertEqual(
            job[-len(EXPECTED_TRAILER) - 4 :], b"\x00" * 4 + EXPECTED_TRAILER
        )
        self.assertEqual(tspl.parse_job(job), tspl.parse_job(self.reference))
        self.assertEqual(len(split_payload(job)), 31)  # 30 x 4096 + 1356

    def test_build_job_is_byte_identical_to_reference(self):
        # Our compressor mirrors liblzo2's, so even the payload matches.
        self.assertEqual(tspl.build_job(tspl.pack_raster(self.label)), self.reference)

    def test_rasterize_keeps_the_reference_label_dot_for_dot(self):
        # The verified label is already 812x1218: rasterize() must not scale it.
        self.assertEqual(tspl.build_job(tspl.rasterize(self.label)), self.reference)

    @unittest.skipUnless(LibLzo2.get(), "liblzo2 not available")
    def test_liblzo2_decompresses_every_slice(self):
        lib = LibLzo2.get()
        assert lib is not None
        raster = realistic_raster()
        chunks = split_payload(tspl.build_job(raster))
        rebuilt = b"".join(
            lib.decompress_safe(chunk, min(4096, len(raster) - index * 4096))
            for index, chunk in enumerate(chunks)
        )
        self.assertEqual(rebuilt, raster)


class JobFramingTests(unittest.TestCase):
    """Payload framing and input validation."""

    def test_white_label(self):
        raster = b"\xff" * tspl.RASTER_SIZE
        job = tspl.build_job(raster)
        self.assertEqual(tspl.parse_job(job), raster)
        self.assertLess(len(job), 2000)
        self.assertEqual(job.count(b"PRINT 1,1"), 1)

    def test_wrong_raster_size(self):
        with self.assertRaises(ValueError):
            tspl.build_job(b"\xff" * 100)

    def test_truncated_payload(self):
        payload = tspl.compress_payload(b"\x00" * 9000)
        with self.assertRaises(lzo.LzoError):
            tspl.decompress_payload(payload[:-4])

    def test_parse_job_rejects_bad_trailer(self):
        job = tspl.build_job(b"\xff" * tspl.RASTER_SIZE)
        with self.assertRaises(ValueError):
            tspl.parse_job(job[:-2])


class RasterizeTests(unittest.TestCase):
    """Placement, orientation, threshold and polarity."""

    def test_threshold_and_polarity(self):
        img = Image.new("L", (812, 1218), 255)
        img.putpixel((0, 0), 0xAF)  # dark (<= 0xAF)
        img.putpixel((1, 0), 0xB0)  # light
        img.putpixel((8, 0), 0)
        img.putpixel((811, 1217), 0)
        raster = tspl.pack_raster(img)
        self.assertTrue(bit_is_black(raster, 0, 0))
        self.assertFalse(bit_is_black(raster, 1, 0))
        self.assertEqual(raster[0], 0x7F)  # MSB first, inverted
        self.assertEqual(raster[1], 0x7F)
        self.assertTrue(bit_is_black(raster, 811, 1217))
        # Padding dots (812..815) are white.
        self.assertEqual(raster[-1], 0xEF)  # dot 811 black, dots 812-815 padding
        self.assertEqual(sum(bit_is_black(raster, x, 5) for x in range(812)), 0)

    def test_pack_requires_exact_size(self):
        with self.assertRaises(ValueError):
            tspl.pack_raster(Image.new("L", (100, 100), 0))

    def test_landscape_is_rotated_filled_and_centered(self):
        # A landscape black image: rotated to portrait (1000x1800), scaled to
        # the full label height (100 %, not 95 %) and centered horizontally.
        raster = tspl.rasterize(Image.new("RGB", (1800, 1000), "black"))
        scaled_w = round(1000 * 1218 / 1800)  # 677
        left = (812 - scaled_w) // 2
        self.assertEqual(tspl.label_rect((1000, 1800)), (left, 0, scaled_w, 1218))
        self.assertTrue(bit_is_black(raster, 406, 0))
        self.assertTrue(bit_is_black(raster, 406, 1217))
        self.assertTrue(bit_is_black(raster, left, 609))
        self.assertTrue(bit_is_black(raster, left + scaled_w - 1, 609))
        self.assertFalse(bit_is_black(raster, left - 1, 609))
        self.assertFalse(bit_is_black(raster, left + scaled_w, 609))

    def test_2_3_image_fills_the_whole_label(self):
        for size in ((1200, 1800), (1624, 2436), (1218, 1827), (400, 600)):
            with self.subTest(size=size):
                self.assertEqual(tspl.label_rect(size), (0, 0, 812, 1218))
                raster = tspl.rasterize(Image.new("L", size, 0))
                # Every dot black; the 4 padding dots per row (812..815) white.
                row = b"\x00" * (tspl.BYTES_PER_ROW - 1) + b"\x0f"
                self.assertEqual(raster, row * tspl.LABEL_HEIGHT_DOTS)

    def test_label_sized_image_is_not_resampled(self):
        img = Image.new("L", (812, 1218), 255)
        for x in range(0, 812, 3):  # 1-dot lines every 3 dots survive exactly
            img.paste(0, (x, 0, x + 1, 1218))
        self.assertEqual(tspl.place_on_label(img).tobytes(), img.tobytes())
        self.assertEqual(tspl.rasterize(img), tspl.pack_raster(img))

    def test_label_rect_rejects_empty(self):
        with self.assertRaises(ValueError):
            tspl.label_rect((0, 10))

    def test_orientation_keeps_top_first(self):
        # Portrait 4x6 at 300 dpi with a black band at the top.
        img = Image.new("L", (1200, 1800), 255)
        img.paste(0, (0, 0, 1200, 200))
        raster = tspl.rasterize(img)
        self.assertTrue(bit_is_black(raster, 406, 100))
        self.assertFalse(bit_is_black(raster, 406, 1100))

    def test_transparent_png_is_white(self):
        img = Image.new("RGBA", (812, 1218), (0, 0, 0, 0))
        raster = tspl.rasterize(img)
        self.assertEqual(raster, b"\xff" * tspl.RASTER_SIZE)


def dark_dots(raster: bytes) -> set[tuple[int, int]]:
    """Every printed dot of a packed raster as ``(x, y)``."""
    return {
        (x, y)
        for y in range(tspl.LABEL_HEIGHT_DOTS)
        for x in range(tspl.LABEL_WIDTH_DOTS)
        if bit_is_black(raster, x, y)
    }


def nearest_only(img: Image.Image) -> bytes:
    """The previous rule: threshold after plain nearest-neighbour scaling."""
    img = img.convert("L")
    left, top, new_w, new_h = tspl.label_rect(img.size)
    canvas = Image.new("L", (tspl.LABEL_WIDTH_DOTS, tspl.LABEL_HEIGHT_DOTS), 255)
    canvas.paste(img.resize((new_w, new_h), Image.Resampling.NEAREST), (left, top))
    return tspl.pack_raster(canvas)


class ThinFeatureTests(unittest.TestCase):
    """Features thinner than a dot's footprint keep at least one dot."""

    SIZES = ((1200, 1800), (1218, 1827), (1624, 2436), (1000, 1500))

    def check_rule(self, raster, size, horizontal, pos, span, width=1):
        """Every dot column (row) along the rule has a dark dot near it; thickness."""
        left, top, new_w, new_h = tspl.label_rect(size)
        sx, sy = new_w / size[0], new_h / size[1]
        scale, offset, limit = (
            (sy, top, tspl.LABEL_HEIGHT_DOTS)
            if horizontal
            else (sx, left, tspl.LABEL_WIDTH_DOTS)
        )
        near = range(
            max(0, int(offset + pos * scale) - 1),
            min(limit, int(offset + (pos + width) * scale) + 2),
        )
        if horizontal:
            along = range(int(left + span[0] * sx) + 2, int(left + span[1] * sx) - 2)
            counts = [sum(bit_is_black(raster, a, b) for b in near) for a in along]
        else:
            along = range(int(top + span[0] * sy) + 2, int(top + span[1] * sy) - 2)
            counts = [sum(bit_is_black(raster, b, a) for b in near) for a in along]
        self.assertGreater(len(counts), 100)
        self.assertGreaterEqual(min(counts), 1)
        return max(counts)

    def test_one_pixel_border_survives(self):
        # The hardware failure: a 1 px border on a 300 dpi label printed nothing.
        for size in self.SIZES:
            for inset in range(4):
                with self.subTest(size=size, inset=inset):
                    width, height = size
                    img = Image.new("L", size, 255)
                    ImageDraw.Draw(img).rectangle(
                        [inset, inset, width - 1 - inset, height - 1 - inset],
                        outline=0,
                    )
                    raster = tspl.rasterize(img)
                    sides = (
                        (True, inset, (inset + 20, width - inset - 20)),
                        (True, height - 1 - inset, (inset + 20, width - inset - 20)),
                        (False, inset, (inset + 20, height - inset - 20)),
                        (False, width - 1 - inset, (inset + 20, height - inset - 20)),
                    )
                    for horizontal, pos, span in sides:
                        thickness = self.check_rule(raster, size, horizontal, pos, span)
                        self.assertEqual(thickness, 1)  # no dot gain

    def test_one_and_two_pixel_rules_at_every_phase(self):
        for size in self.SIZES:
            img = Image.new("L", size, 255)
            draw = ImageDraw.Draw(img)
            rules = []
            for index in range(12):
                width = 1 + index % 2
                pos = 100 + index * 21 + index // 2  # every phase mod the scale
                draw.rectangle([60, pos, size[0] - 61, pos + width - 1], fill=0)
                rules.append((True, pos, (60, size[0] - 61), width))
                draw.rectangle(
                    [pos, size[1] // 2, pos + width - 1, size[1] - 100], fill=0
                )
                rules.append((False, pos, (size[1] // 2, size[1] - 100), width))
            raster = tspl.rasterize(img)
            for horizontal, pos, span, width in rules:
                with self.subTest(size=size, horizontal=horizontal, pos=pos):
                    self.check_rule(raster, size, horizontal, pos, span, width)

    def test_lone_pixels_each_keep_a_dot(self):
        img = Image.new("L", (1200, 1800), 255)
        pixels = [(50 + 7 * i, 60 + 9 * j) for i in range(40) for j in range(40)]
        for pixel in pixels:
            img.putpixel(pixel, 0)
        # One dot each (they are 4-6 dots apart), nothing else.
        self.assertEqual(len(dark_dots(tspl.rasterize(img))), len(pixels))

    def test_thick_features_are_plain_nearest(self):
        # Features at least ceil(scale) px wide are left to nearest-neighbour
        # (barcode bars keep their whole-dot edges).
        img = Image.new("L", (1200, 1800), 255)
        draw = ImageDraw.Draw(img)
        for index in range(60):
            x = 40 + index * 18
            draw.rectangle([x, 100, x + 1 + index % 4, 900], fill=0)
        draw.rectangle([20, 1000, 1179, 1700], outline=0, width=2)
        self.assertEqual(tspl.rasterize(img), nearest_only(img))

    def test_upscaled_images_are_plain_nearest(self):
        img = Image.new("L", (400, 600), 255)
        draw = ImageDraw.Draw(img)
        draw.rectangle([5, 5, 394, 594], outline=0)
        draw.line([10, 300, 390, 310], fill=0)
        self.assertEqual(tspl.rasterize(img), nearest_only(img))

    def test_thin_features_mask(self):
        mask = Image.new("L", (40, 40), 255)
        draw = ImageDraw.Draw(mask)
        draw.rectangle([5, 5, 34, 5], fill=0)  # 1 px rule: thin
        draw.rectangle([5, 10, 34, 11], fill=0)  # 2 px rule: not thin for k=2
        draw.rectangle([5, 20, 6, 21], fill=0)  # 2x2 block: not thin
        draw.point((30, 30), fill=0)  # lone pixel: thin
        # pylint: disable-next=protected-access
        thin = tspl._split_thin(mask, 2)[1]
        dark = {
            (x, y) for y in range(40) for x in range(40) if thin.getpixel((x, y)) == 0
        }
        self.assertEqual(dark, {(x, 5) for x in range(5, 35)} | {(30, 30)})
        # pylint: disable-next=protected-access
        thin3 = tspl._split_thin(mask, 3)[1]
        self.assertIn(
            (5, 10),
            {
                (x, y)
                for y in range(40)
                for x in range(40)
                if thin3.getpixel((x, y)) == 0
            },
        )


class ParseLineTests(unittest.TestCase):
    """Printer reply classification."""

    def test_known_lines(self):
        cases = {
            "SSSGETCAP:CLOSE": (EventKind.COVER, "CLOSE"),
            "SSSGETCAP:OPEN": (EventKind.COVER, "OPEN"),
            "SSSGETPAPER:YES": (EventKind.PAPER, "YES"),
            "SSSGETPRINTING:DOING": (EventKind.PRINTING, "DOING"),
            "SSSGETPRINTING:DONE\r": (EventKind.PRINTING, "DONE"),
            "Cmd error:SSSGETPRINTING": (EventKind.COMMAND_ERROR, "SSSGETPRINTING"),
            "Cmd error:": (EventKind.COMMAND_ERROR, ""),
            "hello": (EventKind.OTHER, "hello"),
            "": (EventKind.OTHER, ""),
        }
        for line, (kind, value) in cases.items():
            with self.subTest(line=line):
                event = tspl.parse_line(line)
                self.assertEqual((event.kind, event.value), (kind, value))
                self.assertEqual(event.raw, line)

    def test_cover_closed_property(self):
        self.assertTrue(tspl.parse_line("SSSGETCAP:CLOSE").cover_closed)
        self.assertFalse(tspl.parse_line("SSSGETCAP:OPEN").cover_closed)
        self.assertIsNone(tspl.parse_line("SSSGETPAPER:YES").cover_closed)

    def test_to_log(self):
        self.assertEqual(
            tspl.parse_line("SSSGETPAPER:YES").to_log(),
            {"kind": "paper", "value": "YES", "raw": "SSSGETPAPER:YES"},
        )


if __name__ == "__main__":
    unittest.main()
