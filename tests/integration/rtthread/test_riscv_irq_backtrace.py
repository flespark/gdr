"""Live QEMU RISC-V M-mode trap cross-stack backtrace tests for RT-Thread."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread" or SPEC.target not in {"rv32", "rv64"},
    reason="requires an RT-Thread QEMU RISC-V fixture",
)
ISA = "riscv64" if SPEC.target == "rv64" else "riscv32"


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


def _hold_and_break(session, stop_symbol: str) -> None:
    session.run("set var gdr_irq_hold = 0")
    session.run("tbreak gdr_bt_live_leaf")
    out = session.run("continue", timeout=15)
    assert "Temporary breakpoint" in out, out
    session.run("set var gdr_irq_hold = 1")
    session.run("stepi 4")
    bp_out = session.run(
        f"tbreak {stop_symbol} if rt_current_thread == &worker3_thread"
    )
    assert "Temporary breakpoint" in bp_out, bp_out
    out = session.run("continue", timeout=15)
    assert "Temporary breakpoint" in out, out


def _symbol_address(session, symbol: str) -> int:
    return int(
        _json_line(
            session.run_python(
                f'import gdb, json\nprint(json.dumps({{"addr": int(gdb.parse_and_eval("&{symbol}"))}}))'
            )
        )["addr"]
    )


def test_riscv_trap_callee_unwinds_to_multiframe_worker_task(gdb_session):
    try:
        # rt_tick_increase is called from handle_trap before any switch completes,
        # so the ISR stack has 2 C frames (rt_tick_increase -> handle_trap).
        _hold_and_break(gdb_session, "rt_tick_increase")
        gdb_session.run("stepi 4")
        raw = _json_line(
            gdb_session.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
mask = (1 << (64 if gdb.lookup_type('rt_ubase_t').sizeof == 8 else 32)) - 1
print(json.dumps({
    "mcause": int(frame.read_register("mcause")) & mask,
    "mepc": int(frame.read_register("mepc")) & mask,
    "nest": int(gdb.parse_and_eval("rt_interrupt_nest")),
    "switch_flag": int(gdb.parse_and_eval("rt_thread_switch_interrupt_flag")),
    "thread": int(gdb.parse_and_eval("&worker3_thread")),
}))
"""
            )
        )
        assert raw["nest"] == 1 and raw["switch_flag"] == 0, raw

        data = _trace(gdb_session, "worker3")
        assert data["architecture"] == ISA, data
        assert data["source"] == "current", data
        assert data["stop_reason"] == "task-exit", (
            data["stop_reason"],
            data["messages"],
            data["frames"],
        )
        assert data["complete"] is True, data

        frames = data["frames"]
        symbols = [frame["symbol"] or "" for frame in frames]
        assert any("rt_tick_increase" in sym for sym in symbols), symbols
        assert any("handle_trap" in sym for sym in symbols), symbols
        assert any("gdr_bt_live_leaf" in sym for sym in symbols), symbols
        assert any("gdr_bt_live_middle" in sym for sym in symbols), symbols
        assert any("worker3_entry" in sym for sym in symbols), symbols
        assert "thread_exit" in symbols[-1], symbols

        transitions = [frame for frame in frames if frame["method"] == "exception"]
        assert len(transitions) == 1, data
        assert transitions[0]["pc"] == raw["mepc"], (transitions, raw)
        assert "gdr_bt_live_leaf" in (transitions[0]["symbol"] or ""), transitions
        evidence = " ".join(data["messages"])
        assert f"exception:{raw['mcause']:#x} -> task:{raw['thread']:#x}" in evidence, (
            data
        )
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def test_riscv_raw_trap_entry_recovers_worker_stack(gdb_session):
    try:
        trap_entry = _symbol_address(gdb_session, "trap_entry")
        _hold_and_break(gdb_session, f"*{trap_entry:#x}")
        raw = _json_line(
            gdb_session.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
mask = (1 << (64 if gdb.lookup_type('rt_ubase_t').sizeof == 8 else 32)) - 1
print(json.dumps({"mepc": int(frame.read_register("mepc")) & mask}))
"""
            )
        )
        data = _trace(gdb_session, "worker3")
        assert data["stop_reason"] == "task-exit", (
            data["stop_reason"],
            data["messages"],
            data["frames"],
        )
        assert data["complete"] is True, data
        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        assert "trap_entry" in symbols[0], symbols
        assert data["frames"][1]["method"] == "exception", data
        assert data["frames"][1]["pc"] == raw["mepc"], (data, raw)
        assert any("gdr_bt_live_leaf" in sym for sym in symbols[1:]), symbols
        assert any("gdr_bt_live_middle" in sym for sym in symbols[1:]), symbols
        assert "thread_exit" in symbols[-1], symbols
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def test_riscv_trap_epilogue_switch_region_fails_closed(gdb_session):
    epilogue_pc = int(
        _json_line(
            gdb_session.run_python(
                """
import json
from core.unwind_meta import ElfImage
from rtthread.stack_frame_profile import analyze_riscv_trap_profile
elf = ElfImage.from_path(gdb.current_progspace().filename)
span = analyze_riscv_trap_profile(elf)
assert span is not None
print(json.dumps({"pc": span.call_end}))
"""
            )
        )["pc"]
    )
    try:
        _hold_and_break(gdb_session, f"*{epilogue_pc:#x}")
        data = _trace(gdb_session, "worker3")
        assert data["complete"] is False, data
        assert data["stop_reason"] == "scheduler-transition-unproven", data
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")
