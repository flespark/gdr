"""Unit tests for the FreeRTOS heap snapshot (``frt heap``).

Covers the seven-way algorithm classification (heap_1..heap_5 plus the
heap_3/none split on ``pvPortMalloc`` presence), the computed
``heapBLOCK_ALLOCATED_BITMASK`` (32/64-bit, and heap_2's pre-V10.5 gap), the
struct-size computation fallback, canary deobfuscation of every
``pxNextFreeBlock`` including the chain head, the free-list walk's
terminators (``pxEnd`` vs the ``xEnd`` *value*), the heap_5 zero-size link
block rule, both cross-check verdicts, the uninitialised early exits, the
bump-pointer/opaque reports, the stable 10-key pair order, and the adapter's
``system_summary`` heap fields.  All GDB entry points stay monkeypatchable
through module-level helpers, following ``test_timers.py``.
"""

from __future__ import annotations

import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

import freertos.adapter as adapter_module
import freertos.commands as commands
import freertos.heap as heap
from freertos.heap import HeapBlock, HeapGeometry, HeapSnapshot, HeapWalk
from freertos.layout import FreeRtosConfig, build_layout

_ARCH = SimpleNamespace(ptrsize=4, endian="little")

_HEAP_KEYS = [
    "Algorithm",
    "TotalSize",
    "FreeSize",
    "MinEver",
    "Allocs",
    "Frees",
    "Protector",
    "Blocks",
    "Holes",
    "CrossCheck",
]


class _FakeType:
    def __init__(self, sizeof_: int):
        self.sizeof = sizeof_


class _FakeValue:
    """A minimal gdb.Value stand-in: scalar ``__int__`` + member reads."""

    def __init__(self, members=None, scalar=None, sizeof=None, address=0):
        self._members = members or {}
        self._scalar = scalar
        self.address = address
        self.type = _FakeType(sizeof) if sizeof is not None else None

    def __int__(self):
        if self._scalar is None:
            raise TypeError("scalar value has no int form")
        return self._scalar

    def __getitem__(self, name):
        if name not in self._members:
            raise KeyError(name)
        return self._members[name]


def _symbol_map(symbols: dict[str, _FakeValue]):
    """A lookup_symbol stand-in serving explicit gdb.Value stand-ins."""
    return lambda name: symbols.get(name)


def _scalar_names(values: dict[str, int]):
    """A lookup_symbol stand-in serving scalar symbols from a dict."""
    return lambda name: _FakeValue(scalar=values[name]) if name in values else None


def _memory_for(entries: list[tuple[int, int, int]], canary: int = 0):
    """Build a byte map from ``(address, next, raw_size)`` triples."""
    mem: dict[int, bytes] = {}
    for address, next_addr, raw_size in entries:
        mem[address] = struct.pack("<II", (next_addr or 0) ^ canary, raw_size)
    return mem


def _patch_read_bytes(monkeypatch, mem: dict[int, bytes]):
    # BlockHeader reads go through read_field_at (layout-described DWARF
    # offsets); the stand-in decodes the same way from the fake memory
    # image, whose VALUES are full blocks at their start address -- the two
    # fields of struct A_BLOCK_LINK sit at offsets 0 / ptrsize inside the
    # block, exactly as real read_field_at + member_offset would resolve.
    def fake_read_field_at(addr, _type_name, _layout, field, width, endian):
        raw = mem.get(addr)
        if raw is None:
            return None
        offset = 0 if field == "next_free" else width
        return int.from_bytes(raw[offset : offset + width], byteorder=endian)

    monkeypatch.setattr(heap, "read_field_at", fake_read_field_at)
    # Every walk decodes raw memory with the target byte order.
    monkeypatch.setattr(heap, "get_arch_info", lambda: _ARCH)


