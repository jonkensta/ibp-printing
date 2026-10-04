"""Tests for PM2411BT TSPL rasterizing, job building and line parsing."""

import os
import struct
import unittest

from PIL import Image
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

    def test_landscape_is_rotated_scaled_and_centered(self):
        # A landscape black image: rotated to portrait, fit at 95 %, centered.
        raster = tspl.rasterize(Image.new("RGB", (1800, 1200), "black"))
        scaled_w, scaled_h = int(0.95 * 812), int(0.95 * 812 * 1800 / 1200)
        self.assertLessEqual(scaled_h, 1218)
        left, top = (812 - scaled_w) // 2, (1218 - scaled_h) // 2
        self.assertTrue(bit_is_black(raster, 406, 609))
        self.assertTrue(bit_is_black(raster, left + 2, top + 2))
        self.assertFalse(bit_is_black(raster, left - 2, 609))
        self.assertFalse(bit_is_black(raster, 406, top - 2))
        self.assertFalse(bit_is_black(raster, 0, 0))
        self.assertFalse(bit_is_black(raster, 811, 1217))

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
