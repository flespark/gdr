"""Unit tests for scheduler state, the saved-PC wiring, and the heap map (no GDB)."""

from __future__ import annotations

import pytest

import rtthread.adapter as adapter_module
from gdr.layout import KernelLayout
from rtthread.adapter import RtThreadAdapter, Thread
from rtthread.columns import task_specs
from rtthread.diagnostics import HeapWalk, WalkBlock
from rtthread.heapviews import HeapInfo
from rtthread.navigation import HeapSnapshot


def _adapter(**kwargs) -> RtThreadAdapter:
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

    assert _adapter().scheduler_state() == expected


def test_system_summary_omits_scheduler_state_when_unreadable(monkeypatch):
    """A None state reaches the summary as None, so the row is not printed."""
    adapter = _adapter()
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter, "scheduler_state", lambda: None)
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(adapter, "heap_info", lambda: HeapInfo(algorithm="none"))

    assert adapter.system_summary().scheduler_state is None


def test_task_columns_put_pc_after_baseprio_and_tcb_last():
    """The contract order: last PC before SP, the TCB address as the final column."""
    headers = [spec.key for spec in task_specs(smp=False)]
    assert headers[:6] == ["Name", "State", "Prio", "BasePrio", "PC", "SP"]
    assert headers[-1] == "TCB"
    assert "Addr" not in headers
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

    assert _adapter()._thread_pc(thread, is_current=True) == 0x6002_0100


def test_thread_pc_is_none_for_a_thread_running_on_another_cpu(monkeypatch):
    """Its saved frame is stale; the PC is unknown rather than decoded from it."""
    monkeypatch.setattr(adapter_module, "gdb", object())
    thread = Thread(name="worker", address=0x2000, sp=0x600A_0000, oncpu=1)

    assert _adapter(version=(4, 0, 5))._thread_pc(thread, is_current=False) is None


def _walk(blocks: list[WalkBlock], **overrides: object) -> HeapWalk:
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
    adapter = _adapter(heap_type="small_mem")
    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(algorithm="small_mem", walk=_walk(blocks)),
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
    adapter = _adapter(heap_type="small_mem")
    monkeypatch.setattr(
        adapter,
        "heap_info",
        lambda: HeapInfo(
            algorithm="small_mem",
            walk=_walk([WalkBlock(0x1000, 0x40, True, None)], corrupt=True),
        ),
    )
    heap = adapter.heap_map()
    assert heap is not None
    assert heap.truncated is True
    assert "corrupt" in heap.messages[0]


def test_heap_map_is_none_without_a_heap_or_a_walk(monkeypatch):
    """No heap, or a chain that cannot be resolved, leaves nothing to draw."""
    assert _adapter(heap_type="none").heap_map() is None

    adapter = _adapter(heap_type="small_mem")
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
    _adapter(heap_type="small_mem").heap_map()

    assert calls["count"] == 1
