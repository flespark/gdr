"""Real QEMU checks for the RT-Thread command tree.

COUPLED: fixture object names, waiter relationships, capacities, timer mode and
callback symbols are supplied by the matching patch under
``ci/rt-thread/patches/<target>/<version>/``. Keep changes to those patches and
these assertions synchronized; layout/diagnostic behavior that is independent
of the fixture belongs in unit tests instead.
"""

from __future__ import annotations

import contextlib
import os
import re

import pytest

from tests.support.rtthread_profiles import get_rtthread_test_profile

_VERSION = os.environ.get(
    "GDR_VERSION", os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")
)
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
    "heap",
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


def _assert_clean_command_output(output: str) -> None:
    """Reject GDB/Python failures that can leave partial command output."""
    for marker in (
        "usage: rtthread",
        "[gdr] error:",
        "Traceback (most recent call last)",
        "Python Exception",
    ):
        assert marker not in output, output


def _detail_pairs(output: str) -> dict[str, str]:
    """Parse stable ``Key: Value`` detail output without table assumptions."""
    return {
        key.strip(): value
        for line in output.splitlines()
        if ": " in line
        for key, value in (line.split(": ", 1),)
    }


def test_all_rtt_commands_are_available(rtt_outputs):
    """Every public route completes without usage or Python error output."""
    for output in rtt_outputs.values():
        _assert_clean_command_output(output)


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
from gdr.adapter_api import active
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
        "BasePrio",
        "SP",
        "Stack",
        "Used",
        "HighWater",
        "Entry",
        "Addr",
    ):
        assert header in output
    assert "<worker1_entry" in output

    worker_row = next(
        line
        for line in output.splitlines()
        if line.lstrip().startswith("worker1 ") or line.lstrip().startswith("worker1*")
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
        "Heap allocator:",
    ):
        assert label in output

    # QEMU fixtures are small_mem-as-heap: the heap line names the probed
    # allocator plus an integer used/total pair and rejects ``unavailable``.
    heap_line = next(
        line.strip() for line in output.splitlines() if "Heap allocator:" in line
    )
    heap = heap_line.partition("Heap allocator:")[2].strip()
    assert "small_mem" in heap, heap_line
    assert "unavailable" not in heap, heap_line
    fields = dict(
        token.strip().split(": ", 1) for token in heap.split(",") if ": " in token
    )
    used = fields.get("used")
    total = fields.get("total")
    assert used is not None and total is not None, heap_line
    assert used.isdecimal() and total.isdecimal(), heap_line
    assert int(used) <= int(total), heap_line


def test_rtt_heap_reports_system_heap_detail(rtt_outputs):
    """``rtt heap`` shows basics, a bounded walk, holes, and occupancy."""
    output = rtt_outputs["heap"]
    for label in (
        "Algorithm:",
        "TotalSize:",
        "UsedSize:",
        "MaxUsed:",
        "MemTrace:",
        "Blocks:",
        "Holes:",
    ):
        assert label in output, output
    pairs = _detail_pairs(output)
    assert pairs["Algorithm"] == "small_mem", output
    assert pairs["TotalSize"].isdecimal(), output
    assert pairs["UsedSize"].isdecimal(), output
    assert pairs["MaxUsed"].isdecimal() or pairs["MaxUsed"] == "N/A", output

    # Blocks reports a used/free/total summary from the halted block chain.
    blocks = pairs["Blocks"]
    assert re.match(r"\d+ used, \d+ free, \d+ total", blocks), output

    # Holes follows the exact free-block line shape.
    holes = pairs["Holes"]
    assert holes == "0 free" or re.match(r"\d+ free, largest: \d+, smallest:", holes), (
        output
    )

    # MEMTRACE is enabled on the QEMU fixtures; occupancy is either the N/A
    # degrade line or a per-thread table that lists allocating thread names.
    if "Thread occupancy: N/A" in output:
        assert "Thread occupancy: N/A" in pairs, output
    else:
        assert "Thread occupancy" in pairs, output
        for header in ("Thread", "Blocks", "Bytes"):
            assert header in output, output


def test_rtt_semaphore_command_lists_fixture_data(rtt_outputs):
    """The semaphore command retains its fixture row and normalized columns."""
    output = rtt_outputs["semaphores"]
    assert "test_sem" in output
    assert "Value" in output
    assert "Policy" in output
    assert "Addr" in output


