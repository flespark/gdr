"""FreeRTOS heap snapshotting: free-list walk, linear walk and cross-check.

Decodes the ``portable/MemMang`` heap managers (heap_1..heap_5) for the
``frt heap`` command and the ``Heap ``* fields of ``frt system``.  The
kernel's own statistics entry points (``vPortGetHeapStats``,
``xPortGetFreeHeapSize``) are never called -- they are inferior function
calls -- so the same semantics are replicated here from the static data the
kernel exports (``xStart``/``xEnd``/``pxEnd````, ``xFreeBytesRemaining``, ...)
plus a bounded raw-memory walk of the block chain, mirroring the free-list
traversal of ``vPortGetHeapStats`` (heap_4.c / heap_5.c).

Every GDB entry point goes through module-level helpers (``lookup_symbol``,
``read_int``, ``read_bytes``, ...) so the unit tests can drive the logic
without a GDB session, following ``freertos/timers.py``.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import Literal

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from gdr.adapter_api import ObjectTable
from gdr.constants import GDR_MAX_TRAVERSAL_COUNT
from gdr.gdb_bridge import (
    get_arch_info,
    is_plain_identifier,
    lookup_symbol,
    lookup_type,
    read_bytes,
    read_int,
    read_macro_int,
    read_macro_text_in_source,
    symbol_exists,
    value_address,
)

if gdb is not None:
    _HEAP_ERRORS: tuple[type[BaseException], ...] = (
        gdb.error,
        gdb.MemoryError,
        IndexError,
        TypeError,
        ValueError,
        AttributeError,
    )
else:
    _HEAP_ERRORS = (IndexError, TypeError, ValueError, AttributeError)

# heap_2 only tracks allocation with the size_t MSB from V10.5.0
# (heap_2.c heapBLOCK_ALLOCATED_BITMASK); the V10.3.x/V10.4.x releases store
# the whole ``xBlockSize`` usefully, so the mask must be off for them.
_HEAP2_ALLOCATED_BIT_SINCE = (10, 5, 0)

# heap_2 renamed its structure-size constant to ``xHeapStructSize`` in
# V11.0.0; earlier releases spell it ``heapSTRUCT_SIZE`` (uint16_t).
_HEAP2_STRUCT_SIZE_RENAMED = (11, 0, 0)


@dataclass(frozen=True)
class HeapGeometry:
    """Version/config differences for one heap manager.

    ``heap_low`` is the free-list head (``xStart.pxNextFreeBlock`` after
    canary removal), while ``linear_low`` is the start address of a linear
    walk: for heap_2/heap_4 it is the kernel's own heap base
    (``align_up(&ucHeap)``, prvHeapInit's ``pucAlignedHeap``) -- which only
    equals the free-list head before the first allocation, because heap_4
    carves allocations from the front of the first free block. For heap_5
    the linear walk needs the protector's ``pucHeapLowAddress`` (only
    meaningful with ``configENABLE_HEAP_PROTECTOR``; otherwise region bases
    are unknowable).
    ``free_end`` is the free-list terminator (``pxEnd`` for heap_4/5, the
    address of the ``xEnd`` *value* for heap_2).  ``heap_limit`` is the
    exclusive upper bound of a linear walk.
    """

    kind: int | None
    algorithm: str
    struct_size: int | None = None
    allocated_bitmask: int = 0
    canary: int = 0
    canary_present: bool = False
    heap_low: int | None = None
    linear_low: int | None = None
    heap_limit: int | None = None
    free_end: int | None = None
    pointer_bits: int = 0
    alignment: int | None = None
    alignment_assumed: bool = False
    total: int | None = None
    x_end_size: int | None = None  # heap_2: xEnd.xBlockSize (runtime adjusted)
    version: tuple[int, int, int] | None = None


@dataclass
class HeapBlock:
    address: int
    size: int  # allocated bit stripped; includes the block header
    allocated: bool
    next_free: int | None = None  # canary already removed


@dataclass
class HeapWalk:
    blocks: list[HeapBlock] = field(default_factory=list)
    free_bytes: int | None = None
    free_blocks: int = 0
    largest_free: int | None = None
    smallest_free: int | None = None  # heap_5 skips the zero-size link blocks
    holes: int | None = None  # free segments in the linear walk
    truncated: bool = False
    corrupt: bool = False
    corrupt_reason: str | None = None
    incomplete_reason: str | None = None


@dataclass
class HeapSnapshot:
    geometry: HeapGeometry
    total: int | None = None
    free: int | None = None
    min_ever: int | None = None  # heap_4/5 only
    allocs: int | None = None  # heap_4/5 only
    frees: int | None = None  # heap_4/5 only
    initialised: bool = True
    unavailable_reason: str | None = None
    free_list: HeapWalk | None = None
    linear: HeapWalk | None = None
    cross_check: str = "unavailable"


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def _deobfuscate(pointer: int | None, canary: int) -> int:
    """Undo ``heapPROTECT_BLOCK_POINTER`` (``p ^ xHeapCanary``).

    A zero canary makes the XOR an identity, and without a protector the
    geometry canary is 0, so one code path serves all kinds.  A NULL pointer
    stays NULL even with a nonzero canary (pre-init heads are refused
    earlier by ``_free_list_head``).
    """
    if not pointer:
        return 0
    return pointer ^ canary


def _bits_from_type(type_name: str) -> int | None:
    """Return ``sizeof(T) * 8`` for a DWARF type, or None."""
    typ = lookup_type(type_name)
    if typ is None:
        return None
    try:
        return int(typ.sizeof) * 8
    except _HEAP_ERRORS:
        return None


def _pointer_bits() -> int:
    """Return the ``size_t`` width in bits (mask arithmetic source).

    ``size_t`` is the width the kernel's ``heapBLOCK_ALLOCATED_BITMASK`` is
    computed from; without DWARF for it the target pointer width stands in.
    """
    bits = _bits_from_type("size_t")
    if bits is not None:
        return bits
    arch = get_arch_info()
    if arch is not None:
        return arch.ptrsize * 8
    return 32  # assumed: every supported fixture lane is 32-bit


def _target_endian() -> Literal["little", "big"]:
    """Target byte order for raw BlockLink_t decoding."""
    info = get_arch_info()
    if info is None:
        return "little"
    return "little" if info.endian == "little" else "big"


def _block_values(address: int, geom: HeapGeometry) -> tuple[int, int] | None:
    """Read ``(pxNextFreeBlock, xBlockSize)`` at a raw BlockLink_t address.

    ``struct A_BLOCK_LINK`` is exactly ``{pointer, size_t}`` (heap_4.c/
    heap_5.c/heap_2.c); on every supported port ``size_t`` is pointer-width,
    so the two members are two pointer-width fields at offsets 0/ptrsize.
    """
    width = geom.pointer_bits // 8
    if width not in (4, 8):
        return None
    raw = read_bytes(address, width * 2)  # raw target memory, never a file
    if raw is None:
        return None
    endian = _target_endian()
    next_free = int.from_bytes(raw[:width], byteorder=endian)
    size = int.from_bytes(raw[width:], byteorder=endian)
    return next_free, size


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def _port_alignment() -> int | None:
    """Read ``portBYTE_ALIGNMENT`` (first from the current CU, then heap CU).

    The macro is only in the DWARF macro table after a heap-CU source line
    is selected (``-g3`` fixtures), so the text probe mirrors the version
    probe in ``freertos.version``.
    """
    value = read_macro_int("portBYTE_ALIGNMENT")
    if value is not None:
        return value
    text = read_macro_text_in_source("portBYTE_ALIGNMENT", "pvPortMalloc")
    if text is None:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def _macro_int_in_heap_cu(name: str) -> int | None:
    """Evaluate an integer macro defined in the heap manager's CU, or None.

    GDB's macro table is scoped to the current source location, so a bare
    ``read_macro_int`` misses heap_* macros unless the user happens to be
    stopped in a CU that saw ``FreeRTOSConfig.h``.  This selects a function
    every heap manager defines (``pvPortMalloc``), evaluates the identifier
    there (GDB expands the macro, including references to other macros --
    the fixture's ``configADJUSTED_HEAP_SIZE`` expands to a casted
    expression), and restores the user's source view.
    """
    value = read_macro_int(name)
    if value is not None:
        return value
    if gdb is None or not is_plain_identifier(name):
        return None
    saved_source: str | None = None
    saved_listsize: str | None = None
    try:
        source_info = gdb.execute("info source", to_string=True)
        match = re.search(r"Current source file is (.+)$", source_info, re.M)
        if match:
            saved_source = match.group(1).strip().removesuffix(".")
        saved_listsize = gdb.execute("show listsize", to_string=True)
        gdb.execute("set listsize 1", to_string=True)
        gdb.execute("list pvPortMalloc", to_string=True)
        return read_macro_int(name)
    except _HEAP_ERRORS:
        return None
    finally:
        if saved_listsize:
            match = re.search(r"(?:listsize is|default is) (.+?)\.", saved_listsize)
            if match:
                with contextlib.suppress(*_HEAP_ERRORS):
                    gdb.execute(f"set listsize {match.group(1)}", to_string=True)
        if saved_source:
            with contextlib.suppress(*_HEAP_ERRORS):
                gdb.execute(f"list {saved_source}:1", to_string=True)


def _heap_array_size() -> int | None:
    """Return ``sizeof(ucHeap)`` (== ``configTOTAL_HEAP_SIZE``), or None.

    heap_1/heap_2/heap_4 all declare ``ucHeap`` (heap_4 as ``extern`` under
    ``configAPPLICATION_ALLOCATED_HEAP``); the array's ``sizeof`` is the raw
    heap byte extent without needing a macro read.
    """
    value = lookup_symbol("ucHeap")
    if value is not None:
        try:
            return int(value.type.sizeof)
        except _HEAP_ERRORS:
            return None
    return _macro_int_in_heap_cu("configTOTAL_HEAP_SIZE")


def _adjusted_heap_size(alignment: int | None) -> int | None:
    """Return ``configADJUSTED_HEAP_SIZE`` (heap_1/heap_2 usable extent).

    Derivation order: the macro evaluated in the heap CU, then
    ``sizeof(ucHeap) - portBYTE_ALIGNMENT`` (the macro's own definition,
    heap_1.c / heap_2.c).  The generic 8-byte port alignment is only ever
    assumed as a last resort, matching the ARM/RISC-V GCC ports.
    """
    adjusted = _macro_int_in_heap_cu("configADJUSTED_HEAP_SIZE")
    if adjusted is not None:
        return adjusted
    raw = _heap_array_size()
    if raw is None:
        return None
    align = alignment or 8
    return max(raw - align, 0)


def _allocated_bitmask(
    pointer_bits: int, kind: int | None, version: tuple[int, int, int] | None
) -> int:
    """Return the size_t-MSB allocation mask, or 0 where no bit exists.

    heap_4/heap_5 track allocation with the MSB of ``xBlockSize`` in every
    supported release (``heapBLOCK_ALLOCATED_BITMASK``).  heap_2 only gained
    the bit in V10.5.0; older releases use the full width for the size, so a
    mask must not be applied there (it would fabricate corruption and
    misread sizes).  An unknown version is treated as pre-bit (the
    conservative direction: membership in the free list remains the signal).
    """
    if kind == 2 and (version is None or version < _HEAP2_ALLOCATED_BIT_SINCE):
        return 0
    return 1 << (pointer_bits - 1)


def _struct_size(
    kind: int | None, version: tuple[int, int, int] | None, alignment: int | None
) -> int | None:
    """Return the block-header size ``xHeapStructSize``, or None.

    Preference: the symbol (``xHeapStructSize`` for heap_4/5 in every
    version, heap_2 only from V11; ``heapSTRUCT_SIZE`` for heap_2 before
    that), then the kernel's own definition -- ``align_up(sizeof(BlockLink_t),
    portBYTE_ALIGNMENT)`` (heap_4.c / heap_5.c / heap_2.c).  With no type
    info at all the header size is unknown and the linear walk is refused.
    """
    names: list[str]
    if kind in (4, 5):
        names = ["xHeapStructSize"]
    elif kind == 2:
        if version is None:
            names = ["xHeapStructSize", "heapSTRUCT_SIZE"]
        elif version >= _HEAP2_STRUCT_SIZE_RENAMED:
            names = ["xHeapStructSize"]
        else:
            names = ["heapSTRUCT_SIZE"]
    else:
        return None
    for name in names:
        raw = read_int(lookup_symbol(name))
        if raw and raw > 0:
            return raw
    typ = lookup_type("BlockLink_t") or lookup_type("struct A_BLOCK_LINK")
    if typ is None:
        return None
    try:
        size = int(typ.sizeof)
    except _HEAP_ERRORS:
        return None
    # Reason: ``sizeof(BlockLink_t)`` is already a multiple of the natural
    # alignment and on the GCC ports ``portBYTE_ALIGNMENT`` never exceeds it,
    # so using it as the rounding modulus when the macro is missing yields
    # the same result without inventing a port constant.
    align = alignment or size
    return (size + align - 1) & ~(align - 1)


def _free_list_head(canary: int) -> int | None:
    """Return the deobfuscated free-list head (``xStart.pxNextFreeBlock``).

    Identical for heap_2/4/5; ``None`` pre-init (head is NULL and a nonzero
    canary would XOR it into a garbage address, so the walk refuses to run
    until ``xStart`` carries a real block).
    """
    value = lookup_symbol("xStart")
    if value is None:
        return None
    try:
        raw = int(value["pxNextFreeBlock"])
    except _HEAP_ERRORS:
        return None
    if not raw:
        return None
    return raw ^ canary if canary else raw


def _aligned_heap_base(alignment: int | None) -> int | None:
    """Return ``align_up(&ucHeap, portBYTE_ALIGNMENT)``, or None.

    heap_2's free list is sorted by *size* (heap_2.c prvInsertBlockIntoFreeList),
    so its chain head is not the heap's physical start the way heap_4's is;
    the kernel's own base ("pucAlignedHeap", a function-local variable) is
    ``&ucHeap`` rounded up to the port alignment, which is reproducible from
    the symbol.
    """
    value = lookup_symbol("ucHeap")
    if value is None:
        return None
    base = value_address(value)
    if not base:
        return None
    align = alignment or 8
    return (base + align - 1) & ~(align - 1)


def _heap5_protector_extent(
    canary_present: bool,
) -> tuple[int | None, int | None]:
    """Return ``(linear_low, heap_limit)`` for heap_5, or ``(None, None)``.

    Without ``configENABLE_HEAP_PROTECTOR`` the kernel exports no region
    bounds (``pucHeapLowAddress``/``pucHeapHighAddress`` only exist under
    the protector, heap_5.c), and region bases are not recoverable from any
    symbol, so no linear walk is possible.  With it, the extreme bounds give
    the first-region base and the overall upper bound; the walk still stops
    at the first zero-size region-end marker.
    """
    if not canary_present:
        return None, None
    low = read_int(lookup_symbol("pucHeapLowAddress"))
    high = read_int(lookup_symbol("pucHeapHighAddress"))
    return (low, high) if (low and high) else (None, None)


def heap_geometry(layout: FreeRtosLayout) -> HeapGeometry:
    """Assemble every version/config difference into one geometry object."""
    cfg = layout.config
    version = layout.version
    kind = cfg.heap_kind
    if kind is None:
        # Reason: the layout discriminator reports None for both heap_3
        # (wraps malloc, exports no kernel heap symbol) and a build with no
        # allocator at all; pvPortMalloc presence is the only split
        # (heap_3.c defines it, a static-only build does not).
        algorithm = "heap_3" if symbol_exists("pvPortMalloc") else "none"
        return HeapGeometry(kind=None, algorithm=algorithm, version=version)
    pointer_bits = _pointer_bits()
    alignment = _port_alignment()
    canary_present = symbol_exists("xHeapCanary")
    canary = read_int(lookup_symbol("xHeapCanary")) or 0 if canary_present else 0
    mask = _allocated_bitmask(pointer_bits, kind, version)
    struct_size = _struct_size(kind, version, alignment)
    head = _free_list_head(canary)
    free_end: int | None = None
    linear_low: int | None = None
    heap_limit: int | None = None
    total: int | None = None
    x_end_size: int | None = None
    if kind in (4, 5):
        px_end = read_int(lookup_symbol("pxEnd"))
        free_end = px_end
        if kind == 4:
            heap_limit = px_end
            total = _heap_array_size()
            # Reason: heap_4 carves every allocation from the FRONT of the
            # first free block (heap_4.c pvPortMalloc: the remainder replaces
            # the block in the list), so after any allocation the free-list
            # head sits above allocated blocks at the heap base. The linear
            # walk must start at the kernel's own base (align_up(&ucHeap),
            # prvHeapInit's pucAlignedHeap); without the ucHeap symbol the
            # base is unknowable and the walk is skipped (mirroring heap_5
            # without the protector) rather than silently truncated.
            linear_low = _aligned_heap_base(alignment)
        else:
            linear_low, heap_limit = _heap5_protector_extent(canary_present)
    elif kind == 2:
        free_end = value_address(lookup_symbol("xEnd")) or None
        linear_low = _aligned_heap_base(alignment)
        adjusted = _adjusted_heap_size(alignment)
        x_end_size = _read_x_end_block_size()
        limit_size = x_end_size if x_end_size and x_end_size > 0 else adjusted
        if linear_low is not None and limit_size is not None:
            heap_limit = linear_low + limit_size
        total = adjusted
    elif kind == 1:
        total = _adjusted_heap_size(alignment)
    return HeapGeometry(
        kind=kind,
        algorithm=f"heap_{kind}",
        struct_size=struct_size,
        allocated_bitmask=mask,
        canary=canary,
        canary_present=canary_present,
        heap_low=head,
        linear_low=linear_low,
        heap_limit=heap_limit,
        free_end=free_end,
        pointer_bits=pointer_bits,
        alignment=alignment,
        alignment_assumed=alignment is None,
        total=total,
        x_end_size=x_end_size if kind == 2 else None,
        version=version,
    )


def _read_x_end_block_size() -> int | None:
    """Return heap_2's ``xEnd.xBlockSize`` (the runtime adjusted size)."""
    value = lookup_symbol("xEnd")
    if value is None:
        return None
    try:
        return int(value["xBlockSize"])
    except _HEAP_ERRORS:
        return None


