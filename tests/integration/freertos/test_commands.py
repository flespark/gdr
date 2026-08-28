"""Real QEMU checks for the FreeRTOS command tree.

COUPLED: fixture object names and waiter relationships come from
``ci/freertos/fixture/main.c`` plus ``ci/freertos/fixture/config/<variant>/``.
"""

from __future__ import annotations

import contextlib
import os
import re

import pytest

from tests.support.freertos_fixture_profiles import get_freertos_test_profile

_VERSION = os.environ.get("GDR_VERSION", "10.3.1")
_TARGET = os.environ.get("GDR_QEMU_TARGET", "b-l475e-iot01a")
_VARIANT = os.environ.get("GDR_FIXTURE_VARIANT", "base")
_PROFILE = get_freertos_test_profile(_VARIANT, _VERSION, _TARGET)
# The registry slot names the queue "gdr_queue" (main.c pcQueueName); without
# a registry the symbol channel names it after the handle variable.
_FIXTURE_QUEUE_NAME = "gdr_queue" if _PROFILE.registry_size else "gdr_registered_queue"

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS") != "freertos",
    reason="requires the FreeRTOS QEMU profile",
)

_PUBLIC_COMMANDS = (
    "help",
    "tasks",
    "threads",
    "system",
    "objects",
    "heap",
    "queues",
    "sems",
)

_DETAIL_KEY_ORDER_PREFIX = (
    "Name",
    "Address",
    "Type",
    "State",
    "Priority",
    "BasePriority",
    "SP",
    "Stack",
    "StackSize",
    "Used",
)


def _assert_clean_command_output(output: str, *, allow_usage: bool = False) -> None:
    """Reject GDB/Python failures that can leave partial command output."""
    markers = [
        "[gdr] error:",
        "Traceback (most recent call last)",
        "Python Exception",
    ]
    if not allow_usage:
        markers.append("usage: freertos")
    for marker in markers:
        assert marker not in output, output


def _fixture_row(output: str, name: str) -> list[str]:
    """Return one whitespace-delimited table row by fixture task name."""
    return next(
        line.split()
        for line in output.splitlines()
        if line.lstrip().startswith(name + " ") or line.lstrip().startswith(name + "*")
    )


def _detail_pairs(output: str) -> dict[str, str]:
    """Parse stable ``Key: Value`` detail output without table assumptions."""
    return {
        key.strip(): value
        for line in output.splitlines()
        if ": " in line
        for key, value in (line.split(": ", 1),)
    }


@contextlib.contextmanager
def _with_width(gdb, width: int):
    """Set a GDB width, restoring the harness baseline on exit."""
    gdb.run(f"set width {width}")
    try:
        yield
    finally:
        gdb.run("set width 160")


def test_all_freertos_commands_complete_cleanly(gdb_session):
    """Every public route completes without Python or guard error output."""
    for command in _PUBLIC_COMMANDS:
        output = gdb_session.run(f"freertos {command}", timeout=20)
        _assert_clean_command_output(output)


def test_freertos_tasks_table_matches_fixture_ground_truth(gdb_session):
    """Task rows preserve fixture names, states, and native DWARF fields."""
    converted = gdb_session.run_python(
        """
import gdb
from gdr.adapter_api import active
from freertos import adapter
from freertos.navigation import task_state

thread = gdb.parse_and_eval('$gdr_task("gdr_ready")')
selected = active()
assert isinstance(selected, adapter.FreeRtosAdapter)
value = adapter.value_to_task(
    thread, *task_state(thread, selected.layout), selected.layout
)
print(f"prio={value.current_priority}")
print(f"name={value.name}")
print(f"state={value.state}")
"""
    )
    prio = next(
        line.split("=", 1)[1]
        for line in converted.splitlines()
        if line.startswith("prio=")
    )
    tasks = gdb_session.run("freertos tasks", timeout=20)
    _assert_clean_command_output(tasks)
    row = _fixture_row(tasks, "gdr_ready")
    assert "gdr_ready" in row[0]
    assert prio in row
    assert "Blocked" in row


