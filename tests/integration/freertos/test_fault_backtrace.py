"""Deterministic synchronous-fault backtraces on verified M3/M0 fixtures.

The fault variant runs a low-priority task that repeatedly executes an
undefined instruction. UsageFault stays disabled so the M3 fault escalates
to HardFault (FORCED + UFSR UNDEFINSTR); the M0 fault has no status
registers at all. The board HardFault handler advances the stacked PC past
the faulting word and returns, so each stop below is a fresh hardware fault
entry and the session stays healthy for the tests that follow.
"""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos"
    or (
        SPEC.variant,
        SPEC.target,
        SPEC.version,
    )
    not in (
        ("fault", "mps2-an385", "10.3.1"),
        ("fault", "mps2-an386", "11.3.1"),
        ("fault", "microbit", "11.1.0"),
        ("fault", "mps2-an505", "11.3.1"),
    ),
    reason="requires a FreeRTOS M-profile fault fixture lane",
)
_ISA = (
    "armv6-m"
    if SPEC.target == "microbit"
    else "armv7e-m"
    if SPEC.target == "mps2-an386"
    else "armv8-m.mainline"
    if SPEC.target == "mps2-an505"
    else "armv7-m"
)
_T_BIT = 1 << 24


def _json_line(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"no JSON payload in output:\n{output}")


def _envelope(output: str) -> dict:
    envelope = _json_line(output)
    assert envelope["schema_version"] == 3, envelope
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def _stop_at(session, symbol: str) -> None:
    session.run(f"tbreak {symbol}")
    output = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in output, output


def _stop_at_fault(session, leaf: str) -> None:
    """Pin one fault scenario: halt at its leaf, then at the entry it raises."""
    _stop_at(session, leaf)
    _stop_at(session, "HardFault_Handler")


def _raw_fault_frame(session) -> dict:
    return _json_line(
        session.run_python(
            """
import gdb, json

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {address:#x}", to_string=True).split(" in section ")[0].strip()

psp = int(gdb.parse_and_eval("$psp"))
msp = int(gdb.parse_and_eval("$msp"))
xpsr = int(gdb.parse_and_eval("$xpsr"))
words = [u32(psp + 4 * index) for index in range(8)]
tcb = gdb.parse_and_eval("pxCurrentTCB")
if tcb.type.code == gdb.TYPE_CODE_PTR:
    tcb = tcb.dereference()
print(json.dumps({
    "psp": psp,
    "msp": msp,
    "xpsr": xpsr,
    "words": words,
    "frame_pc": words[6],
    "frame_pc_symbol": symbol(words[6]),
    "frame_xpsr": words[7],
    "stack_base": int(tcb["pxStack"]),
    "stack_high": int(tcb["pxEndOfStack"]) + 4,
    "cfsr": u32(0xE000ED28),
    "hfsr": u32(0xE000ED2C),
}))
"""
        )
    )


def _backtrace(session) -> dict:
    data = _envelope(session.run("gdr api backtrace gdr_fault --json", timeout=30))
    assert data["task"] == "gdr_fault"
    assert data["architecture"] == _ISA, data
    assert data["source"] == "current" and data["bounds"] == "recorded"
    return data


_PRECISE_ISAS = ("armv7-m", "armv7e-m", "armv8-m.mainline")
_MM_ISAS = ("armv7-m", "armv7e-m")
_BUS_FAULT_ADDR = 0xA0000000 if SPEC.target == "mps2-an505" else 0x50000000


@pytest.mark.skipif(_ISA not in _PRECISE_ISAS, reason="M3/M4 precise escalation only")
def test_precise_usagefault_escalation_is_synchronous(gdb_session):
    _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
    raw = _raw_fault_frame(gdb_session)
    assert raw["frame_xpsr"] & _T_BIT
    assert raw["frame_xpsr"] & 0x1FF == 0, raw
    assert raw["xpsr"] & 0x1FF == 3, raw
    assert "gdr_fault_undefinstr_leaf" in raw["frame_pc_symbol"], raw
    assert raw["stack_base"] <= raw["psp"] < raw["stack_high"], raw
    assert raw["cfsr"] & 0x00010000, raw
    assert raw["hfsr"] & 0x40000000, raw

    data = _backtrace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
    evidence = " ".join(data["messages"])
    assert "synchronous fault" in evidence, data
    assert "UNDEFINSTR" in evidence, data
    assert "CFSR=" in evidence and "HFSR=" in evidence, data
    transitions = [f for f in data["frames"] if f["method"] == "exception"]
    assert len(transitions) == 1, data
    assert transitions[0]["pc"] == raw["frame_pc"], data


@pytest.mark.skipif(_ISA not in _MM_ISAS, reason="M3/M4 MemManage lane only")
def test_precise_memmanage_fault_reports_the_blocked_region(gdb_session):
    _stop_at(gdb_session, "gdr_fault_mm_stop")
    _stop_at(gdb_session, "MemManage_Handler")
    raw = _raw_fault_frame(gdb_session)
    mmfar = int(
        gdb_session.run("p/x *(unsigned int *)0xE000ED34").split("=")[1].strip(),
        16,
    )
    assert raw["frame_xpsr"] & _T_BIT
    assert raw["frame_xpsr"] & 0x1FF == 0, raw
    assert raw["xpsr"] & 0x1FF == 4, raw
    assert "gdr_fault_memmanage_leaf" in raw["frame_pc_symbol"], raw
    assert raw["cfsr"] & 0x00000002 and raw["cfsr"] & 0x00000080, raw
    assert mmfar == 0x60000000, hex(mmfar)

    data = _backtrace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
    evidence = " ".join(data["messages"])
    assert "MemManage" in evidence and "DACCVIOL" in evidence, data
    assert "MMFAR=0x60000000" in evidence, data
    transitions = [f for f in data["frames"] if f["method"] == "exception"]
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["frame_pc"], data


