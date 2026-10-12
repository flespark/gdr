"""Unit tests for the RT-Thread capability claims and system-check report."""

from __future__ import annotations

from gdr.layout import KernelLayout, StructField, StructLayout
from rtthread import diagnostics
from rtthread.adapter import HeapInfo, RtThreadAdapter


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


def test_diagnostics_report_skips_a_build_without_a_heap():
    """No heap is reported as skipped rather than silently clean."""
    adapter = _adapter(heap_type="none")

    findings = adapter.diagnostics_report()

    assert len(findings) == 1
    assert findings[0].verdict == "skipped"
    assert "no system heap" in findings[0].detail
