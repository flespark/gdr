"""Live QEMU RISC-V M-mode trap cross-stack backtrace tests for FreeRTOS."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos" or SPEC.target not in {"qemu-virt-rv32", "qemu-virt-rv64"},
    reason="requires a FreeRTOS QEMU RISC-V fixture",
)
ISA = "riscv64" if SPEC.target == "qemu-virt-rv64" else "riscv32"


def _json_line(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"no JSON payload in output:\n{output}")


def _trace(session, target: str) -> dict:
    envelope = _json_line(session.run(f"gdr api backtrace {target} --json", timeout=30))
    assert envelope["schema_version"] == 3, envelope
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def _clear_breakpoints(session) -> None:
    session.run_python("import gdb\nfor bp in gdb.breakpoints() or (): bp.delete()")


def _hold_and_break(session, stop_expr: str, *, conditional: bool = True) -> None:
    session.run("set var gdr_irq_hold = 0")
    session.run("tbreak gdr_rv_spin_leaf")
    out = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in out, out
    session.run("set var gdr_irq_hold = 1")
    # Advance past the leaf prologue so ra/s0 are already on gdr_spin's stack.
    session.run("stepi 4")
    cond = " if pxCurrentTCB == gdr_ready_spin_task" if conditional else ""
    bp_out = session.run(f"tbreak {stop_expr}{cond}")
    assert "Temporary breakpoint" in bp_out, bp_out
    out = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in out, out


def _symbol_address(session, symbol: str) -> int:
    return int(
        _json_line(
            session.run_python(
                f'import gdb, json\nprint(json.dumps({{"addr": int(gdb.parse_and_eval("&{symbol}"))}}))'
            )
        )["addr"]
    )


def test_riscv_timer_isr_unwinds_across_isr_stack_to_multiframe_task(gdb_session):
    try:
        _hold_and_break(gdb_session, "xTaskIncrementTick")
        # Step past prologue so CFI on xISRStack has an initialized frame.
        gdb_session.run("stepi 4")
        raw = _json_line(
            gdb_session.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
mask = (1 << (64 if 'rv64' in gdb.current_progspace().filename else 32)) - 1
tcb = int(gdb.parse_and_eval("pxCurrentTCB"))
mcause = int(frame.read_register("mcause")) & mask
mepc = int(frame.read_register("mepc")) & mask
print(json.dumps({"tcb": tcb, "mcause": mcause, "mepc": mepc}))
"""
            )
        )
        data = _trace(gdb_session, "gdr_spin")
        assert data["architecture"] == ISA, data
        assert data["source"] == "current", data
        assert data["complete"] is True, data
        assert data["stop_reason"] == "task-exit", data

        frames = data["frames"]
        symbols = [frame["symbol"] or "" for frame in frames]
        assert "xTaskIncrementTick" in symbols[0], symbols
        assert any("gdr_rv_spin_leaf" in sym for sym in symbols), symbols
        assert any("gdr_rv_spin_mid" in sym for sym in symbols), symbols
        assert "gdr_ready_spin" in symbols[-1], symbols

        transitions = [frame for frame in frames if frame["method"] == "exception"]
        assert len(transitions) == 1, data
        assert transitions[0]["pc"] == raw["mepc"], (transitions, raw)
        assert "gdr_rv_spin_leaf" in (transitions[0]["symbol"] or ""), transitions
        evidence = " ".join(data["messages"])
        assert f"exception:{raw['mcause']:#x} -> task:{raw['tcb']:#x}" in evidence, data
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def test_riscv_raw_trap_entry_recovers_exact_interrupted_task_pc(gdb_session):
    try:
        trap_entry = _symbol_address(gdb_session, "freertos_risc_v_trap_handler")
        _hold_and_break(gdb_session, f"*{trap_entry:#x}")
        raw = _json_line(
            gdb_session.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
mask = (1 << (64 if 'rv64' in gdb.current_progspace().filename else 32)) - 1
mcause = int(frame.read_register("mcause")) & mask
mepc = int(frame.read_register("mepc")) & mask
tcb = int(gdb.parse_and_eval("pxCurrentTCB"))
print(json.dumps({"mepc": mepc, "mcause": mcause, "tcb": tcb, "pc": int(frame.pc()) & mask}))
"""
            )
        )
        data = _trace(gdb_session, "gdr_spin")
        assert data["complete"] is True, data
        assert data["stop_reason"] == "task-exit", data
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


def test_riscv_vtaskswitchcontext_fails_closed(gdb_session):
    gdb_session.run("tbreak vTaskSwitchContext")
    out = gdb_session.run("continue", timeout=30)
    assert "Temporary breakpoint" in out, out
    gdb_session.run("stepi 4")
    cur_name = gdb_session.run_python(
        'print(gdb.parse_and_eval("pxCurrentTCB->pcTaskName").string())'
    ).strip()
    data = _trace(gdb_session, cur_name)
    assert data["complete"] is False, data
    assert data["stop_reason"] == "scheduler-transition-unproven", data
    _clear_breakpoints(gdb_session)
