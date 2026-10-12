"""FreeRTOS command output before task creation and after scheduler startup."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession, check_tools

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos" or SPEC.variant != "full" or SPEC.target != "mps2-an385",
    reason="requires the single-core FreeRTOS full fixture",
)


def test_commands_before_task_creation_and_after_scheduler_start(qemu_profile):
    """Empty tasks are legitimate before creation; timers must still print.

    Reason: xQueueAddToSet is the executable location immediately after the
    reported main.c:866 stop. Symbol breakpoints avoid coupling to line numbers
    that move when the shared fixture changes.
    """
    check_tools(qemu_profile, SPEC.gdb)
    profile = replace(qemu_profile, extra_env={"GDR_RTOS": "", "GDR_VERSION": ""})
    root = Path(__file__).resolve().parents[3]
    target = QemuSession(profile)
    session = GdbSession(SPEC.gdb, profile, target.gdb_port, root)
    try:
        target.start(SPEC.boot_wait, paused=True)
        session.start()
        session.run("tbreak xQueueAddToSet")
        stopped = session.run("continue", timeout=30)
        assert "Temporary breakpoint" in stopped, stopped
        state = session.run_python(
            """
import gdb
assert int(gdb.parse_and_eval('uxCurrentNumberOfTasks')) == 0
assert int(gdb.parse_and_eval('xSchedulerRunning')) == 0
print('PRE_TASK_CREATION')
"""
        )
        assert "PRE_TASK_CREATION" in state, state
        tasks = session.run("frt tasks")
        assert "(empty)" in tasks, tasks
        timers = session.run("frt timers", timeout=25)
        assert "gdr_active" in timers and "dormant" in timers, timers
        assert "[gdr] error:" not in timers, timers

        # Reason: the reported main.c:1020 stop is just before this entry;
        # task lists exist but next-unblock cache still has its static zero.
        session.run("tbreak vTaskStartScheduler")
        stopped = session.run("continue", timeout=30)
        assert "Temporary breakpoint" in stopped, stopped
        system = session.run("frt system", timeout=25)
        assert "Check[NextUnblockTime]" not in system, system
        assert "Checks:" not in system, system
        checks = session.run("frt checks", timeout=25)
        assert "scheduler not started" in checks, checks
        watermark = session.run_python(
            """
import gdb
from freertos.adapter import value_to_task
from freertos.navigation import iter_tasks
from gdr.adapter_api import active
from gdr.gdb_bridge import eval_identifier
layout = active().layout
assert int(eval_identifier('xSchedulerRunning')) == 0
watermarks = {}
for value, state, core in iter_tasks(layout):
    task = value_to_task(value, state, core, layout)
    assert task.runtime_counter == 0, task
    assert task.stack_end > task.stack_base, task
    raw = bytes(gdb.selected_inferior().read_memory(
        task.stack_base, task.stack_end - task.stack_base + layout.config.stack_word_bytes))
    first_written = next((i for i, byte in enumerate(raw) if byte != 0xa5), len(raw))
    fill_used = len(raw) - first_written // layout.config.stack_word_bytes * layout.config.stack_word_bytes
    sp_used = task.stack_end + layout.config.stack_word_bytes - task.top_of_stack
    expected = max(fill_used, sp_used)
    assert task.stack_size == len(raw), task
    assert task.stack_used == sp_used, task
    assert task.high_water_mark == expected, (task, expected)
    assert task.high_water_mark >= task.stack_used, task
    watermarks[task.name] = expected
assert watermarks['gdr_ready'] == watermarks['gdr_low'] > 0, watermarks
assert watermarks['gdr_exh'] == watermarks['gdr_low'], watermarks
print('INITIAL_WATERMARKS_MATCH_MEMORY')
"""
        )
        assert "INITIAL_WATERMARKS_MATCH_MEMORY" in watermark, watermark
        queue = session.run("frt queue gdr_full_queue", timeout=25)
        assert "Item[0]: 0x" in queue, queue
        assert "01 00 00 00" not in queue, queue
        assert "Checks" not in queue, queue

        session.run("tbreak gdr_semihosting_write0")
        stopped = session.run("continue", timeout=30)
        assert "Temporary breakpoint" in stopped, stopped
        tasks = session.run("frt tasks", timeout=25)
        assert "gdr_ready" in tasks and "Tmr Svc" in tasks, tasks
        assert "(empty)" not in tasks and "[gdr] error:" not in tasks, tasks
        timers = session.run("frt timers", timeout=25)
        active_row = next(
            line for line in timers.splitlines() if line.startswith("gdr_active ")
        )
        assert active_row.split()[1] == "active", timers
        assert "[gdr] error:" not in timers, timers
    finally:
        session.stop()
        target.stop()
