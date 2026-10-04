"""Tests for the pure-Python LZO1X-1 compressor and LZO1X decompressor."""

import ctypes
import ctypes.util
import os
import random
import time
import unittest
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from ibp_printing.direct import lzo

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pm2411bt")


class LibLzo2:
    """liblzo2 via ctypes (Linux dev boxes); None when it is not installed."""

    _instance: Optional["LibLzo2"] = None
    _loaded = False

    def __init__(self, lib: ctypes.CDLL) -> None:
        self.lib = lib
        # __lzo_init_v2(version, sizeof short, int, long, lzo_uint32, lzo_uint,
        # dict_t, char *, lzo_voidp, lzo_callback_t)
        init = getattr(lib, "__lzo_init_v2")
        sizes = (2, 4, ctypes.sizeof(ctypes.c_long), 4, 8, 8, 8, 8, 48)
        if init(0x20A0, *sizes) != 0:
            raise OSError("lzo_init failed")
        self.wrkmem = ctypes.create_string_buffer(16384 * 8)

    @classmethod
    def get(cls) -> Optional["LibLzo2"]:
        """The shared instance, or None without liblzo2 (or on 32-bit)."""
        if not cls._loaded:
            cls._loaded = True
            name = ctypes.util.find_library("lzo2") or "liblzo2.so.2"
            try:
                if ctypes.sizeof(ctypes.c_void_p) == 8:
                    cls._instance = cls(ctypes.CDLL(name))
            except OSError:
                cls._instance = None
        return cls._instance

    def compress(self, src: bytes) -> bytes:
        """lzo1x_1_compress."""
        dst = ctypes.create_string_buffer(lzo.max_compressed_size(len(src)))
        out_len = ctypes.c_size_t(0)
        rc = self.lib.lzo1x_1_compress(
            src, ctypes.c_size_t(len(src)), dst, ctypes.byref(out_len), self.wrkmem
        )
        if rc != 0:
            raise AssertionError(f"lzo1x_1_compress returned {rc}")
        return dst.raw[: out_len.value]

    def decompress_safe(self, src: bytes, expected_len: int) -> bytes:
        """lzo1x_decompress_safe; raises AssertionError on any non-zero code."""
        dst = ctypes.create_string_buffer(max(1, expected_len))
        out_len = ctypes.c_size_t(expected_len)
        rc = self.lib.lzo1x_decompress_safe(
            src, ctypes.c_size_t(len(src)), dst, ctypes.byref(out_len), None
        )
        if rc != 0:
            raise AssertionError(f"lzo1x_decompress_safe returned {rc}")
        return dst.raw[: out_len.value]