def test_rtt_semaphore_waiter_summary(rtt_outputs):
    """An empty semaphore shows its waiting thread by name."""
    output = rtt_outputs["semaphores"]
    row = _fixture_row(output, _PROFILE.wait_semaphore_name)
    assert row[0] == _PROFILE.wait_semaphore_name
    assert row[1] == "0"
    assert row[3] == f"1@{_PROFILE.sem_waiter_thread}"


def test_rtt_mutex_waiter_and_owner(rtt_outputs):
    """A held mutex shows its locker owner and its waiting thread."""
    output = rtt_outputs["mutexes"]
    row = _fixture_row(output, _PROFILE.wait_mutex_name)
    assert row[0] == _PROFILE.wait_mutex_name
    assert row[4] == _PROFILE.locker_thread_name
    assert row[6] == f"1@{_PROFILE.mutex_waiter_thread}"


def test_rtt_event_waiter_summary(rtt_outputs):
    """An event with an unsatisfied waiter lists that thread by name."""
    output = rtt_outputs["events"]
    row = _fixture_row(output, _PROFILE.wait_event_name)
    assert row[0] == _PROFILE.wait_event_name
    assert row[3] == f"1@{_PROFILE.event_waiter_thread}"


def test_rtt_mailbox_receiver_and_sender_waiters(rtt_outputs):
    """Empty and full mailboxes show their receiver and sender waiters."""
    output = rtt_outputs["mailboxs"]
    recv_row = _fixture_row(output, _PROFILE.wait_mailbox_recv_name)
    assert recv_row[0] == _PROFILE.wait_mailbox_recv_name
    assert recv_row[7] == f"1@{_PROFILE.mailbox_recv_thread}"
    assert recv_row[8] == "0"

    send_row = _fixture_row(output, _PROFILE.wait_mailbox_send_name)
    assert send_row[0] == _PROFILE.wait_mailbox_send_name
    assert send_row[7] == "0"
    assert send_row[8] == f"1@{_PROFILE.mailbox_send_thread}"


def test_rtt_messagequeue_receiver_and_sender_waiters(rtt_outputs):
    """Empty and full message queues show receiver and sender waiters."""
    output = rtt_outputs["messagequeues"]
    recv_row = _fixture_row(output, _PROFILE.wait_msgqueue_recv_name)
    assert recv_row[0] == _PROFILE.wait_msgqueue_recv_name
    assert recv_row[6] == f"1@{_PROFILE.msgqueue_recv_thread}"

    if _PROFILE.mq_sender_list:
        send_row = _fixture_row(output, _PROFILE.wait_msgqueue_send_name)
        assert send_row[0] == _PROFILE.wait_msgqueue_send_name
        assert send_row[7] == f"1@{_PROFILE.msgqueue_send_thread}"


def test_rtt_mempool_waiter_summary(rtt_outputs):
    """An exhausted memory pool lists its alloc-waiting thread."""
    output = rtt_outputs["mempools"]
    row = _fixture_row(output, _PROFILE.wait_mempool_name)
    assert row[0] == _PROFILE.wait_mempool_name
    assert row[5] == f"1@{_PROFILE.mempool_waiter_thread}"


def test_rtt_event_command_matches_target_memory(rtt_outputs, ipc_fixture_values):
    """The event table preserves the target's bit set, waiters, and address."""
    output = rtt_outputs["events"]
    assert "Set" in output
    assert "Policy" in output
    assert "Waiters" in output
    assert _fixture_row(output, _PROFILE.event_name) == [
        _PROFILE.event_name,
        hex(int(ipc_fixture_values["event.set"])),
        "FIFO",
        "0",
        hex(int(ipc_fixture_values["event.address"])),
    ]


def test_rtt_mailbox_command_matches_target_memory(rtt_outputs, ipc_fixture_values):
    """The mailbox table preserves occupancy, offsets, waiters, and address."""
    output = rtt_outputs["mailboxs"]
    for header in (
        "Entry",
        "Size",
        "Free",
        "In",
        "Out",
        "Policy",
        "RecvWait",
        "SendWait",
        "Addr",
    ):
        assert header in output
    mailbox_size = int(ipc_fixture_values["mailbox.size"])
    mailbox_entry = int(ipc_fixture_values["mailbox.entry"])
    assert _fixture_row(output, _PROFILE.mailbox_name) == [
        _PROFILE.mailbox_name,
        ipc_fixture_values["mailbox.entry"],
        ipc_fixture_values["mailbox.size"],
        str(max(mailbox_size - mailbox_entry, 0)),
        ipc_fixture_values["mailbox.in_offset"],
        ipc_fixture_values["mailbox.out_offset"],
        "FIFO",
        "0",
        "0",
        hex(int(ipc_fixture_values["mailbox.address"])),
    ]


