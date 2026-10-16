"""Small dependency-free bitmap lettering for exported heap-map legends.

Unsupported glyphs are written as Unicode escape sequences, so a target name
remains distinguishable even when the GDB host has no fonts installed.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from gdr.constants import GDR_HEAP_MAX_PIXEL_WIDTH

# Reason: the legend is pixels, not cells. Each font pixel renders as a 2x2
# block on its own 12-pixel advance, so the text grid never snaps to the
# bitmap's six-pixel cells; those only set the on-screen cell size and the
# hover granularity.
_FONT_SCALE = 2
_CHAR_ADVANCE = 12
_LINE_STEP = 20
_SWATCH_W = 12
_SWATCH_H = 14
_LEGEND_PAD = 8
_BITMAP_LEGEND_GAP = 12
# Reason: left pad + swatch + text offset + right pad, mirroring the
# below-the-map layout's chrome so wrapped lines keep the same measure.
_LEGEND_CHROME = 36
# Reason: wrapping only pays off past eight rows; below that one column stays
# narrower than two, which matters most for inline terminal images that scale
# down to the terminal width but scroll vertically for free.
_LEGEND_WRAP_MIN_LINES = 8
# Reason: endpoint labels use the same ink as the legend text so the
# annotations read as one layer beside the dark bitmap.
_INK_RGB = (32, 32, 32)
# Reason: below this a wrapped line shreds even one `start: 0x..` field; the
# total may then exceed the shared budget on very wide bitmaps, which beats
# an unreadable ranking.
_MIN_LINE_CHARS = 24


def _wrap_label(text: str, capacity: int) -> list[str]:
    """Fold *text* to lines: break after commas first, hard-cut inside fields."""
    lines: list[str] = []
    current = ""
    for field in text.split(", "):
        candidate = field if not current else f"{current}, {field}"
        if len(candidate) <= capacity:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(field) > capacity:
            # Reason: a word break keeps a comma-less note readable where a
            # hard cut would strand one word's tail on the next line.
            cut = field.rfind(" ", 0, capacity + 1)
            cut = cut if cut > 0 else capacity
            lines.append(field[:cut])
            field = field[cut:].lstrip(" ")
        current = field
    if current or not lines:
        lines.append(current)
    return lines


# Five columns by seven rows. Reason: GDB Python has neither a font renderer nor
# an image library; a tiny built-in font keeps PNG export host-independent.
_PATTERNS = {
    "A": "01110/10001/10001/11111/10001/10001/10001",
    "B": "11110/10001/10001/11110/10001/10001/11110",
    "C": "01111/10000/10000/10000/10000/10000/01111",
    "D": "11110/10001/10001/10001/10001/10001/11110",
    "E": "11111/10000/10000/11110/10000/10000/11111",
    "F": "11111/10000/10000/11110/10000/10000/10000",
    "G": "01111/10000/10000/10111/10001/10001/01111",
    "H": "10001/10001/10001/11111/10001/10001/10001",
    "I": "01110/00100/00100/00100/00100/00100/01110",
    "J": "00111/00010/00010/00010/10010/10010/01100",
    "K": "10001/10010/10100/11000/10100/10010/10001",
    "L": "10000/10000/10000/10000/10000/10000/11111",
    "M": "10001/11011/10101/10101/10001/10001/10001",
    "N": "10001/11001/10101/10011/10001/10001/10001",
    "O": "01110/10001/10001/10001/10001/10001/01110",
    "P": "11110/10001/10001/11110/10000/10000/10000",
    "Q": "01110/10001/10001/10001/10101/10010/01101",
    "R": "11110/10001/10001/11110/10100/10010/10001",
    "S": "01111/10000/10000/01110/00001/00001/11110",
    "T": "11111/00100/00100/00100/00100/00100/00100",
    "U": "10001/10001/10001/10001/10001/10001/01110",
    "V": "10001/10001/10001/10001/10001/01010/00100",
    "W": "10001/10001/10001/10101/10101/10101/01010",
    "X": "10001/10001/01010/00100/01010/10001/10001",
    "Y": "10001/10001/01010/00100/00100/00100/00100",
    "Z": "11111/00001/00010/00100/01000/10000/11111",
    "a": "00000/00000/01110/00001/01111/10001/01111",
    "b": "10000/10000/10110/11001/10001/10001/11110",
    "c": "00000/00000/01111/10000/10000/10000/01111",
    "d": "00001/00001/01101/10011/10001/10001/01111",
    "e": "00000/00000/01110/10001/11111/10000/01111",
    "f": "00110/01001/01000/11100/01000/01000/01000",
    "g": "00000/01111/10001/10001/01111/00001/01110",
    "h": "10000/10000/10110/11001/10001/10001/10001",
    "i": "00100/00000/01100/00100/00100/00100/01110",
    "j": "00010/00000/00110/00010/00010/10010/01100",
    "k": "10000/10000/10010/10100/11000/10100/10010",
    "l": "01100/00100/00100/00100/00100/00100/01110",
    "m": "00000/00000/11010/10101/10101/10101/10101",
    "n": "00000/00000/10110/11001/10001/10001/10001",
    "o": "00000/00000/01110/10001/10001/10001/01110",
    "p": "00000/11110/10001/10001/11110/10000/10000",
    "q": "00000/01111/10001/10001/01111/00001/00001",
    "r": "00000/00000/10110/11001/10000/10000/10000",
    "s": "00000/00000/01111/10000/01110/00001/11110",
    "t": "01000/01000/11100/01000/01000/01001/00110",
    "u": "00000/00000/10001/10001/10001/10011/01101",
    "v": "00000/00000/10001/10001/10001/01010/00100",
    "w": "00000/00000/10001/10001/10101/10101/01010",
    "x": "00000/00000/10001/01010/00100/01010/10001",
    "y": "00000/10001/10001/10001/01111/00001/01110",
    "z": "00000/00000/11111/00010/00100/01000/11111",
    "0": "01110/10001/10011/10101/11001/10001/01110",
    "1": "00100/01100/00100/00100/00100/00100/01110",
    "2": "01110/10001/00001/00010/00100/01000/11111",
    "3": "11110/00001/00001/01110/00001/00001/11110",
    "4": "00010/00110/01010/10010/11111/00010/00010",
    "5": "11111/10000/10000/11110/00001/00001/11110",
    "6": "01110/10000/10000/11110/10001/10001/01110",
    "7": "11111/00001/00010/00100/01000/01000/01000",
    "8": "01110/10001/10001/01110/10001/10001/01110",
    "9": "01110/10001/10001/01111/00001/00001/01110",
    " ": "00000/00000/00000/00000/00000/00000/00000",
    ":": "00000/00100/00100/00000/00100/00100/00000",
    ",": "00000/00000/00000/00000/00110/00100/01000",
    ".": "00000/00000/00000/00000/00000/00110/00110",
    "-": "00000/00000/00000/11111/00000/00000/00000",
    "_": "00000/00000/00000/00000/00000/00000/11111",
    "/": "00001/00010/00010/00100/01000/01000/10000",
    "\\": "10000/01000/01000/00100/00010/00010/00001",
    "(": "00010/00100/01000/01000/01000/00100/00010",
    ")": "01000/00100/00010/00010/00010/00100/01000",
    "?": "01110/10001/00001/00010/00100/00000/00100",
}


def printable(text: str) -> str:
    """Lossless font-safe text: escape unsupported glyphs and control bytes."""
    return "".join(
        char if char in _PATTERNS else f"\\u{ord(char):04x}" for char in text
    )


def _draw_text(
    rectangle: Callable[[int, int, int, int, tuple[int, int, int]], None],
    x: int,
    y: int,
    text: str,
) -> None:
    """Paint 5x7 glyphs via the caller's ``rectangle`` closure."""
    for column, char in enumerate(text):
        for row, pattern in enumerate(_PATTERNS[char].split("/")):
            for bit, value in enumerate(pattern):
                if value == "1":
                    rectangle(
                        x + column * _CHAR_ADVANCE + bit * _FONT_SCALE,
                        y + row * _FONT_SCALE,
                        _FONT_SCALE,
                        _FONT_SCALE,
                        _INK_RGB,
                    )


