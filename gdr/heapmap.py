"""Heap occupancy bitmap for ``<rtos> heap dump`` (pure, no ``gdb`` import).

The arena is laid out row-major on an adaptive size-weighted grid. Each block
reserves at least one ten-pixel cell, so even a tiny allocation stays visible.
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

from gdr.adapter_api import HeapMap, HeapMapBlock
from gdr.constants import GDR_HEAP_MAX_PIXEL_WIDTH, GDR_HEAP_MIN_PIXEL_WIDTH

# Reason: reserve at least one visible cell per block; an adaptive rectangular
# grid has no padding cells beyond the arena. Gaps inside it remain unknown.
MAX_CELLS = 16384
SCALE = 10
# Reason: the bitmap stays portrait (1:2) and fits narrow consoles; the panel
# reuses the same grid so its canvas matches the PNG bitmap width. The budget
# itself lives in gdr.constants so the legend shares it without a cycle.
MAX_PIXEL_WIDTH = GDR_HEAP_MAX_PIXEL_WIDTH
MAX_COLUMNS = MAX_PIXEL_WIDTH // SCALE
# Reason: 200 pixels need 20 ten-pixel cells; small arenas draw wide and flat
# instead of a sliver no pointer can land on.
MIN_COLUMNS = -(-GDR_HEAP_MIN_PIXEL_WIDTH // SCALE)

BACKGROUND_RGB = (0x1E, 0x1E, 0x1E)
FREE_RGB = (0xC8, 0xC8, 0xC8)
UNKNOWN_OWNER_RGB = (0x4F, 0x8F, 0xD6)
# Reason: the shared edge's two pixels, owned by the right/lower cell alone
# so lines never double; left and top bars meet corner to corner.
DIVIDER_W = 2
# Reason: row-exact bitmaps tile one window per row; past this many rows the
# PNG and the canvas both stop being navigable, so the flag errors instead.
MAX_ROWS = 4096


def divider_width(cell_px: int) -> int:
    """Block outline width for a cell size: fixed at the extremes, a fifth between."""
    if cell_px <= 9:
        return 2
    if cell_px < 20:
        return max(1, round(cell_px / 5))
    return 4


# Reason: fold hatches are 2-pixel 45-degree stripes, crisp at any cell size
# where a soft shadow blurred into mud on low-density exports.
FOLD_HATCH_RGB = (0x90, 0x90, 0x90)
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


class FoldRecord(TypedDict):
    """One collapsed unused range: hatched bands replacing grid rows."""

    row: int
    rows: int
    height: int
    bytes: int
    kind: str
    start: int
    end: int


class GridLayout(TypedDict):
    """Serialized cell grid and block-index runs, shared with the canvas."""

    width: int
    height: int
    scale: int
    runs: list[list[int]]
    folds: list[FoldRecord]


def _distribute(
    sizes: list[int],
    arena: int,
    cell_px: int = SCALE,
    max_px: int = MAX_PIXEL_WIDTH,
) -> tuple[int, int, list[int]]:
    """Portrait 1:2 rows/columns plus largest-remainder counts (exact fill)."""
    max_columns = max(1, max_px // cell_px)
    min_columns = max(1, math.ceil(min(GDR_HEAP_MIN_PIXEL_WIDTH, max_px) / cell_px))
    desired = max(len(sizes), min(MAX_CELLS, math.ceil(arena / min(sizes))))
    rows = max(1, math.isqrt(desired * 2))
    columns = math.ceil(desired / rows)
    if columns > max_columns:
        columns = max_columns
        rows = math.ceil(desired / columns)
    if columns < min_columns:
        columns = min_columns
        rows = max(1, math.ceil(desired / columns))
    counts = _split_cells(sizes, arena, rows * columns, reserved=True)
    return rows, columns, counts


def _split_cells(
    sizes: list[int], arena: int, total: int, *, reserved: bool
) -> list[int]:
    """Largest-remainder counts filling exactly *total* cells.

    With reservation every span keeps one cell (callers guarantee
    ``total >= len(sizes)``); without it tiny pieces may draw none.
    """
    if reserved:
        remaining = total - len(sizes)
        counts = [1 + remaining * size // arena for size in sizes]
        key = lambda i: remaining * sizes[i] % arena  # noqa: E731
    else:
        counts = [total * size // arena for size in sizes]
        key = lambda i: total * sizes[i] % arena  # noqa: E731
    extra = total - sum(counts)
    ranked = sorted(range(len(sizes)), key=lambda i: (-key(i), i))
    for index in ranked[:extra]:
        counts[index] += 1
    return counts


def _row_exact_layout(
    heap: HeapMap,
    spans: list[tuple[int, int, int]],
    cell_px: int,
    max_px: int,
    row_bytes: int,
) -> GridLayout:
    """Tile every row window with exactly ``row_bytes`` arena bytes."""
    arena = sum(size for _start, size, _index in spans)
    rows = max(1, math.ceil(arena / row_bytes))
    if rows > MAX_ROWS:
        raise ValueError(
            f"--row-bytes {row_bytes} needs {rows} rows for a {arena}-byte arena, "
            f"capped at {MAX_ROWS}; use a larger row size"
        )
    _rows_d, columns, _counts_d = _distribute(
        [size for _start, size, _index in spans], arena, cell_px, max_px
    )
    runs: list[list[int]] = []
    start = 0
    for window in range(rows):
        low = heap.base + window * row_bytes
        high = min(low + row_bytes, heap.end)
        pieces = [
            (overlap, index)
            for (span_start, size, index) in spans
            for overlap in [min(span_start + size, high) - max(span_start, low)]
            if overlap > 0
        ]
        if not pieces:
            continue
        window_arena = sum(size for size, _index in pieces)
        counts = _split_cells(
            [size for size, _index in pieces],
            window_arena,
            columns,
            reserved=len(pieces) <= columns,
        )
        for count, (_size, index) in zip(counts, pieces, strict=True):
            stop = start + count
            if count:
                runs.append([start, stop, index])
            start = stop
    return {
        "width": columns,
        "height": rows,
        "scale": cell_px,
        "runs": runs,
        "folds": [],
    }


def grid_layout(
    heap: HeapMap,
    palette: HeapPalette | None = None,
    cell_px: int = SCALE,
    max_px: int = MAX_PIXEL_WIDTH,
    row_bytes: int | None = None,
) -> GridLayout:
    """Address-ordered, size-weighted runs with at least one cell per span.

    Rounding is distributed by largest remainder after reserving one cell per
    block. Tiny blocks therefore cannot be overwritten by their neighbours.
    Unknown gaps are explicit runs, not invented free blocks. Width times height
    is exactly the number of assigned cells; there is no trailing background.

    A grid row folds only when every cell in it is unused; a partial head
    row stays painted and folding starts from the second row, so dragging
    the width grows and shrinks the folded set row by row instead of
    popping whole spans. ``folds`` records each contiguous folded group
    with its row count, bytes and address span, while ``runs`` tile every
    cell. Each group paints as one band however many rows it covers.
    Fold bands absorb spare units until the bitmap reaches the legend
    height, so the two stay aligned; without folds the bitmap keeps its
    exact-fill height.

    With ``row_bytes`` every row covers exactly that many arena bytes:
    spans are cut at row windows, each window tiles its own cells, and
    folding is off (a collapsed range would break row-exactness).
    """
    spans: list[tuple[int, int, int]] = []
    cursor = heap.base
    for index, block in sorted(
        enumerate(heap.blocks), key=lambda item: item[1].address
    ):
        low = max(block.address, heap.base, cursor)
        high = min(block.address + block.size, heap.end)
        if high <= low:
            continue
        if low > cursor:
            spans.append((cursor, low - cursor, -1))
        spans.append((low, high - low, index))
        cursor = high
    if cursor < heap.end:
        spans.append((cursor, heap.end - cursor, -1))
    if not spans:
        return {"width": 0, "height": 0, "scale": cell_px, "runs": [], "folds": []}
    if row_bytes is not None:
        return _row_exact_layout(heap, spans, cell_px, max_px, row_bytes)
    arena = sum(size for _start, size, _index in spans)
    rows, columns, counts = _distribute(
        [size for _start, size, _index in spans], arena, cell_px, max_px
    )
    runs: list[list[int]] = []
    start = 0
    for count, (_span_start, _size, index) in zip(counts, spans, strict=True):
        runs.append([start, start + count, index])
        start += count
    # Per-cell span position and ordinal, for row coverage and fold ranges.
    cell_span = [-1] * (rows * columns)
    cell_ord = [0] * (rows * columns)
    cursor_cells = 0
    for position, count in enumerate(counts):
        for ordinal in range(count):
            cell_span[cursor_cells] = position
            cell_ord[cursor_cells] = ordinal
            cursor_cells += 1

    def _unused(cell: int) -> bool:
        index = spans[cell_span[cell]][2]
        return index < 0 or not heap.blocks[index].used

    folded = [
        row
        for row in range(rows)
        if all(_unused(cell) for cell in range(row * columns, (row + 1) * columns))
    ]
    if len(folded) == rows:
        # Reason: an all-unused arena has nothing to emphasize; folding every
        # row would leave an empty grid.
        folded = []
    groups: list[list[int]] = []
    for row in folded:
        if groups and row == groups[-1][-1] + 1:
            groups[-1].append(row)
        else:
            groups.append([row])
    folds: list[FoldRecord] = []
    for group in groups:
        total = 0.0
        low = -1.0
        high = -1.0
        seen_gap = False
        seen_free = False
        for row in group:
            for cell in range(row * columns, (row + 1) * columns):
                position = cell_span[cell]
                span_start, size, index = spans[position]
                ordinal = cell_ord[cell]
                share = size / counts[position]
                total += share
                first = span_start + ordinal * share
                low = first if low < 0 else min(low, first)
                high = max(high, first + share)
                if index < 0:
                    seen_gap = True
                else:
                    seen_free = True
        kind = (
            "mixed" if seen_gap and seen_free else ("unknown" if seen_gap else "free")
        )
        folds.append(
            {
                "row": group[0],
                "rows": len(group),
                "height": 1,
                "bytes": round(total),
                "kind": kind,
                "start": round(low),
                "end": round(high),
            }
        )
    if folds:
        colours = palette if palette is not None else build_palette(heap)
        ranking = [text for _colour, text in _ranking_rows(heap, colours)]
        unknown_kept = any(index < 0 for _start, _stop, index in runs)
        unknown_folded = any(fold["kind"] != "free" for fold in folds)
        if unknown_kept or unknown_folded:
            ranking.append("unknown/unreadable arena span")
        if heap.truncated:
            ranking.append("incomplete block walk")
        from .bitmapfont import legend_plan

        _lines, _lanes, _per, _cw, _lw, legend_px = legend_plan(
            [(BACKGROUND_RGB, text) for text in ranking],
            columns * cell_px,
            rows * cell_px,
            max_px,
        )
        # Reason: the floor counts only when a legend exists to align with;
        # an empty ranking leaves the exact-fill height alone.
        legend_units = math.ceil(legend_px / cell_px) if ranking else 0
        # Reason: each folded group paints as one band, so the base is the
        # kept rows plus one unit per group.
        extra = max(rows - len(folded) + len(folds), legend_units) - (
            rows - len(folded) + len(folds)
        )
        for i in range(extra):
            folds[i % len(folds)]["height"] += 1
    return {
        "width": columns,
        "height": rows,
        "scale": cell_px,
        "runs": runs,
        "folds": folds,
    }


def rasterize(
    heap: HeapMap, palette: HeapPalette, layout: GridLayout | None = None
) -> tuple[bytes, int, int]:
    """Paint the adaptive grid; return ``(rgb bytes, width, height)``."""
    layout = layout if layout is not None else grid_layout(heap)
    columns, rows = int(layout["width"]), int(layout["height"])
    scale = int(layout["scale"])
    if not columns or not rows:
        return bytes(BACKGROUND_RGB) * scale * scale, scale, scale
    folds = sorted(layout["folds"], key=lambda fold: int(fold["row"]))
    cells = columns * rows
    colours: list[tuple[int, int, int]] = [BACKGROUND_RGB] * cells
    owners: list[int] = [-1] * cells
    for start, stop, index in layout["runs"]:
        if index < 0:
            continue
        block = heap.blocks[index]
        colour = palette.free if not block.used else palette.used_colour(block.owner)
        colours[start:stop] = [colour] * (stop - start)
        owners[start:stop] = [index] * (stop - start)
    stride = columns * scale
    buf = bytearray(stride * rows * scale * 3)
    for cell, colour in enumerate(colours):
        top = (cell // columns) * scale
        left = (cell % columns) * scale
        for py in range(scale):
            off = ((top + py) * stride + left) * 3
            buf[off : off + scale * 3] = bytes(colour) * scale
    # Reason: each shared edge is owned by its right/lower cell alone, so the
    # line keeps the divider width; left and top bars meet corner to corner,
    # which keeps every right angle closed. Canvas edges and gaps draw nothing.
    bar = bytes(BACKGROUND_RGB)
    rule = divider_width(scale)
    for cell, index in enumerate(owners):
        if index < 0:
            continue
        row, column = divmod(cell, columns)
        if column > 0:
            left = owners[cell - 1]
            if left >= 0 and left != index:
                for py in range(scale):
                    off = ((row * scale + py) * stride + column * scale) * 3
                    buf[off : off + rule * 3] = bar * rule
        if row > 0:
            up = owners[cell - columns]
            if up >= 0 and up != index:
                for py in range(rule):
                    off = ((row * scale + py) * stride + column * scale) * 3
                    buf[off : off + scale * 3] = bar * scale

    # Reason: a wrapping block's inner corner joins a left bar from above
    # with a top bar from the left; without this pixel the two meet only
    # diagonally and the right angle reads open.
    def _has_left(r: int, c: int) -> bool:
        if not (0 <= r < rows and 0 <= c < columns) or c == 0:
            return False
        index = owners[r * columns + c]
        if index < 0:
            return False
        left = owners[r * columns + c - 1]
        return left >= 0 and left != index

    def _has_top(r: int, c: int) -> bool:
        if not (0 <= r < rows and 0 <= c < columns) or r == 0:
            return False
        index = owners[r * columns + c]
        if index < 0:
            return False
        up = owners[(r - 1) * columns + c]
        return up >= 0 and up != index

    for r in range(rows + 1):
        for c in range(columns + 1):
            if _has_left(r - 1, c) and _has_top(r, c - 1):
                for dy in range(rule):
                    off = ((r * scale + dy) * stride + c * scale) * 3
                    buf[off : off + rule * 3] = bar * rule
    fore = [
        bytes(buf[row * scale * stride * 3 : (row + 1) * scale * stride * 3])
        for row in range(rows)
    ]
    by_first: dict[int, list[int]] = {}
    covered: set[int] = set()
    for pos, fold in enumerate(folds):
        first = int(fold["row"])
        by_first.setdefault(first, []).append(pos)
        # Reason: older envelopes carry no row count; one folded row each.
        covered.update(range(first, first + int(fold.get("rows", 1))))
    out: list[bytes] = []
    for row in range(rows):
        for pos in by_first.get(row, []):
            for _ in range(int(folds[pos]["height"])):
                out.append(_hatch_unit(columns, scale, len(out) * scale))
        if row not in covered:
            out.append(fore[row])
    height = len(out) * scale
    return b"".join(out), columns * scale, height


def _hatch_unit(columns: int, scale: int, y_abs: int) -> bytes:
    """One band row of diagonal hatch, tiled — never pixel-looped."""
    width = columns * scale
    out = []
    for py in range(scale):
        phase = (y_abs + py) % 4
        tile = b"".join(
            bytes(FOLD_HATCH_RGB if (x + phase) % 4 < 2 else BACKGROUND_RGB)
            for x in range(4)
        )
        full, rest = divmod(width, 4)
        out.append(tile * full + tile[: rest * 3])
    return b"".join(out)


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


def _block_span(blocks: list[HeapMapBlock]) -> tuple[int, int]:
    """The address span *blocks* cover, for legend rows and log lines."""
    return (
        min(block.address for block in blocks),
        max(block.address + block.size for block in blocks),
    )


def _legend_row(
    colour: tuple[int, int, int], name: str, blocks: list[HeapMapBlock]
) -> tuple[tuple[int, int, int], str]:
    """One ranking row: ``name: sizeB, start-end`` over the blocks' span."""
    size = sum(block.size for block in blocks)
    start, end = _block_span(blocks)
    return (colour, f"{name}: {size}B, {hex(start)}-{hex(end)}")


