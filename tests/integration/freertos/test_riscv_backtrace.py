"""Closed-loop FreeRTOS RISC-V scheduler-switch and trap stage backtrace tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from core.unwind_meta import ElfImage
from freertos.riscv_scheduler_switch import RiscvSchedulerProfile, build_riscv_profile
from tests.support.freertos_backtrace_oracles import (
    current_task_name,
    envelope_data,
    json_line,
    riscv_trap_state,
)
from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile
from tests.support.gdb_process import has_embedded_python
from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession, check_tools

SPEC = load_integration_spec()
GDR_ROOT = Path(__file__).resolve().parents[3]
_ISA_BY_TARGET = {
    "qemu-virt-rv32": "riscv32",
    "qemu-virt-rv64": "riscv64",
}
_ISA = _ISA_BY_TARGET.get(SPEC.target, "")
_XLEN = 64 if _ISA == "riscv64" else 32
_RV_PROFILE_CACHE: RiscvSchedulerProfile | None = None

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos"
    or SPEC.variant == "snapshot"
    or not SPEC.target.startswith("qemu-virt-rv"),
    reason="requires a FreeRTOS QEMU RISC-V fixture",
)


def _profile() -> RiscvSchedulerProfile:
    global _RV_PROFILE_CACHE
    if _RV_PROFILE_CACHE is None:
        profile, reason = build_riscv_profile(ElfImage.from_path(SPEC.elf_path()))
        assert profile is not None, reason
        _RV_PROFILE_CACHE = profile
    return _RV_PROFILE_CACHE


def _backtrace(session: GdbSession, task: str | None = None) -> dict:
    command = (
        f"gdr api backtrace {task} --json"
        if task is not None
        else "gdr api backtrace --json"
    )
    return envelope_data(session.run(command, timeout=30))


def _clear_breakpoints(session: GdbSession) -> None:
    session.run_python("import gdb\nfor bp in gdb.breakpoints() or (): bp.delete()")


def _stop_at(session: GdbSession, stop_expr: str) -> None:
    bp_out = session.run(f"tbreak {stop_expr}")
    assert "Temporary breakpoint" in bp_out, bp_out
    output = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in output, output


def _rv_hold_and_break(
    session: GdbSession, stop_expr: str, *, conditional: bool = True
) -> None:
    session.run("set var gdr_irq_hold = 0")
    _stop_at(session, "gdr_rv_spin_leaf")
    session.run("set var gdr_irq_hold = 1")
    session.run("stepi 4")
    cond = " if pxCurrentTCB == gdr_ready_spin_task" if conditional else ""
    _stop_at(session, f"{stop_expr}{cond}")


def _symbol_address(session: GdbSession, symbol: str) -> int:
    return int(
        json_line(
            session.run_python(
                f'import gdb, json\nprint(json.dumps({{"addr": int(gdb.parse_and_eval("&{symbol}"))}}))'
            )
        )["addr"]
    )


def _exception_frames(data: dict) -> list[dict]:
    return [frame for frame in data["frames"] if frame.get("method") == "exception"]


@contextmanager
def _paused_rv_session():
    profile = replace(get_freertos_qemu_profile(GDR_ROOT, SPEC), init_command=None)
    check_tools(profile, SPEC.gdb)
    if not has_embedded_python(SPEC.gdb):
        pytest.skip("GDB has no embedded Python")
    qemu = QemuSession(profile)
    qemu.start(SPEC.boot_wait, paused=True)
    session = GdbSession(SPEC.gdb, profile, qemu.gdb_port, GDR_ROOT)
    try:
        session.start()
        session.run(f"gdr init freertos {SPEC.version}", timeout=30)
        yield session
    finally:
        session.stop()
        qemu.stop()


@pytest.fixture(scope="module")
def first_start_rv_session():
    with _paused_rv_session() as session:
        yield session


def test_riscv_timer_isr_unwinds_across_isr_stack_to_multiframe_task(gdb_session):
    try:
        _rv_hold_and_break(gdb_session, "xTaskIncrementTick")
        gdb_session.run("stepi 4")
        raw = riscv_trap_state(gdb_session)
        data = _backtrace(gdb_session, "gdr_spin")
        assert data["architecture"] == _ISA, data
        assert data["source"] == "current", data
        assert data["complete"] is True and data["stop_reason"] == "task-exit", data

        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        assert "xTaskIncrementTick" in symbols[0], symbols
        assert any("gdr_rv_spin_leaf" in sym for sym in symbols), symbols
        assert any("gdr_rv_spin_mid" in sym for sym in symbols), symbols
        assert "gdr_ready_spin" in symbols[-1], symbols

        transitions = _exception_frames(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["mepc"], (
            transitions,
            raw,
        )
        assert "gdr_rv_spin_leaf" in (transitions[0]["symbol"] or ""), transitions
        evidence = " ".join(data["messages"])
        assert f"exception:{raw['mcause']:#x} -> task:{raw['tcb']:#x}" in evidence, data
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def test_riscv_raw_trap_entry_recovers_exact_interrupted_task_pc(gdb_session):
    try:
        trap_entry = _symbol_address(gdb_session, "freertos_risc_v_trap_handler")
        _rv_hold_and_break(gdb_session, f"*{trap_entry:#x}")
        raw = riscv_trap_state(gdb_session)
        data = _backtrace(gdb_session, "gdr_spin")
        assert data["complete"] is True and data["stop_reason"] == "task-exit", data
        frames = data["frames"]
        symbols = [frame["symbol"] or "" for frame in frames]
        assert "freertos_risc_v_trap_handler" in symbols[0], symbols
        assert frames[1]["method"] == "exception", frames
        assert frames[1]["pc"] == raw["mepc"], (frames, raw)
        assert any("gdr_rv_spin_leaf" in sym for sym in symbols[1:]), symbols
        assert any("gdr_rv_spin_mid" in sym for sym in symbols[1:]), symbols
        assert "gdr_ready_spin" in symbols[-1], symbols
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def test_riscv_trap_prologue_stages_preserve_interrupted_task(gdb_session):
    prof = _profile()
    try:
        _rv_hold_and_break(gdb_session, f"*{prof.trap_start:#x}")
        gdb_session.run("stepi 2")
        saving_state = riscv_trap_state(gdb_session)
        assert prof.trap_start < saving_state["pc"] <= prof.trap_tcb_store_pc, (
            saving_state
        )
        saving_data = _backtrace(gdb_session, "gdr_spin")
        assert saving_data["complete"] is True, (
            saving_data.get("stop_reason"),
            saving_data.get("messages"),
            saving_data,
        )
        assert saving_data["source"] == "current", saving_data
        assert "stage=trap-prologue-saving" in " ".join(saving_data["messages"]), (
            saving_data
        )
        assert saving_data["frames"][0]["pc"] == saving_state["mepc"], saving_data
        saving_symbols = [frame["symbol"] or "" for frame in saving_data["frames"]]
        assert "gdr_rv_spin_leaf" in saving_symbols[0], saving_symbols
        assert "gdr_ready_spin" in saving_symbols[-1], saving_symbols

        _stop_at(gdb_session, f"*{prof.trap_tcb_store_pc:#x}")
        gdb_session.run("stepi 1")
        written_state = riscv_trap_state(gdb_session)
        written_data = _backtrace(gdb_session, "gdr_spin")
        assert written_data["complete"] is True, written_data
        assert written_data["source"] == "current", written_data
        assert "stage=trap-prologue-tcb-written" in " ".join(
            written_data["messages"]
        ), written_data
        assert written_data["frames"][0]["pc"] == written_state["mepc"], written_data

        _stop_at(gdb_session, f"*{prof.async_epc_store_pc:#x}")
        gdb_session.run("stepi 1")
        epc_state = riscv_trap_state(gdb_session)
        epc_data = _backtrace(gdb_session, "gdr_spin")
        assert epc_data["complete"] is True, epc_data
        assert epc_data["source"] == "saved", epc_data
        assert "stage=trap-prologue-epc-saved" in " ".join(epc_data["messages"]), (
            epc_data
        )
        assert epc_data["frames"][0]["pc"] == epc_state["mepc"], epc_data
        assert any(
            "gdr_ready_spin" in (frame["symbol"] or "") for frame in epc_data["frames"]
        ), epc_data
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def test_riscv_ecall_yield_and_switch_stages(gdb_session):
    prof = _profile()
    gdb_session.run("set var gdr_irq_hold = 0")
    try:
        _stop_at(
            gdb_session, f"*{prof.sync_entry:#x} if pxCurrentTCB == gdr_ready_spin_task"
        )
        sync_raw = riscv_trap_state(gdb_session)
        assert sync_raw["mcause"] == 11, sync_raw
        sync_data = _backtrace(gdb_session, "gdr_spin")
        assert sync_data["complete"] is True, sync_data
        assert sync_data["source"] == "current", sync_data
        assert "stage=trap-prologue-tcb-written" in " ".join(sync_data["messages"]), (
            sync_data
        )
        assert sync_data["frames"][0]["pc"] == sync_raw["mepc"] + 4, sync_data
        assert any(
            "gdr_ready_spin" in (frame["symbol"] or "") for frame in sync_data["frames"]
        ), sync_data

        _stop_at(
            gdb_session,
            f"*{prof.handle_exception_pc:#x} if pxCurrentTCB == gdr_ready_spin_task",
        )
        disp_raw = riscv_trap_state(gdb_session)
        assert disp_raw["mcause"] == 11, disp_raw
        disp_data = _backtrace(gdb_session, "gdr_spin")
        assert disp_data["complete"] is True, disp_data
        assert disp_data["source"] == "saved", disp_data
        assert "stage=trap-sync-dispatch" in " ".join(disp_data["messages"]), disp_data
        assert disp_data["frames"][0]["pc"] == disp_raw["mepc"] + 4, disp_data

        _stop_at(
            gdb_session,
            f"*{prof.switch_update_pc:#x} if pxCurrentTCB == gdr_ready_spin_task",
        )
        before_raw = riscv_trap_state(gdb_session)
        old_tcb = before_raw["tcb"]
        before_named = _backtrace(gdb_session, "gdr_spin")
        before_current = _backtrace(gdb_session)
        for data in (before_named, before_current):
            assert data["complete"] is True, data
            assert data["task"] == "gdr_spin", data
            assert data["source"] == "saved", data
            assert "stage=switch-before-update" in " ".join(data["messages"]), data
            assert data["frames"][0]["pc"] == before_raw["mepc"] + 4, data
            assert any(
                "gdr_ready_spin" in (frame["symbol"] or "") for frame in data["frames"]
            ), data

        gdb_session.run("stepi 1")
        after_raw = riscv_trap_state(gdb_session)
        assert after_raw["pc"] == prof.switch_after_update_pc, after_raw
        new_name = current_task_name(gdb_session)
        after_current = _backtrace(gdb_session)
        assert after_current["complete"] is True, after_current
        assert after_current["task"] == new_name, after_current
        assert "stage=switch-after-update" in " ".join(after_current["messages"]), (
            after_current
        )
        if after_raw["tcb"] != old_tcb:
            after_symbols = [frame["symbol"] or "" for frame in after_current["frames"]]
            assert not any("gdr_ready_spin" in sym for sym in after_symbols), (
                after_symbols
            )
            assert not any("vTaskSwitchContext" in sym for sym in after_symbols), (
                after_symbols
            )
            old_after = _backtrace(gdb_session, "gdr_spin")
            assert old_after["complete"] is True, old_after
            assert old_after["source"] == "saved", old_after
            assert old_after["frames"][0]["pc"] == before_raw["mepc"] + 4, old_after

        _stop_at(gdb_session, f"*{prof.restore_load_sp_pc:#x}")
        pre_restore_data = _backtrace(gdb_session)
        assert pre_restore_data["complete"] is True, pre_restore_data
        assert "stage=switch-after-update" in " ".join(pre_restore_data["messages"]), (
            pre_restore_data
        )

        gdb_session.run("stepi 1")
        restoring_name = current_task_name(gdb_session)
        loaded_data = _backtrace(gdb_session)
        assert loaded_data["complete"] is True, loaded_data
        assert loaded_data["task"] == restoring_name, loaded_data
        assert loaded_data["source"] in ("current", "unstarted"), loaded_data
        assert "stage=restore-sp-loaded" in " ".join(loaded_data["messages"]), (
            loaded_data
        )

        _stop_at(gdb_session, f"*{prof.restore_mret_pc:#x}")
        popped_name = current_task_name(gdb_session)
        popped_raw = riscv_trap_state(gdb_session)
        popped_data = _backtrace(gdb_session)
        assert popped_data["complete"] is True, popped_data
        assert popped_data["task"] == popped_name, popped_data
        assert popped_data["source"] in ("current", "unstarted"), popped_data
        assert "stage=restore-sp-popped" in " ".join(popped_data["messages"]), (
            popped_data
        )
        assert popped_data["frames"][0]["pc"] == popped_raw["mepc"], popped_data
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def test_riscv_first_start_stages_unwind_first_task(first_start_rv_session):
    prof = _profile()
    try:
        _stop_at(first_start_rv_session, f"*{prof.start_first_start:#x}")
        first_name = current_task_name(first_start_rv_session)
        for data in (
            _backtrace(first_start_rv_session),
            _backtrace(first_start_rv_session, first_name),
        ):
            assert data["complete"] is True, data
            assert data["task"] == first_name, data
            assert data["source"] == "unstarted", data
            assert data["stop_reason"] == "task-entry", data
            assert "stage=start-first-entry" in " ".join(data["messages"]), data

        _stop_at(first_start_rv_session, f"*{prof.start_first_sp_load_pc:#x}")
        first_start_rv_session.run("stepi 1")
        loaded = _backtrace(first_start_rv_session)
        assert loaded["complete"] is True, loaded
        assert loaded["source"] == "unstarted", loaded
        assert loaded["stop_reason"] == "task-entry", loaded
        assert "stage=start-first-sp-loaded" in " ".join(loaded["messages"]), loaded
    finally:
        _clear_breakpoints(first_start_rv_session)

    with _paused_rv_session() as popped_session:
        try:
            _stop_at(popped_session, f"*{prof.start_first_ret_pc:#x}")
            popped = _backtrace(popped_session)
            assert popped["complete"] is True, (
                popped.get("stop_reason"),
                popped.get("messages"),
                popped,
            )
            assert popped["task"] == first_name, popped
            assert popped["source"] == "unstarted", popped
            assert popped["stop_reason"] == "task-entry", popped
            assert "stage=start-first-sp-popped" in " ".join(popped["messages"]), popped
        finally:
            _clear_breakpoints(popped_session)


def test_riscv_scheduler_profile_and_sync_fault_fail_closed(gdb_session):
    prof = _profile()
    gdb_session.run("set var gdr_irq_hold = 0")
    try:
        _stop_at(gdb_session, f"*{prof.switch_start:#x}")
        switch_task = current_task_name(gdb_session)
        mismatch = json_line(
            gdb_session.run_python(
                f"""