def _minimal_geometry_probes(monkeypatch, size_t_width: int = 4, alignment: int = 8):
    """Route every geometry probe to "nothing present" defaults.

    The ``type_size`` mock only intercepts name-based lookups (a missing
    DWARF type); object-based reads (``value.type`` already parsed) still
    pass through to the real primitive so ``_heap_array_size`` sees the
    fake value's ``sizeof``.
    """
    original_type_size = heap.type_size

    def fake_type_size(value):
        if isinstance(value, str):
            return size_t_width if value == "size_t" else None
        return original_type_size(value)

    monkeypatch.setattr(heap, "type_size", fake_type_size)
    monkeypatch.setattr(heap, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(heap, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(
        heap, "read_macro_text_in_source", lambda *_args, **_kw: str(alignment)
    )
    monkeypatch.setattr(heap, "get_arch_info", lambda: _ARCH)
    monkeypatch.setattr(heap, "value_address", lambda _value: 0)


def _heap4_geom(**overrides) -> HeapGeometry:
    base = HeapGeometry(
        kind=4,
        algorithm="heap_4",
        pointer_bits=32,
        allocated_bitmask=0x80000000,
        alignment=8,
        heap_low=0x2000,
        linear_low=0x2000,
        heap_limit=0x2040,
        free_end=0x2040,
        canary=0,
        canary_present=False,
    )
    return replace(base, **overrides)


def _heap2_geom(**overrides) -> HeapGeometry:
    base = HeapGeometry(
        kind=2,
        algorithm="heap_2",
        pointer_bits=32,
        allocated_bitmask=0,
        alignment=8,
        heap_low=0x3000,
        linear_low=0x3000,
        heap_limit=0x3000 + 49144,
        free_end=0x3010,
        canary=0,
        canary_present=False,
    )
    return replace(base, **overrides)


def _heap5_geom(**overrides) -> HeapGeometry:
    base = HeapGeometry(
        kind=5,
        algorithm="heap_5",
        pointer_bits=32,
        allocated_bitmask=0x80000000,
        alignment=8,
        heap_low=0x1000,
        linear_low=None,
        heap_limit=None,
        free_end=0x1040,
        canary=0,
        canary_present=False,
    )
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# algorithm classification
# ---------------------------------------------------------------------------


def test_detect_algorithm_covers_seven_cases(monkeypatch):
    """heap_1..heap_5 plus the heap_3/none split on pvPortMalloc presence."""
    monkeypatch.setattr(heap, "lookup_symbol", lambda _name: None)
    _minimal_geometry_probes(monkeypatch)
    for kind, expected in (
        (1, "heap_1"),
        (2, "heap_2"),
        (3, "heap_3"),
        (4, "heap_4"),
        (5, "heap_5"),
    ):
        layout = build_layout(FreeRtosConfig(heap_kind=kind), (10, 3, 1))
        assert heap.heap_geometry(layout).algorithm == expected

    # heap_3 links pvPortMalloc but exports no kernel heap symbol; a build
    # with no allocator exports neither (static-only fixture).
    layout = build_layout(FreeRtosConfig(heap_kind=None), (10, 3, 1))
    monkeypatch.setattr(heap, "symbol_exists", lambda name: name == "pvPortMalloc")
    assert heap.heap_geometry(layout).algorithm == "heap_3"
    monkeypatch.setattr(heap, "symbol_exists", lambda _name: False)
    assert heap.heap_geometry(layout).algorithm == "none"


# ---------------------------------------------------------------------------
# allocated-bit mask
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "expected"),
    [
        (4, 0x80000000),
        # Reason: no live lane has a 64-bit size_t (all fixtures are 32-bit
        # Cortex-M), so the 64-bit branch is unit-tested only.
        (8, 0x8000000000000000),
    ],
)
def test_allocated_bitmask_from_size_t_width(monkeypatch, width, expected):
    """The mask is computed from sizeof(size_t), never read as a symbol."""
    monkeypatch.setattr(heap, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(
        heap,
        "type_size",
        lambda name: width if name == "size_t" else None,
    )
    monkeypatch.setattr(heap, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(heap, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(heap, "read_macro_text_in_source", lambda *_a, **_k: None)
    monkeypatch.setattr(heap, "get_arch_info", lambda: _ARCH)

    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=4), (11, 1, 0)))
    assert geom.allocated_bitmask == expected
    # The deleted xBlockAllocatedBit runtime variable (V10.x) would report 0
    # in a static image; the computed mask must not depend on it at all.
    monkeypatch.setattr(
        heap,
        "lookup_symbol",
        lambda name: (
            _FakeValue({"pxNextFreeBlock": 0}, scalar=0)
            if name in ("xBlockAllocatedBit", "xStart", "xEnd", "pxEnd")
            else None
        ),
    )
    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=4), (10, 3, 1)))
    # The mask still follows sizeof(size_t); xBlockAllocatedBit's static 0
    # never leaks in.
    assert geom.allocated_bitmask == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # heap_2 only gained the MSB allocation bit in V10.5.0 (heap_2.c
        # heapBLOCK_ALLOCATED_BITMASK); earlier releases keep the full width.
        ((10, 3, 1), 0),
        ((10, 4, 6), 0),
        ((10, 5, 0), 0x80000000),
        ((11, 1, 0), 0x80000000),
    ],
)
def test_heap2_allocated_bit_follows_version(monkeypatch, version, expected):
    monkeypatch.setattr(heap, "lookup_symbol", lambda _name: None)
    _minimal_geometry_probes(monkeypatch)
    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=2), version))
    assert geom.allocated_bitmask == expected


# ---------------------------------------------------------------------------
# struct size
# ---------------------------------------------------------------------------


def test_struct_size_falls_back_to_computation(monkeypatch):
    """Without xHeapStructSize/heapSTRUCT_SIZE the header size is computed
    as align_up(sizeof(BlockLink_t), portBYTE_ALIGNMENT) (heap_4.c)."""
    monkeypatch.setattr(heap, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(
        heap,
        "type_size",
        lambda name: (
            8 if name in ("BlockLink_t", "struct A_BLOCK_LINK", "size_t") else None
        ),
    )
    monkeypatch.setattr(heap, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(
        heap, "read_macro_int", lambda name: 8 if name == "portBYTE_ALIGNMENT" else None
    )
    monkeypatch.setattr(heap, "read_macro_text_in_source", lambda *_a, **_k: "8")
    monkeypatch.setattr(heap, "get_arch_info", lambda: _ARCH)

    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=4), (11, 1, 0)))
    assert geom.struct_size == 8


def test_struct_size_prefers_the_symbol(monkeypatch):
    """A present xHeapStructSize wins over the computation."""
    monkeypatch.setattr(
        heap,
        "lookup_symbol",
        lambda name: _FakeValue(scalar=16) if name == "xHeapStructSize" else None,
    )
    _minimal_geometry_probes(monkeypatch)
    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=4), (10, 3, 1)))
    assert geom.struct_size == 16