def _heap_initialised(geom: HeapGeometry) -> bool:
    """Whether the heap manager has run its init routine."""
    if geom.kind == 2:
        flag = read_int(lookup_symbol("xHeapHasBeenInitialised"))
        if flag is not None:
            return bool(flag)
        # Reason: at -Og the kernel's -g3 fixtures can fold the static guard
        # into a non-debugging symbol (``xHeapHasBeenInitialised.0``), so the
        # flag is frequently unreadable; xEnd.xBlockSize is written by
        # prvHeapInit to configADJUSTED_HEAP_SIZE and reads 0 pre-init, and
        # the free-list head is NULL pre-init.
        if geom.x_end_size is not None:
            return geom.x_end_size != 0
        return geom.heap_low is not None
    if geom.kind in (4, 5):
        # heap_4/heap_5 lazily initialise on the first malloc; pxEnd is NULL
        # until then (heap_4.c static initializer).
        return bool(geom.free_end)
    return True


# ---------------------------------------------------------------------------
# walks
# ---------------------------------------------------------------------------


def walk_free_list(geom: HeapGeometry) -> HeapWalk:
    """Walk the kernel's free-block chain, mirroring ``vPortGetHeapStats``.

    Termination: the chain ends at ``pxEnd`` for heap_4/5 and at the address
    of the ``xEnd`` value for heap_2 (heap_2.c ``pxFirstFreeBlock->
    pxNextFreeBlock = &xEnd``).  heap_5's zero-size region link blocks are
    counted as blocks but skipped for the smallest-size statistic, exactly
    like the kernel's own walk (heap_5.c ``vPortGetHeapStats``).
    """
    walk = HeapWalk()
    if geom.kind not in (2, 4, 5):
        walk.incomplete_reason = "no free list for this heap kind"
        return walk
    low = geom.heap_low
    free_end = geom.free_end
    limit = geom.heap_limit
    if low is None or free_end in (None, 0):
        return walk
    # heap_2's free list is size-sorted, so its head address is not the
    # lowest heap address; the linear base is a stricter lower bound.
    lower = geom.linear_low if geom.linear_low is not None else low
    walk.free_bytes = 0
    seen: set[int] = set()
    cur: int = low
    steps = 0
    while cur != free_end:
        if cur == 0:
            walk.corrupt = True
            walk.corrupt_reason = "free list terminated at NULL before the end marker"
            return walk
        if steps >= GDR_MAX_TRAVERSAL_COUNT:
            walk.truncated = True
            return walk
        if cur in seen:
            walk.corrupt = True
            walk.corrupt_reason = f"free-list cycle at {cur:#x}"
            return walk
        seen.add(cur)
        values = _block_values(cur, geom)
        if values is None:
            walk.corrupt = True
            walk.corrupt_reason = f"unreadable block header at {cur:#x}"
            return walk
        raw_next, raw_size = values
        # Reason: a free-list member must never carry the allocation bit --
        # the kernel clears it on free before insertion (heapFREE_BLOCK), so
        # a set bit is corruption, not a hint.
        if geom.allocated_bitmask and (raw_size & geom.allocated_bitmask):
            walk.corrupt = True
            walk.corrupt_reason = f"free-list member {cur:#x} carries the allocated bit"
            return walk
        size = (
            raw_size & ~geom.allocated_bitmask if geom.allocated_bitmask else raw_size
        )
        if geom.alignment and cur % geom.alignment != 0:
            walk.corrupt = True
            walk.corrupt_reason = f"misaligned free-list block at {cur:#x}"
            return walk
        if limit is not None and (cur < lower or cur >= limit):
            walk.corrupt = True
            walk.corrupt_reason = f"free-list block {cur:#x} outside the heap extent"
            return walk
        next_free = _deobfuscate(raw_next, geom.canary)
        walk.blocks.append(
            HeapBlock(address=cur, size=size, allocated=False, next_free=next_free)
        )
        walk.free_blocks += 1
        walk.free_bytes += size
        if walk.largest_free is None or size > walk.largest_free:
            walk.largest_free = size
        if size and (walk.smallest_free is None or size < walk.smallest_free):
            walk.smallest_free = size
        cur = next_free
        steps += 1
    return walk


