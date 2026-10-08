"""RT-Thread column contracts: the shape of every table this adapter prints.

Headers, machine-value semantics, shrink priority and the capability claims all
come from these tuples, so a table and the columns it advertises cannot
disagree.  ``adapter.py`` builds the rows; this module declares the columns.
"""

from __future__ import annotations

from gdr.adapter_api import ColumnSpec
from gdr.layout import KernelLayout

# ---------------------------------------------------------------------------
# Column contracts. Each table's headers, spec metadata and machine values come
# from one place, so the table and the capability claim it makes cannot
# disagree about a column's name or position.
# ---------------------------------------------------------------------------

SEMAPHORE_SPECS = (
    ColumnSpec("Name", elastic=True, doc="object name"),
    ColumnSpec("Value", kind="int", align="right"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Addr", kind="addr"),
)

MUTEX_SPECS = (
    ColumnSpec("Name", elastic=True, doc="object name"),
    ColumnSpec("Value", kind="int", align="right"),
    ColumnSpec("Hold", kind="int", align="right"),
    ColumnSpec("OrigPrio", kind="int", align="right"),
    ColumnSpec("Owner", elastic=True),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Addr", kind="addr"),
)

EVENT_SPECS = (
    ColumnSpec("Name", elastic=True, doc="object name"),
    ColumnSpec("Set", kind="hex", doc="current bit set"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Addr", kind="addr"),
)

MAILBOX_SPECS = (
    ColumnSpec("Name", elastic=True, doc="object name"),
    ColumnSpec("Entry", kind="int", align="right", doc="entry count"),
    ColumnSpec("Size", kind="int", align="right", doc="slot count"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("In", kind="int", align="right"),
    ColumnSpec("Out", kind="int", align="right"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("RecvWait", kind="waiters", elastic=True),
    ColumnSpec("SendWait", kind="waiters", elastic=True),
    ColumnSpec("Addr", kind="addr"),
)

MSGQUEUE_SPECS = (
    ColumnSpec("Name", elastic=True, doc="object name"),
    ColumnSpec("Entry", kind="int", align="right", doc="entry count"),
    ColumnSpec("MsgSize", kind="int", align="right", doc="message bytes"),
    ColumnSpec("MaxMsgs", kind="int", align="right", doc="message slots"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("RecvWait", kind="waiters", elastic=True),
    ColumnSpec("SendWait", kind="waiters", elastic=True),
    ColumnSpec("Addr", kind="addr"),
)

MEMPOOL_SPECS = (
    ColumnSpec("Name", elastic=True, doc="object name"),
    ColumnSpec("BlockSize", kind="int", align="right", doc="block size in bytes"),
    ColumnSpec("Total", kind="int", align="right", doc="block count"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("Used", kind="int", align="right"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Addr", kind="addr"),
)

TIMER_SPECS = (
    ColumnSpec("Name", elastic=True, doc="timer name"),
    ColumnSpec("State"),
    ColumnSpec("Mode"),
    ColumnSpec("Type"),
    ColumnSpec("InitTick", kind="int", align="right"),
    ColumnSpec("TimeoutTick", kind="int", align="right"),
    ColumnSpec("ExpiresIn", kind="int", align="right"),
    ColumnSpec("Callback", kind="symbol", elastic=True),
    ColumnSpec("Addr", kind="addr"),
)

KIND_SPECS: dict[str, tuple[ColumnSpec, ...]] = {
    "semaphore": SEMAPHORE_SPECS,
    "mutex": MUTEX_SPECS,
    "event": EVENT_SPECS,
    "mailbox": MAILBOX_SPECS,
    "msgqueue": MSGQUEUE_SPECS,
    "mempool": MEMPOOL_SPECS,
    "timer": TIMER_SPECS,
}


def smp_columns_present(layout: KernelLayout) -> bool:
    """Whether the thread struct carries SMP-only fields in this build."""
    thread_layout = layout.structs.get("struct rt_thread")
    return bool(
        thread_layout
        and ("oncpu" in thread_layout.fields or "bind_cpu" in thread_layout.fields)
    )


def task_specs(smp: bool) -> tuple[ColumnSpec, ...]:
    """The task table contract; SMP adds the CPU affinity pair."""
    specs = [
        ColumnSpec("Name", elastic=True, doc="thread name; '*' marks the running one"),
        ColumnSpec("State"),
        ColumnSpec("Prio", kind="int", align="right"),
        ColumnSpec("BasePrio", kind="int", align="right"),
        ColumnSpec("SP", kind="addr", doc="stack pointer"),
        ColumnSpec("Stack", kind="int", align="right", doc="stack size in bytes"),
        ColumnSpec("Used", kind="int", align="right", doc="stack bytes in use"),
        ColumnSpec(
            "HighWater", kind="int", align="right", doc="peak stack bytes in use"
        ),
        ColumnSpec("Entry", kind="symbol", elastic=True),
    ]
    if smp:
        specs += [
            ColumnSpec("CPU", kind="int", align="right"),
            ColumnSpec("Bind", kind="int", align="right"),
        ]
    specs.append(ColumnSpec("Addr", kind="addr"))
    return tuple(specs)
