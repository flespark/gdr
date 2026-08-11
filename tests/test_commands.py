"""Real QEMU checks for the RT-Thread command tree."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS", "rtthread") != "rtthread",
    reason="requires an RT-Thread QEMU profile",
)


def test_rtt_threads_lists_rtthread_fixture_tasks(gdb_session):
    """The RT-Thread command tree lists all fixture tasks."""
    output = gdb_session.run("rtt threads")
    for name in ("worker1", "worker2", "worker3"):
        assert name in output
    for header in ("Name", "State", "Prio", "Stack", "Used", "HighWater"):
        assert header in output
    assert "<worker1_entry" in output


def test_rtt_threads_has_complete_table_headers(gdb_session):
    """Task output keeps every normalized presentation column."""
    output = gdb_session.run("rtt threads")
    for header in (
        "Name",
        "State",
        "Prio",
        "SP",
        "Stack",
        "Used",
        "HighWater",
        "Entry",
    ):
        assert header in output


def test_rtt_thread_stack_columns_match_adapter_conversion(gdb_session):
    """Rendered stack values match the layout-aware RT-Thread converter."""
    converted = gdb_session.run_python(
        """
import gdb
from gdr.registry import active
from rtthread import adapter

thread = gdb.parse_and_eval('$gdr_task("worker1")')
selected = active()
assert isinstance(selected, adapter.RtThreadAdapter)
value = adapter.value_to_thread(thread, selected.layout)
print(f"stack_used={value.stack_used}")
print(f"max_stack_used={value.max_stack_used}")
"""
    )
    stack_used = next(
        line.split("=", 1)[1]
        for line in converted.splitlines()
        if line.startswith("stack_used=")
    )
    high_water = next(
        line.split("=", 1)[1]
        for line in converted.splitlines()
        if line.startswith("max_stack_used=")
    )
    output = gdb_session.run("rtt threads")
    worker_row = next(
        line for line in output.splitlines() if line.lstrip().startswith("worker1")
    )
    fields = worker_row.split()
    assert stack_used in fields
    assert high_water in fields


def test_rtt_thread_states_are_known(gdb_session):
    """Task rows only expose recognized RT-Thread state names."""
    output = gdb_session.run("rtt threads")
    valid = {"suspend", "ready", "running", "init", "close", "unknown"}
    lines = output.splitlines()
    separator = next(
        i for i, line in enumerate(lines) if line.lstrip().startswith("---")
    )
    rows = []
    for line in lines[separator + 1 :]:
        if not line.strip() or line.startswith("["):
            break
        parts = line.split()
        if parts:
            rows.append(parts)
    assert rows, output
    for parts in rows:
        assert any(part.rstrip("*").lower() in valid for part in parts), parts


def test_rtt_system_produces_a_normalized_summary(gdb_session):
    """The RT-Thread command exposes task, tick, state, and heap data."""
    output = gdb_session.run("rtt system")
    for label in (
        "Kernel version:",
        "Task count:",
        "Tick count:",
        "semaphore:",
        "Heap:",
    ):
        assert label in output


def test_rtt_system_reports_kernel_tick(gdb_session):
    """System output includes the live kernel tick."""
    assert "Tick count:" in gdb_session.run("rtt system")


def test_rtt_system_reports_object_counts(gdb_session):
    """System output retains object counts from the RT-Thread registry."""
    output = gdb_session.run("rtt system")
    assert "semaphore:" in output
    assert "timer:" in output


def test_rtt_semaphore_command_lists_fixture_semaphore(gdb_session):
    """The RT-Thread command tree lists semaphore data."""
    output = gdb_session.run("rtt semaphores")
    assert "test_sem" in output
    assert "Value" in output


def test_rtt_semaphore_command_has_address_column(gdb_session):
    """Semaphore output retains the target address column."""
    assert "Addr" in gdb_session.run("rtt semaphores")


def test_rtt_timer_table_lists_fixture_timers(gdb_session):
    """The RT-Thread command tree preserves timer-table capability."""
    output = gdb_session.run("rtt timers")
    assert "test_timer" in output
    assert "Kernel tick:" in output
    assert "Callback" in output


def test_rtt_timer_table_reports_kernel_tick(gdb_session):
    """Timer output includes the tick snapshot used to interpret deadlines."""
    assert "Kernel tick:" in gdb_session.run("rtt timers")


def test_rtt_timer_fixture_is_periodic_soft(gdb_session):
    """The fixture timer retains its periodic software-timer mode."""
    output = gdb_session.run("rtt timers")
    timer_row = next(line for line in output.splitlines() if "test_timer" in line)
    assert "periodic" in timer_row.lower()
    assert "soft" in timer_row.lower()


def test_rtt_timer_callback_is_symbolized(gdb_session):
    """Timer callback addresses resolve through target debug symbols."""
    assert "<test_timer_timeout" in gdb_session.run("rtt timers")