def _linear_free_addresses(free_list: HeapWalk | None) -> set[int] | None:
    """Address set of the free-list blocks, for membership discrimination."""
    if free_list is None:
        return None
    return {block.address for block in free_list.blocks}


def walk_linear(geom: HeapGeometry, free_list: HeapWalk | None = None) -> HeapWalk:
    """Walk the linear block extent [linear_low, heap_limit).

    Every byte of the extent belongs to exactly one block (header followed
    by payload), so stepping by ``xBlockSize`` ``& ~mask`` tiles the heap.
    Allocation status comes from the MSB when the mask is in force; before
    heap_2 got the bit (V10.5.0) the header cannot tell free from allocated,
    so membership in the free-list address set is the only signal.

    heap_5 cannot be walked linearly without the protector's region bases
    (see ``_heap5_protector_extent``): ``incomplete_reason`` is set and the
    walk is skipped rather than reporting plausible-looking numbers.  With
    the protector the walk stops at the first zero-size region-end marker,
    marking any walk that did not end at ``pxEnd`` incomplete.
    """
    walk = HeapWalk()
    if geom.kind not in (2, 4, 5):
        walk.incomplete_reason = f"{geom.algorithm} has no block heap"
        return walk
    if geom.kind == 5 and geom.linear_low is None:
        walk.incomplete_reason = (
            "heap_5: region bases unknown (no heap protector); linear walk skipped"
        )
        return walk
    low = geom.linear_low
    limit = geom.heap_limit
    if low is None or limit in (None, 0):
        return walk
    if geom.kind == 5:
        walk.incomplete_reason = "heap_5: only the first region is walked"
    free_addresses = _linear_free_addresses(free_list)
    walk.free_bytes = 0
    walk.holes = 0
    cur: int = low
    steps = 0
    previous_allocated: bool | None = None
    while cur < limit:
        if steps >= GDR_MAX_TRAVERSAL_COUNT:
            walk.truncated = True
            return walk
        if geom.alignment and cur % geom.alignment != 0:
            walk.corrupt = True
            walk.corrupt_reason = f"misaligned block at {cur:#x}"
            return walk
        values = _block_values(cur, geom)
        if values is None:
            walk.corrupt = True
            walk.corrupt_reason = f"unreadable block header at {cur:#x}"
            return walk
        _raw_next, raw_size = values
        size = (
            raw_size & ~geom.allocated_bitmask if geom.allocated_bitmask else raw_size
        )
        if size == 0:
            if geom.kind == 5:
                # A zero-size block is a region-end marker (the final one is
                # pxEnd); a real block never has zero size.
                break
            walk.corrupt = True
            walk.corrupt_reason = f"zero-size block at {cur:#x}"
            return walk
        if cur + size > limit:
            walk.corrupt = True
            walk.corrupt_reason = f"block at {cur:#x} extends past the heap limit"
            return walk
        if geom.allocated_bitmask:
            allocated = bool(raw_size & geom.allocated_bitmask)
        else:
            # Reason: without the MSB the block header is identical for free
            # and allocated blocks, so free-list membership is the only
            # signal (heap_2 < V10.5.0).
            allocated = free_addresses is not None and cur not in free_addresses
        walk.blocks.append(
            HeapBlock(address=cur, size=size, allocated=allocated, next_free=None)
        )
        if not allocated:
            walk.free_bytes += size
            walk.free_blocks += 1
            if previous_allocated is None or previous_allocated:
                walk.holes += 1
        previous_allocated = allocated
        cur += size
        steps += 1
    if geom.kind == 5:
        # Ending at a nonzero count on the region marker vs pxEnd decides
        # whether the walked extent covered the whole heap.
        if cur != geom.free_end:
            walk.incomplete_reason = (
                "heap_5: stopped at a region-end marker before pxEnd"
            )
        else:
            walk.incomplete_reason = None
    elif cur != limit:
        walk.corrupt = True
        walk.corrupt_reason = "linear walk ended before the heap limit"
    return walk


