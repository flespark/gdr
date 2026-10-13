"""Live dual-core SMP assertions (QEMU mps2-an521, kernel V11.3.1).

COUPLED: ci/freertos/fixture/config/smp/FreeRTOSConfig.h builds the smp
variant with two cores, configUSE_CORE_AFFINITY and
configUSE_TASK_PREEMPTION_DISABLE; ci/freertos/fixture/main.c creates
gdr_bound (pinned to core 1 via vTaskCoreAffinitySet) and gdr_preempt
(vTaskPreemptionDisable) under that variant.

Only memory-derived facts are asserted: QEMU exposes the second core as a
separate GDB inferior (per-CPU cluster -> separate process), which the
RTOS-neutral harness never attaches to, so no test reads per-core
registers.  Every user-visible SMP fact comes from shared memory
(pxCurrentTCBs[] and the TCBs themselves).
"""

from __future__ import annotations

import pytest

from tests.support.freertos_fixture_profiles import get_freertos_test_profile
from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
_PROFILE = (
    get_freertos_test_profile(SPEC.variant, SPEC.version, SPEC.target)
    if SPEC.rtos == "freertos" and SPEC.variant != "snapshot"
    else None
)

pytestmark = pytest.mark.skipif(
    _PROFILE is None or _PROFILE.number_of_cores == 1,
    reason="requires the SMP FreeRTOS QEMU profile",
)

# tskNO_AFFINITY (task.h): an unbound task reports this mask as its
# CoreAffinity, so the Affinity column reads it verbatim (32-bit target).
_TSK_NO_AFFINITY = 4294967295

# The state strings the renderer emits (freertos/navigation.adjust_state /
# navigation.py states).  The tasks table's name cell may itself contain a
# space (the kernel names the timer daemon "Tmr Svc"), so fixed column
# indices from the header do not survive a space-split; finding the first
# known State token re-anchors every row at a stable boundary.
_STATES = (
    "Running(yielding)",
    "Running",
    "Ready",
    "Blocked",
    "Suspended",
    "Deleted",
)


def _tasks_table(gdb_session) -> list[str]:
    output = gdb_session.run("freertos tasks", timeout=20)
    assert "[gdr] error:" not in output, output
    assert "Traceback" not in output, output
    return output.splitlines()


def _row(output_lines: list[str], name: str) -> list[str]:
    """Return the table row for name, re-anchored at the State cell.

    The renderer prepends running tasks' names with a " *" marker and the
    kernel's daemon task is named "Tmr Svc", so the raw whitespace split
    has variable-length name cells.  The State cell is the first token that
    is a known state string; the fixed-width columns follow it in header
    order (Prio BasePrio SP HighWater CPU Affinity Addr).
    """
    for line in output_lines:
        stripped = line.lstrip()
        if not (stripped.startswith(name + " ") or stripped.startswith(name + "*")):
            continue
        cells = stripped.split()
        state_index = next(i for i, cell in enumerate(cells) if cell in _STATES)
        return [name] + cells[state_index:]
    raise AssertionError(f"no row starting with {name!r}")


def _pairs_detail(gdb_session, name: str) -> dict[str, str]:
    output = gdb_session.run(f"freertos task {name}", timeout=20)
    assert "Traceback" not in output, output
    return {
        key.strip(): value.strip()
        for line in output.splitlines()
        if ": " in line
        for key, value in (line.split(": ", 1),)
    }


def test_smp_tasks_table_has_an_idle_per_core(gdb_session):
    """SMP kernels name the idle task configIDLE_TASK_NAME + core digit."""
    lines = _tasks_table(gdb_session)
    assert any(
        line.lstrip().startswith("IDLE0 ") or line.lstrip().startswith("IDLE0*")
        for line in lines
    ), lines
    assert any(
        line.lstrip().startswith("IDLE1 ") or line.lstrip().startswith("IDLE1*")
        for line in lines
    ), lines


