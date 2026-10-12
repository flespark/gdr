"""Heap occupancy bitmap for ``<rtos> heap dump`` (pure, no ``gdb`` import).

The arena is laid out row-major on an adaptive size-weighted grid. Each block
reserves at least one six-pixel cell, so even a tiny allocation stays visible.
No padding cells are drawn beyond the arena. Free blocks are grey, used blocks take their
owner's colour from a palette assigned by allocated bytes, and a used block
without owner names (no MEMTRACE) takes one neutral colour.

The PNG is encoded here with ``zlib`` because GDR has no external runtime
dependency. The same colours go into the JSON envelope so the panel draws the
identical picture and can name the owner under the pointer.
"""

from __future__ import annotations

import base64
import math
import struct
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypedDict

from gdr.adapter_api import HeapMap

# Reason: reserve at least one visible cell per block; an adaptive rectangular
# grid has no padding cells beyond the arena. Gaps inside it remain unknown.
MAX_CELLS = 16384
SCALE = 6

BACKGROUND_RGB = (0x1E, 0x1E, 0x1E)
FREE_RGB = (0xC8, 0xC8, 0xC8)
UNKNOWN_OWNER_RGB = (0x4F, 0x8F, 0xD6)
# Reason: a fixed, colour-blind-aware palette order. Owners past its length
# reuse entries; the owner name in the hover disambiguates them.
PALETTE_RGB: tuple[tuple[int, int, int], ...] = (
    (0xE1, 0x5E, 0x3A),
    (0x3A, 0xA6, 0x6C),
    (0x9B, 0x59, 0xB6),
    (0xE8, 0xB1, 0x1C),
    (0x1F, 0xB5, 0xC4),
    (0xD6, 0x4F, 0x8A),
    (0x6E, 0x8B, 0x2F),
    (0x8A, 0x6A, 0x4D),
    (0x5C, 0x6B, 0xC0),
    (0xF0, 0x7F, 0x7F),
    (0x2E, 0x8B, 0x57),
    (0xB0, 0x9F, 0x2B),
)

# Terminal inline images: iTerm2 takes one OSC; Kitty takes chunked APC frames.
_KITTY_CHUNK = 4096
ESC = "\x1b"
BEL = "\x07"


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


@dataclass(frozen=True)
class HeapPalette:
    """Colours of one dump: per owner, the free and unknown-owner colours."""

    owners: dict[str, tuple[int, int, int]]
    free: tuple[int, int, int] = FREE_RGB
    unknown: tuple[int, int, int] = UNKNOWN_OWNER_RGB

    def used_colour(self, owner: str | None) -> tuple[int, int, int]:
        if owner is None:
            return self.unknown
        return self.owners.get(owner, self.unknown)

    def as_json(self) -> dict[str, object]:
        """The palette as envelope fields: owner colours plus the two fixed ones."""
        return {
            "owners": {owner: _hex(rgb) for owner, rgb in self.owners.items()},
            "free": _hex(self.free),
            "unknown": _hex(self.unknown),
        }


def owner_bytes(heap: HeapMap) -> dict[str, int]:
    """Allocated bytes per named owner, the basis of the palette order."""
    totals: dict[str, int] = {}
    for block in heap.blocks:
        if block.used and block.owner is not None:
            totals[block.owner] = totals.get(block.owner, 0) + block.size
    return totals


def build_palette(heap: HeapMap) -> HeapPalette:
    """Assign palette colours to owners, largest allocation first.

    Ties break on the owner name so the colours are stable across refreshes.
    """
    totals = owner_bytes(heap)
    ordered = sorted(totals, key=lambda name: (-totals[name], name))
    owners = {
        name: PALETTE_RGB[index % len(PALETTE_RGB)]
        for index, name in enumerate(ordered)
    }
    return HeapPalette(owners=owners)


class GridLayout(TypedDict):
    """Serialized cell grid and block-index runs, shared with the canvas."""

    width: int
    height: int
    scale: int
    runs: list[list[int]]


