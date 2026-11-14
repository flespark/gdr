"""Deterministic synchronous-fault backtrace on the RT-Thread M3 port.

UsageFault stays disabled so an undefined instruction escalates to HardFault
(FORCED + UFSR UNDEFINSTR). The board fixture installs an
rt_exception_hook that advances the stacked PC past the faulting word and
returns RT_EOK, so each stop below is a fresh hardware fault entry and the
session stays healthy for the tests that follow.
"""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
ISA = {
    "mps2-an385": "armv7-m",
    "mps2-an386": "armv7e-m",
    "mps2-an505": "armv8-m.mainline",
    "microbit": "armv6-m",
}.get(SPEC.target, "")
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread"
    or SPEC.target not in {"mps2-an385", "mps2-an386", "mps2-an505"}
    or SPEC.version != "4.0.5"
    or SPEC.variant != "fault",
    reason="requires an RT-Thread M-profile fault fixture lane",
)


def _json(output):
    for line in output.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"missing fault evidence:\n{output}")


def _stop(session, name):
    session.run(f"tbreak {name}")
    output = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in output, output


def _stop_at_fault(session, leaf):
    """Pin one fault scenario: halt at its leaf, then at the entry it raises."""
    _stop(session, leaf)
    _stop(session, "HardFault_Handler")


def _frame(session):
    return _json(
        session.run_python(
            """
import json
frame = gdb.newest_frame()
base = int(frame.read_register('psp'))
words = [int.from_bytes(bytes(gdb.selected_inferior().read_memory(base + 4*i, 4)), 'little') for i in range(8)]
thread = gdb.lookup_static_symbol('gdr_fault_thread').value()
print(json.dumps({'base': base, 'words': words, 'pc': words[6], 'status': words[7],
                  'symbol': gdb.execute(f'info symbol {words[6]:#x}', to_string=True).split(' in section ')[0].strip(),
                  'live_ipsr': int(frame.read_register('xpsr')) & 0x1ff,
                  'bounds': [int(thread['stack_addr']), int(thread['stack_addr']) + int(thread['stack_size'])],
                  'cfsr': int.from_bytes(bytes(gdb.selected_inferior().read_memory(0xE000ED28, 4)), 'little'),
                  'hfsr': int.from_bytes(bytes(gdb.selected_inferior().read_memory(0xE000ED2C, 4)), 'little')}))
"""
        )
    )


def _trace(session):
    envelope = _json(session.run("gdr api backtrace gdr_fault --json", timeout=30))
    assert envelope["ok"], envelope
    data = envelope["data"]
    assert data["architecture"] == ISA and data["source"] == "current", data
    return data


def _transitions(data):
    return [frame for frame in data["frames"] if frame["method"] == "exception"]


# AN505 aliases the APB window at 0x50000000 into the non-secure view, so the
# M33 bus probe uses the unmapped external-device hole instead.
EXPECTED_BFAR = 0xA0000000 if SPEC.target == "mps2-an505" else 0x50000000


def test_precise_usagefault_escalation_is_synchronous(gdb_session):
    _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
    raw = _frame(gdb_session)
    assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
    assert raw["live_ipsr"] == 3, raw
    assert raw["bounds"][0] <= raw["base"] < raw["bounds"][0] + raw["bounds"][1], raw
    assert raw["cfsr"] & 0x00010000, raw
    assert raw["hfsr"] & 0x40000000, raw
    data = _trace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
    assert "gdr_fault" in transitions[0]["symbol"], data
    assert any("UNDEFINSTR" in message for message in data["messages"]), data
    assert any("CFSR=" in message for message in data["messages"]), data


def test_precise_bus_fault_reports_the_faulting_data_address(gdb_session):
    _stop_at_fault(gdb_session, "gdr_fault_bus_stop")
    raw = _frame(gdb_session)
    bfar = int(
        gdb_session.run("p/x *(unsigned int *)0xE000ED38").split("=")[1].strip(),
        16,
    )
    assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
    assert raw["live_ipsr"] == 3, raw
    assert "gdr_fault_bus_leaf" in raw["symbol"], raw
    assert raw["cfsr"] & 0x00000200 and raw["cfsr"] & 0x00008000, raw
    assert bfar == EXPECTED_BFAR, hex(bfar)
    data = _trace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
    assert any("PRECISERR" in m for m in data["messages"]), data
    assert any(f"BFAR={EXPECTED_BFAR:#010x}" in m for m in data["messages"]), data
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data