def test_freertos_task_detail_key_order(gdb_session):
    """``frt task <name>`` emits the documented keys in the documented order."""
    detail = gdb_session.run("freertos task IDLE", timeout=20)
    _assert_clean_command_output(detail)
    pairs = _detail_pairs(detail)
    keys = list(pairs)
    for expected in _DETAIL_KEY_ORDER_PREFIX:
        assert expected in keys, detail
    prefix = [key for key in keys if key in _DETAIL_KEY_ORDER_PREFIX]
    assert prefix == list(_DETAIL_KEY_ORDER_PREFIX)


def test_freertos_task_detail_with_space_name(gdb_session):
    """The timer daemon name contains a space and must still resolve."""
    daemon = gdb_session.run("freertos task Tmr Svc", timeout=20)
    _assert_clean_command_output(daemon)
    assert "Name: Tmr Svc" in daemon


def test_freertos_tables_fit_80_columns(gdb_session):
    """A narrow GDB width never removes columns; only elastic text shrinks."""
    with _with_width(gdb_session, 80):
        output = gdb_session.run("freertos tasks", timeout=20)
        _assert_clean_command_output(output)
        for header in ("Name", "State", "Prio", "SP", "Addr"):
            assert header in output, output


def test_freertos_tables_fit_120_columns(gdb_session):
    """The 120-character baseline keeps the full column set intact."""
    with _with_width(gdb_session, 120):
        output = gdb_session.run("freertos tasks", timeout=20)
        _assert_clean_command_output(output)
        for header in ("Name", "State", "Prio", "SP", "Addr"):
            assert header in output, output


def test_freertos_negative_paths(gdb_session):
    """Wrong words, extra arity, and missing names degrade without a traceback."""
    bogus = gdb_session.run("freertos bogus", timeout=20)
    extra = gdb_session.run("freertos tasks extra-arg", timeout=20)
    missing = gdb_session.run("freertos task no_such_task", timeout=20)
    for output in (bogus, extra, missing):
        _assert_clean_command_output(output, allow_usage=True)
    assert "usage: freertos" in bogus
    assert "usage: freertos" in extra
    assert "Name:" not in missing or "no_such_task" not in missing


def test_freertos_dwarf_cross_validation(gdb_session):
    """At least four DWARF fields agree with the command table."""
    dwarf = gdb_session.run_python(
        """
import gdb
tcb = gdb.parse_and_eval('$gdr_task("gdr_ready")')
print(f"prio={int(tcb['uxPriority'])}")
print(f"name={tcb['pcTaskName'].string()}")
print(f"address={int(tcb.address)}")
print(f"stack={int(tcb['pxStack'])}")
print(f"top={int(tcb['pxTopOfStack'])}")
"""
    )
    values = {
        key: value
        for line in dwarf.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }
    tasks = gdb_session.run("freertos tasks", timeout=20)
    row = _fixture_row(tasks, "gdr_ready")
    assert values["name"] in row[0]
    assert values["prio"] in row
    assert hex(int(values["address"])) in " ".join(row)
    assert int(values["stack"]) < int(values["top"])
    assert hex(int(values["top"])) in " ".join(row)


def test_freertos_state_coverage_on_fixture(gdb_session):
    """Running / Ready / Blocked / Suspended / portMAX_DELAY each appear."""
    tasks = gdb_session.run("freertos tasks", timeout=20)
    _assert_clean_command_output(tasks)
    assert "IDLE" in tasks
    assert "Running" in tasks
    assert "Ready" in tasks
    assert "gdr_spin" in tasks
    assert "gdr_qrecv" in tasks and "Blocked" in _fixture_row(tasks, "gdr_qrecv")
    assert "gdr_semw" in tasks and "Blocked" in _fixture_row(tasks, "gdr_semw")
    assert "gdr_mtxw" in tasks and "Blocked" in _fixture_row(tasks, "gdr_mtxw")
    assert "gdr_mtxh" in tasks and "Blocked" in _fixture_row(tasks, "gdr_mtxh")
    assert "gdr_evw" in tasks and "Blocked" in _fixture_row(tasks, "gdr_evw")
    assert "gdr_ntfy" in tasks and "Blocked" in _fixture_row(tasks, "gdr_ntfy")
    assert "gdr_maxd" in tasks and "Blocked" in _fixture_row(tasks, "gdr_maxd")
    assert "gdr_susp" in tasks and "Suspended" in _fixture_row(tasks, "gdr_susp")
    assert "Deleted" not in tasks