@pytest.mark.skipif(_ISA not in _PRECISE_ISAS, reason="M3/M4 precise lane only")
def test_precise_bus_fault_reports_the_faulting_data_address(gdb_session):
    _stop_at_fault(gdb_session, "gdr_fault_bus_stop")
    raw = _raw_fault_frame(gdb_session)
    bfar = int(
        gdb_session.run("p/x *(unsigned int *)0xE000ED38").split("=")[1].strip(),
        16,
    )
    assert raw["frame_xpsr"] & _T_BIT
    assert raw["frame_xpsr"] & 0x1FF == 0, raw
    assert raw["xpsr"] & 0x1FF == 3, raw
    assert "gdr_fault_bus_leaf" in raw["frame_pc_symbol"], raw
    assert raw["cfsr"] & 0x00000200 and raw["cfsr"] & 0x00008000, raw
    assert bfar == _BUS_FAULT_ADDR, hex(bfar)

    data = _backtrace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
    evidence = " ".join(data["messages"])
    assert "PRECISERR" in evidence and f"BFAR={_BUS_FAULT_ADDR:#x}" in evidence, data
    transitions = [f for f in data["frames"] if f["method"] == "exception"]
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["frame_pc"], data


@pytest.mark.skipif(_ISA != "armv6-m", reason="M0 has no fault status registers")
def test_m0_hardfault_without_cfsr_is_imprecise(gdb_session):
    _stop_at(gdb_session, "HardFault_Handler")
    data = _envelope(gdb_session.run("gdr api backtrace gdr_fault --json", timeout=30))
    assert data["task"] == "gdr_fault", data
    assert data["architecture"] == _ISA, data
    assert not data["complete"], data
    assert data["stop_reason"] == "fault-frame-imprecise", data
    assert data["frames"] == [], data


@pytest.mark.skipif(_ISA not in _PRECISE_ISAS, reason="M3/M4 precise lane only")
def test_fault_backtrace_preserves_target_registers_and_stack_memory(gdb_session):
    _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
    result = _json_line(
        gdb_session.run_python(
            """
import gdb, hashlib, json
from core.adapter import active
import freertos.backtrace_gdb as bt

def snapshot():
    frame = gdb.newest_frame()
    registers = [int(frame.read_register(name)) for name in
                 [*(f"r{i}" for i in range(13)), "sp", "lr", "pc",
                  "msp", "psp", "xpsr", "control", "primask"]]
    task = gdb.parse_and_eval("pxCurrentTCB").dereference()
    spans = [(int(task["pxStack"]), int(task["pxEndOfStack"]) + 4),
             (int(gdb.parse_and_eval("&__gdr_irq_stack_low")),
              int(gdb.parse_and_eval("&__gdr_irq_stack_top")))]
    memory = [hashlib.sha256(bytes(gdb.selected_inferior().read_memory(low, high-low))).hexdigest()
              for low, high in spans]
    return registers, memory

events = []
for registry in (gdb.events.cont, gdb.events.memory_changed, gdb.events.register_changed):
    registry.connect(events.append)
try:
    before = snapshot()
    report = bt.backtrace(active(), "gdr_fault")
    after = snapshot()
finally:
    for registry in (gdb.events.cont, gdb.events.memory_changed, gdb.events.register_changed):
        registry.disconnect(events.append)
print(json.dumps({"unchanged": before == after, "complete": report.complete,
                  "stop": report.stop_reason, "events": [type(e).__name__ for e in events]}))
"""
        )
    )
    assert result["unchanged"] is True, result
    assert result["stop"] == "synchronous-fault", result
    assert result["events"] == [], result


@pytest.mark.skipif(_ISA not in _PRECISE_ISAS, reason="M3/M4 precise lane only")
def test_fault_backtrace_inside_handler_callee_unwinds_msp_and_psp_frames(gdb_session):
    _stop_at(gdb_session, "gdr_fault_ud_stop")
    _stop_at(gdb_session, "gdr_fault_frame")
    hf_data = _backtrace(gdb_session)
    assert hf_data["complete"] and hf_data["stop_reason"] == "synchronous-fault", (
        hf_data
    )
    hf_symbols = [frame["symbol"] or "" for frame in hf_data["frames"]]
    assert "gdr_fault_frame" in hf_symbols[0], hf_symbols
    assert "HardFault_Handler" in hf_symbols[1], hf_symbols
    assert hf_data["frames"][2]["method"] == "exception", hf_data
    assert "gdr_fault_undefinstr_leaf" in hf_symbols[2], hf_symbols
    assert any("gdr_fault_task" in sym for sym in hf_symbols), hf_symbols

    if _ISA in _MM_ISAS:
        _stop_at(gdb_session, "gdr_fault_mm_stop")
        _stop_at(gdb_session, "MemManage_Handler")
        _stop_at(gdb_session, "gdr_fault_frame")
        mm_data = _backtrace(gdb_session)
        assert mm_data["complete"] and mm_data["stop_reason"] == "synchronous-fault", (
            mm_data
        )
        mm_symbols = [frame["symbol"] or "" for frame in mm_data["frames"]]
        assert "gdr_fault_frame" in mm_symbols[0], mm_symbols
        assert "MemManage_Handler" in mm_symbols[1], mm_symbols
        assert mm_data["frames"][2]["method"] == "exception", mm_data
        assert "gdr_fault_memmanage_leaf" in mm_symbols[2], mm_symbols
        assert any("gdr_fault_task" in sym for sym in mm_symbols), mm_symbols