def cross_validate(
    free_list: HeapWalk | None,
    linear: HeapWalk | None,
    free_bytes: int | None,
    geom: HeapGeometry,
) -> str:
    """Three-way consistency: free-list bytes, linear free bytes, counter.

    ``ok`` only when the byte sums agree and the free-block address sets
    match.  Any disagreement reports the concrete numbers (never a blend or
    an average -- ``FreeSize`` keeps the kernel counter).  heap_5 returns
    ``unavailable`` with the region-bases reason because its linear walk is
    skipped (or partial), so no honest verdict can be produced.
    """
    if geom.kind == 5:
        if linear is None or not linear.blocks:
            return "unavailable: heap_5 region bases unknown (no heap protector)"
        if linear.incomplete_reason:
            return "unavailable: heap_5 partial linear walk"
        # A complete single-region walk (protector + one region) reaches the
        # same three-way comparison as heap_2/4.
    if free_list is None or linear is None or free_bytes is None:
        return "unavailable: free list, linear walk or counter unavailable"
    if free_list.corrupt or linear.corrupt:
        return "unavailable: corrupt walk"
    if free_list.truncated or linear.truncated:
        return "unavailable: truncated walk"
    if free_list.free_bytes != linear.free_bytes or free_bytes != free_list.free_bytes:
        return (
            "mismatch: free-list "
            f"{free_list.free_bytes} vs linear {linear.free_bytes} "
            f"vs counter {free_bytes}"
        )
    if free_list.free_blocks != linear.free_blocks:
        return (
            "mismatch: free-block count free-list "
            f"{free_list.free_blocks} vs linear {linear.free_blocks}"
        )
    if geom.allocated_bitmask:
        free_list_addresses = {block.address for block in free_list.blocks}
        linear_free_addresses = {
            block.address for block in linear.blocks if not block.allocated
        }
        if free_list_addresses != linear_free_addresses:
            return (
                "mismatch: free-block addresses differ (free-list "
                f"{len(free_list_addresses)} vs linear {len(linear_free_addresses)})"
            )
    return "ok"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def heap_snapshot(layout: FreeRtosLayout) -> HeapSnapshot:
    """Collect geometry, kernel counters and both walks into one snapshot.

    Ordering matters: the free-list walk runs before the linear walk because
    heap_2 (< V10.5.0) and the heap_5 partial walks need the free-list
    address set to decide free vs allocated.
    """
    geom = heap_geometry(layout)
    if geom.kind is None:
        snap = HeapSnapshot(geometry=geom, initialised=False)
        if geom.algorithm == "heap_3":
            snap.unavailable_reason = (
                "heap_3 wraps the C library malloc; not inspectable"
            )
            snap.cross_check = "unavailable: libc heap is not inspectable"
        else:
            snap.unavailable_reason = "no heap allocator is linked (no pvPortMalloc)"
            snap.cross_check = "unavailable: no heap allocator"
        return snap
    snap = HeapSnapshot(geometry=geom, total=geom.total)
    if not _heap_initialised(geom):
        # Reason: pre-init the counter still holds its static initializer
        # (0 for heap_4/5, configADJUSTED_HEAP_SIZE for heap_2), so
        # TotalSize/FreeSize stay as statically-known values while the block
        # structure is explicitly marked uninitialised.
        snap.initialised = False
        snap.unavailable_reason = "heap not initialised"
        snap.cross_check = "unavailable: heap not initialised"
        if geom.kind in (2, 4, 5):
            snap.free = read_int(lookup_symbol("xFreeBytesRemaining"))
        return snap
    if geom.kind == 1:
        next_byte = read_int(lookup_symbol("xNextFreeByte"))
        if geom.total is not None and next_byte is not None and next_byte <= geom.total:
            snap.free = geom.total - next_byte
        snap.cross_check = (
            "unavailable: heap_1 bump pointer (no free list or block headers)"
        )
        return snap
    snap.free = read_int(lookup_symbol("xFreeBytesRemaining"))
    if geom.kind in (4, 5):
        snap.min_ever = read_int(lookup_symbol("xMinimumEverFreeBytesRemaining"))
        snap.allocs = read_int(lookup_symbol("xNumberOfSuccessfulAllocations"))
        snap.frees = read_int(lookup_symbol("xNumberOfSuccessfulFrees"))
    free_list = walk_free_list(geom)
    linear = walk_linear(geom, free_list=free_list)
    snap.free_list = free_list
    snap.linear = linear
    snap.cross_check = cross_validate(free_list, linear, snap.free, geom)
    return snap