def test_freertos_high_water_matches_variant(gdb_session):
    """base scans [pxStack, pxTopOfStack); full exposes Stack/Used too."""
    tasks = gdb_session.run("freertos tasks", timeout=20)
    _assert_clean_command_output(tasks)
    if not _PROFILE.stack_watermark:
        # Reason: without the 0xa5 prefill there is no watermark to scan, so
        # the capability column must be absent rather than showing a
        # fabricated value (trace-off variant).
        assert "HighWater" not in tasks, tasks
        return
    assert "HighWater" in tasks
    if _PROFILE.high_water_source == "pxEndOfStack":
        assert "Stack" in tasks
        assert "Used" in tasks
    idle = gdb_session.run("freertos task IDLE", timeout=20)
    high_water = [line for line in idle.splitlines() if "HighWater" in line]
    assert high_water, idle
    assert "unavailable" not in high_water[0]


def test_objects_summary_reports_sources(gdb_session):
    """frt objects prints a provenance summary with the channel breakdown."""
    output = gdb_session.run("freertos objects", timeout=20)
    _assert_clean_command_output(output)
    for header in ("Kind", "Count", "Sources"):
        assert header in output, output
    assert "symbol" in output
    assert "scheduler=" in output
    if _PROFILE.registry_size:
        # Reason: the registry channel reads pcQueueName (a char*) through
        # the core bounded read; a broken read loses the whole channel while
        # the table still looks sane, so the live channel count is asserted.
        assert "registry=" in output, output
    else:
        # Reason: the registry-0 build has no xQueueRegistry symbol; the
        # summary must say so instead of silently dropping the channel.
        assert "queue registry" in output


def test_waiter_channel_hosts_are_known_objects(gdb_session):
    """The waiter channel's heuristic hosts are all real kernel objects.

    The reverse container_of probe is the only heuristic discovery channel;
    live corroboration pins it to at most the waiter-only fixture object:
    every host address must either already be reachable through an earlier
    (registry/symbol/active) channel or be the deliberately anonymous event
    group that only the waiter channel can see (gdr_waiter_only_eg_task's
    stack-local group, COUPLED to ci/freertos/fixture/main.c).
    """
    probe = gdb_session.run_python(
        """
from freertos.layout import detect_config, build_layout
from freertos.navigation import (
    iter_active_timer_hosts,
    iter_mpu_pool_objects,
    iter_registry_entries,
    iter_static_symbol_objects,
    iter_waiter_hosts,
)
layout = build_layout(detect_config())
# Earlier (non-waiter) channels only: the waiter channel's own results must
# not count as corroboration, or the anonymous group would "know itself".
known = {
    obj.address
    for channel in (
        iter_mpu_pool_objects(layout),
        iter_registry_entries(layout),
        iter_active_timer_hosts(layout),
        iter_static_symbol_objects(layout),
    )
    for obj in channel
}
unknown = [obj for obj in iter_waiter_hosts(layout) if obj.address not in known]
print(f"unknown_hosts={len(unknown)}")
if unknown:
    print(f"unknown_kind={unknown[0].kind}")
"""
    )
    _assert_clean_command_output(probe)
    # Exactly one unknown host is allowed: the waiter-only event group (no
    # global handle / registry / static symbol by construction).  Any other
    # unknown host would mean the container_of probe fabricated an object.
    assert "unknown_hosts=1" in probe, probe
    assert "unknown_kind=eventgroup" in probe, probe


