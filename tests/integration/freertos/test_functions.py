"""Convenience-function checks against a live FreeRTOS QEMU fixture."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS") != "freertos",
    reason="requires the FreeRTOS QEMU profile",
)


def _assert_no_python_noise(output: str) -> None:
    for marker in (
        "[gdr] error:",
        "Python Exception",
        "Traceback (most recent call last)",
    ):
        assert marker not in output, output


def test_gdr_task_returns_native_gdb_value(gdb_session):
    """$gdr_task returns a TCB pointer whose DWARF tag is the kernel struct."""
    output = gdb_session.run_python(
        """
import gdb
task = gdb.parse_and_eval('$gdr_task("gdr_ready")')
print(f"tag={task.type.strip_typedefs().tag}")
print(f"code={task.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT}")
print(f"prio={int(task['uxPriority'])}")
"""
    )
    _assert_no_python_noise(output)
    assert "tag=tskTaskControlBlock" in output
    assert "code=True" in output
    assert "prio=4" in output


def test_gdr_tasks_returns_bounded_pointer_array(gdb_session):
    """$gdr_tasks is a finite pointer array covering every fixture task."""
    output = gdb_session.run_python(
        """
import gdb
tasks = gdb.parse_and_eval("$gdr_tasks()")
lo, hi = tasks.type.range()
print(f"is_array={tasks.type.strip_typedefs().code == gdb.TYPE_CODE_ARRAY}")
print(f"count={hi - lo + 1}")
names = []
for i in range(lo, hi + 1):
    names.append(tasks[i].dereference()["pcTaskName"].string())
print(f"names={names}")
"""
    )
    _assert_no_python_noise(output)
    assert "is_array=True" in output
    for name in ("IDLE", "Tmr Svc", "gdr_ready", "gdr_normal", "gdr_low"):
        assert name in output


def test_gdr_object_returns_null_for_missing(gdb_session):
    """A missing object degrades to a null value without Python noise."""
    output = gdb_session.run('p $gdr_task("no_such_task")')
    assert "= 0" in output
    _assert_no_python_noise(output)


def test_gdr_object_queue_by_name_and_address(gdb_session):
    """$gdr_object resolves a queue by symbol name and by 0x address alike.

    Both forms must yield the same native QueueDefinition value: the name
    form goes through the discovery channels, the address form through
    resolve_object's hex parsing, and the final cast is the same.
    """
    output = gdb_session.run_python(
        """
import gdb
by_name = gdb.parse_and_eval('$gdr_object("queue", "gdr_empty_queue")')
handle = int(gdb.parse_and_eval('gdr_empty_queue'))
by_addr = gdb.parse_and_eval(f'$gdr_object("queue", "{hex(handle)}")')
print(f"tag={by_name.type.strip_typedefs().tag}")
print(f"same={int(by_name.address) == int(by_addr.address)}")
print(f"length={int(by_name['uxLength'])}")
"""
    )
    _assert_no_python_noise(output)
    assert "tag=QueueDefinition" in output
    assert "same=True" in output
    assert "length=2" in output