import json
from core.adapter import active
import freertos.backtrace_gdb as bt

original = bt.build_riscv_scheduler_profile
bt.build_riscv_scheduler_profile = lambda _elf: (None, "scheduler-profile-mismatch:test")
try:
    report = bt.backtrace(active(), {switch_task!r})
finally:
    bt.build_riscv_scheduler_profile = original
print(json.dumps({{"complete": report.complete, "stop_reason": report.stop_reason}}))
"""
            )
        )
        assert mismatch == {
            "complete": False,
            "stop_reason": "scheduler-profile-mismatch:test",
        }, mismatch

        _stop_at(
            gdb_session,
            f"*{prof.handle_exception_pc:#x} if pxCurrentTCB == gdr_ready_spin_task",
        )
        fault_task = current_task_name(gdb_session)
        sync_fault = json_line(
            gdb_session.run_python(
                f"""
import dataclasses, json
from core.adapter import active
import freertos.backtrace_gdb as bt

original = bt._riscv_snapshot
def forced_cause(cause):
    def _wrap(*, xlen=32):
        snap, reason = original(xlen=xlen)
        if snap is None:
            return None, reason
        return dataclasses.replace(snap, mcause=cause), "ok"
    return _wrap

bt._riscv_snapshot = forced_cause(2)
try:
    snap, _ = original(xlen={_XLEN})
    report = bt.backtrace(active(), {fault_task!r})
