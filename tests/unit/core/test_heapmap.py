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
    assert _pixel(rgb, width, 0, 0) == main
    grid = heapmap.grid_layout(heap)
    for start, _stop, index in grid["runs"]:
        x = start % grid["width"] * heapmap.SCALE
        y = start // grid["width"] * heapmap.SCALE
        expected = (main, heapmap.FREE_RGB, heapmap.BACKGROUND_RGB)[
            index if index >= 0 else 2
        ]
        assert _pixel(rgb, width, x, y) == expected


def test_a_tiny_block_still_paints_one_cell():
    """A block smaller than one cell is at least one cell wide, so it stays visible."""
    heap = _map([HeapMapBlock(0, 1, True, None)], end=65536 * 1000)
    rgb, width, _height = heapmap.rasterize(heap, heapmap.build_palette(heap))

    assert _pixel(rgb, width, 0, 0) == heapmap.UNKNOWN_OWNER_RGB
    assert _pixel(rgb, width, heapmap.SCALE, 0) == heapmap.BACKGROUND_RGB


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
    assert lines[1].startswith("  owner:main  64 B  #")
    assert lines[2] == "  owner:N/A  32 B  (no owner names in this allocator)"


def test_grid_has_no_padding_and_reserves_six_pixel_cells_for_tiny_blocks():
    heap = _map(
        [HeapMapBlock(0, 1, True, "tiny"), HeapMapBlock(1, 999999, False)], end=1000000
    )
    grid = heapmap.grid_layout(heap)
    assert grid["scale"] == 6
    assert all(stop > start for start, stop, _index in grid["runs"])
    assert [index for _start, _stop, index in grid["runs"]] == [0, 1]
    assert grid["runs"][-1][1] == grid["width"] * grid["height"]
    rgb, width, height = heapmap.rasterize(heap, heapmap.build_palette(heap))
    assert len(rgb) == width * height * 3
    assert _pixel(rgb, width, 5, 5) == heapmap.build_palette(heap).owners["tiny"]
    assert _pixel(rgb, width, width - 1, height - 1) == heapmap.FREE_RGB


def test_png_contains_swatches_and_legend_text_below_the_map():
    from gdr.bitmapfont import append_legend, printable

    heap = _map(
        [HeapMapBlock(0, 16, True, "main"), HeapMapBlock(16, 16, False)], end=32
    )
    palette = heapmap.build_palette(heap)
    entries = heapmap.legend_entries(heap, palette)
    assert entries == [
        (palette.owners["main"], "owner:main, size: 16 B"),
        (palette.free, "owner:(free), size: 16 B"),
    ]
    rgb, width, height = heapmap.rasterize(heap, palette)
    expanded, out_width, out_height = append_legend(rgb, width, height, entries)
    assert out_height > height
    assert _pixel(expanded, out_width, 8, height + 8) == palette.owners["main"]
    assert _pixel(expanded, out_width, 8, height + 28) == palette.free
    png = heapmap.render_png(heap)
    ihdr = struct.unpack(">IIBBBBB", _chunks(png)[0][1])
    assert ihdr[:2] == (out_width, out_height)
    assert printable("任务") == r"\u4efb\u52a1"


def test_unknown_gaps_are_not_misrepresented_as_free():
    heap = _map([HeapMapBlock(8, 8, True, "main")], end=24)
    grid = heapmap.grid_layout(heap)
    assert [index for _start, _stop, index in grid["runs"]] == [-1, 0, -1]
    assert any(
        "unknown" in text
        for _colour, text in heapmap.legend_entries(heap, heapmap.build_palette(heap))
    )
