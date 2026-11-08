"""Real NVIC IRQ and nested-handler backtraces on verified M0/M3 fixtures.

The fixture arms board timers from a dedicated task, so every stop below is
a hardware exception entry with an architecturally written basic frame.  The
oracle reads the raw PSP/MSP frame and the linker-owned stack bounds through
GDB; it never derives the expected PC from GDR's unwinder.
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
        ("full", "mps2-an385", "10.3.1"),
        ("full", "microbit", "11.1.0"),
        ("fpu", "mps2-an386", "11.3.1"),
    ),
    reason="requires a FreeRTOS M-profile IRQ fixture lane",
)
_ISA = (
    "armv6-m"
    if SPEC.target == "microbit"
    else "armv7e-m"
    if SPEC.target == "mps2-an386"
    else "armv7-m"
)
_ABI = "aapcs32-vfp" if SPEC.variant == "fpu" else "aapcs32"

_EXCEPTION = 0x01000000
_T_BIT = 1 << 24
_ALIGN_BIT = 1 << 9


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


def _raw_state(session, frame_register: str) -> dict:
    return _json_line(
        session.run_python(
            f"""
import gdb, json

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {{address:#x}}", to_string=True).split(" in section ")[0].strip()

frame_base = int(gdb.parse_and_eval("${frame_register}"))
psp = int(gdb.parse_and_eval("$psp"))
msp = int(gdb.parse_and_eval("$msp"))
sp = int(gdb.parse_and_eval("$sp"))
xpsr = int(gdb.parse_and_eval("$xpsr"))
control = int(gdb.parse_and_eval("$control"))
tcb = gdb.parse_and_eval("pxCurrentTCB")
if tcb.type.code == gdb.TYPE_CODE_PTR:
    tcb = tcb.dereference()
words = [u32(frame_base + 4 * index) for index in range(8)]
frame_pc = words[6]
frame_lr = words[5]
frame_xpsr = words[7]
restored_sp = frame_base + 0x20 + (4 if frame_xpsr & (1 << 9) else 0)
stkalign = True if {_ISA!r} == "armv6-m" else bool(u32(0xE000ED14) & (1 << 9))
print(json.dumps({{
    "frame_base": frame_base,
    "psp": psp,
    "msp": msp,
    "sp": sp,
    "xpsr": xpsr,
    "control": control,
    "words": words,
    "frame_pc": frame_pc,
    "frame_pc_symbol": symbol(frame_pc),
    "frame_lr": frame_lr,
    "frame_lr_symbol": symbol(frame_lr),
    "frame_xpsr": frame_xpsr,
    "restored_sp": restored_sp,
    "stack_base": int(tcb["pxStack"]),
    "stack_high": int(tcb["pxEndOfStack"]) + 4,
    "irq_low": int(gdb.parse_and_eval("&__gdr_irq_stack_low")),
    "irq_high": int(gdb.parse_and_eval("&__gdr_irq_stack_top")),
    "stkalign": stkalign,
}}))
"""
        )
    )


def _backtrace(session) -> dict:
    data = _envelope(session.run("gdr api backtrace gdr_irq --json", timeout=30))
    assert data["task"] == "gdr_irq"
    assert data["architecture"] == _ISA and data["abi"] == _ABI
    assert data["source"] == "current" and data["bounds"] == "recorded"
    return data


def _exception_frames(data: dict) -> list[dict]:
    return [frame for frame in data["frames"] if frame["method"] == "exception"]


def _assert_task_frame(raw: dict, *, expected_symbol: str) -> None:
    assert raw["stkalign"]
    assert raw["frame_xpsr"] & _T_BIT
    assert raw["frame_xpsr"] & 0x1FF == 0
    assert expected_symbol in raw["frame_pc_symbol"], raw
    assert raw["stack_base"] <= raw["frame_base"] < raw["stack_high"], raw
    assert raw["stack_base"] <= raw["restored_sp"] <= raw["stack_high"], raw
    assert raw["irq_low"] < raw["irq_high"], raw
    assert raw["irq_low"] <= raw["msp"] <= raw["irq_high"], raw
    assert raw["sp"] == raw["msp"], raw
    assert raw["control"] & 0x2 == 0, raw


def test_single_irq_returns_the_exact_interrupted_task_frame(gdb_session):
    _stop_at(gdb_session, "gdr_irq_single_leaf")
    raw = _raw_state(gdb_session, "psp")
    _assert_task_frame(raw, expected_symbol="gdr_irq_wait")
    assert raw["frame_xpsr"] & _ALIGN_BIT == 0

    data = _backtrace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    symbols = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("gdr_irq_single_leaf" in name for name in symbols), symbols
    assert any("gdr_irq0_body" in name for name in symbols), symbols
    assert any("Timer0_Handler" in name for name in symbols), symbols
    transitions = _exception_frames(data)
    assert len(transitions) == 1, data
    assert transitions[0]["pc"] == raw["frame_pc"], data
    assert "gdr_irq_wait" in (transitions[0]["symbol"] or ""), data
    assert any("EXC_RETURN=0xfffffffd" in message for message in data["messages"])


def test_nested_irq_crosses_handler_then_task_with_two_exact_pcs(gdb_session):
    _stop_at(gdb_session, "Timer1_Handler")
    nested = _raw_state(gdb_session, "msp")
    task = _raw_state(gdb_session, "psp")
    _assert_task_frame(task, expected_symbol="gdr_irq_wait")
    assert nested["frame_xpsr"] & _T_BIT
    assert nested["frame_xpsr"] & 0x1FF == 0x18, nested
    assert "gdr_irq_nested_wait" in nested["frame_pc_symbol"], nested
    assert nested["frame_base"] == nested["msp"], nested
    assert nested["sp"] == nested["msp"], nested
    assert nested["irq_low"] <= nested["msp"] <= nested["irq_high"], nested

    data = _backtrace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    symbols = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("Timer1_Handler" in name for name in symbols), symbols
    assert any("gdr_irq_nested_wait" in name for name in symbols), symbols
    assert any("Timer0_Handler" in name for name in symbols), symbols
    assert any("gdr_irq_wait" in name for name in symbols), symbols
    transitions = _exception_frames(data)
    assert len(transitions) == 2, data
    assert transitions[0]["pc"] == nested["frame_pc"], data
    assert transitions[1]["pc"] == task["frame_pc"], data
    evidence = " ".join(data["messages"])
    assert "exception:25 -> exception:24" in evidence, data
    assert "exception:24 -> task:" in evidence, data
    assert "EXC_RETURN=0xfffffff1" in evidence, data
    assert "EXC_RETURN=0xfffffffd" in evidence, data


def test_handler_c_call_chain_is_ordinary_cfi_before_the_exception_boundary(
    gdb_session,
):
    _stop_at(gdb_session, "gdr_irq_nested_leaf")
    data = _backtrace(gdb_session)
    methods = [frame["method"] for frame in data["frames"]]
    symbols = [frame["symbol"] or "" for frame in data["frames"]]
    leaf = next(i for i, name in enumerate(symbols) if "gdr_irq_nested_leaf" in name)
    body = next(i for i, name in enumerate(symbols) if "gdr_irq1_body" in name)
    handler = next(i for i, name in enumerate(symbols) if "Timer1_Handler" in name)
    assert leaf < body < handler, symbols
    assert methods[body] in ("cfi", "ehabi"), data
    assert methods[handler] in ("cfi", "ehabi"), data
    assert methods[handler + 1] == "exception", data
    assert len(_exception_frames(data)) == 2, data


def test_switch_window_without_scheduler_profile_refuses_task_ownership(gdb_session):
    if SPEC.target != "mps2-an386":
        pytest.skip("requires the AN386 lane without compiled scheduler evidence")
    _stop_at(gdb_session, "PendSV_Handler")
    output = gdb_session.run_python(
        "import gdb, json\n"
        "tcb = gdb.parse_and_eval('pxCurrentTCB')\n"
        "print(json.dumps(tcb['pcTaskName'].string()))"
    )
    name = json.loads(output.strip().splitlines()[-1]).rstrip("\x00")
    data = _json_line(gdb_session.run(f"gdr api backtrace {name} --json", timeout=30))
    assert data["ok"] is True, data
    data = data["data"]
    assert not data["complete"], data
    assert data["stop_reason"] == "scheduler-transition-unproven", data
    assert data["frames"] == [], data


def test_fp_isr_grows_the_msp_frame_with_lazy_stacking(gdb_session):
    if SPEC.target != "mps2-an386":
        pytest.skip(
            "requires the AN386 FPU IRQ fixture "
            "(FP ISR over an FP-active thread: lazy-stacked extended frame)"
        )
    _stop_at(gdb_session, "gdr_irq_fp_leaf")
    raw = _raw_state(gdb_session, "psp")
    _assert_task_frame(raw, expected_symbol="gdr_irq_wait")
    fpccr = int(
        gdb_session.run("p/x *(unsigned int *)0xE000EF34").split("=")[1].strip(),
        16,
    )
    assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
    data = _backtrace(gdb_session)
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    transitions = _exception_frames(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["frame_pc"], data
    assert "gdr_irq_wait" in (transitions[0]["symbol"] or ""), data
    assert any("EXC_RETURN=0xffffffed" in message for message in data["messages"]), data
    assert any("extended" in message for message in data["messages"]), data


def test_stkalign_frame_retains_the_four_byte_padding(gdb_session):
    _stop_at(gdb_session, "gdr_irq_align_leaf")
    raw = _raw_state(gdb_session, "psp")
    _assert_task_frame(raw, expected_symbol="gdr_irq_misaligned_wait")
    assert raw["frame_xpsr"] & _ALIGN_BIT, raw
    assert raw["frame_base"] % 8 == 0, raw
    assert raw["restored_sp"] % 8 == 4, raw
    assert raw["restored_sp"] == raw["frame_base"] + 0x24, raw

    data = _backtrace(gdb_session)
    transitions = _exception_frames(data)
    assert len(transitions) == 1, data
    assert transitions[0]["pc"] == raw["frame_pc"], data
    assert "xpsr=0x" in " ".join(data["messages"])


def test_handler_entry_uses_the_vector_proof_without_a_cfi_padding_row(gdb_session):
    _stop_at(gdb_session, "Timer0_Handler")
    raw = _raw_state(gdb_session, "psp")
    _assert_task_frame(raw, expected_symbol="gdr_irq_wait")
    data = _backtrace(gdb_session)
    assert data["frames"][0]["symbol"] == "Timer0_Handler", data
    transitions = _exception_frames(data)
    assert len(transitions) == 1, data
    assert transitions[0]["pc"] == raw["frame_pc"], data
    assert data["complete"] and data["stop_reason"] == "task-exit", data


def _internal_reason(session, body: str) -> str:
    output = session.run_python(
        f"""
import freertos.backtrace_gdb as bt
from core.adapter import active
{body}
"""
    )
    for line in output.splitlines():
        if line.startswith("REASON="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(output)


def test_real_handler_without_linker_bounds_is_refused(gdb_session):
    _stop_at(gdb_session, "gdr_irq_single_leaf")
    reason = _internal_reason(
        gdb_session,
        """
original = bt._irq_stack_bounds
bt._irq_stack_bounds = lambda: None
try:
    report = bt.backtrace(active(), "gdr_irq")
finally:
    bt._irq_stack_bounds = original
print(f"REASON={report.stop_reason} frames={len(report.frames)}")
""",
    )
    assert reason.startswith("exception-stack-bounds-unavailable frames=0"), reason


@pytest.mark.parametrize(
    "option,value,reason",
    [
        ("max_exception_depth", 1, "exception-depth-exceeded"),
        ("max_exception_bytes", 32, "exception-read-budget-exceeded"),
    ],
)
def test_real_nested_stop_obeys_independent_budgets(gdb_session, option, value, reason):
    _stop_at(gdb_session, "Timer1_Handler")
    result = _internal_reason(
        gdb_session,
        f"""
original = bt.unwind_chain
def limited(seed, metadata, **kwargs):
    kwargs[{option!r}] = {value}
    return original(seed, metadata, **kwargs)
bt.unwind_chain = limited
try:
    report = bt.backtrace(active(), "gdr_irq")
finally:
    bt.unwind_chain = original
print(f"REASON={{report.stop_reason}} transitions={{len(report.transitions)}}")
""",
    )
    assert result.startswith(f"{reason} transitions=1"), result


def test_irq_backtrace_preserves_target_registers_and_stack_memory(gdb_session):
    _stop_at(gdb_session, "gdr_irq_single_leaf")
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
    return registers, memory, str(gdb.parse_and_eval("pxCurrentTCB")), int(gdb.parse_and_eval("xTickCount"))

events = []
def changed(event):
    events.append(type(event).__name__)
registries = [gdb.events.cont, gdb.events.memory_changed, gdb.events.register_changed]
for registry in registries:
    registry.connect(changed)
try:
    before = snapshot()
    report = bt.backtrace(active(), "gdr_irq")
    after = snapshot()
finally:
    for registry in registries:
        registry.disconnect(changed)
print(json.dumps({"unchanged": before == after, "complete": report.complete,
                  "transitions": len(report.transitions), "events": events}))
"""
        )
    )
    assert result == {
        "unchanged": True,
        "complete": True,
        "transitions": 1,
        "events": [],
    }


@pytest.mark.skipif(_ISA != "armv6-m", reason="Armv6-M fixed-vector port contract")
def test_m0_live_port_reads_cpuid_and_fixed_vectors_without_v7_registers(gdb_session):
    _stop_at(gdb_session, "Timer0_Handler")
    result = _json_line(
        gdb_session.run_python(
            """
import json
from core.adapter import active
import freertos.backtrace_gdb as bt
original = bt.read_bytes
reads = []
def guarded(address, size):
    assert address not in (0xE000E004, 0xE000ED08, 0xE000ED14), hex(address)
    reads.append((address, size))
    return original(address, size)
bt.read_bytes = guarded
try:
    report = bt.backtrace(active(), "gdr_irq")
    limit = bt._armv6m_port_limit()
finally:
    bt.read_bytes = original
print(json.dumps({"complete": report.complete, "limit": limit,
                  "cpuid": (0xE000ED00, 4) in reads, "vector": (24 * 4, 4) in reads,
                  "transitions": len(report.transitions)}))
"""
        )
    )
    assert result == {
        "complete": True,
        "limit": 47,
        "cpuid": True,
        "vector": True,
        "transitions": 1,
    }
