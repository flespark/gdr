"""Real QEMU checks for the FreeRTOS command tree.

COUPLED: fixture object names and waiter relationships come from
``ci/freertos/fixture/main.c`` plus ``ci/freertos/fixture/config/<variant>/``.
"""

from __future__ import annotations

import contextlib
import os

import pytest

from tests.support.freertos_fixture_profiles import get_freertos_test_profile

_VERSION = os.environ.get("GDR_VERSION", "10.3.1")
_TARGET = os.environ.get("GDR_QEMU_TARGET", "b-l475e-iot01a")
_VARIANT = os.environ.get("GDR_FIXTURE_VARIANT", "base")
_PROFILE = get_freertos_test_profile(_VARIANT, _VERSION, _TARGET)

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
    live corroboration pins it to zero fabricated hosts: every host address
    must already be reachable through an earlier (registry/symbol/active)
    channel on fixtures whose objects all hold global handles.
    """
    probe = gdb_session.run_python(
        """
from freertos.layout import detect_config, build_layout
from freertos.navigation import discover, iter_waiter_hosts
layout = build_layout(detect_config())
known = {
    obj.address
    for kind in ("queue", "semaphore", "mutex", "eventgroup", "timer")
    for obj in discover(kind, layout)
}
unknown = [obj for obj in iter_waiter_hosts(layout) if obj.address not in known]
print(f"unknown_hosts={len(unknown)}")
"""
    )
    _assert_clean_command_output(probe)
    assert "unknown_hosts=0" in probe, probe