def _ranking_rows(
    heap: HeapMap, palette: HeapPalette
) -> list[tuple[tuple[int, int, int], str]]:
    """Owner/N/A/free rows with byte totals and spans, gap notes excluded."""
    by_owner: dict[str, list[HeapMapBlock]] = {}
    for block in heap.blocks:
        if block.used and block.owner is not None:
            by_owner.setdefault(block.owner, []).append(block)
    rows = [
        _legend_row(palette.owners[name], name, blocks)
        for name, blocks in by_owner.items()
    ]
    for used, name, colour in (
        (True, "N/A", palette.unknown),
        (False, "(free)", palette.free),
    ):
        selected = [
            block
            for block in heap.blocks
            if block.used == used and (not used or block.owner is None)
        ]
        if selected:
            rows.append(_legend_row(colour, name, selected))
    return rows


def render_png(
    heap: HeapMap,
    palette: HeapPalette | None = None,
    cell_px: int = SCALE,
    max_px: int = MAX_PIXEL_WIDTH,
    row_bytes: int | None = None,
) -> bytes:
    """The heap-occupancy PNG for one heap map."""
    colours = palette if palette is not None else build_palette(heap)
    layout = grid_layout(heap, colours, cell_px, max_px, row_bytes)
    pixels, width, height = rasterize(heap, colours, layout)
    from .bitmapfont import append_endpoint_labels, append_legend

    pixels, width, height = append_legend(
        pixels,
        width,
        height,
        legend_entries(heap, colours),
        used_height=int(layout["height"]) * cell_px,
        max_px=max_px,
    )
    pixels, width, height = append_endpoint_labels(
        pixels, width, height, f"start: {hex(heap.base)}", f"end: {hex(heap.end)}"
    )
    return encode_png(width, height, pixels)