def test_heap2_struct_size_symbol_names_follow_version(monkeypatch):
    """heap_2 spells its header size heapSTRUCT_SIZE before V11."""
    symbol_values: dict[str, int] = {"heapSTRUCT_SIZE": 8}

    def lookup(name):
        return _FakeValue(scalar=symbol_values[name]) if name in symbol_values else None

    monkeypatch.setattr(heap, "lookup_symbol", lookup)
    _minimal_geometry_probes(monkeypatch)
    old = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=2), (10, 3, 1)))
    assert old.struct_size == 8

    symbol_values = {"xHeapStructSize": 8}
    monkeypatch.setattr(heap, "lookup_symbol", lookup)
    new = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=2), (11, 1, 0)))
    assert new.struct_size == 8


# ---------------------------------------------------------------------------
# canary deobfuscation
# ---------------------------------------------------------------------------


def test_canary_deobfuscates_every_pointer(monkeypatch):
    """xStart.pxNextFreeBlock and every member pointer are XORed back; the
    chain still ends at pxEnd (heap_4.c heapPROTECT_BLOCK_POINTER)."""
    canary = 0xBEEF
    head = 0x2000
    end = 0x2100
    mem = _memory_for([(0x2000, 0x2020, 32), (0x2020, end, 48)], canary=canary)
    _patch_read_bytes(monkeypatch, mem)
    _minimal_geometry_probes(monkeypatch)
    monkeypatch.setattr(heap, "symbol_exists", lambda name: name == "xHeapCanary")
    monkeypatch.setattr(
        heap,
        "lookup_symbol",
        lambda name: (
            _FakeValue({"pxNextFreeBlock": head ^ canary})
            if name == "xStart"
            else _FakeValue(scalar=end)
            if name == "pxEnd"
            else _FakeValue(scalar=canary)
            if name == "xHeapCanary"
            else None
        ),
    )

    layout = build_layout(FreeRtosConfig(heap_kind=4, heap_protector=True), (11, 1, 0))
    geom = heap.heap_geometry(layout)
    assert geom.heap_low == head  # chain head deobfuscated

    walk = heap.walk_free_list(geom)
    assert not walk.corrupt
    assert [block.address for block in walk.blocks] == [head, 0x2020]
    assert walk.blocks[0].next_free == 0x2020
    assert walk.blocks[1].next_free == end
    assert walk.free_bytes == 80
    assert walk.free_blocks == 2


# ---------------------------------------------------------------------------
# free-list walk
# ---------------------------------------------------------------------------


def test_free_list_member_with_allocated_bit_is_corrupt(monkeypatch):
    """A free-list block whose xBlockSize carries the MSB can only be a
    corrupt heap; the snapshot keeps the kernel counter as FreeSize."""
    mem = {
        0x2000: struct.pack("<II", 0x2020, 0x80000000 | 32),
        0x2020: struct.pack("<II", 0x2040, 48),
    }
    _patch_read_bytes(monkeypatch, mem)
    values = {
        "xStart": _FakeValue({"pxNextFreeBlock": 0x2000}),
        "pxEnd": _FakeValue(scalar=0x2040),
        "xFreeBytesRemaining": _FakeValue(scalar=1000),
        "xMinimumEverFreeBytesRemaining": _FakeValue(scalar=500),
        "xNumberOfSuccessfulAllocations": _FakeValue(scalar=3),
        "xNumberOfSuccessfulFrees": _FakeValue(scalar=2),
        "xHeapStructSize": _FakeValue(scalar=8),
    }
    monkeypatch.setattr(heap, "lookup_symbol", _symbol_map(values))
    _minimal_geometry_probes(monkeypatch)

    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(heap_kind=4), (10, 3, 1)))
    assert snap.free == 1000  # the kernel counter is never resynthesised
    assert snap.free_list.corrupt is True
    assert "allocated bit" in (snap.free_list.corrupt_reason or "")
    assert snap.cross_check.startswith("unavailable")
    assert heap.heap_status(snap) == "corrupt"


def test_free_list_cycle_is_corrupt(monkeypatch):
    """A repeating address is reported as a cycle, not a hang."""
    mem = {
        0x2000: struct.pack("<II", 0x2008, 8),
        0x2008: struct.pack("<II", 0x2000, 8),
    }
    _patch_read_bytes(monkeypatch, mem)
    walk = heap.walk_free_list(_heap4_geom())
    assert walk.corrupt is True
    assert "cycle" in (walk.corrupt_reason or "")


def test_heap2_free_list_terminates_at_x_end(monkeypatch):
    """heap_2's free list ends at &xEnd (a BlockLink_t value), and xEnd is
    not counted as a block (heap_2.c prvHeapInit)."""
    mem = {
        0x3000: struct.pack("<II", 0x3008, 8),
        0x3008: struct.pack("<II", 0x3010, 16),
    }
    _patch_read_bytes(monkeypatch, mem)
    walk = heap.walk_free_list(_heap2_geom())
    assert [block.address for block in walk.blocks] == [0x3000, 0x3008]
    assert walk.blocks[-1].next_free == 0x3010  # the xEnd address
    assert walk.free_blocks == 2
    assert not walk.corrupt