def grid_layout(heap: HeapMap) -> GridLayout:
    """Address-ordered, size-weighted runs with at least one cell per span.

    Rounding is distributed by largest remainder after reserving one cell per
    block. Tiny blocks therefore cannot be overwritten by their neighbours.
    Unknown gaps are explicit runs, not invented free blocks. Width times height
    is exactly the number of assigned cells; there is no trailing background.
    """
    spans: list[tuple[int, int]] = []
    cursor = heap.base
    for index, block in sorted(
        enumerate(heap.blocks), key=lambda item: item[1].address
    ):
        low = max(block.address, heap.base, cursor)
        high = min(block.address + block.size, heap.end)
        if high <= low:
            continue
        if low > cursor:
            spans.append((low - cursor, -1))
        spans.append((high - low, index))
        cursor = high
    if cursor < heap.end:
        spans.append((heap.end - cursor, -1))
    if not spans:
        return {"width": 0, "height": 0, "scale": SCALE, "runs": []}
    arena = sum(size for size, _index in spans)
    desired = max(
        len(spans),
        min(MAX_CELLS, math.ceil(arena / min(size for size, _index in spans))),
    )
    rows = max(1, math.isqrt(desired // 4))
    columns = math.ceil(desired / rows)
    remaining = rows * columns - len(spans)
    counts = [1 + remaining * size // arena for size, _index in spans]
    extra = rows * columns - sum(counts)
    ranked = sorted(
        range(len(spans)), key=lambda i: (-(remaining * spans[i][0] % arena), i)
    )
    for index in ranked[:extra]:
        counts[index] += 1
    runs: list[list[int]] = []
    start = 0
    for count, (_size, index) in zip(counts, spans, strict=True):
        runs.append([start, start + count, index])
        start += count
    return {"width": columns, "height": rows, "scale": SCALE, "runs": runs}


def rasterize(heap: HeapMap, palette: HeapPalette) -> tuple[bytes, int, int]:
    """Paint the adaptive grid; return ``(rgb bytes, width, height)``."""
    layout = grid_layout(heap)
    columns, rows = int(layout["width"]), int(layout["height"])
    if not columns or not rows:
        return bytes(BACKGROUND_RGB) * SCALE * SCALE, SCALE, SCALE
    grid: list[tuple[int, int, int]] = [BACKGROUND_RGB] * (columns * rows)
    for start, stop, index in layout["runs"]:
        if index < 0:
            continue
        block = heap.blocks[index]
        colour = palette.free if not block.used else palette.used_colour(block.owner)
        grid[start:stop] = [colour] * (stop - start)
    lines = [
        b"".join(
            bytes(rgb) * SCALE for rgb in grid[row * columns : (row + 1) * columns]
        )
        for row in range(rows)
    ]
    return b"".join(line * SCALE for line in lines), columns * SCALE, rows * SCALE


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return (
        struct.pack(">I", len(data))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode 8-bit RGB pixels as a PNG (filter type 0 on every row)."""
    stride = width * 3
    raw = b"".join(
        b"\x00" + rgb[row * stride : (row + 1) * stride] for row in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def render_png(heap: HeapMap, palette: HeapPalette | None = None) -> bytes:
    """The heap-occupancy PNG for one heap map."""
    colours = palette if palette is not None else build_palette(heap)
    pixels, width, height = rasterize(heap, colours)
    from .bitmapfont import append_legend

    pixels, width, height = append_legend(
        pixels, width, height, legend_entries(heap, colours)
    )
    return encode_png(width, height, pixels)


def legend_entries(
    heap: HeapMap, palette: HeapPalette
) -> list[tuple[tuple[int, int, int], str]]:
    """Colour swatches and byte totals, including free and unreadable spans."""
    totals = owner_bytes(heap)
    entries = [
        (palette.owners[name], f"owner:{name}, size: {size} B")
        for name, size in totals.items()
    ]
    for used, name, colour in (
        (True, "N/A", palette.unknown),
        (False, "(free)", palette.free),
    ):
        size = sum(
            block.size
            for block in heap.blocks
            if block.used == used and (not used or block.owner is None)
        )
        if size:
            entries.append((colour, f"owner:{name}, size: {size} B"))
    if any(index < 0 for _start, _stop, index in grid_layout(heap)["runs"]):
        entries.append((BACKGROUND_RGB, "unknown/unreadable arena span"))
    if heap.truncated:
        entries.append((BACKGROUND_RGB, "incomplete block walk"))
    return entries


def output_mode(environment: Mapping[str, str]) -> str:
    """Where the bitmap goes: ``iterm``, ``kitty`` or ``file``.

    ``GDR_HEAP_IMAGE`` forces a mode. Otherwise the terminal decides: iTerm2
    sets ``TERM_PROGRAM=iTerm.app``; Kitty sets ``KITTY_WINDOW_ID``. Anything
    else (VS Code's debug console, tmux, a plain terminal) writes a file.
    """
    forced = environment.get("GDR_HEAP_IMAGE", "").strip().lower()
    if forced in ("iterm", "kitty", "file"):
        return forced
    if environment.get("TERM_PROGRAM") == "iTerm.app":
        return "iterm"
    if environment.get("KITTY_WINDOW_ID") or environment.get("TERM") == "xterm-kitty":
        return "kitty"
    return "file"


def inline_sequence(png: bytes, mode: str) -> str:
    """The escape sequence that shows *png* inline in *mode*'s terminal."""
    encoded = base64.b64encode(png).decode("ascii")
    if mode == "iterm":
        return (
            f"{ESC}]1337;File=inline=1;size={len(png)};preserveAspectRatio=1:"
            f"{encoded}{BEL}"
        )
    if mode == "kitty":
        chunks = [
            encoded[start : start + _KITTY_CHUNK]
            for start in range(0, len(encoded), _KITTY_CHUNK)
        ] or [""]
        frames: list[str] = []
        for index, chunk in enumerate(chunks):
            more = 1 if index < len(chunks) - 1 else 0
            control = "a=T,f=100," if index == 0 else ""
            frames.append(f"{ESC}_G{control}m={more};{chunk}{ESC}\\")
        return "".join(frames)
    raise ValueError(f"no inline sequence for mode {mode!r}")


def summary_lines(heap: HeapMap, palette: HeapPalette | None = None) -> list[str]:
    """Human-readable owner totals printed beside the bitmap."""
    colours = palette if palette is not None else build_palette(heap)
    used = sum(block.size for block in heap.blocks if block.used)
    free = sum(block.size for block in heap.blocks if not block.used)
    lines = [
        f"{heap.algorithm}: {len(heap.blocks)} blocks, {used} B used, "
        f"{free} B free, arena {heap.end - heap.base} B"
    ]
    totals = owner_bytes(heap)
    lines.extend(
        f"  owner:{name}  {totals[name]} B  {_hex(colours.owners[name])}"
        for name in sorted(totals, key=lambda owner: (-totals[owner], owner))
    )
    unknown = sum(
        block.size for block in heap.blocks if block.used and block.owner is None
    )
    if unknown:
        lines.append(f"  owner:N/A  {unknown} B  (no owner names in this allocator)")
    return lines


def palette_fields(heap: HeapMap) -> Iterable[tuple[str, str]]:
    """The palette as ``(owner, hex)`` pairs in palette order, for documentation tests."""
    palette = build_palette(heap)
    return [(name, _hex(rgb)) for name, rgb in palette.owners.items()]