# Reason: endpoint strips annotate the bitmap edges at any size, where a
# side gutter's labels would overlap each other on a short map.
_STRIP_PAD = 4
_STRIP_H = _STRIP_PAD + 7 * _FONT_SCALE + _STRIP_PAD


def append_endpoint_labels(
    rgb: bytes,
    width: int,
    height: int,
    start_text: str,
    end_text: str,
) -> tuple[bytes, int, int]:
    """Used-area start/end labels in full-width strips above and below."""
    if height <= 0:
        return rgb, width, height
    start, end = printable(start_text), printable(end_text)
    # Reason: addresses must stay fully readable; a bitmap narrower than its
    # own labels widens to fit them instead of spilling glyphs into rows.
    out_width = max(
        width, _LEGEND_PAD + max(len(start), len(end)) * _CHAR_ADVANCE + _LEGEND_PAD
    )
    out_height = height + 2 * _STRIP_H
    pixels = bytearray(b"\xff\xff\xff" * out_width * out_height)
    for row in range(height):
        offset = (row + _STRIP_H) * out_width * 3
        pixels[offset : offset + width * 3] = rgb[
            row * width * 3 : (row + 1) * width * 3
        ]

    def rectangle(x: int, y: int, w: int, h: int, colour: tuple[int, int, int]) -> None:
        for row in range(y, y + h):
            start_px = (row * out_width + x) * 3
            pixels[start_px : start_px + w * 3] = bytes(colour) * w

    _draw_text(rectangle, _LEGEND_PAD, _STRIP_PAD, start)
    _draw_text(rectangle, _LEGEND_PAD, _STRIP_H + height + _STRIP_PAD, end)
    return bytes(pixels), out_width, out_height


