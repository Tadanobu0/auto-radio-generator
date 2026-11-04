"""Contact sheet of image candidates (021): one picture with every candidate and its number.

The Mac side (radio_director) looks only at this picture to pick a candidate, so the tiles
are kept large: the long side of the sheet is about 2048 px (3 x 3 tiles of 16:9 -> 2048 x 1166, 672 x 378 per tile),
and each tile has its number drawn big in the top-left corner.
"""
import math
from pathlib import Path
from typing import Optional, Sequence, Union

from PIL import Image, ImageDraw, ImageFont, ImageOps

DEFAULT_LONG_SIDE = 2048
DEFAULT_ASPECT = 16 / 9
GAP_PX = 8
JPEG_QUALITY = 90
BACKGROUND = (24, 24, 24)
FAILED_TILE = (70, 70, 70)
LABEL_BOX = (0, 0, 0)
LABEL_TEXT = (255, 255, 255)
LABEL_RATIO = 0.28  # number height / tile height

_FONT_CANDIDATES = (
    "arialbd.ttf",
    "Arial Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arial.ttf",
    "DejaVuSans.ttf",
)

ImagePath = Optional[Union[str, Path]]


def grid_shape(count: int) -> tuple[int, int]:
    """(columns, rows): ceil(sqrt(N)) columns, as many rows as needed."""
    count = max(1, int(count))
    cols = math.ceil(math.sqrt(count))
    return cols, math.ceil(count / cols)


def sheet_layout(count: int, aspect: float = DEFAULT_ASPECT, long_side: int = DEFAULT_LONG_SIDE) -> dict:
    """Sizes of the sheet: cols, rows, tile_w, tile_h, width, height (the long side is `long_side`)."""
    cols, rows = grid_shape(count)
    aspect = aspect if aspect and aspect > 0 else DEFAULT_ASPECT
    if cols * aspect >= rows:  # wider than tall: the width is the long side
        tile_w = (long_side - GAP_PX * (cols + 1)) // cols
        tile_h = max(1, round(tile_w / aspect))
    else:
        tile_h = (long_side - GAP_PX * (rows + 1)) // rows
        tile_w = max(1, round(tile_h * aspect))
    return {
        "cols": cols,
        "rows": rows,
        "tile_w": tile_w,
        "tile_h": tile_h,
        "width": cols * tile_w + GAP_PX * (cols + 1),
        "height": rows * tile_h + GAP_PX * (rows + 1),
    }


def make_contact_sheet(
    items: Sequence[tuple[int, ImagePath]],
    out_path: Union[str, Path],
    long_side: int = DEFAULT_LONG_SIDE,
) -> Path:
    """Write the sheet of (number, image path) items to out_path (JPEG) and return the path.

    An item without an image, or one that cannot be opened, gets a grey tile with "failed".
    Raises ValueError when there is no item at all.
    """
    items = list(items)
    if not items:
        raise ValueError("no candidates for the contact sheet")
    images = [_open(path) for _, path in items]
    first = next((img for img in images if img is not None), None)
    aspect = first.width / first.height if first is not None and first.height else DEFAULT_ASPECT
    layout = sheet_layout(len(items), aspect, long_side)
    tile_w, tile_h = layout["tile_w"], layout["tile_h"]

    sheet = Image.new("RGB", (layout["width"], layout["height"]), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    label_px = max(12, int(tile_h * LABEL_RATIO))
    font = _label_font(label_px)
    small_font = _label_font(max(10, label_px // 2))
    for k, ((number, _), image) in enumerate(zip(items, images)):
        col, row = k % layout["cols"], k // layout["cols"]
        x = GAP_PX + col * (tile_w + GAP_PX)
        y = GAP_PX + row * (tile_h + GAP_PX)
        if image is not None:
            tile = ImageOps.fit(image.convert("RGB"), (tile_w, tile_h), Image.LANCZOS)
            sheet.paste(tile, (x, y))
        else:
            draw.rectangle([x, y, x + tile_w - 1, y + tile_h - 1], fill=FAILED_TILE)
            _draw_text(sheet, draw, "failed", (x + tile_w // 2, y + tile_h // 2), small_font,
                       max(10, label_px // 2), anchor_center=True)
        _draw_label(sheet, draw, str(number), x, y, font, label_px)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "JPEG", quality=JPEG_QUALITY)
    for image in images:
        if image is not None:
            image.close()
    return out_path


def _open(path: ImagePath) -> Optional[Image.Image]:
    if not path:
        return None
    try:
        image = Image.open(str(path))
        image.load()
        return image
    except Exception:
        return None


def _label_font(size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except Exception:
        return None  # the small bitmap font is scaled up instead (_draw_text)


def _draw_label(sheet: Image.Image, draw: ImageDraw.ImageDraw, text: str, x: int, y: int, font, px: int) -> None:
    """The candidate number on a black box in the top-left corner of the tile."""
    pad = max(4, px // 6)
    w, h = _text_size(draw, text, font, px)
    draw.rectangle([x, y, x + w + 2 * pad, y + h + 2 * pad], fill=LABEL_BOX)
    _draw_text(sheet, draw, text, (x + pad, y + pad), font, px)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font, px: int) -> tuple[int, int]:
    if font is None:
        base = ImageFont.load_default()
        left, top, right, bottom = draw.textbbox((0, 0), text, font=base)
        scale = px / max(1, bottom - top)
        return int((right - left) * scale), px
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _draw_text(
    sheet: Image.Image, draw: ImageDraw.ImageDraw, text: str, pos: tuple[int, int], font, px: int,
    anchor_center: bool = False,
) -> None:
    w, h = _text_size(draw, text, font, px)
    x, y = pos
    if anchor_center:
        x, y = x - w // 2, y - h // 2
    if font is not None:
        left, top, _, _ = draw.textbbox((0, 0), text, font=font)
        draw.text((x - left, y - top), text, font=font, fill=LABEL_TEXT)
        return
    # no scalable font: draw with the bitmap font and scale it up
    base = ImageFont.load_default()
    left, top, right, bottom = draw.textbbox((0, 0), text, font=base)
    small = Image.new("L", (max(1, right - left), max(1, bottom - top)), 0)
    ImageDraw.Draw(small).text((-left, -top), text, font=base, fill=255)
    big = small.resize((max(1, w), max(1, h)), Image.NEAREST)
    sheet.paste(Image.new("RGB", big.size, LABEL_TEXT), (x, y), big)