def test_queue_family_tables_match_fixture_ground_truth(gdb_session):
    """frt queues/semaphores/mutexes agree with the fixture's objects.

    COUPLED: object names come from ci/freertos/fixture/main.c -- the
    registry names (gdr_queue/gdr_semaphore/gdr_mutex), the handle symbols
    found via the symbol channel (gdr_full_queue/gdr_empty_queue/
    gdr_recursive_mutex) and the blocked-task waiters created in main.
    """
    with _with_width(gdb_session, 200):
        queues = gdb_session.run("freertos queues", timeout=20)
        semaphores_out = gdb_session.run("freertos semaphores", timeout=20)
        mtxs = gdb_session.run("freertos mutexes", timeout=20)
    for output in (queues, semaphores_out, mtxs):
        _assert_clean_command_output(output)

    gdr_queue = _fixture_row(queues, _FIXTURE_QUEUE_NAME)
    # Name Type Items Length ItemSize Free SendWait RecvWait Locks Src Addr
    assert gdr_queue[3] == "4" and gdr_queue[4] == "4"

    # The full queue (1/1 item) has a blocked sender; the empty queue a
    # blocked receiver -- waiter summaries carry the names.
    full_row = _fixture_row(queues, "gdr_full_queue")
    assert full_row[2:6] == ["1", "1", "4", "0"]
    assert "1@gdr_qsend" in full_row[6]
    empty_row = _fixture_row(queues, "gdr_empty_queue")
    assert empty_row[2] == "0"
    assert "1@gdr_qrecv" in empty_row[7]

    sem_row = _fixture_row(semaphores_out, "gdr_semaphore")
    assert sem_row[2] == "0" and sem_row[3] == "3"  # Count=0 Max=3
    assert "1@gdr_semw" in sem_row[4]

    mtx_row = _fixture_row(mtxs, "gdr_mutex")
    assert mtx_row[2] in ("yes", "1")  # Held (taken, count == 0)
    # gdr_mtxh takes the mutex from a task after the scheduler starts, so the
    # kernel records a real holder (a pre-scheduler take would store NULL).
    assert mtx_row[3] == "gdr_mtxh"
    assert "1@gdr_mtxw" in mtx_row[5]
    recursive_row = _fixture_row(mtxs, "gdr_recursive_mutex")
    assert recursive_row[2] == "yes"
    assert recursive_row[3] == "gdr_recm"
    assert recursive_row[4] == "2"  # recursive mutex held two levels