def test_smp_px_current_tcbs_match_table_cpu_column(gdb_session):
    """Every pxCurrentTCBs[core] TCB appears with CPU == core in frt tasks.

    The CPU column is derived from the TCB's xTaskRunState; pxCurrentTCBs is
    the kernel's own running-task array.  The two must agree for every core,
    whichever task happens to be running at the snapshot instant.
    """
    probe = gdb_session.run_python(
        """
import gdb
current = gdb.parse_and_eval("pxCurrentTCBs")
lo, hi = current.type.range()
print("cores={}".format(hi - lo + 1))
for i in range(2):
    tcb = current[i].dereference()
    print("addr%d=%x" % (i, int(tcb.address)))
    print("name%d=%s" % (i, tcb["pcTaskName"].string()))
"""
    )
    values = {
        key: value
        for line in probe.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }
    assert values["cores"] == "2", probe
    current = {}
    for i in range(2):
        current[i] = (values[f"name{i}"], values[f"addr{i}"])
    lines = _tasks_table(gdb_session)
    header = next(line for line in lines if line.lstrip().startswith("Name ")).split()
    cpu_index = header.index("CPU")
    addr_index = header.index("Addr")
    for core, (name, address) in current.items():
        row = _row(lines, name)
        assert row[addr_index] == hex(int(address, 16)), (
            f"row {name!r} addr {row[addr_index]} != pxCurrentTCBs[{core}] {address}"
        )
        assert row[cpu_index] == str(core), (
            f"row {name!r} CPU {row[cpu_index]} != running core {core}"
        )


def test_smp_affinity_column_reports_bound_and_unbound(gdb_session):
    """vTaskCoreAffinitySet(gdr_bound, 0x2) pins it to core 1; everyone
    else keeps tskNO_AFFINITY (the default, not 0x3)."""
    lines = _tasks_table(gdb_session)
    header = next(line for line in lines if line.lstrip().startswith("Name ")).split()
    affinity_index = header.index("Affinity")
    bound = _row(lines, "gdr_bound")
    assert bound[affinity_index] == "2", bound
    unbound = _row(lines, "gdr_ready")
    assert unbound[affinity_index] == str(_TSK_NO_AFFINITY), unbound


def test_smp_preemption_disable_task_detail(gdb_session):
    """The vTaskPreemptionDisable task's detail shows a real value,
    not N/A (xPreemptionDisable is set in main.c)."""
    pairs = _pairs_detail(gdb_session, "gdr_preempt")
    assert pairs["PreemptionDisable"] != "N/A", pairs
    assert pairs["PreemptionDisable"] != "-", pairs


def test_smp_idle_detail_shows_smp_keys(gdb_session):
    """frt task <idle> carries the SMP-only detail keys on this lane."""
    pairs = _pairs_detail(gdb_session, "IDLE0")
    assert "RunState" in pairs, pairs
    assert "CoreAffinity" in pairs, pairs
    assert "PreemptionDisable" in pairs, pairs


def test_smp_no_dedicated_core_registers_needed(gdb_session):
    """The tasks table exposes both running cores through shared memory
    even while the harness stays on a single GDB inferior."""
    lines = _tasks_table(gdb_session)
    # Every running row holds a valid core id, and both cores appear among
    # the running rows.  Skip the table separator line; rows are re-anchored
    # at the State token as in _row(), because the space-named "Tmr Svc"
    # daemon row (pinned to core 0 like the other waiters, but still a
    # possible current task) would shift every column under a naive split.
    header = next(line for line in lines if line.lstrip().startswith("Name ")).split()
    cpu_index = header.index("CPU") - 1  # _row-style rows start at State
    running_cores = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or set(stripped) <= {"-", " "}:
            continue
        cells = stripped.split()
        if not any(cell in _STATES for cell in cells):
            continue
        state_index = next(i for i, cell in enumerate(cells) if cell in _STATES)
        aligned = cells[state_index:]
        if len(aligned) <= cpu_index or not aligned[cpu_index].isdigit():
            continue
        running_cores.append(int(aligned[cpu_index]))
    assert 0 in running_cores and 1 in running_cores, lines
