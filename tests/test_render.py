"""Tests for image orientation, flattening and placement math."""

import unittest

from PIL import Image

from ibp_printing.models import DeviceCaps
from ibp_printing.render import (
    compute_draw_rect,
    describe_image,
    flatten_for_print,
    orient_portrait,
)


def old_shippy_rect(img_size, caps):
    """The draw-rectangle math from shippy's original windows.print_image."""
    printable_w, printable_h = caps.horzres, caps.vertres
    ratios = [printable_w / img_size[0], printable_h / img_size[1]]
    scale = 0.95 * min(ratios)
    total_w, total_h = caps.physical_width, caps.physical_height
    scaled_w, scaled_h = [int(scale * i) for i in img_size]
    lhs_x = int((total_w - scaled_w) / 2)
    lhs_y = int((total_h - scaled_h) / 2)
    return (lhs_x, lhs_y, lhs_x + scaled_w, lhs_y + scaled_h)


class OrientTests(unittest.TestCase):
    """orient_portrait."""

    def test_landscape_is_rotated_without_cropping(self):
        img = Image.new("RGB", (1800, 1200), "black")
        rotated = orient_portrait(img)
        self.assertEqual(rotated.size, (1200, 1800))
        # Every pixel of the original survives: no white fill from cropping.
        self.assertEqual(rotated.getextrema(), ((0, 0), (0, 0), (0, 0)))

    def test_rotation_direction(self):
        img = Image.new("L", (300, 200), 255)
        img.putpixel((0, 0), 0)  # top-left of the landscape image
        rotated = orient_portrait(img)
        # rotate(90) is counter-clockwise: top-left moves to bottom-left.
        self.assertEqual(rotated.getpixel((0, 299)), 0)

    def test_portrait_and_square_are_untouched(self):
        for size in ((1200, 1800), (500, 500)):
            img = Image.new("RGB", size)
            self.assertIs(orient_portrait(img), img)


class FlattenTests(unittest.TestCase):
    """flatten_for_print."""

    def test_gdi_modes_pass_through(self):
        for mode in ("1", "L", "RGB"):
            img = Image.new(mode, (4, 4))
            self.assertIs(flatten_for_print(img), img)

    def test_transparent_rgba_becomes_white(self):
        img = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        img.putpixel((1, 1), (255, 0, 0, 255))
        flat = flatten_for_print(img)
        self.assertEqual(flat.mode, "RGB")
        self.assertEqual(flat.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(flat.getpixel((1, 1)), (255, 0, 0))

    def test_palette_with_transparency(self):
        img = Image.new("P", (4, 4), 0)
        img.putpalette([0, 0, 0, 0, 0, 255] + [0] * 762)
        img.putpixel((2, 2), 1)
        img.info["transparency"] = 0
        flat = flatten_for_print(img)
        self.assertEqual(flat.mode, "RGB")
        self.assertEqual(flat.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(flat.getpixel((2, 2)), (0, 0, 255))

    def test_palette_without_transparency(self):
        img = Image.new("P", (4, 4), 0)
        img.putpalette([10, 20, 30] + [0] * 765)
        flat = flatten_for_print(img)
        self.assertEqual(flat.mode, "RGB")
        self.assertEqual(flat.getpixel((0, 0)), (10, 20, 30))

    def test_la_becomes_white_rgb(self):
        img = Image.new("LA", (4, 4), (0, 0))
        img.putpixel((3, 3), (0, 255))
        flat = flatten_for_print(img)
        self.assertEqual(flat.mode, "RGB")
        self.assertEqual(flat.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(flat.getpixel((3, 3)), (0, 0, 0))

    def test_other_modes_convert_to_rgb(self):
        self.assertEqual(flatten_for_print(Image.new("CMYK", (2, 2))).mode, "RGB")


class DrawRectTests(unittest.TestCase):
    """compute_draw_rect."""

    CAPS = [
        # DYMO 4XL at 300 DPI, 4x6 label.
        DeviceCaps(1200, 1800, 1248, 1872, 24, 36, 300, 300),
        # Same printer reporting zero physical offsets.
        DeviceCaps(1218, 1800, 1218, 1800),
        # Zebra at 203 DPI.
        DeviceCaps(812, 1218, 832, 1248, 10, 15, 203, 203),
        # Letter paper on an office printer at 600 DPI.
        DeviceCaps(4900, 6400, 5100, 6600, 100, 100, 600, 600),
    ]
    IMAGES = [(1200, 1800), (800, 1200), (1700, 2200), (1000, 1000), (1201, 1799)]

    def test_matches_old_shippy_math(self):
        for caps in self.CAPS:
            for size in self.IMAGES:
                with self.subTest(caps=caps, size=size):
                    self.assertEqual(
                        compute_draw_rect(size, caps), old_shippy_rect(size, caps)
                    )

    def test_rect_fits_inside_printable_area(self):
        for caps in self.CAPS:
            for size in self.IMAGES:
                left, top, right, bottom = compute_draw_rect(size, caps)
                self.assertLessEqual(right - left, caps.horzres)
                self.assertLessEqual(bottom - top, caps.vertres)

    def test_scale_factor_is_respected(self):
        caps = DeviceCaps(1000, 1000, 1000, 1000)
        self.assertEqual(
            compute_draw_rect((100, 100), caps, scale_factor=1.0), (0, 0, 1000, 1000)
        )

    def test_errors(self):
        caps = DeviceCaps(1200, 1800, 1248, 1872)
        with self.assertRaises(ValueError):
            compute_draw_rect((0, 100), caps)
        with self.assertRaises(ValueError):
            compute_draw_rect((100, -1), caps)
        with self.assertRaises(ValueError):
            compute_draw_rect((100, 100), DeviceCaps(0, 1800, 1248, 1872))
        with self.assertRaises(ValueError):
            compute_draw_rect((100, 100), DeviceCaps(1200, 0, 1248, 1872))


class DescribeImageTests(unittest.TestCase):
    """describe_image."""

    def test_fields(self):
        img = Image.new("RGB", (10, 20))
        img.info["dpi"] = (300, 300)
        self.assertEqual(
            describe_image(img),
            {"size": [10, 20], "mode": "RGB", "format": None, "dpi": [300, 300]},
        )


if __name__ == "__main__":
    unittest.main()
