"""FreeRTOS column contracts: the shape of every table this adapter prints.

Headers, machine-value semantics, shrink priority and the capability claims all
come from these tuples, so a table and the columns it advertises cannot
disagree.  ``adapter.py`` builds the rows; this module declares the columns.

The optional columns mirror the kernel configuration that produces them, which
is why several tables here are functions of a build flag rather than constants:
a column that cannot be populated is not declared at all, instead of being
printed as a whole column of ``N/A``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gdr.adapter_api import ColumnSpec

if TYPE_CHECKING:
    from freertos.layout import FreeRtosLayout


def task_column_flags(layout: FreeRtosLayout) -> dict[str, bool]:
    """Which optional task columns this build can populate.

    ``high_water`` is deliberately absent: it depends on the runtime data (a
    stack actually carrying the watermark), so the table decides it per
    snapshot while the capability claim describes the gating config.
    """
    fields = layout.structs["struct tskTaskControlBlock"].fields
    return {
        "base_priority": "base_priority" in fields,
        "stack": "stack_end" in fields,
        "runtime": "runtime_counter" in fields,
        "smp": layout.config.smp,
        "affinity": "core_affinity" in fields,
    }


def task_specs(
    *,
    base_priority: bool,
    stack: bool,
    high_water: bool,
    runtime: bool,
    smp: bool,
    affinity: bool,
) -> tuple[ColumnSpec, ...]:
    """The task table contract; optional columns follow the same order."""
    specs = [
        ColumnSpec("Name", elastic=True, doc="task name; '*' marks the running one"),
        ColumnSpec("State", doc="scheduler state derived from the list it is on"),
        ColumnSpec("Prio", kind="int", align="right"),
    ]
    if base_priority:
        specs.append(ColumnSpec("BasePrio", kind="int", align="right"))
    specs.append(ColumnSpec("SP", kind="addr", doc="top of stack"))
    if stack:
        specs += [
            ColumnSpec("Stack", kind="int", align="right"),
            ColumnSpec("Used", kind="int", align="right"),
        ]
    if high_water:
        specs.append(ColumnSpec("HighWater", kind="int", align="right"))
    if runtime:
        specs.append(ColumnSpec("Runtime", kind="int", align="right"))
    if smp:
        specs.append(ColumnSpec("CPU", kind="int", align="right"))
    if affinity:
        specs.append(ColumnSpec("Affinity", kind="hex"))
    specs.append(ColumnSpec("Addr", kind="addr"))
    return tuple(specs)


SUMMARY_SPECS = (
    ColumnSpec("Kind", doc="semantic object kind"),
    ColumnSpec("Count", kind="int", align="right"),
    ColumnSpec("Sources", elastic=True, doc="discovery channels that found it"),
)

QUEUE_SPECS_HEAD = (
    ColumnSpec("Name", elastic=True, doc="queue name from the registry"),
    ColumnSpec("Type", kind="enum", doc="ucQueueType, or inferred from pcHead"),
    ColumnSpec("Items", kind="int", align="right", doc="queued items"),
    ColumnSpec("Length", kind="int", align="right", doc="capacity"),
    ColumnSpec("ItemSize", kind="int", align="right", doc="bytes per item"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("SendWait", kind="waiters", elastic=True),
    ColumnSpec("RecvWait", kind="waiters", elastic=True),
    ColumnSpec("Locks", doc="rx/tx lock counters from a critical section"),
)

QUEUE_SPECS_TAIL = (
    ColumnSpec("Src", doc="discovery channels"),
    ColumnSpec("Addr", kind="addr"),
)


def queue_specs(queue_sets: bool) -> tuple[ColumnSpec, ...]:
    """The queue table contract; ``Set`` exists only with queue sets."""
    specs = list(QUEUE_SPECS_HEAD)
    if queue_sets:
        specs.append(
            ColumnSpec("Set", kind="addr", doc="owning queue set, '-' when none")
        )
    return (*specs, *QUEUE_SPECS_TAIL)


SEMAPHORE_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Type", kind="enum"),
    ColumnSpec("Count", kind="int", align="right", doc="available count"),
    ColumnSpec("Max", kind="int", align="right", doc="upper bound"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

MUTEX_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Type", kind="enum"),
    ColumnSpec("Held", kind="enum", doc="'yes' when taken; value is the count"),
    ColumnSpec("Owner", doc="holder task, '-' when free"),
    ColumnSpec("Recursive", kind="int", align="right", doc="nested take count"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

TIMER_SPECS = (
    ColumnSpec("Name", elastic=True, doc="pcTimerName"),
    ColumnSpec("State", doc="list-derived state; '?' marks a transition"),
    ColumnSpec("Mode", doc="auto-reload vs one-shot"),
    ColumnSpec("Period", kind="int", align="right", doc="ticks"),
    ColumnSpec("Expiry", kind="int", align="right", doc="absolute tick"),
    ColumnSpec("ExpiresIn", kind="int", align="right", doc="wrap-safe ticks left"),
    ColumnSpec("Callback", kind="symbol", elastic=True),
    ColumnSpec("ID", kind="hex", doc="pvTimerID, '-' when NULL"),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

EVENT_GROUP_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Bits", kind="hex", doc="current event bits"),
    ColumnSpec("Waiters", kind="int", align="right"),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

HEAP_LINEAR_SPECS = (
    ColumnSpec("Address", kind="addr"),
    ColumnSpec("Size", kind="int", align="right"),
    ColumnSpec("State", kind="enum", doc="allocated vs free"),
)

HEAP_FREE_LIST_SPECS = (
    ColumnSpec("Address", kind="addr"),
    ColumnSpec("Size", kind="int", align="right"),
    ColumnSpec("Next", kind="addr", elastic=True, doc="next free block, '-' at end"),
)


STREAM_BUFFER_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Type", doc="byte stream vs message buffer"),
    ColumnSpec("Bytes", kind="int", align="right"),
    ColumnSpec("Space", kind="int", align="right"),
    ColumnSpec("Capacity", kind="int", align="right"),
    ColumnSpec("Trigger", kind="int", align="right"),
    ColumnSpec("NextMsg", kind="hex", doc="message length prefix, 'N/A' for streams"),
    ColumnSpec("RecvWait", doc="single receiving task handle"),
    ColumnSpec("SendWait", doc="single sending task handle"),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)
