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
    assert "Heap allocator: unavailable" in system
    assert "Heap status:" not in system

    task_value = gdb_session.run('p $gdr_task("gdr_ready").uxPriority')
    task_array = gdb_session.run("p $gdr_tasks()[0]")
    assert "4" in task_value
    assert "*" in task_array or "0x" in task_array


def test_freertos_state_column_is_accurate_on_fixture(gdb_session):
    """The State column reflects real list membership, not traversal origin.

    Regression lock for the pxContainer/pvContainer DWARF-spelling bug: the
    default backward-compat build names the list-item container member
    ``pvContainer``, and a layout that only reads ``pxContainer`` made every
    container read fail, which fell through to a bogus ``Deleted`` for every
    non-running task. IDLE runs on the kernel idle loop while the four app/
    daemon tasks block on vTaskDelay(1000) (fixture main.c), so the column
    must read exactly one Running + four Blocked with no phantom Deleted.
    """
    tasks = gdb_session.run("freertos tasks", timeout=20)

    assert "Deleted" not in tasks, tasks
    assert "IDLE *" in tasks and "IDLE *      Running" in tasks, tasks
    assert "gdr_low" in tasks and "gdr_low     Blocked" in tasks, tasks
    assert "gdr_ready   Blocked" in tasks, tasks
    assert "gdr_normal  Blocked" in tasks, tasks
    assert "Tmr Svc     Blocked" in tasks, tasks


def test_freertos_high_water_available_without_stack_end_field(gdb_session):
    """HighWater must be computed when pxEndOfStack is absent.

    The default downward-growing build has no pxEndOfStack member (the
    configRECORD_STACK_HIGH_ADDRESS gating is off), but the 0xa5 watermark
    fill is present under configUSE_TRACE_FACILITY. The scan reads the
    [pxStack, pxTopOfStack] window instead, so the HighWater column must show
    integer words (not ``unavailable`` / ``N/A``) for every task.
    """
    tasks = gdb_session.run("freertos tasks", timeout=20)

    assert "HighWater" in tasks, tasks
    # Every task's stack was prefilled with 0xa5, so no cell may degrade to
    # the unavailable marker; scan numeric HighWater cells across the rows.
    for line in tasks.splitlines():
        if not line.strip() or line.strip().startswith("---") or "HighWater" in line:
            continue
        assert "unavailable" not in line, line
        assert "N/A" not in line, line

    # The detail view must agree with the table: HighWater does not depend on
    # StackSize, which stays N/A here because pxEndOfStack is absent.
    detail = gdb_session.run("freertos task IDLE", timeout=20)
    assert "StackSize: N/A" in detail, detail
    high_water = [line for line in detail.splitlines() if "HighWater" in line]
    assert high_water, detail
    assert "unavailable" not in high_water[0], high_water


def test_freertos_printers_fold_typedef_spelled_values(gdb_session):
    """Folds must trigger on the kernel typedefs users actually type.

    ``pxCurrentTCB`` is a ``TCB_t *`` and ``pxReadyTasksLists`` is a
    ``List_t[]``; a value reached through those aliases reports
    ``gdb.Type.tag is None``, so matching the alias layer would leave every
    fold dead outside an explicit ``struct`` cast.
    """
    task = gdb_session.run("p *pxCurrentTCB", timeout=20)
    ready = gdb_session.run("p pxReadyTasksLists[0]", timeout=20)
    item = gdb_session.run("p pxCurrentTCB->xStateListItem", timeout=20)

    assert "Task(" in task, task
    assert 'name="IDLE"' in task, task
    assert "List(count=" in ready, ready
    assert "ListItem(" in item, item


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


def test_freertos_phase1_command_surface_is_reachable(gdb_session):
    """Every Phase 1 command word dispatches without guard/Python noise.

    Unit tests cover the dispatch table in isolation; this locks the real GDB
    command object, so a registration or routing regression (aliases, plural
    kinds with no data channel yet, singular detail, usage warnings) cannot
    pass on unit tests alone.
    """
    for command in (
        "freertos help",
        "freertos threads",
        "freertos objects",
        "freertos heap",
        "freertos queues",
        "freertos sems",
        "freertos task IDLE",
        "freertos task Tmr Svc",
        "freertos task no_such_task",
        "freertos bogus",
        "freertos tasks extra-arg",
    ):
        output = gdb_session.run(command, timeout=20)
        for marker in (
            "[gdr] error:",
            "Python Exception",
            "Traceback (most recent call last)",
        ):
            assert marker not in output, (command, output)

    # The alias resolves to the same table the canonical word renders.
    assert "IDLE" in gdb_session.run("freertos threads", timeout=20)
    # Unimplemented kinds say so instead of reporting a fabricated zero.
    assert "not reliably enumerable" in gdb_session.run("freertos queues", timeout=20)
    # A wrong word/arity lands on the usage line, not a traceback.
    assert "usage: freertos" in gdb_session.run("freertos bogus", timeout=20)
    assert "usage: freertos" in gdb_session.run("freertos tasks extra", timeout=20)
    # A name containing a space (the timer daemon) must reach its detail view.
    daemon = gdb_session.run("freertos task Tmr Svc", timeout=20)
    assert "Name: Tmr Svc" in daemon, daemon
    assert "usage: freertos" not in daemon, daemon