def _bytes_cases(seed: int = 1234) -> list[tuple[str, bytes]]:
    """Edge sizes and content shapes (random, runs, sparse alphabets, patterns)."""
    rnd = random.Random(seed)
    sizes = [0, 1, 2, 3, 4, 5, 17, 18, 19, 20, 21, 31, 32, 33, 40, 100, 255]
    sizes += [256, 300, 1000, 4095, 4096, 4097, 49151, 49152, 49153, 100000]
    cases: list[tuple[str, bytes]] = []
    for size in sizes:
        cases.append((f"random-{size}", rnd.randbytes(size)))
        cases.append((f"white-{size}", b"\xff" * size))
        cases.append(
            (
                f"sparse-{size}",
                bytes(rnd.choice(b"\xff\xff\xff\x00\x0f") for _ in range(size)),
            )
        )
        noisy = bytearray(b"\xff" * size)
        for _ in range(size // 40):
            at = rnd.randrange(size)
            noisy[at : at + rnd.randrange(1, 40)] = rnd.randbytes(rnd.randrange(1, 40))
        cases.append((f"noisy-{size}", bytes(noisy[:size])))
        cases.append(
            (f"pattern-{size}", (b"\x00\x01\x02\xfe" * (size // 4 + 1))[:size])
        )
    return cases


def realistic_label(seed: int = 7) -> Image.Image:
    """A dense, shipping-label-like 812x1218 grey image (text, barcodes, boxes)."""
    rnd = random.Random(seed)
    img = Image.new("L", (812, 1218), 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.rectangle([4, 4, 807, 1213], outline=0, width=4)
    for row in range(60):
        y = 20 + row * 9
        text = "".join(
            rnd.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ") for _ in range(120)
        )
        draw.text((12, y), text, font=font, fill=0)
    for band in range(3):
        x, top = 30, 600 + band * 200
        while x < 780:
            width = rnd.choice((2, 2, 4, 6, 8))
            if rnd.random() < 0.55:
                draw.rectangle([x, top, x + width - 1, top + 170], fill=0)
            x += width
    for _ in range(300):
        x, y = rnd.randrange(812), rnd.randrange(1218)
        draw.point((x, y), fill=rnd.randrange(256))
    return img


def realistic_raster(seed: int = 7) -> bytes:
    """``realistic_label`` packed like the printer raster (0 = black)."""
    # Local import keeps this module usable without tspl.
    from ibp_printing.direct import tspl  # pylint: disable=import-outside-toplevel

    return tspl.pack_raster(realistic_label(seed))


class PurePythonRoundTripTests(unittest.TestCase):
    """compress -> decompress reproduces the input exactly."""

    def test_round_trip_cases(self):
        for name, data in _bytes_cases():
            with self.subTest(name):
                packed = lzo.compress(data)
                self.assertLessEqual(len(packed), lzo.max_compressed_size(len(data)))
                self.assertEqual(lzo.decompress(packed), data)

    def test_round_trip_realistic_raster_slices(self):
        raster = realistic_raster()
        for offset in range(0, len(raster), 4096):
            chunk = raster[offset : offset + 4096]
            self.assertEqual(lzo.decompress(lzo.compress(chunk)), chunk)

    def test_empty_input_is_just_the_eof_marker(self):
        self.assertEqual(lzo.compress(b""), b"\x11\x00\x00")

    def test_white_slice_compresses_well(self):
        self.assertLess(len(lzo.compress(b"\xff" * 4096)), 64)

    def test_speed_on_a_dense_label(self):
        raster = realistic_raster()
        started = time.perf_counter()
        for offset in range(0, len(raster), 4096):
            lzo.compress(raster[offset : offset + 4096])
        elapsed = time.perf_counter() - started
        # Measured ~0.05 s here; generous so a slow CI box never flakes.
        self.assertLess(elapsed, 5.0)


class DecompressValidationTests(unittest.TestCase):
    """The decompressor refuses malformed streams instead of guessing."""

    def test_truncated_stream(self):
        packed = lzo.compress(b"hello world, hello world, hello world!" * 4)
        for cut in (0, 1, len(packed) // 2, len(packed) - 1):
            with self.subTest(cut=cut), self.assertRaises(lzo.LzoError):
                lzo.decompress(packed[:cut])

    def test_trailing_garbage(self):
        with self.assertRaises(lzo.LzoError):
            lzo.decompress(lzo.compress(b"abc" * 30) + b"\x00")

    def test_lookbehind_overrun(self):
        # One literal, then an M2 match reaching 9 bytes back.
        with self.assertRaises(lzo.LzoError):
            lzo.decompress(bytes([18, 0x41, 0x60, 0x01, 0x11, 0x00, 0x00]))

    def test_output_limit(self):
        packed = lzo.compress(b"\xff" * 5000)
        with self.assertRaises(lzo.LzoError):
            lzo.decompress(packed, max_out=4096)
        self.assertEqual(len(lzo.decompress(packed, max_out=5000)), 5000)


@unittest.skipUnless(LibLzo2.get(), "liblzo2 not available")
class LibLzo2CompatibilityTests(unittest.TestCase):
    """liblzo2 accepts our streams, and we match its compressor byte for byte."""

    def setUp(self):
        lib = LibLzo2.get()
        assert lib is not None
        self.lib = lib

    def test_liblzo2_decompresses_our_output(self):
        for name, data in _bytes_cases(seed=99):
            with self.subTest(name):
                self.assertEqual(
                    self.lib.decompress_safe(lzo.compress(data), len(data)), data
                )

    def test_identical_to_liblzo2_compressor(self):
        for name, data in _bytes_cases(seed=5):
            with self.subTest(name):
                self.assertEqual(lzo.compress(data), self.lib.compress(data))

    def test_our_decompressor_reads_liblzo2_output(self):
        for name, data in _bytes_cases(seed=6):
            with self.subTest(name):
                self.assertEqual(lzo.decompress(self.lib.compress(data)), data)

    def test_realistic_and_reference_rasters(self):
        from ibp_printing.direct import tspl  # pylint: disable=import-outside-toplevel

        rasters = [realistic_raster(seed) for seed in (1, 2, 3)]
        reference = os.path.join(DATA_DIR, "test_label.png")
        if os.path.exists(reference):
            with Image.open(reference) as img:
                rasters.append(tspl.pack_raster(img))
        for index, raster in enumerate(rasters):
            for offset in range(0, len(raster), 4096):
                chunk = raster[offset : offset + 4096]
                with self.subTest(raster=index, offset=offset):
                    ours = lzo.compress(chunk)
                    self.assertEqual(self.lib.decompress_safe(ours, len(chunk)), chunk)
                    self.assertEqual(ours, self.lib.compress(chunk))


if __name__ == "__main__":
    unittest.main()
