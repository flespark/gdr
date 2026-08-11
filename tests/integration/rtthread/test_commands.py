"""Real QEMU checks for the RT-Thread command tree."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS", "rtthread") != "rtthread",
    reason="requires an RT-Thread QEMU profile",
)

_PUBLIC_COMMANDS = (
    "threads",
    "semaphores",
    "mutexes",
    "events",
    "mailboxs",
    "messagequeues",
    "mempools",
    "timers",
    "system",
)


@pytest.fixture(scope="module")
def rtt_outputs(gdb):
    """Execute every public command once for availability and detail checks."""
    return {
        command: gdb.run(f"rtt {command}", timeout=20) for command in _PUBLIC_COMMANDS
    }


def test_all_rtt_commands_are_available(rtt_outputs):
    """Every public route completes without usage or Python error output."""
    for command, output in rtt_outputs.items():
        assert "usage: rtthread" not in output, f"rtt {command}:\n{output}"
        assert "[gdr] error:" not in output, f"rtt {command}:\n{output}"
        assert "Traceback (most recent call last)" not in output, (
            f"rtt {command}:\n{output}"
        )
        assert "Python Exception" not in output, f"rtt {command}:\n{output}"


def test_rtt_threads_render_normalized_fixture_tasks(gdb, rtt_outputs):
    """Task rows preserve fixture data, normalized columns, and known states."""
    converted = gdb.run_python(
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
    output = rtt_outputs["threads"]

    for name in ("worker1", "worker2", "worker3"):
        assert name in output
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
    assert "<worker1_entry" in output

    worker_row = next(
        line for line in output.splitlines() if line.lstrip().startswith("worker1")
    )
    fields = worker_row.split()
    assert stack_used in fields
    assert high_water in fields

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
    assert all(
        any(part.rstrip("*").lower() in valid for part in parts) for parts in rows
    ), rows


def test_rtt_system_produces_a_normalized_summary(rtt_outputs):
    """The system command exposes task, tick, object, state, and heap data."""
    output = rtt_outputs["system"]
    for label in (
        "Kernel version:",
        "Task count:",
        "Tick count:",
        "semaphore:",
        "timer:",
        "Heap:",
    ):
        assert label in output


def test_rtt_semaphore_command_lists_fixture_data(rtt_outputs):
    """The semaphore command retains its fixture row and normalized columns."""
    output = rtt_outputs["semaphores"]
    assert "test_sem" in output
    assert "Value" in output
    assert "Addr" in output


def test_rtt_timer_command_lists_fixture_data(rtt_outputs):
    """The timer command retains timing, mode, and symbolized callback data."""
    output = rtt_outputs["timers"]
    assert "test_timer" in output
    assert "Kernel tick:" in output
    assert "Callback" in output
    timer_row = next(line for line in output.splitlines() if "test_timer" in line)
    assert "periodic" in timer_row.lower()
    assert "soft" in timer_row.lower()
    assert "<test_timer_timeout" in timer_row
