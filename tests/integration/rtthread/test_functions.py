"""Real QEMU checks for stable RTOS-neutral convenience functions."""

from __future__ import annotations

import os

import pytest

from tests.support.rtthread_profiles import get_rtthread_test_profile

_VERSION = os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")
_TARGET = os.environ.get("GDR_QEMU_TARGET", "cortex-a9")
_PROFILE = get_rtthread_test_profile(_VERSION, _TARGET)
_DEFAULT_POINTER_BYTES = "8" if _TARGET == "rv64" else "4"
_EXPECTED_POINTER_BYTES = int(
    os.environ.get("GDR_EXPECT_POINTER_BYTES", _DEFAULT_POINTER_BYTES)
)

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS", "rtthread") != "rtthread",
    reason="requires an RT-Thread QEMU profile",
)


def test_gdr_task_returns_a_target_native_rtthread_value(gdb_session):
    output = gdb_session.run('p $gdr_task("worker1").current_priority')
    assert "20" in output


def test_gdr_task_exposes_the_target_name_field(gdb_session):
    output = gdb_session.run('p $gdr_task("worker1").name')
    assert "worker1" in output


def test_gdr_task_returns_zero_for_an_unknown_name(gdb_session):
    output = gdb_session.run('p $gdr_task("nonexistent")')
    assert "= 0" in output


def test_gdr_tasks_returns_every_target_native_task_pointer(gdb_session):
    output = gdb_session.run_python(
        """
import gdb

tasks = gdb.parse_and_eval("$gdr_tasks()")
lo, hi = tasks.type.range()
names = [tasks[i].dereference()["name"].string() for i in range(lo, hi + 1)]
print(f"is_array={tasks.type.strip_typedefs().code == gdb.TYPE_CODE_ARRAY}")
print(f"names={names}")
"""
    )
    assert "is_array=True" in output
    for name in ("worker1", "worker2", "worker3"):
        assert name in output


def test_gdr_values_preserve_target_struct_types_and_addresses(gdb_session):
    output = gdb_session.run_python(
        f'''
import gdb

thread = gdb.parse_and_eval('$gdr_task("worker1")')
semaphore = gdb.parse_and_eval(
    '$gdr_object("semaphore", "{_PROFILE.semaphore_name}")'
)
print(f"thread_tag={{thread.type.strip_typedefs().tag}}")
print(f"thread_is_struct={{thread.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT}}")
print(
    "thread_address_matches="
    f"{{int(thread.address) == int(gdb.parse_and_eval('worker1_thread').address)}}"
)
print(f"semaphore_tag={{semaphore.type.strip_typedefs().tag}}")
print(
    "semaphore_is_struct="
    f"{{semaphore.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT}}"
)
print(
    "semaphore_address_matches="
    f"{{int(semaphore.address) == int(gdb.parse_and_eval('test_sem').address)}}"
)
for kind, name, symbol, tag in (
    ("event", "{_PROFILE.event_name}", "test_event", "rt_event"),
    ("mailbox", "{_PROFILE.mailbox_name}", "test_mailbox", "rt_mailbox"),
    (
        "msgqueue",
        "{_PROFILE.msgqueue_name}",
        "test_msgqueue",
        "rt_messagequeue",
    ),
    ("mempool", "{_PROFILE.mempool_name}", "test_mempool", "rt_mempool"),
):
    value = gdb.parse_and_eval(f'$gdr_object("{{kind}}", "{{name}}")')
    print(f"{{kind}}_tag={{value.type.strip_typedefs().tag}}")
    print(
        f"{{kind}}_is_struct="
        f"{{value.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT}}"
    )
    print(
        f"{{kind}}_address_matches="
        f"{{int(value.address) == int(gdb.parse_and_eval(symbol).address)}}"
    )
'''
    )
    for expected in (
        "thread_tag=rt_thread",
        "thread_is_struct=True",
        "thread_address_matches=True",
        "semaphore_tag=rt_semaphore",
        "semaphore_is_struct=True",
        "semaphore_address_matches=True",
        "event_tag=rt_event",
        "event_is_struct=True",
        "event_address_matches=True",
        "mailbox_tag=rt_mailbox",
        "mailbox_is_struct=True",
        "mailbox_address_matches=True",
        "msgqueue_tag=rt_messagequeue",
        "msgqueue_is_struct=True",
        "msgqueue_address_matches=True",
        "mempool_tag=rt_mempool",
        "mempool_is_struct=True",
        "mempool_address_matches=True",
    ):
        assert expected in output, output


def test_current_task_matches_the_selected_cpu(gdb_session):
    output = gdb_session.run_python(
        f"""
import gdb
from rtthread.navigation import get_current_thread

expected = gdb.parse_and_eval({_PROFILE.current_thread_expression!r})
current = get_current_thread()
print(f"expected_non_null={{int(expected) != 0}}")
print(f"current_found={{current is not None}}")
print(
    "current_matches_selected_cpu="
    f"{{current is not None and int(current.address) == int(expected)}}"
)
"""
    )
    assert "expected_non_null=True" in output, output
    assert "current_found=True" in output, output
    assert "current_matches_selected_cpu=True" in output, output


def test_target_pointer_width_matches_the_qemu_profile(gdb_session):
    output = gdb_session.run("p sizeof(void *)")
    assert f"= {_EXPECTED_POINTER_BYTES}" in output, output


def test_arch_info_matches_the_connected_target(gdb_session):
    output = gdb_session.run_python(
        """
from gdr.gdb_bridge import get_arch_info

arch = get_arch_info()
print(f"arch_found={arch is not None}")
if arch is not None:
    print(f"ptrsize={arch.ptrsize}")
    print(f"endian={arch.endian}")
"""
    )
    assert "arch_found=True" in output, output
    assert f"ptrsize={_EXPECTED_POINTER_BYTES}" in output, output
    assert "endian=little" in output, output