def test_queue_detail_and_semaphore_detail_field_contract(gdb_session):
    """The detail views pin the queue Locks/Checks/FIFO and the semaphore's
    deliberate lack of an Owner block."""
    with _with_width(gdb_session, 200):
        queue = gdb_session.run(f"freertos queue {_FIXTURE_QUEUE_NAME}", timeout=20)
        sem = gdb_session.run("freertos semaphore gdr_semaphore", timeout=20)
        mtx = gdb_session.run("freertos mutex gdr_mutex", timeout=20)
        full = gdb_session.run("freertos queue gdr_full_queue", timeout=20)
    for output in (queue, sem, mtx, full):
        _assert_clean_command_output(output)

    q = _detail_pairs(queue)
    assert q["Locks"] == "-"  # queueUNLOCKED (-1/-1)
    assert q["Items"] == "0"
    # Reason: the verdict row carries counts only; a healthy queue must not
    # emit any Check[...] problem row (see freertos/details.checks_pairs).
    assert q["Checks"].startswith("ok (")
    assert not any(key.startswith("Check[") for key in q)
    assert not any(key.startswith("Item[") for key in q)  # empty queue

    s = _detail_pairs(sem)
    assert not any(key.startswith("Owner") for key in s)
    assert s["Count"] == "0" and s["Max"] == "3"
    assert "1@gdr_semw" in s["Waiters"]

    m = _detail_pairs(mtx)
    assert m["Held"] == "yes"
    assert m["RecursiveCallCount"] == "0"
    # The holder block is read from the holder TCB: gdr_mtxh takes the mutex at
    # base priority 1 and the priority-3 waiter gdr_mtxw then blocks on it, so
    # the kernel raises uxPriority to 3 while uxBasePriority stays 1. Asserting
    # both numbers pins that OwnerPriority and OwnerBasePriority read different
    # TCB members -- a single-member bug would make them equal.
    assert m["Owner"] == "gdr_mtxh"
    assert m["OwnerPriority"] == "3"
    assert m["OwnerBasePriority"] == "1"
    holder_row = _fixture_row(gdb_session.run("freertos tasks", timeout=20), "gdr_mtxh")
    assert holder_row[2] == m["OwnerPriority"]
    assert holder_row[3] == m["OwnerBasePriority"]
    # count(0) + holder(non-NULL) == 1 now holds, so no problem row is emitted.
    assert m["Checks"].startswith("ok (")
    assert not any(key.startswith("Check[") for key in m)

    rec = _detail_pairs(
        gdb_session.run("freertos mutex gdr_recursive_mutex", timeout=20)
    )
    assert rec["Owner"] == "gdr_recm"
    assert rec["OwnerPriority"] != "N/A"
    assert "OwnerBasePriority" in rec
    assert rec["RecursiveCallCount"] == "2"

    # The FIFO dump starts at pcReadFrom + item size: the full queue holds
    # the uint32 1U, little-endian on the Cortex-M fixture.
    f = _detail_pairs(full)
    assert f["Items"] == "1"
    assert f["Item[0]"].endswith(": 01 00 00 00")


def test_timers_table_matches_fixture_ground_truth(gdb_session):
    """frt timers renders the ten-column contract with live fixture timers.

    COUPLED: ci/freertos/fixture/main.c creates gdr_active (started,
    auto-reload), gdr_stopped (started then stopped), gdr_oneshot (a 1 ms
    one-shot, long expired), plus gdr_idle/gdr_created (never started) --
    the stopped/expired/never-started states must each be reported honestly
    instead of being guessed from a stale list item.
    """
    with _with_width(gdb_session, 200):
        timers = gdb_session.run("freertos timers", timeout=20)
    _assert_clean_command_output(timers)
    header = next(
        line for line in timers.splitlines() if line.lstrip().startswith("Name ")
    ).split()
    assert header == [
        "Name",
        "State",
        "Mode",
        "Period",
        "Expiry",
        "ExpiresIn",
        "Callback",
        "ID",
        "Src",
        "Addr",
    ], timers
    assert "Kernel tick" in timers
    active = _fixture_row(timers, "gdr_active")
    assert active[1] == "active"
    assert active[2] == "auto"
    assert "active" in active[8]  # Src provenance
    assert "gdr_timer_callback" in active[6]  # Callback symbolised
    assert active[7] == "-"  # pvTimerID is NULL in the fixture
    # Dormant rows are named by their pcTimerName and never render a stale
    # list-item value as a live deadline.
    created = _fixture_row(timers, "gdr_created")
    assert created[1] == "dormant"
    assert created[4] == "N/A"  # Expiry
    assert created[5] == "N/A"  # ExpiresIn


def test_timer_detail_reports_list_and_owner(gdb_session):
    """frt timer <name> pins List epoch, OwnerCheck and the control keys.

    A never-started timer's list item was only vListInitialiseItem'd (its
    pvOwner is heap garbage), so OwnerCheck must be decided by the container
    member and report uninitialised for it.
    """
    with _with_width(gdb_session, 200):
        active = gdb_session.run("freertos timer gdr_active", timeout=20)
        created = gdb_session.run("freertos timer gdr_created", timeout=20)
        stopped = gdb_session.run("freertos timer gdr_stopped", timeout=20)
    for output in (active, created, stopped):
        _assert_clean_command_output(output)
    a = _detail_pairs(active)
    assert a["State"] == "active"
    assert a["Mode"] == "auto"
    assert a["List"].startswith(("current(", "overflow("))
    assert a["OwnerCheck"] == "ok"
    assert "gdr_timer_callback" in a["Callback"]
    c = _detail_pairs(created)
    assert c["List"] == "none"
    assert c["OwnerCheck"] == "uninitialised"
    assert c["Expiry"] == "N/A"
    assert c["ExpiresIn"] == "N/A"
    s = _detail_pairs(stopped)
    assert s["State"] == "dormant"
    assert s["OwnerCheck"] == "uninitialised"


