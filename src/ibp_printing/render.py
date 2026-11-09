"""Pure image placement math shared by every backend."""

from PIL import Image

from ibp_printing.models import DeviceCaps

# Empirically chosen backoff so the label is never chopped at the page edges.
PRINT_SCALE_FACTOR = 0.95


def orient_portrait(img: Image.Image) -> Image.Image:
    """Rotate landscape images to portrait without cropping them."""
    if img.size[0] > img.size[1]:
        return img.rotate(90, expand=True)
    return img


def fill_page(img: Image.Image, page_size: tuple[int, int]) -> Image.Image:
    """Turn ``img`` to the page's orientation and scale it to fill the page.

    A landscape image on a portrait page (or the reverse) is rotated 90
    degrees counter-clockwise (as :func:`orient_portrait` does). The aspect
    ratio is kept: the image is scaled to the largest size that fits and
    centered on a white page of ``page_size`` pixels. An image that is
    already the page size is returned unscaled, pixel for pixel.
    """
    page_w, page_h = page_size
    if page_w <= 0 or page_h <= 0:
        raise ValueError(f"page has no area: {page_size}")
    if img.size[0] <= 0 or img.size[1] <= 0:
        raise ValueError(f"image has no area: {img.size}")
    if (img.size[0] > img.size[1] and page_w < page_h) or (
        img.size[0] < img.size[1] and page_w > page_h
    ):
        img = img.rotate(90, expand=True)
    if img.size == (page_w, page_h):
        return img
    scale = min(page_w / img.size[0], page_h / img.size[1])
    scaled = img.resize(
        (max(1, round(img.size[0] * scale)), max(1, round(img.size[1] * scale))),
        Image.Resampling.LANCZOS,
    )
    page = Image.new(scaled.mode, (page_w, page_h), "white")
    page.paste(scaled, ((page_w - scaled.size[0]) // 2, (page_h - scaled.size[1]) // 2))
    return page


def flatten_for_print(img: Image.Image) -> Image.Image:
    """Convert to a mode GDI DIBs accept, compositing transparency onto white."""
    if img.mode in ("1", "L", "RGB"):
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def compute_draw_rect(
    img_size: tuple[int, int],
    caps: DeviceCaps,
    scale_factor: float = PRINT_SCALE_FACTOR,
) -> tuple[int, int, int, int]:
    """Return ``(left, top, right, bottom)`` to draw a scaled, centered image.

    The image is scaled to fit the printable area (``HORZRES``/``VERTRES``)
    and centered on the physical page, matching the long-standing shippy math.
    """
    width, height = img_size
    if width <= 0 or height <= 0:
        raise ValueError(f"image has no area: {img_size}")
    if caps.horzres <= 0 or caps.vertres <= 0:
        raise ValueError(f"printer reports no printable area: {caps}")

    scale = scale_factor * min(caps.horzres / width, caps.vertres / height)
    scaled_w = int(scale * width)
    scaled_h = int(scale * height)
    left = int((caps.physical_width - scaled_w) / 2)
    top = int((caps.physical_height - scaled_h) / 2)
    return (left, top, left + scaled_w, top + scaled_h)


def describe_image(img: Image.Image) -> dict[str, object]:
    """Image facts worth logging before every print."""
    return {
        "size": list(img.size),
        "mode": img.mode,
        "format": img.format,
        "dpi": list(img.info["dpi"]) if "dpi" in img.info else None,
    }
