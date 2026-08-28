"""Consistency checks for FreeRTOS queue-family objects.

The checks decode ``Queue_t`` fields directly and are deliberately scoped to
the invariant each kernel object actually maintains:

* real data queues own a storage window ``[pcHead, pcTail)`` whose pointer
  fields are comparable (queue.c xQueueGenericReset);
* a mutex keeps its reset-time ``pcWriteTo`` behind a NULL ``pcHead``
  (``pcHead == NULL`` is the mutex marker, queue.h ``queueQUEUE_IS_MUTEX``)
  and ``uxMessagesWaiting + (holder != NULL) == 1`` is its accounting
  invariant (queue.c: the mutex is a semaphore flipped to "taken but free");
* a counting/binary semaphore has ``uxItemSize == 0`` and sets ``pcHead``
  to the object address as a benign value, while ``pcReadFrom == pcTail``,
  so the pointer invariants do not apply to it at all.

Applying the data-queue pointer invariants to a mutex or semaphore would
fabricate failures that look like target corruption; inapplicable checks are
therefore reported explicitly as ``skipped`` instead of silently "ok".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gdr.gdb_bridge import (
    lookup_symbol,
    lookup_type,
    read_int,
    safe_dereference,
    value_address,
)
from gdr.layout import read_path

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

    return results


# timers.c tmrSTATUS_IS_ACTIVE
_TMR_STATUS_ACTIVE = 0x01


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