def test_timer_commands_section_is_honest(gdb_session):
    """The daemon queue section either shows the pending table or states why
    it is empty -- a silent absence would hide a stale command queue."""
    with _with_width(gdb_session, 200):
        detail = gdb_session.run("freertos timer gdr_active", timeout=20)
    _assert_clean_command_output(detail)
    assert "Commands:" in detail
    if "no pending timer commands" not in detail:
        header_line = next(line for line in detail.splitlines() if "Command" in line)
        for token in ("Seq", "Command", "Timer", "Value"):
            assert token in header_line, detail


def test_queue_set_column_only_on_full_variant(gdb_session):
    """The Set column exists only when configUSE_QUEUE_SETS is on.

    The member's pxQueueSetContainer is the queue-set address; without the
    config the column must disappear entirely instead of rendering N/A.
    """
    with _with_width(gdb_session, 200):
        queues = gdb_session.run("freertos queues", timeout=20)
    _assert_clean_command_output(queues)
    header = next(
        line for line in queues.splitlines() if line.lstrip().startswith("Name ")
    ).split()
    if not _PROFILE.queue_sets:
        assert "Set" not in header, queues
        return
    assert "Set" in header, queues
    set_addr = gdb_session.run_python(
        "import gdb; print(hex(int(gdb.parse_and_eval('gdr_queue_set'))))"
    ).strip()
    member = _fixture_row(queues, "gdr_set_member_a")
    assert member[header.index("Set")] == set_addr, queues


# ---------------------------------------------------------------------------
# event group / stream buffer commands
# ---------------------------------------------------------------------------


def test_event_group_table_and_detail_decode_live_waiter(gdb_session):
    """frt eventgroups/detail pin the waiter decode on the live gdr_evw.

    COUPLED: ci/freertos/fixture/main.c makes gdr_evw wait ALL on bits 0x3 of
    gdr_event_group forever and never calls xEventGroupSetBits, so Bits=0x0,
    missing=0x3 and the waiter is unsatisfied -- the ``(satisfied —
    mid-unblock)`` marker must not appear.
    """
    with _with_width(gdb_session, 200):
        egs = gdb_session.run("freertos eventgroups", timeout=20)
    _assert_clean_command_output(egs)
    header = next(
        line for line in egs.splitlines() if line.lstrip().startswith("Name ")
    ).split()
    assert header == ["Name", "Bits", "Waiters", "Src", "Addr"], egs
    row = _fixture_row(egs, "gdr_event_group")
    assert row[1] == "0x0"  # nothing ever sets the bits
    assert row[2] == "1"  # gdr_evw blocked with ALL
    detail = gdb_session.run("freertos eventgroup gdr_event_group", timeout=20)
    _assert_clean_command_output(detail)
    assert "wants=0x3" in detail
    assert "mode=ALL" in detail
    assert "clearOnExit=no" in detail
    assert "missing=0x3" in detail
    assert "(satisfied" not in detail


