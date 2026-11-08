"""Raw hardware-frame oracles for RT-Thread external IRQ cross-stack unwinds."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread"
    or SPEC.target not in {"mps2-an385", "mps2-an386", "microbit"}
    or SPEC.version != "4.0.5"
    or (SPEC.target == "mps2-an386" and SPEC.variant != "fpu"),
    reason="requires an RT-Thread Arm M-profile IRQ fixture",
)
ISA = (
    "armv6-m"
    if SPEC.target == "microbit"
    else ("armv7e-m" if SPEC.target == "mps2-an386" else "armv7-m")
)


def _json(output):
    for line in output.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"missing IRQ evidence:\n{output}")


def _stop(session, name):
    session.run(f"tbreak {name}")
    output = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in output, output


def _frame(session, register):
    return _json(
        session.run_python(f"""
import json
frame = gdb.newest_frame()
base = int(frame.read_register('{register}'))
words = [int.from_bytes(bytes(gdb.selected_inferior().read_memory(base + 4*i, 4)), 'little') for i in range(8)]
thread = gdb.lookup_static_symbol('gdr_irq_thread').value()
print(json.dumps({{'base': base, 'words': words, 'pc': words[6], 'status': words[7],
                  'sp': base + 32 + (4 if words[7] & (1 << 9) else 0),
                  'bounds': [int(thread['stack_addr']), int(thread['stack_addr']) + int(thread['stack_size'])],
                  'live_ipsr': int(frame.read_register('xpsr')) & 0x1ff}}))
""")
    )


def _trace(session):
    envelope = _json(session.run("gdr api backtrace gdr_irq --json", timeout=30))
    assert envelope["ok"], envelope
    data = envelope["data"]
    assert data["architecture"] == ISA and data["source"] == "current", data
    return data


def _transitions(data):
    return [frame for frame in data["frames"] if frame["method"] == "exception"]


def test_single_irq_returns_to_the_exact_task_pc(gdb_session):
    _stop(gdb_session, "gdr_irq_single_stop")
    raw = _frame(gdb_session, "psp")
    assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
    assert raw["bounds"][0] <= raw["base"] < raw["sp"] <= raw["bounds"][1]
    data = _trace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
    assert "gdr_irq_wait" in transitions[0]["symbol"], data
    assert any("EXC_RETURN=0xfffffffd" in message for message in data["messages"]), data


@pytest.mark.skipif(
    SPEC.target != "mps2-an386",
    reason="requires the AN386 FPU IRQ fixture (FP ISR over an FP-active thread: lazy-stacked extended frame)",
)
def test_fp_isr_grows_the_msp_frame_with_lazy_stacking(gdb_session):
    _stop(gdb_session, "gdr_irq_fp_stop")
    raw = _frame(gdb_session, "psp")
    assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
    assert raw["bounds"][0] <= raw["base"] < raw["sp"] <= raw["bounds"][1]
    fpccr = int(
        gdb_session.run("p/x *(unsigned int *)0xE000EF34").split("=")[1].strip(), 16
    )
    assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
    data = _trace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
    assert "gdr_irq" in transitions[0]["symbol"], data
    assert any("EXC_RETURN=0xffffffed" in message for message in data["messages"]), data
    assert any("extended" in message for message in data["messages"]), data


def test_switch_request_window_refuses_task_ownership_live(gdb_session):
    gdb_session.run("tbreak PendSV_Handler")
    gdb_session.run("continue", timeout=60)
    data = _json(gdb_session.run("gdr api backtrace gdr_irq --json", timeout=30))[
        "data"
    ]
    assert not data["complete"], data
    assert data["stop_reason"] == "scheduler-transition-unproven", data
    assert data["frames"] == [], data


def test_nested_handler_entry_recovers_both_hardware_frames(gdb_session):
    _stop(gdb_session, "*Timer1_Handler")
    nested = _frame(gdb_session, "msp")
    task = _frame(gdb_session, "psp")
    assert nested["live_ipsr"] == 25 and nested["status"] & 0x1FF == 24
    assert task["status"] & 0x1FF == 0
    data = _trace(gdb_session)
    transitions = _transitions(data)
    assert [frame["pc"] for frame in transitions] == [nested["pc"], task["pc"]], data
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    assert "gdr_irq_nested_wait" in transitions[0]["symbol"], data
    assert any(
        "exception:25 -> exception:24" in message for message in data["messages"]
    ), data


def test_alignment_padding_is_restored_without_custom_fixture_cfi(gdb_session):
    _stop(gdb_session, "gdr_irq_align_stop")
    raw = _frame(gdb_session, "psp")
    assert raw["status"] & (1 << 9) and raw["base"] % 8 == 0
    assert raw["sp"] == raw["base"] + 36 and raw["sp"] % 8 == 4
    data = _trace(gdb_session)
    assert len(_transitions(data)) == 1 and _transitions(data)[0]["pc"] == raw["pc"]
    assert data["complete"] and data["stop_reason"] == "task-exit", data


def test_irq_queries_do_not_change_registers_stacks_or_scheduler_memory(gdb_session):
    _stop(gdb_session, "gdr_irq_nested_stop")
    code = """
import hashlib, json
frame = gdb.newest_frame()
registers = {name: int(frame.read_register(name)) for name in ('sp','lr','psp','msp','xpsr','control')}
thread = gdb.lookup_static_symbol('gdr_irq_thread').value()
spans = [(int(thread['stack_addr']), int(thread['stack_size'])),
         (int(gdb.parse_and_eval('&__StackLimit')), int(gdb.parse_and_eval('&__StackTop')) - int(gdb.parse_and_eval('&__StackLimit')))]
print(json.dumps({'registers': registers, 'pc': int(frame.pc()),
                  'current': int(gdb.parse_and_eval('rt_current_thread')),
                  'tick': int(gdb.parse_and_eval('rt_tick')),
                  'pending': int(gdb.parse_and_eval('rt_thread_switch_interrupt_flag')),
                  'hashes': [hashlib.sha256(bytes(gdb.selected_inferior().read_memory(base, size))).hexdigest() for base,size in spans]}))
"""
    before = _json(gdb_session.run_python(code))
    data = _trace(gdb_session)
    after = _json(gdb_session.run_python(code))
    assert before == after and before["pending"] == 0
    assert len(_transitions(data)) == 2 and data["complete"], data


def test_pending_switch_refuses_task_ownership_without_writing_the_target(gdb_session):
    _stop(gdb_session, "gdr_irq_single_stop")
    result = _json(
        gdb_session.run_python("""
import json
import rtthread.backtrace_gdb as bridge
original = bridge.read_int
try:
    bridge.read_int = lambda value: 1
    print(gdb.execute('gdr api backtrace gdr_irq --json', to_string=True))
finally:
    bridge.read_int = original
""")
    )
    assert result["data"]["stop_reason"] == "scheduler-transition-unproven", result
    assert result["data"]["frames"] == []


def test_partial_msp_override_cannot_enable_an_unbounded_walk(gdb_session):
    _stop(gdb_session, "gdr_irq_single_stop")
    result = _json(
        gdb_session.run_python("""
import rtthread.backtrace_gdb as bridge
original = bridge.linker_symbol_address
try:
    bridge.linker_symbol_address = lambda name: 0x203fc000 if name == '__gdr_irq_stack_low' else None
    print(gdb.execute('gdr api backtrace gdr_irq --json', to_string=True))
finally:
    bridge.linker_symbol_address = original
""")
    )
    assert result["data"]["stop_reason"] == "exception-stack-bounds-unavailable", result
    assert result["data"]["frames"] == []
