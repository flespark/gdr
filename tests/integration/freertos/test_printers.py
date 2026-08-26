"""Pretty-printer folds for FreeRTOS kernel structs on a live fixture."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS") != "freertos",
    reason="requires the FreeRTOS QEMU profile",
)


def test_task_fold_contains_name_and_priority(gdb_session):
    """A TCB reached through the kernel typedef still folds as Task(...)."""
    output = gdb_session.run("p *pxCurrentTCB", timeout=20)
    assert "Task(" in output, output
    assert "name=" in output, output
    assert "current_priority=" in output, output


def test_queue_fold(gdb_session):
    """A queue handle folds as Queue(...) (or List when only the list is typed)."""
    output = gdb_session.run_python(
        """
import gdb
queue = gdb.parse_and_eval("gdr_registered_queue")
print(queue.dereference())
"""
    )
    assert "Queue(" in output or "List(" in output, output


def test_timer_fold(gdb_session):
    """An active timer folds as Timer(...)."""
    output = gdb_session.run_python(
        """
import gdb
timer = gdb.parse_and_eval("gdr_active_timer")
print(timer.dereference())
"""
    )
    assert "Timer(" in output, output
    assert "name=" in output, output


def test_list_fold(gdb_session):
    """pxReadyTasksLists[0] folds as List(count=...)."""
    output = gdb_session.run("p pxReadyTasksLists[0]", timeout=20)
    assert "List(count=" in output, output


def test_list_item_fold(gdb_session):
    """A TCB state-list item folds as ListItem(...)."""
    output = gdb_session.run("p pxCurrentTCB->xStateListItem", timeout=20)
    assert "ListItem(" in output, output