def test_stream_buffer_table_matches_fixture_geometry(gdb_session):
    """frt streambuffers renders the 11-column contract with live geometry.

    COUPLED: the fixture never sends to its stream/message buffers, so
    Bytes=0, Space==Capacity and empty single-handle waiter cells; Capacity
    is xLength-1, which differs between the dynamic (32) and static-only
    (31) creation paths (stream_buffer.c xBufferSizeBytes++ runs on the
    dynamic path only).
    """
    with _with_width(gdb_session, 200):
        sbs = gdb_session.run("freertos streambuffers", timeout=20)
    _assert_clean_command_output(sbs)
    header = next(
        line for line in sbs.splitlines() if line.lstrip().startswith("Name ")
    ).split()
    assert header == [
        "Name",
        "Type",
        "Bytes",
        "Space",
        "Capacity",
        "Trigger",
        "NextMsg",
        "RecvWait",
        "SendWait",
        "Src",
        "Addr",
    ], sbs
    stream_row = _fixture_row(sbs, "gdr_stream_buffer")
    assert stream_row[1] == "stream"
    msg_row = _fixture_row(sbs, "gdr_message_buffer")
    assert msg_row[1] == "message"
    assert msg_row[6] == "-"  # empty message buffer: no length prefix yet
    assert stream_row[2] == "0"  # Bytes
    assert stream_row[3] == stream_row[4]  # Space == Capacity when empty
    assert stream_row[7] == "-" and stream_row[8] == "-"  # single-handle waiters
    expected_capacity = (
        "31" if _PROFILE.static_allocation and not _PROFILE.static_and_dynamic else "32"
    )
    assert stream_row[4] == expected_capacity, sbs


def test_notification_index_gated_by_kernel_version(gdb_session):
    """uxNotificationIndex exists from V11.1.0 only; the detail says so."""
    detail = gdb_session.run("freertos streambuffer gdr_stream_buffer", timeout=20)
    _assert_clean_command_output(detail)
    major, minor, _patch = (int(part) for part in _VERSION.split("."))
    if (major, minor) >= (11, 1):
        assert "NotificationIndex: 0" in detail, detail
    else:
        assert "NotificationIndex" in detail
        assert "N/A (kernel < 11.1.0)" in detail, detail


def test_batching_buffer_only_on_11_1(gdb_session):
    """The batching buffer exists on V11.1+ lanes only (Type=batching)."""
    with _with_width(gdb_session, 200):
        sbs = gdb_session.run("freertos streambuffers", timeout=20)
    _assert_clean_command_output(sbs)
    if _PROFILE.batching_buffer:
        row = _fixture_row(sbs, "gdr_batching_buffer")
        assert row[1] == "batching"
    else:
        assert "gdr_batching_buffer" not in sbs, sbs


def test_waiter_only_event_group_is_listed(gdb_session):
    """The waiter-only event group is listed by frt eventgroups as '-'
    and shows up in frt objects' waiter= provenance count.

    The anonymous group exists only on the waiter task's stack (or heap) and
    is deliberately invisible to the symbol/registry channels; the waiter
    channel's container_of reconstruction must surface it as a real row.
    """
    probe = gdb_session.run_python(
        """
from freertos.layout import detect_config, build_layout
from freertos.navigation import discover
layout = build_layout(detect_config())
anon = [obj for obj in discover("eventgroup", layout) if not obj.name]
print(f"anon={len(anon)}")
print(f"addr={hex(anon[0].address)}" if anon else "addr=none")
"""
    )
    _assert_clean_command_output(probe)
    addr = next(
        (
            line.split("=", 1)[1]
            for line in probe.splitlines()
            if line.startswith("addr=")
        ),
        None,
    )
    assert addr is not None and addr != "none", probe

    egs = gdb_session.run("freertos eventgroups", timeout=20)
    _assert_clean_command_output(egs)
    assert addr in egs, egs
    # The anonymous group renders as a '-' name row.
    assert any(line.lstrip().startswith("- ") for line in egs.splitlines()), egs

    objects = gdb_session.run("freertos objects", timeout=20)
    _assert_clean_command_output(objects)
    eg_line = next(
        line for line in objects.splitlines() if line.lstrip().startswith("eventgroup ")
    )
    assert "waiter=1" in eg_line, objects


