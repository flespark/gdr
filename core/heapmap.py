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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypedDict

from core.adapter import HeapMap, HeapMapBlock
from core.constants import GDR_HEAP_MAX_PIXEL_WIDTH, GDR_HEAP_MIN_PIXEL_WIDTH

# One visible cell per block; gaps inside the arena stay unknown.
MAX_CELLS = 16384
SCALE = 10
# NOTE: the panel reuses this grid for its canvas; the budget lives in
# core.constants so the legend shares it without a cycle.
MAX_PIXEL_WIDTH = GDR_HEAP_MAX_PIXEL_WIDTH
MAX_COLUMNS = MAX_PIXEL_WIDTH // SCALE
MIN_COLUMNS = -(-GDR_HEAP_MIN_PIXEL_WIDTH // SCALE)

BACKGROUND_RGB = (0x1E, 0x1E, 0x1E)
FREE_RGB = (0xC8, 0xC8, 0xC8)
UNKNOWN_OWNER_RGB = (0x4F, 0x8F, 0xD6)
DIVIDER_W = 2
# NOTE: past this many rows the PNG stops being navigable; --row-bytes errors.
MAX_ROWS = 4096


def divider_width(cell_px: int) -> int:
    """Block outline width for a cell size: fixed at the extremes, a fifth between."""
    if cell_px <= 9:
        return 2
    if cell_px < 20:
        return max(1, round(cell_px / 5))
    return 4


FOLD_HATCH_RGB = (0x90, 0x90, 0x90)
# Fixed palette order; owners past its length wrap around.
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

        _lines, _lanes, _per, _cw, _lw, legend_px = legend_plan(
            [(BACKGROUND_RGB, text) for text in ranking],
            columns * cell_px,
            rows * cell_px,
            max_px,
        )
        legend_units = math.ceil(legend_px / cell_px) if ranking else 0
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
    # NOTE: shared edges belong to the right/lower cell so divider lines
    # never double-draw.
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
        # NOTE: older envelopes carry no row count; assume one folded row each.
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


# ---------------------------------------------------------------------------
# Dependency-free bitmap lettering for exported legends.
# ---------------------------------------------------------------------------


# NOTE: the legend is pixels, not cells. Each font pixel renders as a 2x2
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
_LEGEND_CHROME = 36
_LEGEND_WRAP_MIN_LINES = 8
_INK_RGB = (32, 32, 32)
# NOTE: below this width even one `start: 0x..` field would shred.
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
            cut = field.rfind(" ", 0, capacity + 1)
            cut = cut if cut > 0 else capacity
            lines.append(field[:cut])
            field = field[cut:].lstrip(" ")
        current = field
    if current or not lines:
        lines.append(current)
    return lines


# NOTE: 5x7 built-in font; GDB Python has no font renderer or image library.
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
    # NOTE: a bitmap narrower than its labels widens to fit them.
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