def test_rtt_messagequeue_command_matches_target_memory(
    rtt_outputs, ipc_fixture_values
):
    """The messagequeue table preserves capacity, waiters, and address."""
    output = rtt_outputs["messagequeues"]
    for header in (
        "Entry",
        "MsgSize",
        "MaxMsgs",
        "Free",
        "Policy",
        "RecvWait",
        "SendWait",
        "Addr",
    ):
        assert header in output
    sender_cell = "0" if _PROFILE.mq_sender_list else "N/A"
    max_msgs = int(ipc_fixture_values["msgqueue.max_msgs"])
    entry = int(ipc_fixture_values["msgqueue.entry"])
    assert _fixture_row(output, _PROFILE.msgqueue_name) == [
        _PROFILE.msgqueue_name,
        ipc_fixture_values["msgqueue.entry"],
        ipc_fixture_values["msgqueue.msg_size"],
        ipc_fixture_values["msgqueue.max_msgs"],
        str(max(max_msgs - entry, 0)),
        "FIFO",
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
    for header in ("BlockSize", "Total", "Free", "Used", "Waiters", "Addr"):
        assert header in output
    total = int(ipc_fixture_values["mempool.block_total_count"])
    free = int(ipc_fixture_values["mempool.block_free_count"])
    assert _fixture_row(output, _PROFILE.mempool_name) == [
        _PROFILE.mempool_name,
        ipc_fixture_values["mempool.block_size"],
        ipc_fixture_values["mempool.block_total_count"],
        ipc_fixture_values["mempool.block_free_count"],
        str(max(total - free, 0)),
        "0",
        hex(int(ipc_fixture_values["mempool.address"])),
    ]


def test_rtt_timer_command_lists_fixture_data(rtt_outputs):
    """The timer command retains timing, mode, and symbolized callback data."""
    output = rtt_outputs["timers"]
    assert "test_timer" in output
    assert "Kernel tick:" in output
    assert "Callback" in output
    assert "ExpiresIn" in output
    assert "Addr" in output
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
            ("rtt semaphores", ("Name", "Value", "Policy", "Waiters", "Addr")),
            (
                "rtt threads",
                ("Name", "State", "Prio", "BasePrio", "SP", "Stack", "Entry", "Addr"),
            ),
            (
                "rtt mutexes",
                (
                    "Name",
                    "Value",
                    "Hold",
                    "OrigPrio",
                    "Owner",
                    "Policy",
                    "Waiters",
                    "Addr",
                ),
            ),
            ("rtt events", ("Name", "Set", "Policy", "Waiters", "Addr")),
            (
                "rtt mailboxs",
                (
                    "Name",
                    "Entry",
                    "Size",
                    "Free",
                    "In",
                    "Out",
                    "Policy",
                    "RecvWait",
                    "SendWait",
                    "Addr",
                ),
            ),
            (
                "rtt messagequeues",
                (
                    "Name",
                    "Entry",
                    "MsgSize",
                    "MaxMsgs",
                    "Free",
                    "Policy",
                    "RecvWait",
                    "SendWait",
                    "Addr",
                ),
            ),
            (
                "rtt mempools",
                ("Name", "BlockSize", "Total", "Free", "Used", "Waiters", "Addr"),
            ),
            (
                "rtt timers",
                (
                    "Name",
                    "State",
                    "Mode",
                    "Type",
                    "InitTick",
                    "TimeoutTick",
                    "ExpiresIn",
                    "Callback",
                    "Addr",
                ),
            ),
        ):
            output = gdb.run(command, timeout=20)
            _assert_clean_command_output(output)
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
    for label in (
        "Name:",
        "Address:",
        "Type:",
        "State:",
        "Priority:",
        "Entry:",
        "Error:",
        "RemainingTick:",
    ):
        assert label in output, output
    assert "worker1" in output


