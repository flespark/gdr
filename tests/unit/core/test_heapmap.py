"""Unit tests for the heap-occupancy bitmap (pure: no GDB session needed)."""

from __future__ import annotations

import base64
import random
import struct
import zlib

from gdr import heapmap
from gdr.adapter_api import HeapMap, HeapMapBlock


def _map(blocks, *, base=0, end=65536, owners=True) -> HeapMap:
    return HeapMap(
        algorithm="small_mem", base=base, end=end, blocks=list(blocks), owners=owners
    )


def _pixel(rgb: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * width + x) * 3
    return (rgb[offset], rgb[offset + 1], rgb[offset + 2])


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    out = []
    position = 8
    while position < len(png):
        (length,) = struct.unpack(">I", png[position : position + 4])
        kind = png[position + 4 : position + 8]
        data = png[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(
            ">I", png[position + 8 + length : position + 12 + length]
        )
        assert zlib.crc32(kind + data) & 0xFFFFFFFF == crc, "bad chunk CRC"
        out.append((kind, data))
        position += 12 + length
    return out


def test_png_has_a_valid_header_and_zlib_stream():
    """The encoder writes IHDR, one zlib IDAT with a filter byte per row, and IEND."""
    width, height = 3, 2
    rgb = bytes(range(width * height * 3))

    png = heapmap.encode_png(width, height, rgb)

    chunks = _chunks(png)
    assert [kind for kind, _data in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    ihdr = struct.unpack(">IIBBBBB", chunks[0][1])
    assert ihdr == (width, height, 8, 2, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == height * (1 + width * 3)
    assert raw[0] == 0 and raw[1 + width * 3] == 0


def test_free_blocks_are_grey_and_owners_take_palette_colours():
    """A block paints the cells it covers; the palette colour follows the owner."""
    heap = _map(
        [
            HeapMapBlock(0, 16, True, "main"),
            HeapMapBlock(16, 1, False),
        ],
        end=65536,
    )
    palette = heapmap.build_palette(heap)
    rgb, width, _height = heapmap.rasterize(heap, palette)

    main = palette.owners["main"]
    assert _pixel(rgb, width, 2, 2) == main
    grid = heapmap.grid_layout(heap)
    for start, _stop, index in grid["runs"]:
        # Reason: run corners carry the outline now; the interior proves colour.
        x = start % grid["width"] * heapmap.SCALE + 2
        y = start // grid["width"] * heapmap.SCALE + 2
        expected = (main, heapmap.FREE_RGB, heapmap.BACKGROUND_RGB)[
            index if index >= 0 else 2
        ]
        assert _pixel(rgb, width, x, y) == expected


def test_a_tiny_block_still_paints_one_cell():
    """A block smaller than one cell is at least one cell wide, so it stays visible."""
    heap = _map([HeapMapBlock(0, 1, True, None)], end=65536 * 1000)
    rgb, width, height = heapmap.rasterize(heap, heapmap.build_palette(heap))

    assert _pixel(rgb, width, 2, 2) == heapmap.UNKNOWN_OWNER_RGB
    assert _pixel(rgb, width, 0, 12) == heapmap.FOLD_HATCH_RGB
    assert _pixel(rgb, width, 4, 10) == heapmap.BACKGROUND_RGB
    grid = heapmap.grid_layout(heap)
    # Reason: rows tile every cell now; the gap stays in the runs and the
    # whole folded tail collapses into one band instead of one band per row.
    assert grid["runs"] == [[0, 1, 0], [1, 16400, -1]]
    assert grid["folds"] == [
        {
            "row": 1,
            "rows": 204,
            "height": 7,
            "bytes": 65220288,
            "kind": "unknown",
            "start": 315712,
            "end": 65536 * 1000,
        }
    ]
    assert grid["folds"][0]["start"] > 1, "the partial head row stays painted"
    assert (width, height) == (80 * heapmap.SCALE, 8 * heapmap.SCALE)


def test_narrow_arena_draws_at_least_200_pixels_wide():
    """Two blocks still span thirty-four cells: pointers can land on them."""
    heap = _map(
        [HeapMapBlock(0, 16, True, "main"), HeapMapBlock(16, 16, False)], end=32
    )
    grid = heapmap.grid_layout(heap)

    assert heapmap.MIN_COLUMNS == 20
    assert grid["width"] == heapmap.MIN_COLUMNS
    assert grid["width"] * heapmap.SCALE >= 200
    assert grid["runs"][-1][1] == grid["width"] * grid["height"]


def test_blocks_outside_the_arena_are_clipped_not_wrapped():
    """A corrupt chain can name addresses past the arena; they clip to its edges."""
    heap = _map([HeapMapBlock(0x100000, 64, True, "main")], base=0, end=1024)
    rgb, width, height = heapmap.rasterize(heap, heapmap.build_palette(heap))

    assert len(rgb) == width * height * 3
    assert _pixel(rgb, width, width - 1, height - 1) == heapmap.BACKGROUND_RGB


def test_palette_orders_owners_by_allocated_bytes_then_name():
    """The largest owner takes the first palette colour; ties sort by name."""
    heap = _map(
        [
            HeapMapBlock(0, 100, True, "worker"),
            HeapMapBlock(100, 300, True, "main"),
            HeapMapBlock(400, 100, True, "aio"),
        ]
    )
    palette = heapmap.build_palette(heap)

    assert palette.owners["main"] == heapmap.PALETTE_RGB[0]
    assert palette.owners["aio"] == heapmap.PALETTE_RGB[1]
    assert palette.owners["worker"] == heapmap.PALETTE_RGB[2]


def test_used_blocks_without_owner_names_take_the_neutral_colour():
    """FreeRTOS and MEMTRACE-less RT-Thread have no owner: one neutral colour."""
    heap = _map([HeapMapBlock(0, 64, True, None)], owners=False)
    palette = heapmap.build_palette(heap)

    assert palette.used_colour(None) == heapmap.UNKNOWN_OWNER_RGB
    assert palette.as_json()["unknown"] == "#4f8fd6"


def test_output_mode_follows_the_terminal_and_the_override():
    """iTerm2 and Kitty are detected from their environment; the rest writes a file."""
    assert heapmap.output_mode({"TERM_PROGRAM": "iTerm.app"}) == "iterm"
    assert heapmap.output_mode({"KITTY_WINDOW_ID": "3"}) == "kitty"
    assert heapmap.output_mode({"TERM": "xterm-kitty"}) == "kitty"
    assert heapmap.output_mode({"TERM_PROGRAM": "vscode"}) == "file"
    assert heapmap.output_mode({}) == "file"
    assert (
        heapmap.output_mode({"TERM_PROGRAM": "iTerm.app", "GDR_HEAP_IMAGE": "file"})
        == "file"
    )
    assert heapmap.output_mode({"GDR_HEAP_IMAGE": "KITTY"}) == "kitty"


def test_iterm_sequence_is_one_osc_with_the_png_inside():
    """iTerm2's inline image is one OSC 1337 File= frame carrying the base64 PNG."""
    png = heapmap.encode_png(2, 2, bytes(12))

    sequence = heapmap.inline_sequence(png, "iterm")

    head = f"\x1b]1337;File=inline=1;size={len(png)};preserveAspectRatio=1:"
    assert sequence.startswith(head)
    assert sequence.endswith("\x07")
    payload = sequence[len(head) : -1]
    assert base64.b64decode(payload) == png


def test_kitty_sequence_chunks_the_payload_and_marks_the_last_frame():
    """Kitty takes 4096-byte chunks; only the first names the format, the last ends it."""
    # Reason: incompressible pixels, so the PNG is large enough to need chunks.
    noise = random.Random(7).randbytes(96 * 96 * 3)
    png = heapmap.encode_png(96, 96, noise)
    encoded = base64.b64encode(png).decode("ascii")

    sequence = heapmap.inline_sequence(png, "kitty")

    frames = [frame for frame in sequence.split("\x1b\\") if frame]
    assert len(frames) == -(-len(encoded) // 4096)
    assert len(frames) > 1, "the payload must span several frames"
    assert frames[0].startswith("\x1b_Ga=T,f=100,m=1;")
    assert frames[-1].startswith("\x1b_Gm=0;")
    assert all(frame.startswith("\x1b_G") for frame in frames)
    rebuilt = "".join(frame.split(";", 1)[1] for frame in frames)
    assert rebuilt == encoded


def test_summary_lists_owner_totals_and_the_unowned_remainder():
    """The printed summary names every owner with its bytes and the N/A remainder."""
    heap = _map(
        [
            HeapMapBlock(0, 64, True, "main"),
            HeapMapBlock(64, 32, True, None),
            HeapMapBlock(96, 16, False),
        ],
        end=112,
    )

    lines = heapmap.summary_lines(heap)

    assert lines[0] == "small_mem: 3 blocks, 96 B used, 16 B free, arena 112 B"
    assert lines[1] == "  #e15e3a owner: main, size: 64 B, start: 0x0, end: 0x40"
    assert lines[2] == (
        "  #4f8fd6 owner: N/A, size: 32 B, start: 0x40, end: 0x60 "
        "(no owner names in this allocator)"
    )
    assert lines[3] == "  #c8c8c8 owner: (free), size: 16 B, start: 0x60, end: 0x70"


def test_grid_has_no_padding_and_reserves_six_pixel_cells_for_tiny_blocks():
    heap = _map(
        [HeapMapBlock(0, 1, True, "tiny"), HeapMapBlock(1, 999999, False)], end=1000000
    )
    grid = heapmap.grid_layout(heap)
    assert grid["scale"] == heapmap.SCALE
    assert all(stop > start for start, stop, _index in grid["runs"])
    assert [index for _start, _stop, index in grid["runs"]] == [0, 1]
    assert grid["runs"][-1][1] == grid["width"] * grid["height"]
    # Reason: every row past the first is fully free, so the whole tail is
    # one folded group stretched to the legend height.
    assert grid["folds"] == [
        {
            "row": 1,
            "rows": 204,
            "height": 7,
            "bytes": 995182,
            "kind": "free",
            "start": 4818,
            "end": 1000000,
        }
    ]
    rgb, width, height = heapmap.rasterize(heap, heapmap.build_palette(heap))
    assert len(rgb) == width * height * 3
    assert _pixel(rgb, width, 2, 2) == heapmap.build_palette(heap).owners["tiny"]
    assert _pixel(rgb, width, 0, 12) == heapmap.FOLD_HATCH_RGB
    assert _pixel(rgb, width, 4, 10) == heapmap.BACKGROUND_RGB


def test_png_contains_swatches_and_legend_text_right_of_the_map():
    from gdr.bitmapfont import append_endpoint_labels, append_legend, printable

    heap = _map(
        [HeapMapBlock(0, 16, True, "main"), HeapMapBlock(16, 16, False)], end=32
    )
    palette = heapmap.build_palette(heap)
    entries = heapmap.legend_entries(heap, palette)
    assert entries == [
        (palette.owners["main"], "main: 16B, 0x0-0x10"),
        (palette.free, "(free): 16B, 0x10-0x20"),
    ]
    layout = heapmap.grid_layout(heap)
    rgb, width, height = heapmap.rasterize(heap, palette, layout)
    expanded, out_width, out_height = append_legend(rgb, width, height, entries)
    final, final_width, final_height = append_endpoint_labels(
        expanded, out_width, out_height, "start: 0x0", "end: 0x20"
    )
    base_x = width + 12 + 8
    # Reason: two rows stay in one column below the eight-row wrap floor;
    # the 22-char `(free)` row sets the lane width.
    assert out_width == base_x + 20 + 22 * 12 + 8
    assert out_height > height
    assert _pixel(expanded, out_width, base_x, 8) == palette.owners["main"]
    assert _pixel(expanded, out_width, base_x, 28) == palette.free
    assert (final_width, final_height) == (out_width, out_height + 44)
    assert _pixel(final, final_width, 10, 8) == (32, 32, 32)
    assert _pixel(final, final_width, 10, out_height + 30) == (32, 32, 32)
    png = heapmap.render_png(heap)
    ihdr = struct.unpack(">IIBBBBB", _chunks(png)[0][1])
    assert ihdr[:2] == (final_width, final_height)
    assert printable("任务") == r"\u4efb\u52a1"


def test_wrap_label_breaks_after_commas_then_words():
    from gdr.bitmapfont import _wrap_label

    assert _wrap_label("owner:main, size: 16 B, start: 0x0", 24) == [
        "owner:main, size: 16 B",
        "start: 0x0",
    ]
    assert _wrap_label("unknown/unreadable arena span", 24) == [
        "unknown/unreadable arena",
        "span",
    ]
    assert _wrap_label("short", 24) == ["short"]


def test_adjacent_blocks_share_a_two_pixel_divider():
    """Same-owner neighbours stop merging: the shared edge reads dark."""
    heap = _map(
        [
            HeapMapBlock(0, 64, True, "main"),
            HeapMapBlock(64, 64, True, "main"),
            HeapMapBlock(128, 64, True, "main"),
        ],
        end=192,
    )
    palette = heapmap.build_palette(heap)
    grid = heapmap.grid_layout(heap)
    rgb, width, _height = heapmap.rasterize(heap, palette, grid)

    main = palette.owners["main"]
    columns = grid["width"]
    assert columns >= 2, "one column cannot show a vertical divider"
    # Reason: the first run boundary falls inside the first row, so the
    # divider sits on a vertical shared edge, not a row wrap.
    first_stop = grid["runs"][0][1]
    assert first_stop < columns
    # Reason: the shared edge is owned by the right cell alone: its left
    # two pixels read dark while the left cell keeps its own right edge.
    left = (first_stop - 1) * heapmap.SCALE
    right = first_stop * heapmap.SCALE
    assert _pixel(rgb, width, left + 2, 4) == main
    assert _pixel(rgb, width, left + 8, 4) == main
    assert _pixel(rgb, width, left + 9, 4) == main
    assert _pixel(rgb, width, right, 4) == heapmap.BACKGROUND_RGB
    assert _pixel(rgb, width, right + 1, 4) == heapmap.BACKGROUND_RGB
    assert _pixel(rgb, width, right + 2, 4) == main
    assert _pixel(rgb, width, left + 2, 0) == main
    assert _pixel(rgb, width, left + 2, 9) == main


def test_wrapping_block_inner_corner_closes():
    """A block wrapping rows joins its left bar to the next row's top bar."""
    heap = _map(
        [
            HeapMapBlock(0, 64, True, "a"),
            HeapMapBlock(64, 2048, True, "main"),
            HeapMapBlock(2112, 64, True, "c"),
        ],
        end=2176,
    )
    palette = heapmap.build_palette(heap)
    grid = heapmap.grid_layout(heap)
    rgb, width, _height = heapmap.rasterize(heap, palette, grid)

    main = palette.owners["main"]
    assert grid["width"] == 20
    assert grid["runs"] == [[0, 2, 0], [2, 38, 1], [38, 40, 2]]
    # Reason: the wrap corner is the top-left 2x2 of the cell below the
    # boundary; its neighbours on both sides keep their own bars' pixels.
    for dx in (0, 1):
        for dy in (0, 1):
            assert _pixel(rgb, width, 20 + dx, 10 + dy) == heapmap.BACKGROUND_RGB
    assert _pixel(rgb, width, 18, 10) == heapmap.BACKGROUND_RGB
    assert _pixel(rgb, width, 22, 12) == main
    assert _pixel(rgb, width, 0, 0) == palette.owners["a"]


def test_gap_and_canvas_sides_draw_no_divider():
    """Canvas edges and gaps draw nothing: only block-to-block edges earn lines."""
    heap = _map([HeapMapBlock(8, 8, True, "main")], end=24)
    palette = heapmap.build_palette(heap)
    grid = heapmap.grid_layout(heap)
    rgb, width, _height = heapmap.rasterize(heap, palette, grid)

    main = palette.owners["main"]
    start, stop, _index = grid["runs"][1]
    first = start * heapmap.SCALE
    last = (stop - 1) * heapmap.SCALE + heapmap.SCALE - 1
    assert _pixel(rgb, width, first + 2, 4) == main
    assert _pixel(rgb, width, first, 4) == main
    assert _pixel(rgb, width, first + 2, 0) == main
    assert _pixel(rgb, width, last - 1, 4) == main
    assert _pixel(rgb, width, last, 4) == main


def test_endpoint_strips_label_the_used_area_start_and_end():
    """The start/end strips span the full width above and below the bitmap."""
    from gdr.bitmapfont import append_endpoint_labels

    rgb, width, height = b"\xff\xff\xff" * 10, 10, 1
    out, out_width, out_height = append_endpoint_labels(
        rgb, width, height, "start: 0x0", "end: 0x20"
    )

    # Reason: the 10-pixel bitmap widens to the 10-char `start` label; the
    # ink sits on the third glyph row of each strip's first letter.
    assert (out_width, out_height) == (8 + 10 * 12 + 8, height + 44)
    assert _pixel(out, out_width, 10, 8) == (32, 32, 32)
    assert _pixel(out, out_width, 10, height + 30) == (32, 32, 32)
    assert _pixel(out, out_width, 0, 0) == (255, 255, 255)


def test_legend_wraps_ten_rows_into_two_lanes():
    """Ten owners clear the eight-row floor and the used-height budget."""
    blocks = [HeapMapBlock(i * 64, 64, True, f"owner{i:02d}") for i in range(10)]
    heap = _map(blocks, end=10 * 64)
    layout = heapmap.grid_layout(heap)
    entries = heapmap.legend_entries(heap, heapmap.build_palette(heap))
    rgb, width, height = heapmap.rasterize(heap, heapmap.build_palette(heap), layout)
    from gdr.bitmapfont import append_legend

    expanded, out_width, out_height = append_legend(rgb, width, height, entries)
    # Reason: ten rows split five and five; the second lane starts one column
    # past the first.
    assert len(entries) == 10
    assert out_height == 12 + 5 * 20
    first = heapmap.build_palette(heap).owners["owner00"]
    fifth = heapmap.build_palette(heap).owners["owner05"]
    assert _pixel(expanded, out_width, width + 12 + 8, 8) == first
    lane = width + 12 + 36 + max(len(text) for _c, text in entries) * 12 + 12 + 8
    assert _pixel(expanded, out_width, lane, 8) == fifth


def test_legend_wraps_past_twice_used_height():
    """Sixty owners cannot stand in one column; lanes share the used budget."""
    blocks = [HeapMapBlock(i * 64, 64, True, f"owner{i:02d}") for i in range(60)]
    heap = _map(blocks, end=60 * 64)
    png = heapmap.render_png(heap)
    ihdr = struct.unpack(">IIBBBBB", _chunks(png)[0][1])
    layout = heapmap.grid_layout(heap)
    used = layout["height"] * heapmap.SCALE
    one_column = 12 + 60 * 20

    assert one_column > 2 * used
    # Reason: the ranking wraps once into two lanes of thirty, never more.
    assert ihdr[1] == max(layout["height"] * heapmap.SCALE, 12 + 30 * 20) + 44
    assert ihdr[1] < one_column


def test_unknown_gaps_are_not_misrepresented_as_free():
    heap = _map([HeapMapBlock(8, 8, True, "main")], end=24)
    grid = heapmap.grid_layout(heap)
    assert [index for _start, _stop, index in grid["runs"]] == [-1, 0, -1]
    assert any(
        "unknown" in text
        for _colour, text in heapmap.legend_entries(heap, heapmap.build_palette(heap))
    )


def test_divider_width_is_fixed_at_the_extremes_and_a_fifth_between():
    """Small cells keep a visible 2-pixel line; huge cells stop at 4 pixels."""
    assert [heapmap.divider_width(px) for px in (1, 6, 9)] == [2, 2, 2]
    assert [heapmap.divider_width(px) for px in (10, 13, 15, 18, 19)] == [2, 3, 3, 4, 4]
    assert [heapmap.divider_width(px) for px in (20, 40, 64)] == [4, 4, 4]


def test_big_cells_paint_proportional_dividers():
    """A 20-pixel cell draws its 4-pixel shared edge on the owner's side."""
    heap = _map(
        [
            HeapMapBlock(0, 64, True, "main"),
            HeapMapBlock(64, 64, True, "main"),
        ],
        end=128,
    )
    palette = heapmap.build_palette(heap)
    layout = heapmap.grid_layout(heap, palette, cell_px=20)
    rgb, width, _height = heapmap.rasterize(heap, palette, layout)

    assert layout["scale"] == 20
    assert layout["width"] >= 2
    boundary = layout["runs"][1][0] * 20
    assert _pixel(rgb, width, boundary - 1, 2) == palette.owners["main"]
    for dx in range(4):
        assert _pixel(rgb, width, boundary + dx, 2) == heapmap.BACKGROUND_RGB
    assert _pixel(rgb, width, boundary + 4, 2) == palette.owners["main"]


def test_narrow_width_budget_clamps_bitmap_columns():
    """A 200-pixel budget on 10-pixel cells keeps twenty columns or fewer."""
    heap = _map(
        [
            HeapMapBlock(0, 64, True, "main"),
            HeapMapBlock(64, 64, True, "main"),
        ],
        end=128,
    )
    layout = heapmap.grid_layout(heap, heapmap.build_palette(heap), max_px=200)

    assert layout["width"] * 10 <= 200 + 10
    assert layout["runs"][-1][1] == layout["width"] * layout["height"]


def test_render_png_honours_cell_and_width_options():
    """Custom sizes flow from the CLI flags into the PNG dimensions."""
    heap = _map(
        [HeapMapBlock(0, 16, True, "main"), HeapMapBlock(16, 16, False)], end=32
    )
    palette = heapmap.build_palette(heap)
    layout = heapmap.grid_layout(heap, palette, cell_px=20, max_px=400)

    assert layout["scale"] == 20
    assert (layout["width"], layout["height"]) == (10, 1)
    png = heapmap.render_png(heap, palette, cell_px=20, max_px=400)
    ihdr = struct.unpack(">IIBBBBB", _chunks(png)[0][1])
    # Reason: 200-pixel bitmap plus one 300-pixel legend lane, then strips.
    assert ihdr[:2] == (512, 96)


def test_row_bytes_tiles_exact_windows_without_folds():
    """Each row covers exactly N arena bytes; spans cut at window edges."""
    heap = _map(
        [
            HeapMapBlock(0, 64, True, "main"),
            HeapMapBlock(64, 32, True, None),
            HeapMapBlock(96, 16, False),
        ],
        end=112,
    )
    palette = heapmap.build_palette(heap)
    layout = heapmap.grid_layout(heap, palette, row_bytes=32)

    assert (layout["width"], layout["height"]) == (20, 4)
    assert layout["folds"] == []
    assert layout["runs"] == [[0, 20, 0], [20, 40, 0], [40, 60, 1], [60, 80, 2]]
    rgb, width, height = heapmap.rasterize(heap, palette, layout)
    assert (width, height) == (200, 40)
    assert _pixel(rgb, width, 2, 2) == palette.owners["main"]
    assert _pixel(rgb, width, 2, 32) == palette.free


def test_row_bytes_too_small_for_the_arena_errors():
    """A row size needing past 4096 rows refuses with a larger-size hint."""
    heap = _map([HeapMapBlock(0, 100000, True, "main")], end=100000)

    try:
        heapmap.grid_layout(heap, heapmap.build_palette(heap), row_bytes=1)
    except ValueError as exc:
        assert "capped at 4096" in str(exc)
    else:
        raise AssertionError("expected ValueError")
