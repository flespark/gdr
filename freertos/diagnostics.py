"""Consistency checks for FreeRTOS kernel objects and scheduler lists.

The checks decode kernel fields directly and are deliberately scoped to the
invariant each kernel object actually maintains:

* real data queues own a storage window ``[pcHead, pcTail)`` whose pointer
  fields are comparable (queue.c xQueueGenericReset), and their lock
  counters stay ``queueUNLOCKED`` (-1) while the scheduler runs;
* a mutex keeps its reset-time ``pcWriteTo`` behind a NULL ``pcHead``
  (``pcHead == NULL`` is the mutex marker, queue.h ``queueQUEUE_IS_MUTEX``)
  and ``uxMessagesWaiting + (holder != NULL) == 1`` is its accounting
  invariant (queue.c: the mutex is a semaphore flipped to "taken but free");
* a counting/binary semaphore has ``uxItemSize == 0`` and sets ``pcHead``
  to the object address as a benign value, while ``pcReadFrom == pcTail``,
  so the pointer invariants do not apply to it at all;
* a ``List_t`` chain is a doubly-linked walk terminated by ``xListEnd`` with
  ``uxNumberOfItems`` matching the walk length and ``pxIndex`` parked on
  ``xListEnd`` between scheduler rotations (SMP only);
* the timer daemon unblocks a satisfied event-group waiter in the same
  ``xEventGroupSetBits`` call that sets its bits, so a satisfied waiter
  still on the list is a mid-unblock transient;
* the system-wide counters (``uxCurrentNumberOfTasks``,
  ``uxSchedulerSuspended``, ``xNextTaskUnblockTime``) agree with the
  scheduler lists, and the heap's free-list/linear/counter triple agrees.

Applying the data-queue pointer invariants to a mutex or semaphore would
fabricate failures that look like target corruption; inapplicable checks are
therefore reported explicitly as ``skipped`` instead of silently "ok".
``walk_list_raw`` and the list checks never report a bounded walk that hit a
corruption or the traversal limit as a clean short list: a corrupted chain is
a ``fail`` with its reason, and a truncated walk is a ``skipped`` so no
fabricated comparison number is ever derived from a partial walk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from freertos.events import _iter_waiters
from freertos.heap import heap_snapshot
from freertos.navigation import _mapped_ranges, iter_tasks
from gdr.constants import GDR_MAX_TRAVERSAL_COUNT
from gdr.gdb_bridge import (
    get_arch_info,
    lookup_symbol,
    lookup_type,
    read_bytes,
    read_int,
    safe_dereference,
    value_address,
)
from gdr.layout import member_offset, read_field, read_path

if TYPE_CHECKING:
    from freertos.layout import FreeRtosLayout


def queue_checks(
    value,
    kind: str,
    layout: FreeRtosLayout,  # noqa: ARG001 (uniform signature)
) -> list[tuple[str, str]]:
    """Run the consistency checks for one queue-family object.

    Args:
        value: The native ``Queue_t`` ``gdb.Value``.
        kind: Discriminated kind (``"queue"``, ``"semaphore"`` or
            ``"mutex"``).
        layout: Active FreeRTOS layout (reserved for checks that need
            layout context; the current checks decode *value* directly).

    Returns:
        ``(check_name, status)`` pairs where status is ``ok``, ``fail: ...``
        or ``skipped: <reason>``.  An inapplicable check is stated
        explicitly so a reader never mistakes "not checked" for "verified".
    """
    pc_head = read_int(read_path(value, ("pcHead",)))
    item_size = read_int(read_path(value, ("uxItemSize",)))
    length = read_int(read_path(value, ("uxLength",)))
    count = read_int(read_path(value, ("uxMessagesWaiting",)))
    # Reason: the pointer/span invariants only exist for real data queues;
    # a semaphore's pcReadFrom equals pcTail and a mutex's pcWriteTo is the
    # leftover reset value behind a NULL pcHead (queue.c prvInitialiseMutex).
    data_queue = pc_head not in (None, 0) and item_size not in (None, 0)
    results: list[tuple[str, str]] = []

    if count is not None and length is not None:
        results.append(
            (
                "Count",
                "ok" if count <= length else f"fail: {count} queued > {length} slots",
            )
        )
    else:
        results.append(("Count", "skipped: unreadable"))

    if not data_queue:
        results.extend(
            (name, "skipped: not a data queue")
            for name in ("Storage", "WritePtr", "ReadPtr")
        )
    else:
        pc_tail = read_int(read_path(value, ("u", "xQueue", "pcTail")))
        pc_write = read_int(read_path(value, ("pcWriteTo",)))
        pc_read = read_int(read_path(value, ("u", "xQueue", "pcReadFrom")))
        if (
            pc_head is None
            or pc_tail is None
            or pc_write is None
            or pc_read is None
            or length is None
            or item_size is None
        ):
            results.extend(
                (name, "skipped: unreadable")
                for name in ("Storage", "WritePtr", "ReadPtr")
            )
        else:
            expected = length * item_size
            span = pc_tail - pc_head
            results.append(
                (
                    "Storage",
                    "ok"
                    if span == expected
                    else f"fail: pcTail-pcHead = {span:#x} != {expected:#x}",
                )
            )
            results.append(
                (
                    "WritePtr",
                    "ok"
                    if pc_head <= pc_write < pc_tail
                    else "fail: pcWriteTo outside [pcHead, pcTail)",
                )
            )
            results.append(
                (
                    "ReadPtr",
                    "ok"
                    if pc_head <= pc_read < pc_tail
                    else "fail: pcReadFrom outside [pcHead, pcTail)",
                )
            )

    if kind == "mutex":
        holder = read_int(read_path(value, ("u", "xSemaphore", "xMutexHolder")))
        if (
            holder is not None
            and count is not None
            and length is not None
            and item_size is not None
        ):
            # Reason: a free mutex holds count=1 with no holder; taking it
            # decrements the count and records the holder, so the sum stays
            # 1 (recursive takes only bump uxRecursiveCallCount, queue.c).
            accounting = count + (1 if holder else 0)
            ok = accounting == 1 and length == 1 and item_size == 0
            results.append(
                (
                    "MutexAccounting",
                    "ok"
                    if ok
                    else f"fail: {count} + holder({holder != 0}) != 1 accounting",
                )
            )
        else:
            results.append(("MutexAccounting", "skipped: unreadable"))
    else:
        results.append(("MutexAccounting", "skipped: not a mutex"))

    if kind == "semaphore":
        obj_address = value_address(value)
        if pc_head is not None and item_size is not None and obj_address:
            ok = pc_head == obj_address and item_size == 0
            results.append(
                (
                    "SemaphoreSelfHead",
                    "ok" if ok else "fail: pcHead != object address or item size != 0",
                )
            )
        else:
            results.append(("SemaphoreSelfHead", "skipped: unreadable"))
    else:
        results.append(("SemaphoreSelfHead", "skipped: not a semaphore"))

    # QueueLock: the kernel writes cRxLock/cTxLock only while the scheduler
    # is suspended (queue.c prvLockQueue is reached from the vTaskSuspendAll
    # region of xQueueGenericSend/Receive and prvUnlockQueue runs before the
    # suspend count drops), so a non-queueUNLOCKED value with the scheduler
    # running is a stale lock left behind by an interrupted resume.
    rx_lock = read_int(read_path(value, ("cRxLock",)))
    tx_lock = read_int(read_path(value, ("cTxLock",)))
    if rx_lock is None or tx_lock is None:
        results.append(("QueueLock", "skipped: unreadable"))
    else:
        # Reason: int8_t queueUNLOCKED (-1) can read back as 255 through an
        # unsigned-typed probe; normalize so both spellings compare equal.
        rx = rx_lock if rx_lock < 128 else rx_lock - 256
        tx = tx_lock if tx_lock < 128 else tx_lock - 256
        try:
            suspended = read_int(lookup_symbol("uxSchedulerSuspended"))
        except RuntimeError:
            # Reason: outside GDB (unit tests) the suspend state is unknown;
            # the verdict then follows the dossier contract (stale locks with
            # the scheduler not proven suspended are failures).
            suspended = None
        if (rx == -1 and tx == -1) or suspended not in (None, 0):
            results.append(("QueueLock", "ok"))
        else:
            # Reason: "not suspended" is only claimable when the counter was
            # actually read as 0; an unreadable uxSchedulerSuspended must not
            # be silently upgraded into a definite statement.
            state = (
                "scheduler not suspended"
                if suspended == 0
                else "scheduler suspend state unreadable"
            )
            results.append(
                (
                    "QueueLock",
                    f"fail: stale queue lock counts rx={rx} tx={tx} ({state})",
                )
            )

    return results


# timers.c tmrSTATUS_IS_ACTIVE
_TMR_STATUS_ACTIVE = 0x01


@dataclass
class ListWalk:
    """Result of a bounded raw walk of a List_t item chain.

    Mirrors ``freertos.heap.HeapWalk``: corruption and truncation are
    explicit verdicts, never silent.  ``incomplete_reason`` states why no
    walk ran (missing head, member offsets unavailable).
    """

    items: list[int] = field(default_factory=list)
    owners: list[int | None] = field(default_factory=list)
    truncated: bool = False
    corrupt: bool = False
    corrupt_reason: str | None = None
    incomplete_reason: str | None = None


def _arch() -> tuple[int, Literal["little", "big"]]:
    """Return (pointer width in bytes, target byte order) for raw reads."""
    info = get_arch_info()
    if info is None or info.ptrsize not in (4, 8):
        return (4, "little")
    endian: Literal["little", "big"] = "little" if info.endian == "little" else "big"
    return (info.ptrsize, endian)


def _read_field_at(address: int, offset: int, size: int) -> int | None:
    """Read one raw integer field at ``address + offset``, or None."""
    _ptrsize, endian = _arch()
    raw = read_bytes(address + offset, size)
    if raw is None:
        return None
    return int.from_bytes(raw, byteorder=endian)


@dataclass(frozen=True)
class _ListOffsets:
    """Raw member offsets of List_t / ListItem_t resolved from DWARF.

    Resolving offsets (rather than hard-coding them) keeps the walk correct
    when ``configUSE_LIST_DATA_INTEGRITY_CHECK_BYTES`` shifts every member.
    """

    count: int
    index: int
    end: int
    end_value: int
    end_next: int
    item_value: int
    item_next: int
    item_owner: int
    list_integrity_1: int | None
    list_integrity_2: int | None


def _list_offsets(layout: FreeRtosLayout) -> _ListOffsets | None:
    """Resolve List_t / ListItem_t raw member offsets, or None."""
    try:
        count = member_offset("struct xLIST", ("uxNumberOfItems",))
        index = member_offset("struct xLIST", ("pxIndex",))
        end = member_offset("struct xLIST", ("xListEnd",))
        end_value = member_offset("struct xMINI_LIST_ITEM", ("xItemValue",))
        end_next = member_offset("struct xMINI_LIST_ITEM", ("pxNext",))
        item_value = member_offset("struct xLIST_ITEM", ("xItemValue",))
        item_next = member_offset("struct xLIST_ITEM", ("pxNext",))
        item_owner = member_offset("struct xLIST_ITEM", ("pvOwner",))
    except RuntimeError:
        # Reason: member_offset refuses to run outside GDB; unit tests drive
        # the walk by monkeypatching the module-level helper instead.
        return None
    if count is None or index is None or end is None:
        return None
    if end_value is None or end_next is None:
        return None
    if item_value is None or item_next is None or item_owner is None:
        return None
    if layout.config.list_integrity_check:
        integrity_1 = member_offset("struct xLIST", ("xListIntegrityValue1",))
        integrity_2 = member_offset("struct xLIST", ("xListIntegrityValue2",))
    else:
        integrity_1 = integrity_2 = None
    return _ListOffsets(
        count,
        index,
        end,
        end_value,
        end_next,
        item_value,
        item_next,
        item_owner,
        integrity_1,
        integrity_2,
    )


def walk_list_raw(
    head_address: int | None,
    layout: FreeRtosLayout,
    max_count: int = GDR_MAX_TRAVERSAL_COUNT,
) -> ListWalk:
    """Walk a List_t item chain from raw memory, bounded and cycle-safe.

    Starts at ``xListEnd.pxNext`` and follows ``pxNext`` until the sentinel
    (the address a healthy ``vListInitialise``/insert leaves in place).  Any
    violation -- a node outside every loadable section, a repeated node
    (cycle), an unreadable item -- stops the walk and records ``corrupt``
    with the reason; reaching ``max_count`` records ``truncated``.  Neither
    is ever reported as a clean short list.
    """
    walk = ListWalk()
    if not head_address:
        walk.incomplete_reason = "no list head address"
        return walk
    offsets = _list_offsets(layout)
    if offsets is None:
        walk.incomplete_reason = "list member offsets unavailable"
        return walk
    ptrsize, _endian = _arch()
    end_addr = head_address + offsets.end
    ranges = _mapped_ranges()
    node = _read_field_at(end_addr, offsets.end_next, ptrsize)
    if node is None:
        walk.corrupt = True
        walk.corrupt_reason = f"unreadable list head at {head_address:#x}"
        return walk
    seen: set[int] = set()
    steps = 0
    while node != end_addr:
        if steps >= max_count:
            walk.truncated = True
            return walk
        # Reason: a healthy chain never holds a NULL pxNext -- vListInsert
        # always links back toward xListEnd -- so a zero read is a cut chain,
        # not the end of the list; reporting it as a clean short walk would
        # fabricate a healthy-looking count.
        if not node:
            walk.corrupt = True
            source = walk.items[-1] if walk.items else end_addr
            walk.corrupt_reason = f"null pxNext from {source:#x}"
            return walk
        # Reason: a corrupt next pointer into the NULL page or outside every
        # loadable section is not a list node; stop before reading it.  Skip
        # the check when no map is available so unit tests with synthetic
        # addresses still exercise the walk.
        if ranges and not any(low <= node < high for low, high in ranges):
            walk.corrupt = True
            walk.corrupt_reason = f"list node {node:#x} outside every loadable section"
            return walk
        if node in seen:
            walk.corrupt = True
            walk.corrupt_reason = f"list cycle at {node:#x}"
            return walk
        seen.add(node)
        walk.items.append(node)
        walk.owners.append(_read_field_at(node, offsets.item_owner, ptrsize))
        next_node = _read_field_at(node, offsets.item_next, ptrsize)
        if next_node is None:
            walk.corrupt = True
            walk.corrupt_reason = f"unreadable list item at {node:#x}"
            return walk
        node = next_node
        steps += 1
    return walk


def _integrity_magic(tick_bits: int) -> int:
    """Return pdINTEGRITY_CHECK_VALUE for the TickType_t width (projdefs.h)."""
    if tick_bits >= 64:
        return 0x5A5A5A5A5A5A5A5A
    if tick_bits >= 32:
        return 0x5A5A5A5A
    return 0x5A5A


_LIST_CHECK_NAMES = (
    "ListInit",
    "ListCount",
    "ListIndex",
    "ListIntegrity",
    "ListIntegrityBytes",
)


def list_checks(
    head_address: int | None,
    layout: FreeRtosLayout,
    max_count: int = GDR_MAX_TRAVERSAL_COUNT,
) -> list[tuple[str, str]]:
    """Run the List_t integrity checks for the list at *head_address*.

    ``ListInit`` verifies the ``xListEnd`` sentinel value; ``ListCount``
    compares ``uxNumberOfItems`` with the raw walk length; ``ListIndex``
    (SMP only, where ``pxIndex`` parks on ``xListEnd`` between scheduler
    rotations) verifies the index cursor; ``ListIntegrity`` reports the walk
    verdict -- a corrupted chain is a failure with its reason, a walk that
    hit the traversal bound is ``skipped`` (never a fabricated comparison);
    ``ListIntegrityBytes`` verifies the ``configUSE_LIST_DATA_INTEGRITY_``
    ``CHECK_BYTES`` magic when that config is on.
    """
    if not head_address:
        return [(name, "skipped: no container list") for name in _LIST_CHECK_NAMES]
    offsets = _list_offsets(layout)
    if offsets is None:
        return [
            (name, "skipped: list offsets unavailable") for name in _LIST_CHECK_NAMES
        ]
    ptrsize, _endian = _arch()
    end_addr = head_address + offsets.end
    count = _read_field_at(head_address, offsets.count, ptrsize)
    index = _read_field_at(head_address, offsets.index, ptrsize)
    end_value = _read_field_at(end_addr, offsets.end_value, ptrsize)
    walk = walk_list_raw(head_address, layout, max_count)
    results: list[tuple[str, str]] = []
    tick_mask = (1 << layout.config.tick_bits) - 1

    if end_value is None:
        results.append(("ListInit", "skipped: unreadable"))
    elif end_value != tick_mask:
        results.append(
            (
                "ListInit",
                f"fail: xListEnd value {end_value:#x} != portMAX_DELAY {tick_mask:#x}",
            )
        )
    else:
        results.append(("ListInit", "ok"))

    if walk.corrupt:
        results.append(("ListIntegrity", f"fail: {walk.corrupt_reason}"))
    elif walk.truncated:
        results.append(("ListIntegrity", "skipped: traversal bound reached"))
    else:
        results.append(("ListIntegrity", "ok"))

    if count is None:
        results.append(("ListCount", "skipped: unreadable"))
    elif walk.corrupt:
        results.append(("ListCount", f"fail: walk corrupt ({walk.corrupt_reason})"))
    elif walk.truncated:
        results.append(("ListCount", "skipped: traversal bound reached"))
    elif count != len(walk.items):
        results.append(
            (
                "ListCount",
                f"fail: uxNumberOfItems {count} != {len(walk.items)} walked",
            )
        )
    else:
        results.append(("ListCount", "ok"))

    if not layout.config.smp:
        # Reason: on single-core builds pxIndex legitimately stops on a
        # rotation cursor, so "pxIndex != &xListEnd" is normal scheduling.
        results.append(
            ("ListIndex", "skipped: single core (pxIndex is a rotation cursor)")
        )
    elif index is None:
        results.append(("ListIndex", "skipped: unreadable"))
    elif walk.corrupt:
        results.append(("ListIndex", f"fail: walk corrupt ({walk.corrupt_reason})"))
    elif index != end_addr:
        results.append(
            (
                "ListIndex",
                f"fail: pxIndex {index:#x} != &xListEnd {end_addr:#x}",
            )
        )
    else:
        results.append(("ListIndex", "ok"))

    if not layout.config.list_integrity_check:
        results.append(
            (
                "ListIntegrityBytes",
                "skipped: configUSE_LIST_DATA_INTEGRITY_CHECK_BYTES off",
            )
        )
    elif offsets.list_integrity_1 is None or offsets.list_integrity_2 is None:
        results.append(("ListIntegrityBytes", "skipped: unreadable"))
    else:
        expected = _integrity_magic(layout.config.tick_bits)
        tick_bytes = layout.config.tick_bits // 8
        v1 = _read_field_at(head_address, offsets.list_integrity_1, tick_bytes)
        v2 = _read_field_at(head_address, offsets.list_integrity_2, tick_bytes)
        if v1 is None or v2 is None:
            results.append(("ListIntegrityBytes", "skipped: unreadable"))
        elif v1 != expected or v2 != expected:
            results.append(
                (
                    "ListIntegrityBytes",
                    f"fail: integrity values {v1:#x}/{v2:#x} != {expected:#x}",
                )
            )
        else:
            results.append(("ListIntegrityBytes", "ok"))
    return results


_TASK_ITEM_CHECKS = ("ItemOwner", "ItemContainer")


def _build_prefills_stacks(layout: FreeRtosLayout) -> bool:
    """Whether this build memsets task stacks with ``tskSTACK_FILL_BYTE``.

    The kernel gates the fill on ``tskSET_NEW_STACKS_TO_KNOWN_VALUE``
    (FreeRTOS.h), but that macro is not always visible to GDB (the macro
    table is CU-scoped), so the fill's *presence* in the current task
    population is the evidence: a task whose watermark window starts with
    0xa5 proves the build prefills.  A population with no fill at all means
    the config turned the prefill off, so a fill-less task is structurally
    inapplicable rather than corrupted (mirrors the HighWater column gate in
    the adapter's task table).
    """
    sl = layout.structs["struct tskTaskControlBlock"]
    try:
        tasks = iter_tasks(layout)
    except Exception:
        return False
    for value, _state, _core in tasks:
        base = read_int(read_field(value, sl, "stack_base"))
        top = read_int(read_field(value, sl, "top_of_stack"))
        stack_end = read_int(read_field(value, sl, "stack_end"))
        if base is None or top is None:
            continue
        water_end = stack_end if (stack_end and stack_end >= base) else top
        if water_end <= base:
            continue
        raw = read_bytes(base, water_end - base)
        if raw is None:
            continue
        if raw[:1] == b"\xa5":
            return True
    return False


def _stack_fill_present(value, sl, layout: FreeRtosLayout) -> tuple[str, str]:
    """StackFillPresent: the 0xa5 watermark must be visible at the stack base.

    A stack whose base is not 0xa5 is either from a build where the prefill
    is off (reported as skipped once ``_build_prefills_stacks`` finds no fill
    anywhere) or a corrupted/relocated stack.
    """
    base = read_int(read_field(value, sl, "stack_base"))
    top = read_int(read_field(value, sl, "top_of_stack"))
    stack_end = read_int(read_field(value, sl, "stack_end"))
    if base is None or top is None:
        return ("StackFillPresent", "skipped: unreadable")
    water_end = stack_end if (stack_end and stack_end >= base) else top
    if water_end <= base:
        return ("StackFillPresent", "skipped: empty window")
    raw = read_bytes(base, water_end - base)
    if raw is None:
        return ("StackFillPresent", "skipped: unreadable")
    if raw[:1] == b"\xa5":
        return ("StackFillPresent", "ok")
    if not _build_prefills_stacks(layout):
        return (
            "StackFillPresent",
            "skipped: stacks not prefilled (tskSET_NEW_STACKS_TO_KNOWN_VALUE off)",
        )
    return ("StackFillPresent", "fail: no 0xa5 fill at the stack base")


def task_checks(
    value,
    task_address: int,
    layout: FreeRtosLayout,
    max_count: int = GDR_MAX_TRAVERSAL_COUNT,
) -> list[tuple[str, str]]:
    """Run the per-task consistency checks for the TCB at *value*.

    ``ItemOwner`` verifies both list items' ``pvOwner`` (the kernel stamps
    both at creation, tasks.c prvInitialiseNewTask); ``ItemContainer``
    verifies the claimed container actually contains the item (a linked item
    whose ``pxContainer`` points elsewhere is a corruption); the container's
    List_t checks follow; ``StackFillPresent`` checks the 0xa5 watermark.
    """
    sl = layout.structs["struct tskTaskControlBlock"]
    item_layout = layout.structs["struct xLIST_ITEM"]
    state_item = read_field(value, sl, "state_list_item")
    container: int | None = None
    results: list[tuple[str, str]] = []
    if state_item is None:
        results.append(("ItemOwner", "skipped: unreadable"))
        results.append(("ItemContainer", "skipped: unreadable"))
    else:
        owner = read_int(read_field(state_item, item_layout, "owner"))
        container = read_int(read_field(state_item, item_layout, "container"))
        # Reason: the kernel stamps both list items with the TCB owner at
        # creation (tasks.c prvInitialiseNewTask listSET_LIST_ITEM_OWNER)
        # and vListRemove only clears pxContainer, so a wrong or NULL owner
        # on either item is a corruption.  Checking both keeps the negative
        # fixture on the event item, which task discovery does not walk.
        event_item = read_field(value, sl, "event_list_item")
        event_owner = (
            read_int(read_field(event_item, item_layout, "owner"))
            if event_item is not None
            else None
        )
        mismatches = []
        if owner is not None and owner != task_address:
            mismatches.append(("state item", owner))
        if event_owner is not None and event_owner != task_address:
            mismatches.append(("event item", event_owner))
        if owner is None and event_owner is None:
            results.append(("ItemOwner", "skipped: unreadable"))
        elif not mismatches:
            results.append(("ItemOwner", "ok"))
        else:
            parts = ", ".join(
                f"{what} pvOwner={owner:#x}" for what, owner in mismatches
            )
            results.append(("ItemOwner", f"fail: {parts} != task {task_address:#x}"))
        if container is None:
            results.append(("ItemContainer", "skipped: unreadable"))
        elif not container:
            # Reason: vListRemove clears pxContainer when the item is unlinked
            # (list.c), so a NULL container is a legitimate unlinked item.
            results.append(("ItemContainer", "skipped: not linked"))
        else:
            item_address = value_address(state_item)
            walk = walk_list_raw(container, layout, max_count)
            if walk.corrupt:
                results.append(
                    (
                        "ItemContainer",
                        f"fail: container walk corrupt ({walk.corrupt_reason})",
                    )
                )
            elif walk.truncated:
                results.append(("ItemContainer", "skipped: traversal bound reached"))
            elif item_address in walk.items:
                results.append(("ItemContainer", "ok"))
            else:
                results.append(
                    (
                        "ItemContainer",
                        f"fail: item {item_address:#x} not reachable from "
                        f"container {container:#x}",
                    )
                )
    if container:
        results.extend(list_checks(container, layout, max_count))
    else:
        results.extend((name, "skipped: not linked") for name in _LIST_CHECK_NAMES)
    results.append(_stack_fill_present(value, sl, layout))
    return results


# ---------------------------------------------------------------------------
# system-wide checks (``frt system``)
# ---------------------------------------------------------------------------


def _next_unblock_check(layout: FreeRtosLayout) -> tuple[str, str]:
    """NextUnblockTime: xNextTaskUnblockTime vs the active delayed list front.

    The kernel keeps ``xNextTaskUnblockTime`` at the front (minimum) value of
    the active delayed list, or ``portMAX_DELAY`` when it is empty (tasks.c
    xTaskIncrementTick); a disagreement means the counter was not refreshed.
    """
    expected = read_int(lookup_symbol("xNextTaskUnblockTime"))
    delayed = safe_dereference(lookup_symbol(layout.lists["delayed_current"]))
    if delayed is None:
        return ("NextUnblockTime", "skipped: active delayed list unavailable")
    walk = walk_list_raw(value_address(delayed), layout)
    if walk.corrupt:
        return (
            "NextUnblockTime",
            f"fail: delayed list corrupt ({walk.corrupt_reason})",
        )
    if walk.truncated:
        return ("NextUnblockTime", "skipped: traversal bound reached")
    offsets = _list_offsets(layout)
    if offsets is None:
        return ("NextUnblockTime", "skipped: list offsets unavailable")
    ptrsize, _endian = _arch()
    if walk.items:
        values = []
        for item in walk.items:
            value = _read_field_at(item, offsets.item_value, ptrsize)
            if value is None:
                return ("NextUnblockTime", "skipped: unreadable")
            values.append(value)
        minimum = min(values)
    else:
        minimum = (1 << layout.config.tick_bits) - 1
    if expected is None:
        return ("NextUnblockTime", "skipped: unreadable")
    if expected == minimum:
        return ("NextUnblockTime", "ok")
    return (
        "NextUnblockTime",
        f"fail: xNextTaskUnblockTime {expected} != front delayed item {minimum}",
    )


def _heap_cross_check(layout: FreeRtosLayout) -> tuple[str, str]:
    """HeapCrossCheck: surface the ``frt heap`` CrossCheck verdict as a check.

    A mismatch reports the three concrete numbers (free-list vs linear vs
    counter); an ``unavailable`` verdict (heap_3, no allocator, not
    initialised, corrupt walk) is a structural skip, never a failure.
    """
    try:
        verdict = heap_snapshot(layout).cross_check
    except Exception:
        return ("HeapCrossCheck", "skipped: unreadable")
    if verdict == "ok":
        return ("HeapCrossCheck", "ok")
    if verdict.startswith("mismatch"):
        return ("HeapCrossCheck", f"fail: {verdict}")
    if verdict.startswith("unavailable"):
        return ("HeapCrossCheck", f"skipped: {verdict.removeprefix('unavailable: ')}")
    return ("HeapCrossCheck", f"fail: {verdict}")


def system_checks(layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """Run the system-wide consistency checks for ``frt system``.

    ``TaskCount`` compares ``uxCurrentNumberOfTasks`` with the scheduler-list
    walk; ``SchedulerSuspended`` reports the suspend counter (nonzero means
    the snapshot sits inside a ``vTaskSuspendAll`` -- queue locks and lists
    may be transient); ``NextUnblockTime`` and ``HeapCrossCheck`` reuse the
    delayed-list walk and the heap cross-validation.
    """
    total = read_int(lookup_symbol("uxCurrentNumberOfTasks"))
    try:
        discovered = len(list(iter_tasks(layout)))
    except Exception:
        discovered = None
    if total is None:
        results: list[tuple[str, str]] = [("TaskCount", "skipped: unreadable")]
    elif discovered is None:
        results = [("TaskCount", "skipped: task lists unreadable")]
    elif total == discovered:
        results = [("TaskCount", "ok")]
    else:
        results = [
            (
                "TaskCount",
                f"fail: uxCurrentNumberOfTasks {total} != {discovered} discovered",
            )
        ]

    suspended = read_int(lookup_symbol("uxSchedulerSuspended"))
    if suspended is None:
        results.append(("SchedulerSuspended", "skipped: unreadable"))
    elif suspended == 0:
        results.append(("SchedulerSuspended", "ok"))
    else:
        results.append(
            (
                "SchedulerSuspended",
                f"fail: uxSchedulerSuspended = {suspended} (a vTaskSuspendAll "
                "is in effect; queue locks and lists may be transient)",
            )
        )

    results.append(_next_unblock_check(layout))
    results.append(_heap_cross_check(layout))
    return results


# ---------------------------------------------------------------------------
# event group checks
# ---------------------------------------------------------------------------


def event_checks(value, layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """EventWaiterSatisfied: a satisfied waiter must not stay on the list.

    The kernel unblocks a waiter in the same ``xEventGroupSetBits`` call
    that sets its bits (event_groups.c), so a satisfied-but-listed waiter is
    the mid-unblock transient the detail renders as ``(satisfied —
    mid-unblock)`` -- surfaced here rather than silently absorbed.
    """
    bits = read_int(read_path(value, ("uxEventBits",)))
    if bits is None:
        return [("EventWaiterSatisfied", "skipped: unreadable")]
    satisfied = [
        waiter for waiter in _iter_waiters(value, layout, bits) if waiter.satisfied
    ]
    if not satisfied:
        return [("EventWaiterSatisfied", "ok")]
    names = ", ".join(waiter.task for waiter in satisfied)
    return [
        (
            "EventWaiterSatisfied",
            f"fail: {names} wants satisfied but still on the wait list "
            "(xEventGroupSetBits mid-unblock)",
        )
    ]


def timer_checks(
    value,
    layout: FreeRtosLayout,
) -> list[tuple[str, str]]:
    """Run the consistency checks for one timer object.

    Args:
        value: The native ``Timer_t`` (struct tmrTimerControl) ``gdb.Value``.
        layout: Active FreeRTOS layout, used for the list-item container
            member name (``pvContainer`` vs ``pxContainer``) and the command
            queue item-size comparison.

    Returns:
        ``(check_name, status)`` pairs where status is ``ok``, ``fail: ...``
        or ``skipped: <reason>``.  A disagreement between ``ucStatus`` and
        list membership is reported -- the daemon updates both together, so
        it normally means a queued start/stop command -- rather than hidden.
    """
    status = read_int(read_path(value, ("ucStatus",)))
    period = read_int(read_path(value, ("xTimerPeriodInTicks",)))
    callback = read_int(read_path(value, ("pxCallbackFunction",)))
    container_field = layout.config.list_item_container_field or "pxContainer"
    container = read_int(read_path(value, ("xTimerListItem", container_field)))
    results: list[tuple[str, str]] = []

    if status is not None and container is not None:
        linked = container not in (None, 0)
        active = bool(status & _TMR_STATUS_ACTIVE)
        if linked == active:
            results.append(("StatusListSync", "ok"))
        else:
            # Reason: the daemon clears IS_ACTIVE and unlinks in one command
            # (timers.c prvProcessReceivedCommands), so a mismatch means a
            # queued command is still pending, not corruption -- but it must
            # be surfaced, not silently absorbed by the State cell.
            results.append(
                (
                    "StatusListSync",
                    "fail: ucStatus and the active list disagree "
                    "(a queued start/stop command?)",
                )
            )
    else:
        results.append(("StatusListSync", "skipped: unreadable"))

    if period is not None:
        results.append(("Period", "ok" if period != 0 else "fail: period is 0 ticks"))
    else:
        results.append(("Period", "skipped: unreadable"))

    if callback is not None:
        results.append(("Callback", "ok" if callback else "fail: callback is NULL"))
    else:
        results.append(("Callback", "skipped: unreadable"))

    queue = safe_dereference(lookup_symbol("xTimerQueue"))
    msg_type = lookup_type("struct tmrTimerQueueMessage")
    if queue is None or msg_type is None:
        results.append(("TimerQueueItemSize", "skipped: unreadable"))
    else:
        item_size = read_int(read_path(queue, ("uxItemSize",)))
        try:
            expected = int(msg_type.sizeof)
        except (TypeError, ValueError, AttributeError):
            results.append(("TimerQueueItemSize", "skipped: unreadable"))
            return results
        if item_size is None:
            results.append(("TimerQueueItemSize", "skipped: unreadable"))
        elif item_size == expected:
            results.append(("TimerQueueItemSize", "ok"))
        else:
            results.append(
                (
                    "TimerQueueItemSize",
                    f"fail: queue item size {item_size} != "
                    f"sizeof(DaemonTaskMessage_t) {expected}",
                )
            )

    return results
