"""Unit tests for the RT-Thread capability claims and system-check report."""

from __future__ import annotations

import pytest

import rtthread.adapter as adapter_module
from core.layout import (
    KernelLayout,
    StructField,
    StructLayout,
)
from rtthread import diagnostics
from rtthread.adapter import (
    HeapInfo,
    RtThreadAdapter,
    Thread,
)
from rtthread.diagnostics import (
    HeapWalk,
    WalkBlock,
)
from rtthread.navigation import HeapSnapshot
from rtthread.views import task_specs


def _adapter(*, smp: bool = False, heap_type: str = "small_mem") -> RtThreadAdapter:
    """Adapter whose layout carries (or omits) the SMP thread fields."""
    layout = KernelLayout()
    fields = (
        {
            "oncpu": StructField("oncpu", ("oncpu",)),
            "bind_cpu": StructField("bind_cpu", ("bind_cpu",)),
        }
        if smp
        else {}
    )
    layout.structs["struct rt_thread"] = StructLayout("struct rt_thread", fields=fields)
    return RtThreadAdapter(layout, heap_type=heap_type)


def test_capabilities_claim_columns_from_the_table_contracts(monkeypatch):
    """Kind columns come from the same specs the tables print."""
    adapter = _adapter()
    monkeypatch.setattr(adapter, "object_counts", lambda: {"task": 2, "semaphore": 1})

    claims = adapter.capabilities()

    assert claims.kinds["task"].columns[:3] == ("Name", "State", "Prio")
    assert claims.kinds["semaphore"].columns == (
        "Name",
        "Value",
        "Policy",
        "Waiters",
        "Addr",
    )
    assert claims.kinds["task"].enumerable is True
    assert claims.kinds["mutex"].enumerable is False
    assert claims.kinds["timer"].detail is True


def test_capabilities_gate_the_smp_columns_and_name_the_reason(monkeypatch):
    """A missing SMP column carries a note a UI can turn into advice."""
    plain = _adapter()
    monkeypatch.setattr(plain, "object_counts", lambda: {"task": 1})
    smp = _adapter(smp=True)
    monkeypatch.setattr(smp, "object_counts", lambda: {"task": 1})

    assert "CPU" not in plain.capabilities().kinds["task"].columns
    assert "CPU" in smp.capabilities().kinds["task"].columns
    assert "SMP" in (smp.capabilities().kinds["task"].note or "")
    assert smp.capabilities().extra["smp"] is True


def test_capabilities_report_the_heap_and_message_queue_limits(monkeypatch):
    """Adapter-owned limitations are structured, not only baked into text."""
    adapter = _adapter(heap_type="none")
    monkeypatch.setattr(adapter, "object_counts", lambda: {"msgqueue": 1})

    claims = adapter.capabilities()

    assert claims.extra["heap_type"] == "none"
    assert "v3.1.4" in (claims.kinds["msgqueue"].note or "")


def _walk(**overrides) -> diagnostics.HeapWalk:
    base = {
        "used_blocks": 2,
        "free_blocks": 1,
        "hole_sizes": [],
    }
    base.update(overrides)
    return diagnostics.HeapWalk(**base)


def test_build_advice_gates_smp_and_does_not_repeat_release_notes(monkeypatch):
    plain = _adapter()
    smp = _adapter(smp=True)
    for adapter in (plain, smp):
        monkeypatch.setattr(adapter, "object_counts", lambda: {"task": 1})
    assert "RT_USING_SMP" in " ".join(plain.capabilities().build_hints)
    assert smp.capabilities().build_hints == []
    assert not any("SendWait" in hint for hint in plain.capabilities().build_hints)


def test_diagnostics_report_flags_a_corrupt_or_truncated_chain(monkeypatch):
    """A corrupt chain is a failure; a bounded walk is explicitly skipped."""
    adapter = _adapter()
    monkeypatch.setattr(diagnostics, "system_checks", lambda _layout: [])
    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(
            algorithm="small_mem", used=5, total=9, walk=_walk(corrupt=True)
        ),
    )

    findings = adapter.diagnostics_report()

    assert [finding.verdict for finding in findings] == ["fail", "skipped"]
    assert "corrupt" in findings[0].detail

    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(
            algorithm="small_mem",
            used=5,
            total=9,
            walk=_walk(truncated=True, used_bytes=5, total_bytes=9),
        ),
    )
    truncated = adapter.diagnostics_report()
    assert [finding.verdict for finding in truncated] == ["skipped", "ok"]