def legend_capacity(bitmap_width: int, max_px: int) -> int:
    """Characters per legend line within what is left of the pixel budget."""
    # Reason: wrap by character count within what is left of the shared pixel
    # budget after the bitmap and the gap, so the total stays bounded no
    # matter how wide the grid is.
    return min(
        max(1, (max_px - _LEGEND_CHROME) // _CHAR_ADVANCE),
        max(
            _MIN_LINE_CHARS,
            (max_px - bitmap_width - _BITMAP_LEGEND_GAP - _LEGEND_CHROME)
            // _CHAR_ADVANCE,
        ),
    )


def legend_plan(
    entries: list[tuple[tuple[int, int, int], str]],
    bitmap_width: int,
    used_height: int,
    max_px: int = GDR_HEAP_MAX_PIXEL_WIDTH,
) -> tuple[list[tuple[tuple[int, int, int], str]], int, int, int, int, int]:
    """Wrapped lines, lanes, per-lane count, column/legend width and height in pixels.

    The ranking flows top-to-bottom in vertical columns; once one column would
    exceed twice the used height it wraps into a second list on the right.
    Shared by the painter and the grid planner so the fold floor matches the
    legend the PNG actually draws.
    """
    capacity = legend_capacity(bitmap_width, max_px)
    lines = [
        (colour, part)
        for colour, text in [(colour, printable(text)) for colour, text in entries]
        for part in _wrap_label(text, capacity)
    ]
    # Reason: a ranking taller than twice the used height wraps once into a
    # second vertical list on the right; two columns cap the width while the
    # height may still exceed the budget on pathological owner counts.
    budget = 2 * used_height
    one_column = _LEGEND_PAD + 4 + len(lines) * _LINE_STEP
    columns = 2 if len(lines) > _LEGEND_WRAP_MIN_LINES and one_column > budget else 1
    per_column = math.ceil(len(lines) / columns) if lines else 0
    column_width = (
        _LEGEND_CHROME + max(len(text) for _, text in lines) * _CHAR_ADVANCE
        if lines
        else 0
    )
    legend_width = columns * column_width + (columns - 1) * _BITMAP_LEGEND_GAP
    legend_height = _LEGEND_PAD + 4 + per_column * _LINE_STEP
    return lines, columns, per_column, column_width, legend_width, legend_height


def append_legend(
    rgb: bytes,
    width: int,
    height: int,
    entries: list[tuple[tuple[int, int, int], str]],
    used_height: int | None = None,
    max_px: int = GDR_HEAP_MAX_PIXEL_WIDTH,
) -> tuple[bytes, int, int]:
    """Add readable two-times-scaled text and 12-pixel colour swatches right.

    ``used_height`` is the painted used-area height (fold bands excluded);
    it defaults to the full height for direct callers.
    """
    if not entries:
        return rgb, width, height
    lines, _columns, per_column, column_width, legend_width, legend_height = (
        legend_plan(
            entries, width, height if used_height is None else used_height, max_px
        )
    )
    out_width = width + _BITMAP_LEGEND_GAP + legend_width
    out_height = max(height, legend_height)
    pixels = bytearray(b"\xff\xff\xff" * out_width * out_height)
    for row in range(height):
        offset = row * out_width * 3
        pixels[offset : offset + width * 3] = rgb[
            row * width * 3 : (row + 1) * width * 3
        ]

    def rectangle(x: int, y: int, w: int, h: int, colour: tuple[int, int, int]) -> None:
        for row in range(y, y + h):
            start = (row * out_width + x) * 3
            pixels[start : start + w * 3] = bytes(colour) * w

    for index, (colour, text) in enumerate(lines):
        lane, rank = divmod(index, per_column)
        base_x = width + _BITMAP_LEGEND_GAP + lane * (column_width + _BITMAP_LEGEND_GAP)
        y = _LEGEND_PAD + rank * _LINE_STEP
        rectangle(base_x + _LEGEND_PAD, y, _SWATCH_W, _SWATCH_H, colour)
        _draw_text(rectangle, base_x + _LEGEND_PAD + 20, y, text)
    return bytes(pixels), out_width, out_height