def test_heap5_skips_zero_sized_block_for_smallest(monkeypatch):
    """The zero-size region link block counts as a block but is skipped for
    the smallest statistic (heap_5.c vPortGetHeapStats)."""
    mem = {
        0x1000: struct.pack("<II", 0x1020, 100),
        0x1020: struct.pack("<II", 0x1030, 0),
        0x1030: struct.pack("<II", 0x1040, 50),
    }
    _patch_read_bytes(monkeypatch, mem)
    walk = heap.walk_free_list(_heap5_geom())
    assert walk.free_blocks == 3
    assert walk.free_bytes == 150
    assert walk.smallest_free == 50
    assert walk.largest_free == 100
    assert not walk.corrupt


def test_free_list_terminated_at_null_is_corrupt(monkeypatch):
    """A chain that hits NULL before reaching pxEnd is corruption."""
    mem = {0x2000: struct.pack("<II", 0, 32)}
    _patch_read_bytes(monkeypatch, mem)
    walk = heap.walk_free_list(_heap4_geom())
    assert walk.corrupt is True
    assert "NULL" in (walk.corrupt_reason or "")


# ---------------------------------------------------------------------------
# linear walk
# ---------------------------------------------------------------------------


def test_walk_linear_records_alloc_holes_and_sum(monkeypatch):
    """The linear walk tiles the extent, flags allocated blocks from the
    MSB, and counts free segments (Holes) across adjacent frees."""
    mem = {
        0x2000: struct.pack("<II", 0x0, 0x80000000 | 16),  # allocated
        0x2010: struct.pack("<II", 0x0, 8),  # free
        0x2018: struct.pack("<II", 0x0, 8),  # free (adjacent -> same hole)
        0x2020: struct.pack("<II", 0x0, 0x80000000 | 16),  # allocated
        0x2030: struct.pack("<II", 0x0, 16),  # free
    }
    _patch_read_bytes(monkeypatch, mem)
    geom = _heap4_geom(free_end=0x2040)
    walk = heap.walk_linear(geom, free_list=heap.walk_free_list(geom))
    assert not walk.corrupt
    assert walk.free_bytes == 8 + 8 + 16
    assert walk.free_blocks == 3
    assert walk.holes == 2  # two separate free runs
    states = [block.allocated for block in walk.blocks]
    assert states == [True, False, False, True, False]


def test_walk_linear_uses_membership_for_pre_v11_heap2(monkeypatch):
    """Without the MSB (heap_2 < V10.5.0) the header cannot tell free from
    allocated; the free-list address set decides."""
    mem = {
        0x3000: struct.pack("<II", 0x3020, 16),  # free (in the free list)
        0x3010: struct.pack("<II", 0x0, 16),  # allocated (not in the list)
        0x3020: struct.pack("<II", 0x3040, 16),  # free
        0x3030: struct.pack("<II", 0x0, 16),  # allocated (not in the list)
    }
    _patch_read_bytes(monkeypatch, mem)
    geom = _heap2_geom(free_end=0x3040, heap_limit=0x3040)
    free_list = heap.walk_free_list(geom)
    assert free_list.free_blocks == 2  # 0x3000, 0x3020
    walk = heap.walk_linear(geom, free_list=free_list)
    assert not walk.corrupt
    assert walk.free_blocks == 2
    assert walk.free_bytes == 32
    assert [block.allocated for block in walk.blocks] == [False, True, False, True]
    assert walk.holes == 2


def test_walk_linear_zero_size_block_is_corrupt_for_heap4(monkeypatch):
    """A zero-size block inside heap_4's extent is corruption (no region
    link blocks exist there; only pxEnd itself is size 0)."""
    mem = {0x2000: struct.pack("<II", 0, 0)}
    _patch_read_bytes(monkeypatch, mem)
    walk = heap.walk_linear(_heap4_geom(), free_list=HeapWalk())
    assert walk.corrupt is True
    assert "zero-size" in (walk.corrupt_reason or "")


def test_heap5_linear_walk_skipped_without_protector():
    """No region base exists without the protector, so the linear walk is
    explicitly skipped and the cross-check stays unavailable."""
    geom = _heap5_geom()
    walk = heap.walk_linear(geom, free_list=None)
    assert walk.blocks == []
    assert "region bases unknown" in (walk.incomplete_reason or "")
    assert heap.cross_validate(None, walk, 150, geom) == (
        "unavailable: heap_5 region bases unknown (no heap protector)"
    )


def test_heap5_linear_walk_uses_protector_bounds(monkeypatch):
    """With pucHeapLowAddress/HighAddress a single region is walked from its
    base to pxEnd and the cross-check may pass (unit-tested; no live lane
    has a heap_5+protector fixture)."""
    mem = {
        0x1000: struct.pack("<II", 0x1010, 8),  # free
        0x1008: struct.pack("<II", 0x0, 0x80000000 | 8),  # allocated
        0x1010: struct.pack("<II", 0x1018, 8),  # free
        0x1018: struct.pack("<II", 0x0, 0),  # pxEnd marker (size 0)
    }
    _patch_read_bytes(monkeypatch, mem)
    geom = _heap5_geom(
        linear_low=0x1000,
        heap_limit=0x1018,
        heap_low=0x1000,
        free_end=0x1018,
        canary_present=True,
    )
    free_list = heap.walk_free_list(geom)
    walk = heap.walk_linear(geom, free_list=free_list)
    assert len(walk.blocks) == 3
    assert walk.blocks[1].allocated is True
    assert walk.incomplete_reason is None  # walked to pxEnd -> complete
    assert heap.cross_validate(free_list, walk, 16, geom) == "ok"