def test_diagnostics_report_cross_checks_the_snapshot_counters(monkeypatch):
    """Exact walk accounting is compared against the snapshot, not restated."""
    adapter = _adapter()
    monkeypatch.setattr(diagnostics, "system_checks", lambda _layout: [])

    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(
            algorithm="small_mem",
            used=5,
            total=9,
            walk=_walk(used_bytes=5, total_bytes=9),
        ),
    )
    assert [f.verdict for f in adapter.diagnostics_report()] == ["ok", "ok"]

    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(
            algorithm="small_mem",
            used=6,
            total=9,
            walk=_walk(used_bytes=5, total_bytes=9),
        ),
    )
    mismatch = adapter.diagnostics_report()
    assert mismatch[1].verdict == "fail"
    assert "used 6 B of 9 B" in mismatch[1].detail
    assert "walk used 5 B" in mismatch[1].detail

    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(algorithm="small_mem", used=10, total=9, walk=None),
    )
    overrun = adapter.diagnostics_report()
    assert overrun[1].verdict == "fail"
    assert "exceeds total" in overrun[1].detail


def test_diagnostics_report_skips_a_build_without_a_heap(monkeypatch):
    """No heap is reported as skipped rather than silently clean."""
    adapter = _adapter(heap_type="none")
    monkeypatch.setattr(diagnostics, "system_checks", lambda _layout: [])

    findings = adapter.diagnostics_report()

    assert len(findings) == 1
    assert findings[0].verdict == "skipped"
    assert "no system heap" in findings[0].detail


def test_diagnostics_report_leads_with_system_verdicts(monkeypatch):
    """System identity checks ride the report ahead of the heap verdicts."""
    adapter = _adapter()
    monkeypatch.setattr(
        diagnostics, "system_checks", lambda _layout: [("Tick", "ok: 100")]
    )
    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(
            algorithm="small_mem", used=5, total=9, walk=_walk(used_bytes=5)
        ),
    )

    findings = adapter.diagnostics_report()

    assert findings[0].name == "Tick"
    assert findings[0].scope == "system"


def test_thread_stack_used_uses_a_valid_downward_stack_pointer():
    """Stack consumption is derived only from a saved SP inside the stack."""
    assert (
        Thread(
            stack_addr=0x1000, stack_size=0x100, sp=0x1080, stack_grows_up=False
        ).stack_used
        == 0x80
    )
    assert (
        Thread(
            stack_addr=0x1000, stack_size=0x100, sp=0x0FFF, stack_grows_up=False
        ).stack_used
        is None
    )
    assert (
        Thread(
            stack_addr=0x1000, stack_size=0x100, sp=0x1101, stack_grows_up=False
        ).stack_used
        is None
    )


def test_thread_stack_used_supports_upward_stack_growth():
    """Upward stacks measure consumed space from the low stack address."""
    thread = Thread(stack_addr=0x1000, stack_size=0x100, sp=0x1080, stack_grows_up=True)

    assert thread.stack_used == 0x80


def test_thread_stack_used_is_unavailable_without_a_known_direction():
    """An unknown direction must not be guessed from the saved stack pointer."""
    assert Thread(stack_addr=0x1000, stack_size=0x100, sp=0x1080).stack_used is None


def _heap_adapter(**kwargs) -> RtThreadAdapter:
    return RtThreadAdapter(KernelLayout(), **kwargs)


@pytest.mark.parametrize(
    ("irq", "lock", "expected"),
    [
        (1, 0, "In ISR (nest 1)"),
        (2, 3, "In ISR (nest 2)"),  # an ISR wins over a lock held inside it
        (0, 2, "Locked (nest 2)"),
        (0, 0, "Unlocked"),
        (None, 0, "Unlocked"),
        (0, None, None),  # no lock counter readable: never a fabricated state
        (None, None, None),
    ],
)
def test_scheduler_state_reports_the_nest_counters(monkeypatch, irq, lock, expected):
    """Scheduler state comes from the interrupt and lock counters, not a constant."""
    monkeypatch.setattr(adapter_module, "read_irq_nest", lambda: irq)
    monkeypatch.setattr(
        adapter_module, "read_scheduler_lock_nest", lambda _thread: lock
    )
    monkeypatch.setattr(adapter_module, "get_current_thread", lambda: None)

    assert _heap_adapter().scheduler_state() == expected


def test_system_summary_omits_scheduler_state_when_unreadable(monkeypatch):
    """A None state reaches the summary as None, so the row is not printed."""
    adapter = _heap_adapter()
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter, "scheduler_state", lambda: None)
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(adapter, "heap_info", lambda: HeapInfo(algorithm="none"))

    assert adapter.system_summary().scheduler_state is None