def heap_status(snap: HeapSnapshot) -> str | None:
    """Classify heap health for ``SystemSummary.heap_status``.

    ``good``/``corrupt`` follow the RT-Thread convention; ``None`` means the
    system command should omit the line (no allocator, not initialised).
    """
    if snap.geometry.kind is None or not snap.initialised:
        return None
    if snap.cross_check.startswith("mismatch"):
        return "corrupt"
    for walk in (snap.free_list, snap.linear):
        if walk is not None and (walk.corrupt or walk.truncated):
            return "corrupt"
    return "good"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _num(value: int | None) -> str:
    return str(value) if value is not None else "unavailable"


def _walk_suffix(walk: HeapWalk) -> str:
    if walk.corrupt:
        return " (corrupt)"
    if walk.truncated:
        return " (truncated)"
    return ""


def _protector_cell(geom: HeapGeometry) -> str:
    if not geom.canary_present:
        return "unavailable"
    if geom.canary == 0:
        return "enabled(canary=0)"
    return "enabled"


def heap_pairs(snap: HeapSnapshot) -> list[tuple[str, str]]:
    """Render the ``frt heap`` vertical pairs with the stable 10-key order.

    The key order is fixed for every kind so columns never drift between
    variants; kinds without a symbol keep the key and write ``unavailable``.
    """
    geom = snap.geometry
    algorithm = geom.algorithm
    if geom.kind == 1:
        # Reason: heap_1 is a bump allocator; Algorithm names it as such so
        # the absence of blocks never reads as a bug.
        algorithm = "heap_1 (bump pointer)"
    pairs: list[tuple[str, str]] = [
        ("Algorithm", algorithm),
        ("TotalSize", _num(snap.total)),
        ("FreeSize", _num(snap.free)),
        ("MinEver", _num(snap.min_ever)),
        ("Allocs", _num(snap.allocs)),
        ("Frees", _num(snap.frees)),
        ("Protector", _protector_cell(geom)),
    ]
    if geom.kind in (2, 4, 5):
        free_list = snap.free_list
        linear = snap.linear
        if free_list is None:
            blocks = "unavailable"
        else:
            blocks = f"{free_list.free_blocks}{_walk_suffix(free_list)}"
        if linear is None or linear.holes is None:
            holes = "unavailable"
        else:
            holes = f"{linear.holes}{_walk_suffix(linear)}"
        pairs += [("Blocks", blocks), ("Holes", holes)]
    else:
        pairs += [("Blocks", "unavailable"), ("Holes", "unavailable")]
    pairs.append(("CrossCheck", snap.cross_check))
    return pairs