def test_heap5_complete_walk_makes_extremes_a_total(monkeypatch):
    """A complete single-region heap_5 walk (protector + one region) turns
    the region extremes into a true total; an incomplete (multi-region)
    walk must keep TotalSize unavailable -- the extremes span gaps."""
    geom = _heap5_geom(
        linear_low=0x1000,
        heap_limit=0x1018,
        free_end=0x1018,
        canary_present=True,
    )
    free_walk = HeapWalk(
        free_bytes=16,
        free_blocks=2,
        blocks=[
            HeapBlock(address=0x1000, size=8, allocated=False),
            HeapBlock(address=0x1010, size=8, allocated=False),
        ],
    )
    complete = HeapWalk(
        free_bytes=16,
        free_blocks=2,
        holes=2,
        blocks=[
            HeapBlock(address=0x1000, size=8, allocated=False),
            HeapBlock(address=0x1008, size=8, allocated=True),
            HeapBlock(address=0x1010, size=8, allocated=False),
        ],
        incomplete_reason=None,
    )
    partial = replace(
        complete, incomplete_reason="heap_5: only the first region is walked"
    )

    monkeypatch.setattr(heap, "heap_geometry", lambda _layout: geom)
    monkeypatch.setattr(heap, "_heap_initialised", lambda _geom: True)
    monkeypatch.setattr(heap, "lookup_symbol", lambda _name: object())
    monkeypatch.setattr(heap, "read_int", lambda _value: 16)

    monkeypatch.setattr(heap, "walk_free_list", lambda _geom, _layout=None: free_walk)
    monkeypatch.setattr(heap, "walk_linear", lambda *_a, **_k: complete)
    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(heap_kind=5), (10, 3, 1)))
    assert snap.total == 0x1018 - 0x1000
    assert snap.cross_check == "ok"

    monkeypatch.setattr(heap, "walk_linear", lambda *_a, **_k: partial)
    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(heap_kind=5), (10, 3, 1)))
    assert snap.total is None
    assert "partial" in snap.cross_check


# ---------------------------------------------------------------------------
# cross-validate
# ---------------------------------------------------------------------------


def _consistent_walks():
    free_list = HeapWalk(
        free_bytes=100,
        free_blocks=2,
        blocks=[
            HeapBlock(address=0x2000, size=50, allocated=False),
            HeapBlock(address=0x2030, size=50, allocated=False),
        ],
    )
    linear = HeapWalk(
        free_bytes=100,
        free_blocks=2,
        holes=2,
        blocks=[
            HeapBlock(address=0x2000, size=50, allocated=False),
            HeapBlock(address=0x2030, size=50, allocated=False),
            HeapBlock(address=0x2060, size=16, allocated=True),
        ],
    )
    return free_list, linear


def test_cross_validate_ok_and_mismatch():
    """Consistent sums+sets are ``ok``; a one-sided change reports the three
    concrete numbers and drives heap_status to ``corrupt``."""
    geom = _heap4_geom()
    free_list, linear = _consistent_walks()
    assert heap.cross_validate(free_list, linear, 100, geom) == "ok"

    linear.free_bytes = 90
    verdict = heap.cross_validate(free_list, linear, 100, geom)
    assert verdict.startswith("mismatch:")
    assert "90" in verdict and "100" in verdict

    # mismatch between counter and walks (all three numbers shown).
    free_list.free_bytes = 80
    linear.free_bytes = 90
    verdict = heap.cross_validate(free_list, linear, 100, geom)
    assert verdict.startswith("mismatch: free-list 80 vs linear 90 vs counter 100")

    # address-set disagreement surfaces too.
    free_list, linear = _consistent_walks()
    linear.blocks[0].allocated = True
    verdict = heap.cross_validate(free_list, linear, 100, geom)
    assert "addresses differ" in verdict

    snap = HeapSnapshot(
        geometry=geom, free=100, free_list=free_list, linear=linear, initialised=True
    )
    snap.cross_check = verdict
    assert heap.heap_status(snap) == "corrupt"


# ---------------------------------------------------------------------------
# uninitialised early exits
# ---------------------------------------------------------------------------


def test_heap4_linear_base_is_uc_heap_not_free_list_head(monkeypatch):
    """heap_4 carves allocations from the FRONT of the first free block
    (heap_4.c pvPortMalloc), so after any allocation the free-list head sits
    above allocated blocks at the heap base; the linear walk must start at
    align_up(&ucHeap) (prvHeapInit's pucAlignedHeap), not at the head."""
    _minimal_geometry_probes(monkeypatch)
    monkeypatch.setattr(
        heap, "value_address", lambda value: getattr(value, "address", 0)
    )

    def lookup(name):
        if name == "ucHeap":
            return _FakeValue(sizeof=0x1000, address=0x2000)
        if name == "xStart":
            return _FakeValue({"pxNextFreeBlock": 0x2100})
        if name == "pxEnd":
            return _FakeValue(scalar=0x2140)
        if name == "xHeapStructSize":
            return _FakeValue(scalar=8)
        return None

    monkeypatch.setattr(heap, "lookup_symbol", lookup)
    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=4), (10, 3, 1)))
    assert geom.heap_low == 0x2100  # the free-list head
    assert geom.linear_low == 0x2000  # the kernel's own heap base