def test_rtt_semaphore_detail_shows_fixture_value(gdb):
    """``rtt semaphore <name>`` renders the fixture's public fields."""
    output = gdb.run(f"rtt semaphore {_PROFILE.semaphore_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "Value:"):
        assert label in output, output
    assert _PROFILE.semaphore_name in output


def test_rtt_mutex_detail_shows_priority_inheritance_fields(gdb):
    """Mutex detail retains owner, original priority, policy, and waiters."""
    output = gdb.run(f"rtt mutex {_PROFILE.wait_mutex_name}", timeout=20)
    for label in (
        "Name:",
        "Address:",
        "Type: Mutex",
        "Value:",
        "Hold:",
        f"Owner: {_PROFILE.locker_thread_name}",
        "OriginalPriority:",
        "Waiters: 1@",
    ):
        assert label in output, output
    # RT-Thread v3 retains the configured FIFO flag while newer kernels force
    # mutex inheritance to PRIO. Both are target-valid policy renderings.
    assert _detail_pairs(output)["Policy"] in {"FIFO", "PRIO"}, output
    assert _PROFILE.mutex_waiter_thread in output
    _assert_clean_command_output(output)


def test_rtt_semaphore_detail_shows_policy_and_waiters(gdb):
    """Semaphore detail includes the scheduler policy and blocking relation."""
    output = gdb.run(f"rtt semaphore {_PROFILE.wait_semaphore_name}", timeout=20)
    for label in ("Policy: FIFO", "Waiters: 1@"):
        assert label in output, output
    assert _PROFILE.sem_waiter_thread in output


def test_rtt_event_detail_shows_waiter_conditions(gdb):
    """``rtt event <name>`` pairs each waiter with its mask and mode."""
    output = gdb.run(f"rtt event {_PROFILE.wait_event_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "Set:"):
        assert label in output, output
    assert _PROFILE.wait_event_name in output
    assert "Policy: FIFO" in output
    assert "Waiters: 1@" in output
    assert _PROFILE.event_waiter_thread in output
    assert f"Waiter: {_PROFILE.event_waiter_thread}: set=0x40 mode=AND|CLEAR" in output
    _assert_clean_command_output(output)


def test_rtt_mailbox_detail_shows_waiters(gdb):
    """``rtt mailbox <name>`` renders the fixture's public fields and slots."""
    output = gdb.run(f"rtt mailbox {_PROFILE.mailbox_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "Entry:", "Size:", "MsgPool:"):
        assert label in output, output
    assert _PROFILE.mailbox_name in output
    # Valid ring offsets yield FIFO slot lines and an explicit successful
    # verdict; invalid offsets carry their reason in the same stable field.
    assert "Slot[" in output
    assert "OffsetCheck: ok" in output
    assert "[gdr] error:" not in output, output


def test_rtt_mailbox_detail_shows_receiver_and_sender_waiters(gdb):
    """Mailbox detail distinguishes receive and send blocking lists."""
    recv = gdb.run(f"rtt mailbox {_PROFILE.wait_mailbox_recv_name}", timeout=20)
    send = gdb.run(f"rtt mailbox {_PROFILE.wait_mailbox_send_name}", timeout=20)
    assert "RecvWait: 1@" in recv, recv
    assert _PROFILE.mailbox_recv_thread in recv, recv
    assert "Entry: 0" in recv, recv
    assert "OffsetCheck: ok" in recv, recv
    assert "SendWait: 1@" in send, send
    assert _PROFILE.mailbox_send_thread in send, send
    assert "Entry: 4" in send, send
    for slot in range(4):
        assert f"Slot[{slot}]:" in send, send
    _assert_clean_command_output(recv)
    _assert_clean_command_output(send)


def test_rtt_messagequeue_detail_shows_waiters(gdb):
    """``rtt messagequeue <name>`` renders capacity, nodes, and consistency."""
    output = gdb.run(f"rtt messagequeue {_PROFILE.msgqueue_name}", timeout=20)
    for label in ("Name:", "Address:", "Type:", "MaxMsgs:", "MsgPool:", "Consistency:"):
        assert label in output, output
    assert _PROFILE.msgqueue_name in output
    assert "[gdr] error:" not in output, output


def test_rtt_messagequeue_detail_shows_node_counts_and_waiters(gdb):
    """Message-queue detail combines memory consistency and blocking data."""
    recv = gdb.run(f"rtt messagequeue {_PROFILE.wait_msgqueue_recv_name}", timeout=20)
    recv_pairs = _detail_pairs(recv)
    max_msgs = int(recv_pairs["MaxMsgs"])
    assert recv_pairs["Entry"] == "0", recv
    assert recv_pairs["ActiveNodes"] == "0", recv
    assert recv_pairs["FreeNodes"] == str(max_msgs), recv
    assert (
        recv_pairs["Consistency"] == f"ok (entry=0, free={max_msgs}, max={max_msgs})"
    ), recv
    assert "RecvWait: 1@" in recv, recv
    assert _PROFILE.msgqueue_recv_thread in recv, recv

    if not _PROFILE.mq_sender_list:
        # COUPLED: pre-v3.1.4 and v4.0.0-v4.0.1 fixture patches intentionally
        # omit wait_mq_send because their kernel has no sender wait list.
        assert recv_pairs["SendWait"] == "N/A", recv
        _assert_clean_command_output(recv)
        return

    send = gdb.run(f"rtt messagequeue {_PROFILE.wait_msgqueue_send_name}", timeout=20)
    send_pairs = _detail_pairs(send)
    assert int(send_pairs["Entry"]) == max_msgs, send
    assert send_pairs["ActiveNodes"] == str(max_msgs), send
    assert send_pairs["FreeNodes"] == "0", send
    assert (
        send_pairs["Consistency"] == f"ok (entry={max_msgs}, free=0, max={max_msgs})"
    ), send
    assert "SendWait: 1@" in send, send
    assert _PROFILE.msgqueue_send_thread in send, send
    _assert_clean_command_output(recv)
    _assert_clean_command_output(send)


def test_rtt_mempool_detail_shows_block_detail(gdb):
    """``rtt mempool <name>`` renders pool range, alignment, and free count."""
    output = gdb.run(f"rtt mempool {_PROFILE.mempool_name}", timeout=20)
    for label in (
        "Name:",
        "Address:",
        "Type:",
        "StartAddress:",
        "PoolSize:",
        "BlockList:",
        "AlignmentCheck:",
        "FreeCountCheck:",
    ):
        assert label in output, output
    assert _PROFILE.mempool_name in output
    assert "[gdr] error:" not in output, output


def test_rtt_mempool_detail_shows_waiters(gdb):
    """Memory-pool detail includes the blocking-thread relation."""
    output = gdb.run(f"rtt mempool {_PROFILE.wait_mempool_name}", timeout=20)
    assert "Free: 0" in output, output
    assert "FreeCountCheck: ok (0)" in output, output
    assert "Waiters: 1@" in output, output
    assert _PROFILE.mempool_waiter_thread in output, output
    _assert_clean_command_output(output)


def test_rtt_timer_detail_shows_fixture_timer(gdb):
    """``rtt timer <name>`` renders the fixture timer's callback symbol."""
    output = gdb.run(f"rtt timer {_PROFILE.timer_name}", timeout=20)
    for label in (
        "Name:",
        "Address:",
        "Type:",
        "State:",
        "Callback:",
        "Parameter:",
        "KernelTick:",
        "ExpiresIn:",
    ):
        assert label in output, output
    assert _PROFILE.timer_name in output
    assert "Mode: periodic" in output, output
    assert "TimerType: soft" in output, output
    assert "Callback: <test_timer_timeout" in output, output
    assert "Parameter: N/A" in output, output
    kernel_tick = next(
        line.split(":", 1)[1].strip()
        for line in output.splitlines()
        if line.strip().startswith("KernelTick:")
    )
    assert kernel_tick.isdecimal(), output
    state = next(
        line.split(":", 1)[1].strip().lower()
        for line in output.splitlines()
        if line.strip().startswith("State:")
    )
    expires_in = next(
        line.split(":", 1)[1].strip()
        for line in output.splitlines()
        if line.strip().startswith("ExpiresIn:")
    )
    # QEMU keeps advancing between commands. Assert the dynamic contract, not
    # a particular tick snapshot; an inactive timer legitimately reports N/A.
    if state == "inactive":
        assert expires_in == "N/A", output
    else:
        assert expires_in.isdecimal() and int(expires_in) >= 0, output
    _assert_clean_command_output(output)


def test_rtt_detail_reports_missing_objects(gdb):
    """Unknown object names produce a clear diagnostic, not a crash."""
    output = gdb.run("rtt semaphore no_such_object", timeout=20)
    assert "not found or type not enabled" in output, output
    assert "[gdr] error:" not in output, output
    assert "Traceback" not in output, output
