"""RT-Thread column contracts: the shape of every table this adapter prints.

Headers, machine-value semantics, shrink priority and the capability claims all
come from these tuples, so a table and the columns it advertises cannot
disagree.  ``adapter.py`` builds the rows; this module declares the columns.
"""

from dataclasses import dataclass

from core.adapter import (
    Cell,
    CheckFinding,
    ColumnSpec,
    symbol_cell,
)
from core.gdb_bridge import (
    lookup_symbol_at,
    read_int,
    source_definition_at,
    source_location_at,
)
from core.layout import KernelLayout, read_field
from rtthread import diagnostics
from rtthread.navigation import find_thread

# ---------------------------------------------------------------------------
# Column contracts. Each table's headers, spec metadata and machine values come
# from one place, so the table and the capability claim it makes cannot
# disagree about a column's name or position.
# ---------------------------------------------------------------------------

SEMAPHORE_SPECS = (
    ColumnSpec("Name", elastic="left", doc="object name"),
    ColumnSpec("Value", kind="int", align="right"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("Waiters", kind="waiters", elastic="left"),
    ColumnSpec("Addr", kind="addr"),
)

MUTEX_SPECS = (
    ColumnSpec("Name", elastic="left", doc="object name"),
    ColumnSpec("Value", kind="int", align="right"),
    ColumnSpec("Hold", kind="int", align="right"),
    ColumnSpec("OrigPrio", kind="int", align="right"),
    ColumnSpec("Owner", elastic="left"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("Waiters", kind="waiters", elastic="left"),
    ColumnSpec("Addr", kind="addr"),
)

EVENT_SPECS = (
    ColumnSpec("Name", elastic="left", doc="object name"),
    ColumnSpec("Set", kind="hex", doc="current bit set"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("Waiters", kind="waiters", elastic="left"),
    ColumnSpec("Addr", kind="addr"),
)

MAILBOX_SPECS = (
    ColumnSpec("Name", elastic="left", doc="object name"),
    ColumnSpec("Entry", kind="int", align="right", doc="entry count"),
    ColumnSpec("Size", kind="int", align="right", doc="slot count"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("In", kind="int", align="right"),
    ColumnSpec("Out", kind="int", align="right"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("RecvWait", kind="waiters", elastic="left"),
    ColumnSpec("SendWait", kind="waiters", elastic="left"),
    ColumnSpec("Addr", kind="addr"),
)

MSGQUEUE_SPECS = (
    ColumnSpec("Name", elastic="left", doc="object name"),
    ColumnSpec("Entry", kind="int", align="right", doc="entry count"),
    ColumnSpec("MsgSize", kind="int", align="right", doc="message bytes"),
    ColumnSpec("MaxMsgs", kind="int", align="right", doc="message slots"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("Policy", kind="enum"),
    ColumnSpec("RecvWait", kind="waiters", elastic="left"),
    ColumnSpec("SendWait", kind="waiters", elastic="left"),
    ColumnSpec("Addr", kind="addr"),
)

MEMPOOL_SPECS = (
    ColumnSpec("Name", elastic="left", doc="object name"),
    ColumnSpec("BlockSize", kind="int", align="right", doc="block size in bytes"),
    ColumnSpec("Total", kind="int", align="right", doc="block count"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("Used", kind="int", align="right"),
    ColumnSpec("Waiters", kind="waiters", elastic="left"),
    ColumnSpec("Addr", kind="addr"),
)

TIMER_SPECS = (
    ColumnSpec("Name", elastic="left", doc="timer name"),
    ColumnSpec("State"),
    ColumnSpec("Mode"),
    ColumnSpec("TimerType", doc="soft timer thread vs hard interrupt callback"),
    ColumnSpec("InitTick", kind="int", align="right"),
    ColumnSpec("TimeoutTick", kind="int", align="right"),
    ColumnSpec("ExpiresIn", kind="int", align="right"),
    ColumnSpec("Callback", kind="symbol", elastic="left"),
    ColumnSpec("Src", doc="registry and/or active-list evidence"),
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
    """The task table contract; SMP adds the CPU affinity pair.

    ``PC`` is the thread's last program counter (``pc`` kind: a source link when
    the line resolves, the memory view otherwise). ``Addr`` is the thread control
    block address and the row identity; it has no property meaning to a reader.
    """
    specs = [
        ColumnSpec(
            "Name", elastic="left", doc="thread name; '*' marks the running one"
        ),
        ColumnSpec("State"),
        ColumnSpec("Prio", kind="int", align="right"),
        ColumnSpec("BasePrio", kind="int", align="right"),
        ColumnSpec(
            "PC",
            kind="pc",
            doc="last PC: the running PC, or the return address on the saved switch frame",
        ),
        ColumnSpec("SP", kind="addr", doc="stack pointer"),
        ColumnSpec("Stack", kind="int", align="right", doc="stack size in bytes"),
        ColumnSpec("Used", kind="int", align="right", doc="stack bytes in use"),
        ColumnSpec(
            "HighWater", kind="int", align="right", doc="peak stack bytes in use"
        ),
        ColumnSpec("Entry", kind="symbol", elastic="left"),
    ]
    if smp:
        specs += [
            ColumnSpec("CPU", kind="int", align="right"),
            ColumnSpec("Bind", kind="int", align="right"),
        ]
    specs.append(ColumnSpec("Addr", kind="addr", doc="thread control block address"))
    return tuple(specs)


# ---------------------------------------------------------------------------
# Typed detail-table navigation cells.
# ---------------------------------------------------------------------------
DETAIL_SPECS = (
    ColumnSpec("Callback", kind="symbol"),
    ColumnSpec("Parameter", kind="symbol"),
    ColumnSpec("Waiters", kind="waiters"),
    ColumnSpec("RecvWait", kind="waiters"),
    ColumnSpec("SendWait", kind="waiters"),
    ColumnSpec("Slot", kind="addresses"),
)


def detail_cells(kind: str, value, layout: KernelLayout, pairs) -> dict[str, Cell]:
    """Keep typed waiter/slot values and timer navigation alongside CLI text."""
    from rtthread.adapter import value_to_timer

    cells: dict[str, Cell] = {}
    for key, text in pairs:
        if key in ("Waiters", "RecvWait", "SendWait"):
            # NOTE: N/A (no wait list) must stay distinct from an empty list.
            names = text.partition("@")[2].split(",") if "@" in text else []
            records = []
            for name in names:
                thread = find_thread(name, layout)
                entry = (
                    read_int(
                        read_field(thread, layout.structs["struct rt_thread"], "entry")
                    )
                    if thread is not None and "struct rt_thread" in layout.structs
                    else None
                )
                source = source_location_at(entry) if entry else None
                records.append(
                    {
                        "name": name,
                        "address": entry,
                        "source": {
                            "file": source.fullname,
                            "name": source.name,
                            "line": source.line,
                        }
                        if source
                        else None,
                    }
                )
            cells[key] = Cell(text, None if text == "N/A" else records)
    slots = []
    for key, text in pairs:
        if key.startswith("Slot["):
            try:
                address = int(text, 16)
            except ValueError:
                address = None
            slots.append({"text": f"{key}: {text}", "address": address})
    if slots:
        cells["Slot"] = Cell(
            text="; ".join(slot["text"] for slot in slots), value=slots
        )
    if kind == "timer":
        timer = value_to_timer(value, layout)
        for key, address in (
            ("Callback", timer.callback),
            ("Parameter", timer.parameter),
        ):
            cells[key] = symbol_cell(
                address,
                lookup_symbol_at(address) if address else None,
                (
                    source_location_at(address)
                    if key == "Callback"
                    else source_definition_at(address)
                )
                if address
                else None,
            )
    return cells


# ---------------------------------------------------------------------------
# Heap snapshot views and verdicts.
# ---------------------------------------------------------------------------


@dataclass
class HeapInfo:
    """One snapshot+walk collection shared by ``rtt system`` and ``rtt heap``.

    ``used``/``total`` are the snapshot counters, replaced by exact walk
    bytes only when a closed small_mem/memheap walk is allowed to fill a
    missing value. ``max_used`` stays snapshot-only. ``walk`` is the bounded
    block walk, or ``None`` when the chain is unresolvable.
    """

    algorithm: str
    used: int | None = None
    total: int | None = None
    max_used: int | None = None
    from_walk: bool = False
    walk: diagnostics.HeapWalk | None = None


@dataclass
class HeapDetail:
    """Formatted heap block line from a bounded system-heap walk.

    ``pairs`` holds the Blocks line; ``status`` is heap health
    (``good`` / ``corrupt`` / ``overrun``), copied onto
    ``SystemSummary.heap_status``; a truncated walk is only a Blocks suffix.
    """

    pairs: list[tuple[str, str]]
    status: str | None = "good"


def _heap_status(
    walk: diagnostics.HeapWalk | None,
    used: int | None = None,
    total: int | None = None,
) -> str | None:
    """Classify heap health; a truncated walk is not a status value.

    ``truncated`` only means the bounded walk hit ``GDR_MAX_TRAVERSAL_COUNT``.
    ``overrun`` is used exceeding total. Corrupt walks win over overrun.
    ``None`` means the system command should omit Heap status.
    """
    if walk is not None and walk.corrupt:
        return "corrupt"
    if used is not None and total is not None and used > total:
        return "overrun"
    if walk is None:
        return None
    return "good"


def _heap_detail_from_walk(
    walk: diagnostics.HeapWalk | None,
    used: int | None = None,
    total: int | None = None,
) -> HeapDetail | None:
    """Assemble the Blocks line from one bounded walk result."""
    if walk is None:
        return None
    status = _heap_status(walk, used, total)
    if walk.corrupt:
        suffix = " (corrupt)"
    elif walk.truncated:
        suffix = " (truncated)"
    else:
        suffix = ""
    return HeapDetail(
        pairs=[
            (
                "Blocks",
                f"{walk.used_blocks} used, {walk.free_blocks} free, "
                f"{walk.used_blocks + walk.free_blocks} total{suffix}",
            ),
        ],
        status=status,
    )


def _heap_accounting_finding(info: HeapInfo) -> CheckFinding:
    """Cross-check the heap snapshot counters against exact chain accounting.

    The walk reports exact chain bytes only when it closed cleanly, so this is
    a real cross-check rather than a restated snapshot: a disagreement means
    one of the two sources is stale or the chain is not the whole heap.

    An overrun (used above total) is caught first because it needs no walk.
    """
    used, total = info.used, info.total
    if used is not None and total is not None and used > total:
        return CheckFinding(
            "HeapAccounting", "fail", f"used {used} B exceeds total {total} B", "system"
        )
    walk = info.walk
    if walk is None or walk.used_bytes is None or walk.total_bytes is None:
        return CheckFinding(
            "HeapAccounting", "skipped", "exact chain accounting unavailable", "system"
        )
    if used is None or total is None:
        return CheckFinding(
            "HeapAccounting", "skipped", "snapshot counters unavailable", "system"
        )
    if walk.used_bytes == used and walk.total_bytes == total:
        return CheckFinding("HeapAccounting", "ok", scope="system")
    return CheckFinding(
        "HeapAccounting",
        "fail",
        f"snapshot used {used} B of {total} B != walk used {walk.used_bytes} B "
        f"of {walk.total_bytes} B",
        "system",
    )


def _heap_chain_finding(walk: diagnostics.HeapWalk | None) -> CheckFinding:
    """Verdict for the bounded block-chain walk."""
    if walk is None:
        return CheckFinding(
            "HeapChain", "skipped", "block chain not resolvable", "system"
        )
    if walk.corrupt:
        return CheckFinding("HeapChain", "fail", "the block chain is corrupt", "system")
    if walk.truncated:
        return CheckFinding(
            "HeapChain", "skipped", "the walk hit the traversal bound", "system"
        )
    return CheckFinding("HeapChain", "ok", scope="system")
