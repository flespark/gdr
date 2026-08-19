"""Closed-loop checks for the FreeRTOS B-L475E-IOT01A fixture."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS") != "freertos",
    reason="requires the FreeRTOS QEMU profile",
)


def test_freertos_kernel_types_are_visible_to_gdb(gdb_session):
    """The fixture retains DWARF for all three required kernel structures."""
    output = gdb_session.run_many(
        "ptype struct tskTaskControlBlock",
        "ptype struct QueueDefinition",
        "ptype struct tmrTimerControl",
    )

    assert "tskTaskControlBlock" in output
    assert "QueueDefinition" in output
    assert "tmrTimerControl" in output
    assert "No struct type named" not in output


def test_freertos_profile_uses_32_bit_pointers_and_persistent_gdb(
    gdb_session, qemu_profile
):
    """The ARM profile and persistent connection survive sequential commands."""
    pointer_output = gdb_session.run("p sizeof(void *)")
    expressions = gdb_session.run_many("p 1 + 1", "p 2 + 2")

    assert str(qemu_profile.pointer_width) in pointer_output
    assert "2" in expressions
    assert "4" in expressions


def test_freertos_tasks_and_system_commands_navigate_fixture(gdb_session):
    """Commands enumerate scheduler lists through DWARF ownership."""
    tasks = gdb_session.run("freertos tasks", timeout=20)
    system = gdb_session.run("freertos system", timeout=20)

    for name in ("IDLE", "Tmr Svc", "gdr_ready", "gdr_normal", "gdr_low"):
        assert name in tasks
    assert "IDLE *" in tasks
    assert "Kernel version: 10.3.1" in system
    assert "Task count: 5" in system
    assert "Scheduler state: running" in system
    assert "Ready: 1" in system
    assert "Delayed: 4" in system
    assert "Heap: unavailable" in system

    task_value = gdb_session.run('p $gdr_task("gdr_ready").uxPriority')
    task_array = gdb_session.run("p $gdr_tasks()[0]")
    assert "4" in task_value
    assert "*" in task_array or "0x" in task_array


def test_freertos_unknown_task_degrades_to_null(gdb_session):
    """A missing task name returns a null value without raw Python noise."""
    output = gdb_session.run('p $gdr_task("no_such_task")')
    assert "= 0" in output
    for marker in (
        "[gdr] error:",
        "Python Exception",
        "Traceback (most recent call last)",
    ):
        assert marker not in output, output


def test_freertos_commands_report_cleanly_without_failures(gdb_session):
    """Aggregate commands complete without Python or guard error noise."""
    for command in ("freertos tasks", "freertos system"):
        output = gdb_session.run(command, timeout=20)
        for marker in (
            "[gdr] error:",
            "Python Exception",
            "Traceback (most recent call last)",
        ):
            assert marker not in output, output