def heap_block_table(snap: HeapSnapshot) -> ObjectTable | None:
    """Build the block table under ``frt heap``, or None when no walk ran.

    Prefers the linear walk (free and allocated blocks with their state);
    falls back to the free-list chain (address/size/next) when the linear
    walk is unavailable.
    """
    linear = snap.linear
    free_list = snap.free_list
    if linear is not None and linear.blocks:
        rows = [
            [
                hex(block.address),
                str(block.size),
                "free" if not block.allocated else "alloc",
            ]
            for block in linear.blocks
        ]
        messages = [
            "linear walk: "
            f"{len(linear.blocks)} block(s), {linear.free_blocks} free "
            f"in {linear.holes if linear.holes is not None else 0} hole(s)"
        ]
        if linear.incomplete_reason:
            messages.append(linear.incomplete_reason)
        return ObjectTable(
            headers=["Address", "Size", "State"], rows=rows, messages=messages
        )
    if free_list is not None and free_list.blocks:
        rows = [
            [
                hex(block.address),
                str(block.size),
                hex(block.next_free) if block.next_free else "-",
            ]
            for block in free_list.blocks
        ]
        messages = [
            "free-list walk: "
            f"{free_list.free_blocks} free block(s), "
            f"largest {free_list.largest_free}"
        ]
        if free_list.corrupt and free_list.corrupt_reason:
            messages.append(free_list.corrupt_reason)
        return ObjectTable(
            headers=["Address", "Size", "Next"],
            rows=rows,
            messages=messages,
            elastic=("Next",),
        )
    return None