_HEAP_KEYS = [
    "Algorithm",
    "TotalSize",
    "FreeSize",
    "MinEver",
    "Allocs",
    "Frees",
    "Protector",
    "Blocks",
    "Holes",
    "CrossCheck",
]


def test_freertos_heap_contract_matches_variant(gdb_session):
    """frt heap renders the ten stable keys; live values follow the variant.

    COUPLED: heap expectations come from ci/freertos/fixture/config/<variant>/
    (which MemMang source is linked) and the profile table in
    tests/support/freertos_fixture_profiles.py.
    """
    output = gdb_session.run("freertos heap", timeout=20)
    _assert_clean_command_output(output)
    # The block-table messages ride on [gdr] info lines; only the vertical
    # Key: Value pairs belong to the ten-key contract.
    pairs = {
        key: value
        for key, value in _detail_pairs(output).items()
        if not key.startswith("[gdr]")
    }
    assert list(pairs) == _HEAP_KEYS, output

    kind = _PROFILE.heap_kind
    if kind == 1:
        assert pairs["Algorithm"] == "heap_1 (bump pointer)"
        assert pairs["CrossCheck"].startswith("unavailable")
        assert pairs["Blocks"] == "unavailable" and pairs["Holes"] == "unavailable"
    elif kind == 2:
        assert pairs["Algorithm"] == "heap_2"
        assert pairs["MinEver"] == "unavailable"
        assert pairs["Allocs"] == "unavailable"
        assert pairs["Frees"] == "unavailable"
        assert pairs["CrossCheck"] == "ok"
        assert pairs["Blocks"].split()[0].isdigit()
    elif kind == 4:
        assert pairs["Algorithm"] == "heap_4"
        assert pairs["CrossCheck"] == "ok"
        assert pairs["Blocks"].split()[0].isdigit()
        # The linear walk must start at the heap base (align_up(&ucHeap)),
        # not at the free-list head: heap_4 carves allocations from the front
        # of the first free block, so the fixture's boot-time task stacks and
        # objects sit below the head and must show up in the block count
        # (b-l475e base: 57 blocks, only 1 free).
        match = re.search(r"linear walk: (\d+) block", output)
        assert match, output
        assert int(match.group(1)) > 1, output
        if _PROFILE.heap_protector:
            # The fixture's canary is 0xBEEF: without XOR decoding the first
            # chain hop would land on garbage and CrossCheck could never be ok.
            assert pairs["Protector"] == "enabled"
    elif kind == 5:
        # heap_5 without the heap protector exports no region bases, so the
        # linear walk is skipped and CrossCheck states the reason.
        assert pairs["Algorithm"] == "heap_5"
        assert pairs["CrossCheck"] != "ok"
        assert "heap_5" in pairs["CrossCheck"]
        assert pairs["Blocks"].split()[0].isdigit()
    elif _PROFILE.variant == "heap-3":
        assert pairs["Algorithm"] == "heap_3"
        assert "not inspectable" in output
    elif _PROFILE.variant == "static-only":
        assert pairs["Algorithm"] == "none"
    else:
        pytest.skip(f"no heap contract expectation for {_PROFILE.variant}")


def test_freertos_system_reports_heap_fields(gdb_session):
    """frt system exposes the allocator and status for walkable heaps."""
    output = gdb_session.run("freertos system", timeout=20)
    _assert_clean_command_output(output)
    pairs = _detail_pairs(output)

    kind = _PROFILE.heap_kind
    if kind in (1, 2, 4, 5):
        assert pairs["Heap allocator"] == f"heap_{kind}"
        assert pairs["Heap status"] == "good"
        if kind == 5:
            # No region bases without the protector -> no trustworthy total.
            assert "Heap total" not in output
        else:
            assert "Heap total" in output
    elif _PROFILE.variant == "heap-3":
        assert pairs["Heap allocator"] == "heap_3"
    elif _PROFILE.variant == "static-only":
        assert pairs["Heap allocator"] == "unavailable"
    else:
        pytest.skip(f"no heap system expectation for {_PROFILE.variant}")
