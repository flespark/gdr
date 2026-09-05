"""Event group inspection: control-bit decode and per-waiter blocking state.

An ``EventGroupDef_t`` owns a bit vector (``uxEventBits``) and one wait list
(``xTasksWaitingForBits``, an *unordered* ``List_t``).  Blocked tasks encode
their request in the list item's ``xItemValue``: the bits they wait for OR'd
with control bits that sit above the application bits (event_groups.h /
event_groups.c).  The core value of this module is decoding ``xItemValue``
back into a human-readable "why is this task still blocked" line (wants /
mode / clear-on-exit / missing), plus the transient
``(satisfied — mid-unblock)`` marker when the kernel has unblocked the task
but the setter task has not run yet.

The control bits are compile-time macros that are **not** in DWARF, and
``EventBits_t`` is ``TickType_t``, so every mask is derived from
``cfg.tick_bits`` -- never hard-coded for 32-bit ticks.

Every GDB entry point goes through module-level helpers (``read_path``,
``read_int``, ...) so the unit tests can drive the logic without a GDB
session, following ``freertos/timers.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from freertos.layout import FreeRtosLayout
from freertos.navigation import mapped_ranges, source_label, task_name_at
from gdr.adapter_api import ObjectTable
from gdr.constants import GDR_MAX_TRAVERSAL_COUNT
from gdr.formatting import format_address
from gdr.gdb_bridge import (
    TARGET_ACCESS_ERRORS,
    read_int,
    safe_dereference,
    safe_int,
    value_address,
    warn,
)
from gdr.layout import read_field

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


def event_bit_masks(tick_bits: int) -> tuple[int, int, int, int]:
    """Return the four control-bit masks for a tick width W in ``{16,32,64}``.

    ``EventBits_t`` is ``TickType_t`` (event_groups.h), so the control bits
    live in the top byte: ``CLEAR_ON_EXIT=1<<(W-8)``,
    ``UNBLOCKED_DUE_TO_BIT_SET=2<<(W-8)``, ``WAIT_FOR_ALL=4<<(W-8)`` and
    ``CONTROL_BYTES=0xff<<(W-8)`` (event_groups.h).  These are preprocessor
    constants with no DWARF representation, so they must be derived -- on a
    16-bit tick a hard-coded ``0x01000000`` would fall outside EventBits_t
    and every ``wants``/``missing`` decode would silently keep the control
    bits.
    """
    shift = tick_bits - 8
    return (1 << shift, 2 << shift, 4 << shift, 0xFF << shift)


@dataclass
class EventWaiter:
    """One decoded waiter on an event group's ``xTasksWaitingForBits`` list.

    ``raw`` is the list item's ``xItemValue``: application bits OR'd with the
    control bits AND ``eventIN_USE`` (event_groups.c vTaskPlaceOnUnordered-
    EventList ORs ``eventIN_USE = 1<<(W-1)`` onto the request, tasks.c).  The
    decode order strips ``IN_USE`` and ``CONTROL_BYTES`` first, leaving
    ``wants``; a waiter whose ``in_use`` bit is clear is a suspicious value
    and is rendered with a marker instead of silently treated as valid.
    """

    task: str = "-"
    raw: int = 0
    wants: int = 0
    wait_all: bool = False
    clear_on_exit: bool = False
    satisfied: bool = False
    missing: int = 0
    in_use: bool = False


def decode_waiter(
    raw: int | None,
    current_bits: int | None,
    masks: tuple[int, int, int, int],
) -> EventWaiter | None:
    """Decode one list-item value into an :class:`EventWaiter`, ``None`` on an
    unreadable value.

    The satisfied/missing computation follows event_groups.c
    ``prvTestWaitCondition``: ALL mode needs every wanted bit set
    (``(cur & wants) == wants``, missing is what is still absent), ANY mode
    needs a single wanted bit (``(cur & wants) != 0``, missing stays the
    full request because any one bit would unblock it).
    """
    if raw is None:
        return None
    clear_on_exit, _unblocked, wait_for_all, control_bytes = masks
    # Reason: eventIN_USE = 1 << (W-1) is the top bit, and the top byte
    # (CONTROL_BYTES) spans it; deriving it from the control mask keeps the
    # decode width-agnostic without threading tick_bits through every call.
    in_use_mask = 1 << (control_bytes.bit_length() - 1)
    wants = raw & ~(control_bytes | in_use_mask)
    wait_all = bool(raw & wait_for_all)
    if current_bits is None:
        return EventWaiter(
            task="-",
            raw=raw,
            wants=wants,
            wait_all=wait_all,
            clear_on_exit=bool(raw & clear_on_exit),
            satisfied=False,
            missing=wants if wait_all else wants,
            in_use=bool(raw & in_use_mask),
        )
    if wait_all:
        satisfied = (current_bits & wants) == wants
        missing = wants & ~current_bits
    else:
        satisfied = (current_bits & wants) != 0
        missing = wants
    return EventWaiter(
        task="-",
        raw=raw,
        wants=wants,
        wait_all=wait_all,
        clear_on_exit=bool(raw & clear_on_exit),
        satisfied=satisfied,
        missing=missing,
        in_use=bool(raw & in_use_mask),
    )


def format_waiter_line(waiter: EventWaiter) -> str:
    """Render one waiter as ``wants=0x.. mode=ALL|ANY clearOnExit=yes|no
    missing=0x..``, appending ``(satisfied — mid-unblock)`` when the bits are
    already satisfied.

    The mid-unblock suffix is only possible while the item is still linked:
    the kernel ORs ``eventUNBLOCKED_DUE_TO_BIT_SET`` into the *return* value
    and removes the item from the list at the same time (event_groups.c), so
    a satisfied waiter still on the list means ``xEventGroupSetBits`` has not
    run yet -- exactly the transient the marker names.
    """
    mode = "ALL" if waiter.wait_all else "ANY"
    clear = "yes" if waiter.clear_on_exit else "no"
    line = (
        f"wants=0x{waiter.wants:x} mode={mode} clearOnExit={clear} "
        f"missing=0x{waiter.missing:x}"
    )
    if waiter.satisfied:
        line += " (satisfied — mid-unblock)"
    if not waiter.in_use:
        line += " (IN_USE clear — suspicious item value)"
    return line


@dataclass
class FreeRtosEventGroupObject:
    """Display model for one event group (struct EventGroupDef_t).

    ``statically_allocated`` is only populated when the build has both static
    and dynamic allocation (the ``ucStaticallyAllocated`` member only exists
    then); otherwise static-ness must come from the discovery channel.
    """

    name: str = "-"
    address: int = 0
    bits: int | None = None
    waiters: list[EventWaiter] = None  # type: ignore[assignment]
    source: str = ""
    extra_sources: tuple[str, ...] = ()
    statically_allocated: bool | None = None

    def __post_init__(self) -> None:
        if self.waiters is None:
            self.waiters = []


def iter_waiters(
    value,
    layout: FreeRtosLayout,
    current_bits: int | None,
):
    """Yield decoded :class:`EventWaiter` entries from ``xTasksWaitingForBits``.

    The event-group wait list is an *unordered* list of ``xLIST_ITEM`` nodes
    whose owner is the blocked task's TCB; the request bits live in the *list
    item's* ``xItemValue`` (event_groups.c), so decoding must read the item,
    never recompute from the TCB.
    """
    head = read_field(value, layout.structs["struct EventGroupDef_t"], "waiting")
    if head is None:
        return
    masks = event_bit_masks(layout.config.tick_bits)
    try:
        list_layout = layout.structs["struct xLIST"]
        end = read_field(head, list_layout, "end")
        end_addr = value_address(end)
        mini_layout = layout.structs["struct xMINI_LIST_ITEM"]
        node = read_field(end, mini_layout, "next")
        item_layout = layout.structs["struct xLIST_ITEM"]
        seen: set[int] = set()
        ranges = mapped_ranges()
        for _ in range(GDR_MAX_TRAVERSAL_COUNT):
            node_addr = safe_int(node)
            if not node_addr or node_addr == end_addr:
                return
            # Reason: same corruption guard as navigation._iter_list -- a
            # next pointer outside every loadable section is not a list node;
            # stop before dereference (skipped when no map is available so
            # unit tests with synthetic addresses still exercise the walk).
            if ranges and not any(low <= node_addr < high for low, high in ranges):
                warn(
                    f"FreeRTOS waiter list traversal stopped at out-of-range "
                    f"node {node_addr:#x}"
                )
                return
            if node_addr in seen:
                warn(
                    f"FreeRTOS waiter list traversal stopped at repeated "
                    f"node {node_addr:#x}"
                )
                return
            seen.add(node_addr)
            item = safe_dereference(node)
            if item is None:
                return
            raw = read_int(read_field(item, item_layout, "value"))
            owner = read_int(read_field(item, item_layout, "owner"))
            waiter = decode_waiter(raw, current_bits, masks)
            if waiter is not None:
                name = task_name_at(owner, layout) if owner else None
                waiter.task = name or "-"
                yield waiter
            node = read_field(item, item_layout, "next")
    except TARGET_ACCESS_ERRORS:
        return


def value_to_event_group_object(
    value,
    found,
    layout: FreeRtosLayout,
) -> FreeRtosEventGroupObject:
    """Convert a discovered event-group address into the display model."""
    obj = FreeRtosEventGroupObject(
        name=found.name or "-",
        address=found.address,
        source=found.source,
        extra_sources=found.extra_sources,
    )
    if value is None:
        return obj
    bits = read_int(
        read_field(value, layout.structs["struct EventGroupDef_t"], "value")
    )
    obj.bits = bits
    obj.waiters = list(iter_waiters(value, layout, bits))
    if layout.config.static_and_dynamic:
        static = read_int(
            read_field(value, layout.structs["struct EventGroupDef_t"], "static_alloc")
        )
        obj.statically_allocated = bool(static) if static is not None else None
    return obj


def bits_cell(bits: int | None) -> str:
    """Render the Bits cell: hex, ``-`` when unreadable."""
    return "-" if bits is None else f"0x{bits:x}"


_EVENT_GROUP_HEADERS = ["Name", "Bits", "Waiters", "Src", "Addr"]


def event_group_table(
    objects: list[FreeRtosEventGroupObject],
    layout: FreeRtosLayout,
) -> ObjectTable:
    """Build the ``frt eventgroups`` table with the verbatim header contract.

    A build without event groups answers with an explicit capability note
    instead of an empty table that reads as "zero event groups".
    """
    messages: list[str] = []
    if not layout.config.event_groups:
        messages.append("no event groups in this build (configUSE_EVENT_GROUPS=0)")
    rows = [
        [
            obj.name,
            bits_cell(obj.bits),
            str(len(obj.waiters)),
            source_label(obj.source, obj.extra_sources),
            format_address(obj.address),
        ]
        for obj in objects
    ]
    return ObjectTable(
        headers=_EVENT_GROUP_HEADERS,
        rows=rows,
        messages=messages,
        elastic=("Name",),
    )