finally:
    bt._riscv_snapshot = original

bt._riscv_snapshot = forced_cause(24)
try:
    refused = bt.backtrace(active(), {fault_task!r})
finally:
    bt._riscv_snapshot = original

print(json.dumps({{
    "complete": report.complete,
    "stop_reason": report.stop_reason,
    "mepc": snap.mepc if snap else None,
    "pcs": [f.pc for f in report.frames],
    "messages": report.messages,
    "refused_complete": refused.complete,
    "refused_reason": refused.stop_reason,
}}))
"""
            )
        )
        assert sync_fault["complete"] is True, sync_fault
        assert sync_fault["stop_reason"] == "synchronous-fault", sync_fault
        assert sync_fault["pcs"][0] == sync_fault["mepc"], sync_fault
        assert any("Exception:IllegalInstruction" in m for m in sync_fault["messages"])
        assert sync_fault["refused_complete"] is False, sync_fault
        assert sync_fault["refused_reason"] == "exception-frame-type-unproven", (
            sync_fault
        )

        _stop_at(
            gdb_session,
            f"*{prof.trap_start:#x} if pxCurrentTCB == gdr_ready_spin_task",
        )
        trap_sync = json_line(
            gdb_session.run_python(
                f"""
import dataclasses, json
from core.adapter import active
import freertos.backtrace_gdb as bt

original = bt._riscv_snapshot
def forced_illegal(*, xlen=32):
    snap, reason = original(xlen=xlen)
    if snap is None:
        return None, reason
    return dataclasses.replace(snap, mcause=2, mtval=0xDEADBEEF), "ok"

bt._riscv_snapshot = forced_illegal
try:
    snap, _ = original(xlen={_XLEN})
    report = bt.backtrace(active(), {fault_task!r})
finally:
    bt._riscv_snapshot = original
print(json.dumps({{
    "complete": report.complete,
    "stop_reason": report.stop_reason,
    "mepc": snap.mepc if snap else None,
    "frames": [{{"pc": f.pc, "method": f.method}} for f in report.frames],
    "messages": report.messages,
}}))
"""
            )
        )
        assert trap_sync["complete"] is True, trap_sync
        assert trap_sync["stop_reason"] == "synchronous-fault", trap_sync
        assert trap_sync["frames"][1]["method"] == "exception", trap_sync
        assert trap_sync["frames"][1]["pc"] == trap_sync["mepc"], trap_sync
        assert any("Exception:IllegalInstruction" in m for m in trap_sync["messages"])
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")
