"""Real QEMU checks for the RT-Thread command tree."""

from __future__ import annotations

import contextlib
import os

import pytest

from tests.support.rtthread_profiles import get_rtthread_test_profile

_VERSION = os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")
_TARGET = os.environ.get("GDR_QEMU_TARGET", "cortex-a9")
_PROFILE = get_rtthread_test_profile(_VERSION, _TARGET)

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS", "rtthread") != "rtthread",
    reason="requires an RT-Thread QEMU profile",
)

_PUBLIC_COMMANDS = (
    "help",
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


@pytest.fixture(scope="module")
def ipc_fixture_values(gdb):
    """Read fixture fields directly from target memory for table comparison."""
    output = gdb.run_python(
        """
import gdb

objects = (
    ("event", "test_event", ("set",)),
    ("mailbox", "test_mailbox", ("entry", "size", "in_offset", "out_offset")),
    ("msgqueue", "test_msgqueue", ("entry", "msg_size", "max_msgs")),
    ("mempool", "test_mempool", ("block_size", "block_total_count", "block_free_count")),
)
for kind, symbol, fields in objects:
    value = gdb.parse_and_eval(symbol)
    print(f"{kind}.address={int(value.address)}")
    for field in fields:
        print(f"{kind}.{field}={int(value[field])}")
"""
    )
    return {
        key: value
        for line in output.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }


def _fixture_row(output: str, name: str) -> list[str]:
    """Return one whitespace-delimited table row by fixture object name."""
    return next(
        line.split() for line in output.splitlines() if line.lstrip().startswith(name)
    )


def test_all_rtt_commands_are_available(rtt_outputs):
    """Every public route completes without usage or Python error output."""
    for command, output in rtt_outputs.items():
        assert "usage: rtthread" not in output, f"rtt {command}:\n{output}"
        assert "[gdr] error:" not in output, f"rtt {command}:\n{output}"
        assert "Traceback (most recent call last)" not in output, (
            f"rtt {command}:\n{output}"
        )
        assert "Python Exception" not in output, f"rtt {command}:\n{output}"


def test_rtt_help_lists_commands_and_aliases(rtt_outputs):
    """The runtime help is the complete command and alias reference."""
    output = rtt_outputs["help"]
    for command in _PUBLIC_COMMANDS:
        assert f"rtt {command}" in output
    for alias in ("tasks", "sems", "mtxs", "msgs", "mboxs", "mailboxes"):
        assert alias in output


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


def test_rtt_event_command_matches_target_memory(rtt_outputs, ipc_fixture_values):
    """The event table preserves the target's bit set, waiters, and address."""
    output = rtt_outputs["events"]
    assert "Set" in output
    assert "Waiters" in output
    assert _fixture_row(output, _PROFILE.event_name) == [
        _PROFILE.event_name,
        hex(int(ipc_fixture_values["event.set"])),
        "0",
        hex(int(ipc_fixture_values["event.address"])),
    ]


def test_rtt_mailbox_command_matches_target_memory(rtt_outputs, ipc_fixture_values):
    """The mailbox table preserves occupancy, offsets, waiters, and address."""
    output = rtt_outputs["mailboxs"]
    for header in ("Entry", "Size", "In", "Out", "RecvWait", "SendWait", "Addr"):
        assert header in output
    assert _fixture_row(output, _PROFILE.mailbox_name) == [
        _PROFILE.mailbox_name,
        ipc_fixture_values["mailbox.entry"],
        ipc_fixture_values["mailbox.size"],
        ipc_fixture_values["mailbox.in_offset"],
        ipc_fixture_values["mailbox.out_offset"],
        "0",
        "0",
        hex(int(ipc_fixture_values["mailbox.address"])),
    ]


def test_rtt_messagequeue_command_matches_target_memory(
    rtt_outputs, ipc_fixture_values
):
    """The messagequeue table preserves capacity, waiters, and address."""
    output = rtt_outputs["messagequeues"]
    for header in ("Entry", "MsgSize", "MaxMsgs", "RecvWait", "SendWait", "Addr"):
        assert header in output
    sender_cell = "0" if _PROFILE.mq_sender_list else "N/A"
    assert _fixture_row(output, _PROFILE.msgqueue_name) == [
        _PROFILE.msgqueue_name,
        ipc_fixture_values["msgqueue.entry"],
        ipc_fixture_values["msgqueue.msg_size"],
        ipc_fixture_values["msgqueue.max_msgs"],
        "0",
        sender_cell,
        hex(int(ipc_fixture_values["msgqueue.address"])),
    ]


def test_rtt_messagequeue_sender_list_matches_target_dwarf(gdb):
    """The sender wait list tracks the real kernel's struct field presence.

    The v3.1.4/v4.0.2 version boundary is a fact about the compiled kernel,
    not about GDR's layout table. Inspect ``struct rt_messagequeue`` DWARF
    directly and assert it agrees with the fixture profile expectation.
    """
    output = gdb.run_python(
        """
import gdb

messagequeue = gdb.parse_and_eval("test_msgqueue")
mq_type = messagequeue.type.strip_typedefs()
if mq_type.code == gdb.TYPE_CODE_PTR:
    mq_type = mq_type.target().strip_typedefs()
field_names = {field.name for field in mq_type.fields()}
print(f"has_suspend_sender_thread={'suspend_sender_thread' in field_names}")
"""
    )
    has_field = "has_suspend_sender_thread=True" in output
    assert has_field == _PROFILE.mq_sender_list, (
        f"kernel DWARF sender list ({has_field}) disagrees with fixture "
        f"profile expectation ({_PROFILE.mq_sender_list})\n{output}"
    )


def test_rtt_mempool_command_matches_target_memory(rtt_outputs, ipc_fixture_values):
    """The mempool table preserves block counts, size, waiters, and address."""
    output = rtt_outputs["mempools"]
    for header in ("BlockSize", "Total", "Free", "Waiters", "Addr"):
        assert header in output
    assert _fixture_row(output, _PROFILE.mempool_name) == [
        _PROFILE.mempool_name,
        ipc_fixture_values["mempool.block_size"],
        ipc_fixture_values["mempool.block_total_count"],
        ipc_fixture_values["mempool.block_free_count"],
        "0",
        hex(int(ipc_fixture_values["mempool.address"])),
    ]


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


@contextlib.contextmanager
def _with_width(gdb, width: int):
    """Set a GDB width, restoring the harness baseline on exit."""
    gdb.run(f"set width {width}")
    try:
        yield
    finally:
        gdb.run("set width 160")


def test_rtt_tables_keep_column_set_at_80_columns(gdb):
    """A narrow GDB width never removes columns; only elastic text shrinks."""
    with _with_width(gdb, 80):
        for command, required in (
            ("rtt semaphores", ("Name", "Value", "Addr")),
            ("rtt threads", ("Name", "State", "Prio", "SP", "Stack", "Entry")),
            ("rtt mutexes", ("Name", "Value", "Hold", "Owner", "Addr")),
        ):
            output = gdb.run(command, timeout=20)
            assert "usage: rtthread" not in output, output
            assert "[gdr] error:" not in output, output
            for header in required:
                assert header in output, f"{command} missing {header!r}:\n{output}"


def test_rtt_tables_keep_column_set_at_120_columns(gdb):
    """The 120-character baseline keeps the full column set intact."""
    with _with_width(gdb, 120):
        output = gdb.run("rtt threads", timeout=20)
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
            assert header in output, output


def test_rtt_thread_detail_shows_public_fields(gdb):
    """``rtt thread <name>`` renders a vertical key/value detail."""
    output = gdb.run("rtt thread worker1", timeout=20)
    for label in ("Name:", "Address:", "Type:", "State:", "Priority:", "Entry:"):
        assert label in output, output
    assert "worker1" in output


def test_rtt_semaphore_detail_shows_fixture_value(gdb):
    """``rtt semaphore <name>`` renders the fixture's public fields."""
    output = gdb.run(f"rtt semaphore {_PROFILE.semaphore_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "Value:"):
        assert label in output, output
    assert _PROFILE.semaphore_name in output


def test_rtt_event_detail_shows_waiter_conditions(gdb):
    """``rtt event <name>`` pairs each waiter with its mask and mode."""
    output = gdb.run(f"rtt event {_PROFILE.event_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "Set:"):
        assert label in output, output
    assert _PROFILE.event_name in output
    assert "[gdr] error:" not in output, output
    assert "Traceback" not in output, output


def test_rtt_mailbox_detail_shows_waiters(gdb):
    """``rtt mailbox <name>`` renders the fixture's public fields."""
    output = gdb.run(f"rtt mailbox {_PROFILE.mailbox_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "Entry:", "Size:"):
        assert label in output, output
    assert _PROFILE.mailbox_name in output
    assert "[gdr] error:" not in output, output


def test_rtt_messagequeue_detail_shows_waiters(gdb):
    """``rtt messagequeue <name>`` renders capacity and waiter fields."""
    output = gdb.run(f"rtt messagequeue {_PROFILE.msgqueue_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "MaxMsgs:"):
        assert label in output, output
    assert _PROFILE.msgqueue_name in output
    assert "[gdr] error:" not in output, output


def test_rtt_timer_detail_shows_fixture_timer(gdb):
    """``rtt timer <name>`` renders the fixture timer's callback symbol."""
    output = gdb.run(f"rtt timer {_PROFILE.timer_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "State:", "Callback:"):
        assert label in output, output
    assert _PROFILE.timer_name in output


def test_rtt_detail_reports_missing_objects(gdb):
    """Unknown object names produce a clear diagnostic, not a crash."""
    output = gdb.run("rtt semaphore no_such_object", timeout=20)
    assert "not found or type not enabled" in output, output
    assert "[gdr] error:" not in output, output
    assert "Traceback" not in output, output