def test_walk_linear_enumerates_blocks_below_free_list_head(monkeypatch):
    """Allocated blocks below the free-list head appear in the linear walk
    and the block table: the walk tiles from the heap base, and the
    cross-check still holds because only free bytes/addresses are
    compared."""
    mem = {
        0x2000: struct.pack("<II", 0x0, 0x80000000 | 0x100),  # allocated
        0x2100: struct.pack("<II", 0x2140, 0x40),  # free == free-list head
    }
    _patch_read_bytes(monkeypatch, mem)
    geom = _heap4_geom(
        heap_low=0x2100, linear_low=0x2000, heap_limit=0x2140, free_end=0x2140
    )
    free_list = heap.walk_free_list(geom)
    assert [block.address for block in free_list.blocks] == [0x2100]
    walk = heap.walk_linear(geom, free_list=free_list)
    assert not walk.corrupt
    assert [block.address for block in walk.blocks] == [0x2000, 0x2100]
    assert walk.blocks[0].allocated is True
    assert walk.free_bytes == 0x40
    assert walk.holes == 1
    assert heap.cross_validate(free_list, walk, 0x40, geom) == "ok"


def test_heap2_linear_base_comes_from_uc_heap(monkeypatch):
    """heap_2's free list is size-sorted, so its head is not the heap base;
    the linear walk starts at align_up(&ucHeap) (the kernel's own function-
    local pucAlignedHeap, heap_2.c prvHeapInit)."""
    monkeypatch.setattr(heap, "lookup_symbol", lambda _name: None)
    _minimal_geometry_probes(monkeypatch)
    monkeypatch.setattr(
        heap, "value_address", lambda value: getattr(value, "address", 0)
    )

    monkeypatch.setattr(
        heap,
        "lookup_symbol",
        lambda name: (
            _FakeValue(sizeof=49152, address=0x20000058) if name == "ucHeap" else None
        ),
    )
    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=2), (10, 3, 1)))
    assert geom.linear_low == 0x20000058  # already 8-aligned
    assert geom.heap_limit == 0x20000058 + 49144

    monkeypatch.setattr(
        heap,
        "lookup_symbol",
        lambda name: (
            _FakeValue(sizeof=49152, address=0x20000056) if name == "ucHeap" else None
        ),
    )
    geom = heap.heap_geometry(build_layout(FreeRtosConfig(heap_kind=2), (10, 3, 1)))
    assert geom.linear_low == 0x20000058  # rounded up to portBYTE_ALIGNMENT


def test_heap2_init_detection_falls_back_to_x_end_size(monkeypatch):
    """At -Og the static init guard can fold to a non-debugging symbol, so
    the flag is unreadable; xEnd.xBlockSize (written by prvHeapInit) decides
    between initialised and pre-init."""
    _minimal_geometry_probes(monkeypatch)
    values = {
        "xStart": _FakeValue({"pxNextFreeBlock": 0x3000}),
        "xEnd": _FakeValue({"xBlockSize": 49144}),
        "xFreeBytesRemaining": _FakeValue(scalar=49144),
    }
    monkeypatch.setattr(heap, "lookup_symbol", _symbol_map(values))
    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(heap_kind=2), (10, 3, 1)))
    assert snap.initialised is True

    values["xEnd"] = _FakeValue({"xBlockSize": 0})
    values["xStart"] = _FakeValue({"pxNextFreeBlock": 0})
    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(heap_kind=2), (10, 3, 1)))
    assert snap.initialised is False
    assert snap.cross_check == "unavailable: heap not initialised"


@pytest.mark.parametrize(
    ("kind", "symbols", "version"),
    [
        (4, {"pxEnd": 0, "xFreeBytesRemaining": 0}, (11, 1, 0)),
        (
            2,
            {"xHeapHasBeenInitialised": 0, "xFreeBytesRemaining": 49144},
            (10, 3, 1),
        ),
    ],
)
def test_uninitialised_heap_short_circuits(monkeypatch, kind, symbols, version):
    """pxEnd == NULL (heap_4/5) or xHeapHasBeenInitialised == pdFALSE
    (heap_2): no walk, static counters only, CrossCheck marked."""
    monkeypatch.setattr(
        heap,
        "lookup_symbol",
        lambda name: (
            _FakeValue({"pxNextFreeBlock": 0})
            if name == "xStart"
            else _FakeValue(scalar=symbols[name])
            if name in symbols
            else None
        ),
    )
    _minimal_geometry_probes(monkeypatch)

    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(heap_kind=kind), version))
    assert snap.initialised is False
    assert snap.free_list is None and snap.linear is None
    assert snap.cross_check == "unavailable: heap not initialised"
    pairs = dict(heap.heap_pairs(snap))
    assert pairs["Blocks"] == "N/A"
    assert pairs["Holes"] == "N/A"
    if kind == 2:
        assert pairs["FreeSize"] == "49144"  # static initializer


# ---------------------------------------------------------------------------
# kind-specific reports
# ---------------------------------------------------------------------------