def legend_entries(
    heap: HeapMap, palette: HeapPalette
) -> list[tuple[tuple[int, int, int], str]]:
    """Colour swatches with byte totals and address spans, plus gap notes."""
    entries = _ranking_rows(heap, palette)
    layout = grid_layout(heap, palette)
    unknown_kept = any(index < 0 for _start, _stop, index in layout["runs"])
    unknown_folded = any(fold["kind"] != "free" for fold in layout["folds"])
    if unknown_kept or unknown_folded:
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
    for name in sorted(totals, key=lambda owner: (-totals[owner], owner)):
        owned = [block for block in heap.blocks if block.used and block.owner == name]
        start, end = _block_span(owned)
        lines.append(
            f"  {_hex(colours.owners[name])} owner: {name}, size: {totals[name]} B, "
            f"start: {hex(start)}, end: {hex(end)}"
        )
    unowned = [block for block in heap.blocks if block.used and block.owner is None]
    if unowned:
        start, end = _block_span(unowned)
        size = sum(block.size for block in unowned)
        lines.append(
            f"  {_hex(colours.unknown)} owner: N/A, size: {size} B, "
            f"start: {hex(start)}, end: {hex(end)} "
            "(no owner names in this allocator)"
        )
    free_blocks = [block for block in heap.blocks if not block.used]
    if free_blocks:
        start, end = _block_span(free_blocks)
        lines.append(
            f"  {_hex(colours.free)} owner: (free), size: {free} B, "
            f"start: {hex(start)}, end: {hex(end)}"
        )
    folds = grid_layout(heap, colours)["folds"]
    if folds:
        folded_bytes = sum(fold["bytes"] for fold in folds)
        lines.append(
            f"  folded: {len(folds)} regions, {folded_bytes} B in hatched bands"
        )
    return lines


def palette_fields(heap: HeapMap) -> Iterable[tuple[str, str]]:
    """The palette as ``(owner, hex)`` pairs in palette order, for documentation tests."""
    palette = build_palette(heap)
    return [(name, _hex(rgb)) for name, rgb in palette.owners.items()]
