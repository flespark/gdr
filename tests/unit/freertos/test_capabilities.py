"""Unit tests for the FreeRTOS capability claims and system-check report."""

from __future__ import annotations

import freertos.adapter as adapter_module
from freertos.adapter import FreeRtosAdapter
from freertos.layout import FreeRtosConfig, build_layout
from gdr.gdb_bridge import TARGET_ACCESS_ERRORS


def _adapter(**overrides) -> FreeRtosAdapter:
    """Adapter over a layout whose config is the documented default plus overrides."""
    return FreeRtosAdapter(build_layout(FreeRtosConfig(**overrides), (11, 1, 0)))


def test_capabilities_declare_every_kind_and_its_columns():
    """Each kind claims the columns its table actually prints."""
    claims = _adapter(
        trace_facility=True,
        queue_registry=True,
        queue_sets=True,
        timers=True,
        event_groups=True,
        stream_buffers=True,
    ).capabilities()

    assert set(claims.kinds) == {
        "task",
        "queue",
        "semaphore",
        "mutex",
        "timer",
        "eventgroup",
        "streambuffer",
    }
    queue_columns = claims.kinds["queue"].columns
    assert "Set" in queue_columns
    assert claims.kinds["queue"].note is None
    assert claims.kinds["timer"].enumerable is True
    assert claims.kinds["streambuffer"].enumerable is True


def test_capabilities_explain_each_gated_kind_and_column():
    """A missing column or kind carries the config that would enable it."""
    claims = _adapter().capabilities()

    assert claims.kinds["timer"].enumerable is False
    assert "configUSE_TIMERS" in (claims.kinds["timer"].note or "")
    assert "configUSE_EVENT_GROUPS" in (claims.kinds["eventgroup"].note or "")
    assert "Set needs configUSE_QUEUE_SETS" in (claims.kinds["queue"].note or "")
    assert "configUSE_TRACE_FACILITY" in (claims.kinds["queue"].note or "")
    assert "configQUEUE_REGISTRY_SIZE" in (claims.kinds["queue"].note or "")
    assert "Set" not in claims.kinds["queue"].columns
    assert "wait lists" in (claims.kinds["streambuffer"].note or "")


def test_capabilities_track_the_task_columns_and_report_config_facts():
    """Task columns follow the TCB fields; extra carries the probed config."""
    claims = _adapter().capabilities()

    task_columns = claims.kinds["task"].columns
    assert task_columns[:3] == ("Name", "State", "Prio")
    assert "CPU" not in task_columns
    assert "HighWater" not in task_columns
    bounded = _adapter(stack_end_field="pxEndOfStack").capabilities()
    assert "HighWater" in bounded.kinds["task"].columns
    assert "configNUMBER_OF_CORES" in (claims.kinds["task"].note or "")
    assert claims.extra["smp"] is False
    assert claims.extra["cores"] == 1


def test_capabilities_add_the_smp_columns_on_a_multicore_build():
    """A multicore build declares CPU and its affinity without a gap note."""
    claims = _adapter(smp=True, number_of_cores=2).capabilities()

    assert "CPU" in claims.kinds["task"].columns
    assert claims.extra["smp"] is True
    assert "configNUMBER_OF_CORES" not in (claims.kinds["task"].note or "")


def test_diagnostics_report_structures_the_system_checks(monkeypatch):
    """The checks verb mirrors the verdicts ``frt system`` renders."""
    monkeypatch.setattr(
        adapter_module,
        "system_checks",
        lambda _layout: [
            ("TaskCount", "ok"),
            ("HeapCrossCheck", "fail: free list disagrees"),
        ],
    )

    findings = _adapter().diagnostics_report()

    assert [(finding.name, finding.verdict) for finding in findings] == [
        ("TaskCount", "ok"),
        ("HeapCrossCheck", "fail"),
    ]
    assert findings[1].detail == "free list disagrees"
    assert findings[0].scope == "system"


def test_diagnostics_report_degrades_to_one_skipped_finding(monkeypatch):
    """An unreadable target must not fail the verb that reports on it."""

    def explode(_layout):
        raise TARGET_ACCESS_ERRORS[0]("cannot access memory")

    monkeypatch.setattr(adapter_module, "system_checks", explode)

    findings = _adapter().diagnostics_report()

    assert len(findings) == 1
    assert findings[0].name == "SystemChecks"
    assert findings[0].verdict == "skipped"
    assert findings[0].detail