def test_heap1_reports_bump_pointer(monkeypatch):
    """heap_1 has no free list; FreeSize comes from the bump pointer and
    Blocks/Holes/CrossCheck are unavailable."""
    monkeypatch.setattr(
        heap,
        "lookup_symbol",
        lambda name: (
            _FakeValue(scalar=2000)
            if name == "xNextFreeByte"
            else _FakeValue(sizeof=49152)
            if name == "ucHeap"
            else None
        ),
    )
    _minimal_geometry_probes(monkeypatch)

    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(heap_kind=1), (10, 3, 1)))
    assert snap.total == 49144  # configADJUSTED_HEAP_SIZE
    assert snap.free == 47144
    pairs = dict(heap.heap_pairs(snap))
    assert pairs["Algorithm"] == "heap_1 (bump pointer)"
    assert pairs["Blocks"] == "N/A"
    assert pairs["Holes"] == "N/A"
    assert pairs["CrossCheck"].startswith("unavailable")
    assert heap.heap_status(snap) == "good"


def test_heap3_reports_opaque(monkeypatch):
    """heap_3 wraps malloc and exports no kernel heap: every counter is
    unavailable and the snapshot explains why."""
    monkeypatch.setattr(heap, "symbol_exists", lambda name: name == "pvPortMalloc")
    monkeypatch.setattr(heap, "lookup_symbol", lambda _name: None)
    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(), (10, 3, 1)))
    for key in ("TotalSize", "FreeSize", "MinEver", "Allocs", "Frees"):
        assert dict(heap.heap_pairs(snap))[key] == "N/A"
    assert heap.heap_status(snap) is None

    monkeypatch.setattr(heap, "symbol_exists", lambda _name: False)
    snap = heap.heap_snapshot(build_layout(FreeRtosConfig(), (10, 3, 1)))
    assert dict(heap.heap_pairs(snap))["Algorithm"] == "none"
    assert heap.heap_status(snap) is None


@pytest.mark.parametrize("kind", [1, 2, 4, 5, None])
def test_heap_pairs_key_order(kind):
    """The ten-key order is stable across every kind."""
    algorithm = "heap_3" if kind is None else f"heap_{kind}"
    geom = HeapGeometry(kind=kind, algorithm=algorithm)
    snap = HeapSnapshot(
        geometry=geom,
        total=100,
        free=50,
        min_ever=20,
        allocs=3,
        frees=2,
        cross_check="ok",
    )
    keys = [key for key, _value in heap.heap_pairs(snap)]
    assert keys == _HEAP_KEYS


def test_protector_cell_states():
    assert heap._protector_cell(HeapGeometry(kind=4, algorithm="heap_4")) == "N/A"
    assert (
        heap._protector_cell(
            HeapGeometry(kind=4, algorithm="heap_4", canary_present=True, canary=0xBEEF)
        )
        == "enabled"
    )
    assert (
        heap._protector_cell(
            HeapGeometry(kind=4, algorithm="heap_4", canary_present=True, canary=0)
        )
        == "enabled(canary=0)"
    )


def test_heap_kind2_pairs_unavailable_without_symbols():
    """heap_2 has no min/alloc/free counters; the keys stay and render
    unavailable so the column order never drifts."""
    geom = HeapGeometry(kind=2, algorithm="heap_2")
    snap = HeapSnapshot(geometry=geom, total=49144, free=49144, cross_check="ok")
    pairs = dict(heap.heap_pairs(snap))
    assert pairs["MinEver"] == "N/A"
    assert pairs["Allocs"] == "N/A"
    assert pairs["Frees"] == "N/A"
    assert pairs["Protector"] == "N/A"


def test_heap_block_table_shapes():
    """The table prefers the linear walk; the free list is its fallback."""
    linear = HeapWalk(
        free_blocks=1,
        holes=1,
        blocks=[
            HeapBlock(address=0x2000, size=50, allocated=False),
            HeapBlock(address=0x2030, size=16, allocated=True),
        ],
    )
    snap = HeapSnapshot(geometry=_heap4_geom(), linear=linear, cross_check="ok")
    table = heap.heap_block_table(snap)
    assert table.headers == ["Address", "Size", "State"]
    assert table.rows[0][2] == "free"
    assert table.rows[1][2] == "alloc"

    free_list = HeapWalk(
        free_bytes=8,
        free_blocks=1,
        largest_free=8,
        blocks=[HeapBlock(address=0x1000, size=8, allocated=False, next_free=0x1020)],
    )
    snap = HeapSnapshot(
        geometry=_heap5_geom(),
        free_list=free_list,
        cross_check="unavailable: heap state not collected",
    )
    table = heap.heap_block_table(snap)
    assert table.headers == ["Address", "Size", "Next"]
    assert "largest 8" in table.messages[0]

    snap = HeapSnapshot(geometry=_heap4_geom())
    assert heap.heap_block_table(snap) is None


# ---------------------------------------------------------------------------
# commands wiring
# ---------------------------------------------------------------------------