def test_task_columns_put_pc_after_baseprio_with_addr_last():
    """The contract order: last PC before SP, the thread address as final column."""
    headers = [spec.key for spec in task_specs(smp=False)]
    assert headers[:6] == ["Name", "State", "Prio", "BasePrio", "PC", "SP"]
    assert headers[-1] == "Addr"
    assert "TCB" not in headers
    pc = next(spec for spec in task_specs(smp=False) if spec.key == "PC")
    assert pc.kind == "pc"


def test_thread_pc_of_the_running_thread_is_the_newest_frame(monkeypatch):
    """The halted CPU's running thread reports the newest frame's PC."""

    class _Frame:
        def pc(self):
            return 0x6002_0100

    class _Gdb:
        error = RuntimeError

        @staticmethod
        def newest_frame():
            return _Frame()

    monkeypatch.setattr(adapter_module, "gdb", _Gdb)
    thread = Thread(name="main", address=0x2000)

    assert _heap_adapter()._thread_pc(thread, is_current=True) == 0x6002_0100


def test_thread_pc_is_none_for_a_thread_running_on_another_cpu(monkeypatch):
    """Its saved frame is stale; the PC is unknown rather than decoded from it."""
    monkeypatch.setattr(adapter_module, "gdb", object())
    thread = Thread(name="worker", address=0x2000, sp=0x600A_0000, oncpu=1)

    assert _heap_adapter(version=(4, 0, 5))._thread_pc(thread, is_current=False) is None


def _heap_walk(blocks: list[WalkBlock], **overrides: object) -> HeapWalk:
    """A walk over *blocks* at 0x1000..0x1100; keyword overrides set flags and totals."""
    used = sum(1 for block in blocks if block.used)
    walk = HeapWalk(
        used_blocks=used,
        free_blocks=len(blocks) - used,
        hole_sizes=[],
        blocks=list(blocks),
        base=0x1000,
        end=0x1100,
    )
    for name, value in overrides.items():
        setattr(walk, name, value)
    return walk


def test_heap_map_lists_the_walked_blocks_with_their_owners(monkeypatch):
    """The map carries every block's span and owner, and flags MEMTRACE availability."""
    blocks = [
        WalkBlock(0x1000, 0x40, True, "main"),
        WalkBlock(0x1040, 0x40, False, None),
        WalkBlock(0x1080, 0x80, True, None),
    ]
    adapter = _heap_adapter(heap_type="small_mem")
    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(algorithm="small_mem", walk=_heap_walk(blocks)),
    )
    monkeypatch.setattr(adapter, "_memtrace_enabled", lambda: True)

    heap = adapter.heap_map()

    assert heap is not None
    assert (heap.algorithm, heap.base, heap.end) == ("small_mem", 0x1000, 0x1100)
    assert [(b.address, b.size, b.used, b.owner) for b in heap.blocks] == [
        (0x1000, 0x40, True, "main"),
        (0x1040, 0x40, False, None),
        (0x1080, 0x80, True, None),
    ]
    assert heap.owners is True
    assert heap.truncated is False
    assert heap.messages == []


def test_heap_map_flags_a_truncated_or_corrupt_walk(monkeypatch):
    """A partial walk still draws what it read, and says why the rest is missing."""
    adapter = _heap_adapter(heap_type="small_mem")
    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(
            algorithm="small_mem",
            walk=_heap_walk([WalkBlock(0x1000, 0x40, True, None)], corrupt=True),
        ),
    )
    heap = adapter.heap_map()
    assert heap is not None
    assert heap.truncated is True
    assert "corrupt" in heap.messages[0]


def test_heap_map_is_none_without_a_heap_or_a_walk(monkeypatch):
    """No heap, or a chain that cannot be resolved, leaves nothing to draw."""
    assert _heap_adapter(heap_type="none").heap_map() is None

    adapter = _heap_adapter(heap_type="small_mem")
    monkeypatch.setattr(
        adapter, "heap_info", lambda: HeapInfo(algorithm="small_mem", walk=None)
    )
    assert adapter.heap_map() is None


def test_heap_map_reads_the_snapshot_once_through_heap_info(monkeypatch):
    """heap_map reuses heap_info's collection; it never walks the heap on its own."""
    calls = {"count": 0}

    def fake_info(_adapter):
        calls["count"] += 1
        return HeapInfo(algorithm="small_mem", walk=None)

    monkeypatch.setattr(RtThreadAdapter, "heap_info", fake_info)
    monkeypatch.setattr(
        adapter_module, "get_heap_snapshot", lambda _t, _l: HeapSnapshot(algorithm="x")
    )
    _heap_adapter(heap_type="small_mem").heap_map()

    assert calls["count"] == 1