@pytest.mark.skipif(
    SPEC.target == "mps2-an505",
    reason="AN505 MPU-blocked loads surface as precise bus faults on QEMU; DACCVIOL live proof stays on the M3 lanes",
)
def test_precise_memmanage_escalation_reports_the_blocked_region(gdb_session):
    _stop_at_fault(gdb_session, "gdr_fault_mm_stop")
    raw = _frame(gdb_session)
    mmfar = int(
        gdb_session.run("p/x *(unsigned int *)0xE000ED34").split("=")[1].strip(),
        16,
    )
    assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
    assert raw["live_ipsr"] == 3, raw
    assert "gdr_fault_memmanage_leaf" in raw["symbol"], raw
    assert raw["cfsr"] & 0x00000002 and raw["cfsr"] & 0x00000080, raw
    assert raw["hfsr"] & 0x40000000, raw
    assert mmfar == 0x60000000, hex(mmfar)
    data = _trace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
    assert any("escalated-MemManage" in m for m in data["messages"]), data
    assert any("DACCVIOL" in m for m in data["messages"]), data
    assert any("MMFAR=0x60000000" in m for m in data["messages"]), data
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data


def test_fault_backtrace_preserves_target_registers_and_stacks(gdb_session):
    _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
    code = """
import hashlib, json
frame = gdb.newest_frame()
registers = {name: int(frame.read_register(name)) for name in ('sp','lr','psp','msp','xpsr','control')}
thread = gdb.lookup_static_symbol('gdr_fault_thread').value()
spans = [(int(thread['stack_addr']), int(thread['stack_size'])),
         (int(gdb.parse_and_eval('&__StackLimit')), int(gdb.parse_and_eval('&__StackTop')) - int(gdb.parse_and_eval('&__StackLimit')))]
print(json.dumps({'registers': registers, 'pc': int(frame.pc()),
                  'current': str(gdb.parse_and_eval('rt_current_thread')),
                  'tick': int(gdb.parse_and_eval('rt_tick')),
                  'hashes': [hashlib.sha256(bytes(gdb.selected_inferior().read_memory(base, size))).hexdigest() for base,size in spans]}))
"""
    before = _json(gdb_session.run_python(code))
    data = _trace(gdb_session)
    after = _json(gdb_session.run_python(code))
    assert before == after
    assert data["stop_reason"] == "synchronous-fault" and data["complete"], data


def test_fault_backtrace_inside_c_handler_and_hook_unwinds_trampoline_and_psp_frame(
    gdb_session,
):
    _stop(gdb_session, "gdr_fault_ud_stop")
    _stop(gdb_session, "rt_hw_hard_fault_exception")
    in_c_handler = _trace(gdb_session)
    assert (
        in_c_handler["complete"] and in_c_handler["stop_reason"] == "synchronous-fault"
    ), in_c_handler
    c_symbols = [frame["symbol"] or "" for frame in in_c_handler["frames"]]
    assert "rt_hw_hard_fault_exception" in c_symbols[0], c_symbols
    assert any(
        sym.startswith(("HardFault_Handler", "_update_done", "update_done"))
        for sym in c_symbols
    ), c_symbols
    assert in_c_handler["frames"][1]["method"] in ("cfi", "ehabi"), in_c_handler
    assert in_c_handler["frames"][2]["method"] == "exception", in_c_handler
    assert any("gdr_fault_undefinstr_leaf" in sym for sym in c_symbols), c_symbols
    assert any("gdr_fault_trigger" in sym for sym in c_symbols), c_symbols
    assert any(
        "rtthread-hardfault-software-frame=" in msg for msg in in_c_handler["messages"]
    ), in_c_handler

    _stop(gdb_session, "gdr_fault_hook")
    in_hook = _trace(gdb_session)
    assert in_hook["complete"] and in_hook["stop_reason"] == "synchronous-fault", (
        in_hook
    )
    hook_symbols = [frame["symbol"] or "" for frame in in_hook["frames"]]
    assert "gdr_fault_hook" in hook_symbols[0], hook_symbols
    assert any("rt_hw_hard_fault_exception" in sym for sym in hook_symbols), (
        hook_symbols
    )
    assert any(
        sym.startswith(("HardFault_Handler", "_update_done", "update_done"))
        for sym in hook_symbols
    ), hook_symbols
    assert in_hook["frames"][2]["method"] in ("cfi", "ehabi"), in_hook
    assert in_hook["frames"][3]["method"] == "exception", in_hook
    assert any("gdr_fault_undefinstr_leaf" in sym for sym in hook_symbols), hook_symbols
    assert any("gdr_fault_trigger" in sym for sym in hook_symbols), hook_symbols