def test_render_heap_prints_pairs_and_table(monkeypatch):
    from gdr import commands as gdr_commands

    adapter = adapter_module.FreeRtosAdapter(
        build_layout(FreeRtosConfig(heap_kind=4), (10, 3, 1))
    )
    snap = HeapSnapshot(
        geometry=_heap4_geom(),
        total=49152,
        free=1000,
        initialised=True,
        cross_check="ok",
        linear=HeapWalk(
            free_blocks=1,
            holes=1,
            blocks=[HeapBlock(address=0x2000, size=8, allocated=False)],
        ),
    )
    monkeypatch.setattr(heap, "heap_snapshot", lambda _layout: snap)
    # The neutral renderer (C1) calls the adapter's HeapReport and prints
    # through the gdr.commands namespace.
    monkeypatch.setattr(gdr_commands, "active", lambda: adapter)
    rendered_pairs: dict[str, str] = {}
    rendered_table: dict[str, object] = {}
    monkeypatch.setattr(
        gdr_commands, "print_detail", lambda pairs: rendered_pairs.update(pairs)
    )
    monkeypatch.setattr(
        gdr_commands,
        "print_table",
        lambda rows, headers, **_kw: rendered_table.update(rows=rows, headers=headers),
    )
    monkeypatch.setattr(gdr_commands, "info", lambda _msg: None)
    gdr_commands.render_heap()
    assert rendered_pairs["Algorithm"] == "heap_4"
    assert rendered_pairs["CrossCheck"] == "ok"
    assert rendered_table["headers"] == ["Address", "Size", "State"]


def test_render_heap_warns_without_adapter(monkeypatch):
    from gdr import commands as gdr_commands

    monkeypatch.setattr(gdr_commands, "active", lambda: None)
    warnings: list[str] = []
    monkeypatch.setattr(gdr_commands, "warn", lambda msg: warnings.append(msg))
    gdr_commands.render_heap()
    assert any("gdr init" in message for message in warnings)


def test_help_mentions_no_owner_attribution():
    """The heap topic explains the allocator ownership limitation."""
    output = commands.render_terminal(commands._HELP_TREE, ("heap",))

    assert commands._COMMAND_DESCRIPTIONS["heap"] == "Inspect the FreeRTOS allocator"
    assert "no owner field" in output
    assert "not implemented" not in output


# ---------------------------------------------------------------------------
# system summary
# ---------------------------------------------------------------------------


def _summary_adapter(monkeypatch, layout):
    monkeypatch.setattr(adapter_module, "iter_converted_tasks", lambda _l: iter([]))
    monkeypatch.setattr(adapter_module, "list_count", lambda _key, _layout: 0)
    monkeypatch.setattr(adapter_module, "system_value", lambda _key, _layout: 0)
    # The system checks read scheduler globals (lookup_symbol) that cannot
    # run outside GDB; they are exercised by test_diagnostics, so the
    # summary tests stub them to "no checks" and focus on the heap fields.
    monkeypatch.setattr(adapter_module, "system_checks", lambda _layout: [])
    return adapter_module.FreeRtosAdapter(layout)


def test_system_summary_exposes_heap_fields(monkeypatch):
    """heap_used = total - free only when both are trustworthy."""
    layout = build_layout(FreeRtosConfig(heap_kind=4), (10, 3, 1))
    adapter = _summary_adapter(monkeypatch, layout)
    snap = HeapSnapshot(
        geometry=HeapGeometry(kind=4, algorithm="heap_4"),
        total=49152,
        free=1000,
        initialised=True,
        cross_check="ok",
    )
    monkeypatch.setattr(heap, "heap_snapshot", lambda _l: snap)
    summary = adapter.system_summary()
    assert summary.heap_allocator == "heap_4"
    assert summary.heap_used == 49152 - 1000
    assert summary.heap_total == 49152
    assert summary.heap_status == "good"

    # An untrustworthy free leaves heap_used None so the renderer omits it
    # (gdr.commands only prints Heap used/total when not None).
    snap.free = None
    summary = adapter.system_summary()
    assert summary.heap_used is None
    assert summary.heap_total == 49152
    assert summary.heap_status == "good"


def test_system_summary_omits_heap_used_when_uninitialised(monkeypatch):
    """Pre-init the counters hold their static initializers (heap_4's
    xFreeBytesRemaining is 0), so total - free would report the whole heap
    as used; the summary must leave heap_used unset instead."""
    layout = build_layout(FreeRtosConfig(heap_kind=4), (10, 3, 1))
    adapter = _summary_adapter(monkeypatch, layout)
    snap = HeapSnapshot(
        geometry=HeapGeometry(kind=4, algorithm="heap_4"),
        total=49152,
        free=0,
        initialised=False,
    )
    monkeypatch.setattr(heap, "heap_snapshot", lambda _l: snap)
    summary = adapter.system_summary()
    assert summary.heap_used is None
    assert summary.heap_total == 49152
    assert summary.heap_status is None


def test_system_summary_heap3_allocator_label(monkeypatch):
    """heap_kind None splits on pvPortMalloc: heap_3 vs unavailable."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    adapter = _summary_adapter(monkeypatch, layout)
    snap = HeapSnapshot(
        geometry=HeapGeometry(kind=None, algorithm="heap_3"), initialised=False
    )
    monkeypatch.setattr(heap, "heap_snapshot", lambda _l: snap)
    assert adapter.system_summary().heap_allocator == "heap_3"

    snap.geometry = HeapGeometry(kind=None, algorithm="none")
    assert adapter.system_summary().heap_allocator is None
