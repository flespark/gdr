"""Closed-loop task, IRQ, scheduler, fault, FPU, and RISC-V backtrace tests."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path

import pytest

from core.elf import ElfImage
from freertos.backtrace.arm_scheduler_switch import (
    SchedulerProfile,
    build_profile,
)
from freertos.backtrace.riscv_scheduler_switch import (
    RiscvSchedulerProfile,
    build_riscv_profile,
)
from tests.support.freertos_backtrace_oracles import (
    compiled_sched_oracle,
    current_task_name,
    envelope_data,
    frame_at,
    json_line,
    m0_fixed_vector_probe,
    raw_fault_frame,
    raw_irq_state,
    read_u32_words,
    riscv_isr_yield_owner_probe,
    riscv_trap_state,
    riscv_vectored_mtvec_probe,
    sched_state,
    scheduler_profile_override,
    systick_probe,
    task_top_of_stack,
    verify_readonly_backtrace,
)
from tests.support.freertos_fixture_profiles import get_freertos_test_profile
from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile
from tests.support.gdb_process import has_embedded_python
from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession, check_tools

SPEC = load_integration_spec()
PROFILE = (
    get_freertos_test_profile(SPEC.variant, SPEC.version, SPEC.target)
    if SPEC.rtos == "freertos"
    else None
)
GDR_ROOT = Path(__file__).resolve().parents[3]

_ISA_BY_TARGET = {
    "microbit": "armv6-m",
    "mps2-an385": "armv7-m",
    "mps2-an386": "armv7e-m",
    "mps2-an505": "armv8-m.mainline",
    "mps2-an521": "armv8-m.mainline",
    "qemu-virt-rv32": "riscv32",
    "qemu-virt-rv64": "riscv64",
}
_ISA = _ISA_BY_TARGET.get(SPEC.target, "")
_XLEN = 64 if _ISA == "riscv64" else 32
_M_FULL_TARGETS = {"microbit", "mps2-an385", "mps2-an386", "mps2-an505"}
_HAS_FULL_BACKTRACE = SPEC.target in _M_FULL_TARGETS and (
    (SPEC.target == "mps2-an386" and SPEC.variant in {"fpu", "soft", "softfp"})
    or (SPEC.target == "mps2-an505" and SPEC.variant in {"full", "fpu"})
    or (SPEC.target not in {"mps2-an386", "mps2-an505"} and SPEC.variant == "full")
)
_ABI = "aapcs32-vfp" if SPEC.variant == "fpu" else "aapcs32"
_TIMER0_EXC = 19 if SPEC.target == "mps2-an505" else 24
_TIMER1_EXC = 20 if SPEC.target == "mps2-an505" else 25
_T_BIT = 1 << 24
_ALIGN_BIT = 1 << 9
_PRECISE_ISAS = ("armv7-m", "armv7e-m", "armv8-m.mainline")
_MM_ISAS = ("armv7-m", "armv7e-m")
_BUS_FAULT_ADDR = 0xA0000000 if SPEC.target == "mps2-an505" else 0x50000000

_PORT_SPECS = {
    "mps2-an385": ("arm-cm3", 0x20, 0x00, False),
    "mps2-an386": (
        ("arm-cm3", 0x20, 0x00, False)
        if SPEC.variant == "soft"
        else ("arm-cm4f", 0x24, 0x00, False)
    ),
    "microbit": ("arm-cm0", 0x24, 0x04, True),
    "mps2-an505": (
        ("arm-cm33-fpu", 0x28, 0x08, True)
        if SPEC.variant in {"fpu", "softfp"}
        else ("arm-cm33", 0x28, 0x08, True)
    ),
}
_PORT, _SW_SIZE, _R4_OFF, _EXC_RET_IN_R3 = _PORT_SPECS.get(
    SPEC.target, _PORT_SPECS["mps2-an385"]
)
_PROFILE_CACHE: SchedulerProfile | None = None
_RV_PROFILE_CACHE: RiscvSchedulerProfile | None = None

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos" or SPEC.variant == "snapshot",
    reason="requires a live FreeRTOS QEMU profile",
)


def _recorded_bounds() -> bool:
    return PROFILE is not None and PROFILE.high_water_source == "pxEndOfStack"


def _irq_skip(*, fpu_only: bool = False):
    return pytest.mark.skipif(
        not _HAS_FULL_BACKTRACE or (fpu_only and SPEC.variant not in {"fpu", "softfp"}),
        reason="requires a FreeRTOS M-profile full/fpu IRQ fixture lane",
    )


def _sched_skip():
    return pytest.mark.skipif(
        not _HAS_FULL_BACKTRACE,
        reason="requires a FreeRTOS M-profile full/fpu scheduler fixture lane",
    )


def _fault_skip(*, precise: bool = False, mm: bool = False, m0: bool = False):
    isa_ok = (
        _ISA == "armv6-m"
        if m0
        else _ISA in _MM_ISAS
        if mm
        else _ISA in _PRECISE_ISAS
        if precise
        else True
    )
    return pytest.mark.skipif(
        not _HAS_FULL_BACKTRACE or not isa_ok,
        reason="requires a matching FreeRTOS M-profile full/fpu fault fixture lane",
    )


def _fpu_skip():
    return pytest.mark.skipif(
        SPEC.variant not in {"fpu", "softfp"},
        reason="requires the live ARM_CM4F FPU fixture",
    )


def _rv_skip():
    return pytest.mark.skipif(
        not SPEC.target.startswith("qemu-virt-rv"),
        reason="requires a FreeRTOS QEMU RISC-V fixture",
    )


def _backtrace(session, task: str | None = None) -> dict:
    command = (
        f"gdr api backtrace {task} --json"
        if task is not None
        else "gdr api backtrace --json"
    )
    return envelope_data(session.run(command, timeout=30))


def _clear_breakpoints(session) -> None:
    session.run_python("import gdb\nfor bp in gdb.breakpoints() or (): bp.delete()")


def _stop_at(session, symbol: str) -> None:
    bp_out = session.run(f"tbreak {symbol}")
    assert "Temporary breakpoint" in bp_out, bp_out
    output = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in output, output


def _write_u32(session, name: str, value: int) -> None:
    session.run_python(
        f"""
import gdb
address = int(gdb.parse_and_eval("&{name}"))
gdb.selected_inferior().write_memory(address, ({value}).to_bytes(4, "little"))
"""
    )


@contextlib.contextmanager
def _with_gate(session, gate_name: str):
    _write_u32(session, gate_name, 1)
    try:
        yield
    finally:
        _clear_breakpoints(session)
        _write_u32(session, gate_name, 0)
        if gate_name == "gdr_irq_gate":
            session.run("tbreak gdr_irq_cycle_done")
            output = session.run("continue", timeout=20)
            assert "breakpoint" in output.lower(), output


def _exception_frames(data: dict) -> list[dict]:
    return [frame for frame in data["frames"] if frame["method"] == "exception"]


def _trace_active(session, task: str) -> dict:
    data = _backtrace(session, task)
    assert data["task"] == task
    assert data["architecture"] == _ISA and data["abi"] == _ABI
    assert data["source"] == "current" and data["bounds"] == "recorded"
    return data


def _internal_reason(session, body: str) -> str:
    output = session.run_python(
        f"""
import freertos.backtrace.unwind_gdb as bt
from core.adapter import active
{body}
"""
    )
    for line in output.splitlines():
        if line.startswith("REASON="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(output)


def _assert_irq_task_frame(raw: dict, *, expected_symbol: str) -> None:
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


def _profile() -> SchedulerProfile:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        elf = ElfImage.from_path(SPEC.elf_path())
        assert elf is not None
        profile, reason = build_profile(elf)
        assert profile is not None, reason
        _PROFILE_CACHE = profile
    return _PROFILE_CACHE


def _halt(session) -> None:
    session.run_python(
        """
import gdb
try:
    gdb.execute("interrupt")
except gdb.error:
    pass
"""
    )


def _state(session) -> dict:
    return sched_state(session, _profile(), _R4_OFF, _SW_SIZE)


def _wait_stage(session, address: int, predicate, attempts: int = 40) -> dict:
    for _ in range(attempts):
        session.run(f"tbreak *{address:#x}")
        output = session.run("continue", timeout=20)
        assert "breakpoint" in output.lower(), output
        state = _state(session)
        if predicate(state):
            return state
    raise AssertionError(f"stage {address:#x} was not reached in {attempts} hits")


def _finish_cycle(session) -> dict:
    state: dict = {}
    for _ in range(4):
        session.run("tbreak gdr_sched_cycle_done")
        output = session.run("continue", timeout=20)
        assert "breakpoint" in output.lower(), output
        state = _state(session)
        if state["gate"] == 0:
            return state
    raise AssertionError(f"fixture gate did not clear: {state}")


def _set_gate(session, phase: int) -> None:
    _halt(session)
    _write_u32(session, "gdr_sched_gate", phase)


def _assert_no_cross_task(frames: list[dict], own: str, other: str) -> None:
    symbols = [frame["symbol"] or "" for frame in frames]
    assert any(own in name for name in symbols), symbols
    assert not any(other in name for name in symbols), symbols


def _assert_sched_task_frame(state: dict, tcb_name: str) -> None:
    tcb = state["tcbs"][tcb_name]
    assert tcb["base"] <= tcb["top"] < tcb["end"], state
    assert tcb["frame"][7] & _T_BIT, state
    assert tcb["frame"][7] & 0x1FF == 0, state
    assert tcb["frame"][6] & 1 == 0, state


def _assert_psp_frame(state: dict, tcb_name: str) -> None:
    tcb = state["tcbs"][tcb_name]
    frame = state["psp_frame"]
    assert len(frame) == 8, state
    assert tcb["base"] <= state["psp"] <= tcb["end"], state
    assert frame[7] & _T_BIT, state
    assert frame[7] & 0x1FF == 0, state
    assert frame[6] & 1 == 0, state


def _stop_at_fault(session, leaf: str) -> None:
    _stop_at(session, leaf)
    _stop_at(session, "HardFault_Handler")


@pytest.fixture(scope="module")
def first_start_session():
    if not _HAS_FULL_BACKTRACE:
        pytest.skip("requires a FreeRTOS M-profile full/fpu scheduler fixture lane")
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


def test_saved_task_backtrace_has_a_known_chain(gdb_session):
    """A blocked task unwinds from its saved frame through CFI or EHABI."""
    data = _backtrace(gdb_session, "gdr_ready")
    assert data["task"] == "gdr_ready"
    if not _recorded_bounds():
        assert data["stop_reason"] == "stack-bounds-unavailable", data
        assert not data["complete"] and data["frames"] == [], data
        return
    assert data["architecture"] == _ISA
    assert "port" not in data and "isa" not in data
    assert data["frames"], data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("vTaskDelay" in function for function in functions), functions
    assert any("gdr_ready_task" in function for function in functions), functions
    delay = next(i for i, name in enumerate(functions) if "vTaskDelay" in name)
    entry = next(i for i, name in enumerate(functions) if "gdr_ready_task" in name)
    assert delay < entry, functions
    assert data["bounds"] == "recorded"
    assert data["source"] == "saved"
    if SPEC.target.startswith("qemu-virt-rv"):
        oracle = json_line(
            gdb_session.run_python(
                f"""
import json
symbol = gdb.lookup_static_symbol('gdr_ready_task')
assert symbol is not None
print(json.dumps({{
    'entry': int(symbol.value().address),
    'lookup_symbol': gdb.execute('info symbol {data["frames"][-1]["pc"] - 1:#x}', to_string=True).split(' in section ')[0].strip(),
}}))
"""
            )
        )
        assert not data["complete"] and data["stop_reason"] == "cfi-stack-overflow", (
            data
        )
        assert len(data["frames"]) == entry + 2, data
        assert data["frames"][-1]["pc"] == oracle["entry"], data
        assert functions[-1] == oracle["lookup_symbol"].replace(" + ", "+"), (
            data,
            oracle,
        )
    else:
        assert data["complete"] and data["stop_reason"] == "task-exit", data
    assert not data["truncated"], data
    assert data["method"] == ("ehabi" if SPEC.target == "mps2-an385" else "cfi"), data
    assert [f["index"] for f in data["frames"]] == list(range(len(functions)))
    assert data["method"] == data["frames"][1]["method"], data


@pytest.mark.skipif(
    not SPEC.target.startswith("qemu-virt-rv"),
    reason="requires the RISC-V zero task-return sentinel",
)
def test_riscv_nonfirst_task_reaches_the_recorded_zero_exit(gdb_session):
    data = _backtrace(gdb_session, "gdr_normal")
    assert data["architecture"] == (
        "riscv32" if SPEC.target.endswith("rv32") else "riscv64"
    ), data
    assert "port" not in data and "isa" not in data
    assert data["bounds"] == "recorded" and data["source"] == "saved", data
    assert data["complete"] and not data["truncated"], data
    assert data["stop_reason"] == "task-exit" and data["method"] == "cfi", data
    names = [frame["symbol"] or "" for frame in data["frames"]]
    assert "vTaskDelay" in names[0] and "gdr_delay_task" in names[-1], names
    assert all(frame["pc"] != 0 for frame in data["frames"]), data


def test_backtrace_command_matches_the_api_and_stays_bounded(gdb_session):
    """The text route returns a frame table and an explicit stop reason."""
    text = gdb_session.run("freertos backtrace gdr_ready --max-depth 1", timeout=30)
    assert "Traceback" not in text
    data = envelope_data(
        gdb_session.run("gdr api backtrace gdr_ready --max-depth 1 --json", timeout=30)
    )
    if _recorded_bounds():
        assert len(data["frames"]) == 1
        assert data["truncated"] and not data["complete"]
        assert data["stop_reason"] == "max-depth"
        assert "max-depth" in text
    else:
        assert data["frames"] == []
        assert data["stop_reason"] == "stack-bounds-unavailable"
        assert "stack-bounds-unavailable" in text


def test_initial_task_does_not_invent_entry_evidence(gdb_session):
    """Fixture existence is independent of the API's completeness verdict."""
    data = _backtrace(gdb_session, "gdr_new")
    if not PROFILE.never_started_task:
        assert data["stop_reason"] == "task-not-found" and data["frames"] == [], data
        return
    assert data["stop_reason"] != "task-not-found", data
    assert data["source"] != "unstarted", data
    assert data["stop_reason"] != "task-entry", data
    if _recorded_bounds():
        assert data["frames"] and "gdr_unstarted" in data["frames"][0]["symbol"], data
    else:
        assert data["stop_reason"] == "stack-bounds-unavailable"


def test_running_task_is_not_classified_as_unstarted(gdb_session):
    data = _backtrace(gdb_session, "gdr_spin")
    assert data["stop_reason"] != "task-not-found", data
    assert data["source"] != "unstarted", data
    assert data["stop_reason"] != "task-entry", data


def test_unknown_task_reports_structured_failure(gdb_session):
    """A missing name is a stop reason, not a traceback or an empty success."""
    data = _backtrace(gdb_session, "no_such_task")
    assert data["stop_reason"] == "task-not-found"
    assert data["frames"] == []


def test_omitted_target_backtrace_unwinds_current_halted_pc(gdb_session):
    """Running ``frt bt`` or ``gdr api backtrace`` without a task uses current PC."""
    cur_name = current_task_name(gdb_session)
    assert cur_name
    cli_out = gdb_session.run("frt bt", timeout=30)
    assert "[gdr] warning: usage:" not in cli_out, cli_out
    assert f"{cur_name}:" in cli_out, cli_out

    current_data = envelope_data(
        gdb_session.run("gdr api backtrace --json", timeout=30)
    )
    explicit_data = _backtrace(gdb_session, cur_name)
    assert current_data["task"] == cur_name
    if _recorded_bounds():
        assert current_data["source"] == "current"
    else:
        assert current_data["source"] == "none"
        assert current_data["stop_reason"] == "stack-bounds-unavailable"
    assert current_data["frames"] == explicit_data["frames"]
    assert current_data["stop_reason"] == explicit_data["stop_reason"]


@_fpu_skip()
def test_fpu_context_registers_are_enabled_on_target(gdb_session):
    fpccr = int(gdb_session.run("p/x *(uint32_t *)0xE000EF34").split("=")[1], 16)
    cpacr = int(gdb_session.run("p/x *(uint32_t *)0xE000ED88").split("=")[1], 16)
    assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
    assert cpacr & 0x00F00000 == 0x00F00000, f"CPACR={cpacr:#x}"


@_fpu_skip()
def test_fpu_task_saved_chain_is_complete(gdb_session):
    data = _backtrace(gdb_session, "gdr_fpu")
    assert data["architecture"] == _ISA, data
    assert data["abi"] == _ABI, data
    assert data["source"] == "saved", data
    assert data["bounds"] == "recorded", data
    assert data["method"] in {"cfi", "ehabi"}, data
    assert data["stop_reason"] == "task-exit", data
    assert data["complete"] and not data["truncated"], data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("gdr_fpu_task" in function for function in functions), functions


@_fpu_skip()
def test_fpu_saved_slot_names_the_extended_frame(gdb_session):
    top = task_top_of_stack(gdb_session, "gdr_fpu")
    words = read_u32_words(gdb_session, top, 36)
    if SPEC.target == "mps2-an505":
        assert words[0] & 0x7 == 0, f"PSPLIM={words[0]:#x}"
        assert words[1] == 0xFFFFFFED, [f"{w:#x}" for w in words[1:2]]
        assert words[33] & _T_BIT != 0, f"xPSR={words[33]:#x}"
    else:
        assert words[8] == 0xFFFFFFED, [f"{w:#x}" for w in words[8:9]]
        assert words[32] & _T_BIT != 0, f"xPSR={words[32]:#x}"
    data = _backtrace(gdb_session, "gdr_fpu")
    assert data["source"] == "saved", data


@_irq_skip()
def test_single_irq_returns_to_the_exact_interrupted_task_frame(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "gdr_irq_single_leaf")
        raw = raw_irq_state(gdb_session, "psp", _ISA)
        _assert_irq_task_frame(raw, expected_symbol="gdr_irq_wait")
        assert raw["frame_xpsr"] & _ALIGN_BIT == 0

        data = _trace_active(gdb_session, "gdr_irq")
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
        current_data = envelope_data(
            gdb_session.run("gdr api backtrace --json", timeout=30)
        )
        assert current_data["frames"] == data["frames"], current_data
        assert "warning: usage:" not in gdb_session.run("frt bt", timeout=30)


@_irq_skip()
def test_nested_irq_crosses_handler_then_task_with_two_exact_pcs(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "Timer1_Handler")
        nested = raw_irq_state(gdb_session, "msp", _ISA)
        task = raw_irq_state(gdb_session, "psp", _ISA)
        _assert_irq_task_frame(task, expected_symbol="gdr_irq_wait")
        assert nested["frame_xpsr"] & _T_BIT
        assert nested["frame_xpsr"] & 0x1FF == _TIMER0_EXC, nested
        assert "gdr_irq_nested_wait" in nested["frame_pc_symbol"], nested
        assert nested["sp"] == nested["msp"], nested
        assert nested["irq_low"] <= nested["msp"] <= nested["irq_high"], nested

        data = _trace_active(gdb_session, "gdr_irq")
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
        assert f"exception:{_TIMER1_EXC} -> exception:{_TIMER0_EXC}" in evidence, data
        assert f"exception:{_TIMER0_EXC} -> task:" in evidence, data
        assert "EXC_RETURN=0xfffffff1" in evidence, data
        assert "EXC_RETURN=0xfffffffd" in evidence, data


@_irq_skip()
def test_handler_c_call_chain_is_ordinary_cfi_before_the_exception_boundary(
    gdb_session,
):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "gdr_irq_nested_leaf")
        data = _trace_active(gdb_session, "gdr_irq")
        methods = [frame["method"] for frame in data["frames"]]
        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        leaf = next(
            i for i, name in enumerate(symbols) if "gdr_irq_nested_leaf" in name
        )
        body = next(i for i, name in enumerate(symbols) if "gdr_irq1_body" in name)
        handler = next(i for i, name in enumerate(symbols) if "Timer1_Handler" in name)
        assert leaf < body < handler, symbols
        assert methods[body] in ("cfi", "ehabi"), data
        assert methods[handler] in ("cfi", "ehabi"), data
        assert methods[handler + 1] == "exception", data
        assert len(_exception_frames(data)) == 2, data


@_irq_skip()
def test_switch_window_without_scheduler_profile_refuses_task_ownership(gdb_session):
    if SPEC.target != "mps2-an386":
        pytest.skip("requires the AN386 lane for PendSV profile-refusal check")
    _set_gate(gdb_session, 1)
    try:
        _stop_at(gdb_session, "PendSV_Handler")
        result = scheduler_profile_override(
            gdb_session, "scheduler-transition-unproven"
        )
        assert result == {
            "stop_reason": "scheduler-transition-unproven",
            "frames": 0,
            "complete": False,
        }, result
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_irq_skip(fpu_only=True)
def test_fp_isr_grows_the_msp_frame_with_lazy_stacking(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "gdr_irq_fp_leaf")
        raw = raw_irq_state(gdb_session, "psp", _ISA)
        _assert_irq_task_frame(raw, expected_symbol="gdr_irq_wait")
        fpccr = int(
            gdb_session.run("p/x *(unsigned int *)0xE000EF34").split("=")[1].strip(),
            16,
        )
        assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
        data = _trace_active(gdb_session, "gdr_irq")
        assert data["complete"] and data["stop_reason"] == "task-exit", data
        transitions = _exception_frames(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["frame_pc"], data
        assert "gdr_irq_wait" in (transitions[0]["symbol"] or ""), data
        assert any(
            "EXC_RETURN=0xffffffed" in message for message in data["messages"]
        ), data
        assert any("extended" in message for message in data["messages"]), data


@_irq_skip()
def test_stkalign_frame_retains_the_four_byte_padding(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "gdr_irq_align_leaf")
        raw = raw_irq_state(gdb_session, "psp", _ISA)
        _assert_irq_task_frame(raw, expected_symbol="gdr_irq_misaligned_wait")
        assert raw["frame_xpsr"] & _ALIGN_BIT, raw
        assert raw["frame_base"] % 8 == 0, raw
        assert raw["restored_sp"] % 8 == 4, raw
        assert raw["restored_sp"] == raw["frame_base"] + 0x24, raw

        data = _trace_active(gdb_session, "gdr_irq")
        transitions = _exception_frames(data)
        assert len(transitions) == 1, data
        assert transitions[0]["pc"] == raw["frame_pc"], data
        assert "xpsr=0x" in " ".join(data["messages"])


@_irq_skip()
def test_handler_entry_uses_the_vector_proof_without_a_cfi_padding_row(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "Timer0_Handler if gdr_irq_scenario == 0")
        raw = raw_irq_state(gdb_session, "psp", _ISA)
        _assert_irq_task_frame(raw, expected_symbol="gdr_irq_wait")
        data = _trace_active(gdb_session, "gdr_irq")
        assert data["frames"][0]["symbol"] == "Timer0_Handler", data
        transitions = _exception_frames(data)
        assert len(transitions) == 1, data
        assert transitions[0]["pc"] == raw["frame_pc"], data
        assert data["complete"] and data["stop_reason"] == "task-exit", data


@_irq_skip()
def test_real_handler_without_linker_bounds_is_refused(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
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


@_irq_skip()
@pytest.mark.parametrize(
    "option,value,reason",
    [
        ("max_exception_depth", 1, "exception-depth-exceeded"),
        ("max_exception_bytes", 32, "exception-read-budget-exceeded"),
    ],
)
def test_real_nested_stop_obeys_independent_budgets(gdb_session, option, value, reason):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "gdr_irq1_body")
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


@_irq_skip()
def test_irq_backtrace_preserves_target_registers_and_stack_memory(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "gdr_irq_single_leaf")
        result = verify_readonly_backtrace(gdb_session, "gdr_irq", include_tick=True)
        assert result == {
            "unchanged": True,
            "complete": True,
            "stop": "task-exit",
            "transitions": 1,
            "events": [],
        }


@_irq_skip()
@pytest.mark.skipif(_ISA != "armv6-m", reason="Armv6-M fixed-vector port contract")
def test_m0_live_port_reads_cpuid_and_fixed_vectors_without_v7_registers(gdb_session):
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "Timer0_Handler if gdr_irq_scenario == 0")
        assert m0_fixed_vector_probe(gdb_session) == {
            "complete": True,
            "limit": 47,
            "cpuid": True,
            "vector": True,
            "transitions": 1,
        }


@_irq_skip()
def test_systick_entry_and_xTaskIncrementTick_callee_unwind_across_msp_to_psp(
    gdb_session,
):
    for stop_symbol, expect_callee in (
        ("SysTick_Handler", False),
        ("xTaskIncrementTick", True),
    ):
        _stop_at(gdb_session, stop_symbol)
        probe = systick_probe(gdb_session)
        assert probe["ipsr"] == 15, probe
        data = _backtrace(gdb_session, probe["task"])
        assert data["complete"] and data["stop_reason"] == "task-exit", data
        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        if expect_callee:
            assert "xTaskIncrementTick" in symbols[0], symbols
            assert any(
                "SysTick_Handler" in sym or "xPortSysTickHandler" in sym
                for sym in symbols
            ), symbols
        transitions = _exception_frames(data)
        assert len(transitions) >= 1, data


@_sched_skip()
def test_compiled_transition_bytes_match_the_independent_oracle(gdb_session):
    prof = _profile()
    oracle = compiled_sched_oracle(gdb_session, prof)
    assert oracle["pendsv"] == prof.pendsv_entry
    assert oracle["svc"] == prof.svc_entry
    assert oracle["start"] == prof.start_first_start
    assert oracle["switch"] == prof.switch_entry
    if _PORT == "arm-cm3" and SPEC.version == "10.3.1":
        assert oracle["pendsv_entry"] == 0x8009F3EF, oracle
        assert oracle["pendsv_save"] == 0x0FF0E920, oracle
        assert oracle["pendsv_psp"] == 0x8809F380, oracle
        assert oracle["pendsv_return"] == 0x4770, oracle
        assert oracle["svc_entry"] == 0x68194B07, oracle
        assert oracle["svc_restored"] == 0x8809F380, oracle
        assert oracle["svc_return"] == 0x4770, oracle
        assert oracle["start_svc"] == 0xDF00, oracle
        assert oracle["switch_update"] == 0x601A, oracle
        assert oracle["switch_after"] == 0xBD10, oracle
    else:
        assert oracle["pendsv_return"] == (0x4718 if _EXC_RET_IN_R3 else 0x4770), oracle
        assert oracle["svc_return"] == (0x4710 if _EXC_RET_IN_R3 else 0x4770), oracle
        assert (oracle["start_svc"] >> 8) == 0xDF, oracle


@_sched_skip()
def test_cooperative_yield_marks_old_task_before_and_after_the_tcb_writeback(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 1)
    try:
        state = _wait_stage(
            gdb_session,
            prof.pendsv_entry,
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 1
                and item["yield_count"] >= 1
            ),
        )
        assert state["pc"] == prof.pendsv_entry, state
        assert state["xpsr"] & 0x1FF == 14, state
        assert state["xpsr"] & _T_BIT, state
        assert state["lr"] == 0xFFFFFFFD, state
        _assert_psp_frame(state, "gdr_sched_task_handle")
        old_pc = state["psp_frame"][6]
        entry_psp = state["psp"]

        data = _backtrace(gdb_session, "gdr_sched")
        assert data["source"] == "current" and data["complete"], data
        assert data["frames"][0]["pc"] == prof.pendsv_entry, data
        assert data["frames"][1]["pc"] == old_pc, data
        assert data["frames"][1]["method"] == "exception", data
        _assert_no_cross_task(data["frames"], "gdr_sched_task", "gdr_sched_tick_hi")
        evidence = " ".join(data["messages"])
        assert "stage=pendsv-entry" in evidence and "owner=old" in evidence, data
        assert prof.profile_id in evidence, data

        state = _wait_stage(
            gdb_session,
            prof.pendsv_save_pending,
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        assert state["pc"] == prof.pendsv_save_pending, state
        data = _backtrace(gdb_session, "gdr_sched")
        assert "stage=pendsv-save-pending" in " ".join(data["messages"]), data

        if prof.pendsv_save_written is not None:
            state = _wait_stage(
                gdb_session,
                prof.pendsv_save_written,
                lambda item: (
                    item["current"] == item["handles"]["gdr_sched_task_handle"]
                ),
            )
            assert state["psp"] == entry_psp, state
            assert state["regs"]["r0"] == state["psp"] - _SW_SIZE, state
            assert state["saved_buffer"] == [
                state["regs"][f"r{index}"] for index in range(4, 12)
            ], state
            data = _backtrace(gdb_session, "gdr_sched")
            assert data["source"] == "current", data
            assert "stage=pendsv-save-written" in " ".join(data["messages"]), data

        state = _wait_stage(
            gdb_session,
            prof.pendsv_writeback,
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        assert (
            state["tcbs"]["gdr_sched_task_handle"]["top"] == state["psp"] - _SW_SIZE
        ), state
        data = _backtrace(gdb_session, "gdr_sched")
        assert data["source"] == "saved" and data["complete"], data
        assert data["frames"][0]["pc"] == prof.pendsv_writeback, data
        assert data["frames"][1]["pc"] == old_pc, data
        assert data["frames"][1]["method"] == "exception", data
        evidence = " ".join(data["messages"])
        assert "stage=pendsv-writeback" in evidence and "owner=old" in evidence, data
        _finish_cycle(gdb_session)
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_sched_skip()
def test_tick_preemption_updates_current_tcb_inside_vTaskSwitchContext(gdb_session):
    prof = _profile()
    _set_gate(gdb_session, 2)
    try:
        oracle = compiled_sched_oracle(gdb_session, prof)
        tick = _wait_stage(
            gdb_session,
            oracle["sys_tick"],
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 2
            ),
        )
        assert tick["xpsr"] & 0x1FF == 15, tick
        assert tick["xpsr"] & _T_BIT, tick
        tick_frame = frame_at(gdb_session, tick["psp"])
        assert tick_frame["ipsr"] == 0, tick_frame
        assert "gdr_sched_task" in tick_frame["pc_symbol"], tick_frame
        tick_before = tick["tick_count"]
        state = _wait_stage(
            gdb_session,
            prof.pendsv_entry,
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 2
            ),
        )
        assert state["tick_count"] > tick_before, (tick_before, state)
        _assert_psp_frame(_state(gdb_session), "gdr_sched_task_handle")

        call = _wait_stage(
            gdb_session,
            prof.pendsv_switch_call,
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        assert call["lr"] == 0xFFFFFFFD, call
        data = _backtrace(gdb_session, "gdr_sched")
        assert data["source"] == "saved" and data["complete"], data
        assert "stage=pendsv-switch-call" in " ".join(data["messages"]), data
        _assert_no_cross_task(
            data["frames"], "gdr_sched_task", "gdr_sched_tick_hi_task"
        )

        entry_state = _wait_stage(
            gdb_session,
            prof.switch_entry,
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        assert (entry_state["lr"] & ~1) == prof.pendsv_after_switch, entry_state
        data = _backtrace(gdb_session, "gdr_sched")
        assert data["source"] == "saved" and data["complete"], data
        assert "stage=switch-entry" in " ".join(data["messages"]), data

        # Single-step inside vTaskSwitchContext body before pxCurrentTCB update
        # (including C body statements such as xYieldPendings[0] = pdFALSE).
        for _ in range(4):
            gdb_session.run("stepi", timeout=10)
            mid_state = _state(gdb_session)
            if (
                prof.switch_start < mid_state["pc"] < prof.switch_update
                and mid_state["current"]
                == mid_state["handles"]["gdr_sched_task_handle"]
            ):
                mid_data = _backtrace(gdb_session)
                assert mid_data["complete"] and mid_data["source"] == "saved", mid_data
                assert mid_data["frames"][0]["pc"] == mid_state["pc"], (
                    mid_data,
                    mid_state,
                )
                assert "vTaskSwitchContext" in (
                    mid_data["frames"][0]["symbol"] or ""
                ), mid_data
                assert "PendSV_Handler" in (mid_data["frames"][1]["symbol"] or ""), (
                    mid_data
                )
                assert mid_data["frames"][2]["method"] == "exception", mid_data
                assert "stage=switch-before-update" in " ".join(mid_data["messages"]), (
                    mid_data
                )

        state = _wait_stage(
            gdb_session,
            prof.switch_update,
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        assert (
            state["tcbs"]["gdr_sched_task_handle"]["top"] == state["psp"] - _SW_SIZE
        ), state
        data = _backtrace(gdb_session, "gdr_sched")
        assert data["source"] == "saved" and data["complete"], data
        assert "stage=switch-before-update" in " ".join(data["messages"]), data

        gdb_session.run("stepi", timeout=10)
        after = _state(gdb_session)
        assert after["current"] == after["handles"]["gdr_sched_tick_handle"], after
        assert after["pc"] == prof.switch_after_update, after
        tick = after["tcbs"]["gdr_sched_tick_handle"]
        _assert_sched_task_frame(after, "gdr_sched_tick_handle")
        data = _backtrace(gdb_session, "gdr_sched_tick")
        assert data["source"] == "saved", data
        assert data["frames"][0]["pc"] == prof.switch_after_update, (data, after)
        assert "vTaskSwitchContext" in (data["frames"][0]["symbol"] or ""), data
        assert "PendSV_Handler" in (data["frames"][1]["symbol"] or ""), data
        assert data["frames"][2]["pc"] == tick["frame"][6] & ~1, (data, after)
        assert data["frames"][2]["method"] == "exception", data
        _assert_no_cross_task(
            data["frames"], "gdr_sched_tick_hi_task", "gdr_sched_task"
        )
        assert "stage=switch-after-update" in " ".join(data["messages"]), data

        after = _wait_stage(
            gdb_session,
            prof.pendsv_after_switch,
            lambda item: item["current"] == item["handles"]["gdr_sched_tick_handle"],
        )
        assert after["pc"] == prof.pendsv_after_switch, after
        data = _backtrace(gdb_session, "gdr_sched_tick")
        assert data["source"] == "saved" and data["complete"], data
        assert "stage=pendsv-after-switch" in " ".join(data["messages"]), data
        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        delay = next(i for i, name in enumerate(symbols) if "vTaskDelay" in name)
        tick_entry = next(
            i for i, name in enumerate(symbols) if "gdr_sched_tick_hi_task" in name
        )
        assert delay < tick_entry, symbols

        state = _wait_stage(
            gdb_session,
            prof.pendsv_select,
            lambda item: item["current"] == item["handles"]["gdr_sched_tick_handle"],
        )
        assert state["regs"]["r1"] == state["handles"]["gdr_sched_tick_handle"], state
        data = _backtrace(gdb_session, "gdr_sched_tick")
        assert data["source"] == "saved", data
        assert "stage=pendsv-select" in " ".join(data["messages"]), data

        state = _wait_stage(
            gdb_session,
            prof.pendsv_saved_sp,
            lambda item: item["current"] == item["handles"]["gdr_sched_tick_handle"],
        )
        assert state["regs"]["r0"] == state["tcbs"]["gdr_sched_tick_handle"]["top"], (
            state
        )
        data = _backtrace(gdb_session, "gdr_sched_tick")
        assert data["source"] == "saved", data
        assert "stage=pendsv-saved-sp" in " ".join(data["messages"]), data
        _finish_cycle(gdb_session)
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_sched_skip()
def test_restore_window_distinguishes_restored_cores_from_the_old_psp(gdb_session):
    prof = _profile()
    _set_gate(gdb_session, 2)
    try:
        _wait_stage(
            gdb_session,
            prof.pendsv_entry,
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 2
            ),
        )
        state = _wait_stage(
            gdb_session,
            prof.pendsv_restored_pending_psp,
            lambda item: item["current"] == item["handles"]["gdr_sched_tick_handle"],
        )
        tick = state["tcbs"]["gdr_sched_tick_handle"]
        assert state["psp"] != tick["top"] + _SW_SIZE, state
        assert state["pc"] == prof.pendsv_restored_pending_psp, state
        assert state["regs"]["r0"] == tick["top"] + _SW_SIZE, state
        if _PORT == "arm-cm0":
            assert [state["regs"][f"r{index}"] for index in range(8, 12)] == tick[
                "saved"
            ][4:], state
        else:
            assert [state["regs"][f"r{index}"] for index in range(4, 12)] == tick[
                "saved"
            ], state
        data = _backtrace(gdb_session, "gdr_sched_tick")
        assert data["source"] == "current", data
        assert data["frames"][0]["pc"] == prof.pendsv_restored_pending_psp, data
        assert data["frames"][1]["pc"] == tick["frame"][6] & ~1, data
        assert data["frames"][1]["method"] == "exception", data
        _assert_no_cross_task(
            data["frames"], "gdr_sched_tick_hi_task", "gdr_sched_task"
        )
        assert "stage=pendsv-restored-pending-psp" in " ".join(data["messages"]), data

        if prof.pendsv_psp_written is not None:
            gdb_session.run("stepi", timeout=10)
            written = _state(gdb_session)
            assert written["pc"] == prof.pendsv_psp_written, written
            assert written["psp"] == tick["top"] + _SW_SIZE, written
            assert written["current"] == written["handles"]["gdr_sched_tick_handle"], (
                written
            )
            data = _backtrace(gdb_session, "gdr_sched_tick")
            assert data["source"] == "current", data
            assert data["frames"][0]["pc"] == prof.pendsv_psp_written, data
            assert data["frames"][1]["pc"] == tick["frame"][6] & ~1, data
            assert data["frames"][1]["method"] == "exception", data
            assert "stage=pendsv-psp-written" in " ".join(data["messages"]), data

        gdb_session.run(f"tbreak *{prof.pendsv_exception_return:#x}")
        gdb_session.run("continue", timeout=10)
        pending = _state(gdb_session)
        assert pending["pc"] == prof.pendsv_exception_return, pending
        if _PORT == "arm-cm0":
            assert [pending["regs"][f"r{index}"] for index in range(4, 12)] == tick[
                "saved"
            ], pending
        exc_ret = pending["regs"]["r3"] if _EXC_RET_IN_R3 else pending["lr"]
        assert exc_ret == 0xFFFFFFFD, pending
        data = _backtrace(gdb_session, "gdr_sched_tick")
        assert "stage=pendsv-exception-return" in " ".join(data["messages"]), data
        gdb_session.run("stepi", timeout=10)
        returned = _state(gdb_session)
        assert (
            tick["top"] + _SW_SIZE <= returned["psp"] <= tick["top"] + _SW_SIZE + 0x20
        ), returned
        assert returned["current"] == returned["handles"]["gdr_sched_tick_handle"], (
            returned
        )
        assert returned["pc"] != prof.pendsv_exception_return, returned
        _finish_cycle(gdb_session)
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_sched_skip()
def test_nested_irq_truly_preempts_timer0_before_requesting_scheduling(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 3)
    try:
        oracle = compiled_sched_oracle(gdb_session, prof)
        timer0 = _wait_stage(
            gdb_session,
            oracle["timer0"],
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 3
            ),
        )
        assert timer0["xpsr"] & 0x1FF == _TIMER0_EXC, timer0
        task_frame = frame_at(gdb_session, timer0["psp"])
        assert task_frame["ipsr"] == 0, task_frame
        assert "gdr_sched_task" in task_frame["pc_symbol"], task_frame

        timer1 = _wait_stage(
            gdb_session,
            oracle["timer1"],
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        assert timer1["xpsr"] & 0x1FF == _TIMER1_EXC, timer1
        nested_frame = frame_at(gdb_session, timer1["msp"])
        assert nested_frame["ipsr"] == _TIMER0_EXC, nested_frame
        assert "Timer0_Handler" in nested_frame["pc_symbol"], nested_frame

        state = _wait_stage(
            gdb_session,
            prof.pendsv_entry,
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 3
                and item["irq_requested"] == 1
            ),
        )
        assert state["xpsr"] & 0x1FF == 14, state
        assert state["xpsr"] & _T_BIT, state
        assert state["lr"] == 0xFFFFFFFD, state
        _assert_psp_frame(state, "gdr_sched_task_handle")
        data = _backtrace(gdb_session, "gdr_sched")
        assert data["source"] == "current" and data["complete"], data
        _assert_no_cross_task(data["frames"], "gdr_sched_task", "Timer1_Handler")
        _finish_cycle(gdb_session)
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_sched_skip()
def test_nested_irq_requesting_scheduling_keeps_the_interrupted_task_owner(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 3)
    try:
        state = _wait_stage(
            gdb_session,
            prof.pendsv_entry,
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 3
                and item["irq_requested"] == 1
            ),
        )
        assert state["xpsr"] & 0x1FF == 14, state
        assert state["xpsr"] & _T_BIT, state
        assert state["lr"] == 0xFFFFFFFD, state
        _assert_psp_frame(state, "gdr_sched_task_handle")
        data = _backtrace(gdb_session, "gdr_sched")
        assert data["source"] == "current" and data["complete"], data
        _assert_no_cross_task(data["frames"], "gdr_sched_task", "Timer1_Handler")
        symbols = " ".join(frame["symbol"] or "" for frame in data["frames"])
        assert "Timer0_Handler" not in symbols and "Timer1_Handler" not in symbols, data

        _wait_stage(
            gdb_session,
            prof.switch_update,
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        gdb_session.run("stepi", timeout=10)
        after = _state(gdb_session)
        assert after["current"] == after["handles"]["gdr_sched_irq_handle"], after
        data = _backtrace(gdb_session, "gdr_sched_irq")
        assert data["source"] == "saved", data
        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        notify = next(
            i
            for i, name in enumerate(symbols)
            if "ulTaskNotifyTake" in name or "ulTaskGenericNotifyTake" in name
        )
        irq_entry = next(
            i for i, name in enumerate(symbols) if "gdr_sched_irq_hi_task" in name
        )
        assert notify < irq_entry, symbols
        assert not any("gdr_sched_task" in name for name in symbols), symbols
        assert "stage=switch-after-update" in " ".join(data["messages"]), data
        _finish_cycle(gdb_session)
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_sched_skip()
def test_unsupported_and_mismatched_scheduler_stops_are_refused(gdb_session):
    """Verify live ambiguous-PC refusal plus synthetic profile-mismatch refusal."""
    prof = _profile()
    ambig_pc = (
        prof.pendsv_entry + 8 if _PORT == "arm-cm33-fpu" else prof.pendsv_entry + 4
    )
    _set_gate(gdb_session, 1)
    try:
        state = _wait_stage(
            gdb_session,
            ambig_pc,
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
        )
        assert state["pc"] == ambig_pc, state
        data = _backtrace(gdb_session, "gdr_sched")
        assert data["frames"] == [], data
        assert data["stop_reason"].startswith("scheduler-stage-ambiguous"), data

        result = scheduler_profile_override(
            gdb_session, "scheduler-profile-mismatch:test", task="gdr_sched"
        )
        assert result["frames"] == 0, result
        assert result["stop_reason"] == "scheduler-profile-mismatch:test", result
        _finish_cycle(gdb_session)
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_sched_skip()
def test_irq_saved_paths_still_complete_while_stopped_in_a_scheduler_stage(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 1)
    try:
        _wait_stage(
            gdb_session,
            prof.pendsv_entry,
            lambda item: (
                item["current"] == item["handles"]["gdr_sched_task_handle"]
                and item["phase"] == 1
            ),
        )
        data = _backtrace(gdb_session, "gdr_ready")
        assert data["source"] == "saved", data
        assert data["complete"] and data["stop_reason"] == "task-exit", data
        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        assert any("gdr_ready_task" in name for name in symbols), symbols
        _finish_cycle(gdb_session)
    finally:
        _clear_breakpoints(gdb_session)
        _write_u32(gdb_session, "gdr_sched_gate", 0)


@_sched_skip()
def test_first_task_start_uses_the_independent_svc_restore_path(first_start_session):
    session = first_start_session
    prof = _profile()
    _stop_at_addr = lambda addr: (  # noqa: E731
        session.run(f"tbreak *{addr:#x}"),
        session.run("continue", timeout=20),
    )

    _, output = _stop_at_addr(prof.start_first_start)
    assert "breakpoint" in output.lower(), output
    state = _state(session)
    assert state["current"] == state["handles"]["gdr_sched_first_handle"], state
    data = _backtrace(session, "gdr_sched_first")
    assert data["frames"] == [], data
    assert data["stop_reason"].startswith("scheduler-stage-ambiguous"), data

    _, output = _stop_at_addr(prof.start_first_svc)
    assert "breakpoint" in output.lower(), output
    state = _state(session)
    assert state["pc"] == prof.start_first_svc, state
    data = _backtrace(session, "gdr_sched_first")
    assert data["frames"] == [], data
    assert data["stop_reason"] == "scheduler-first-start-pending", data

    _, output = _stop_at_addr(prof.svc_entry)
    assert "breakpoint" in output.lower(), output
    state = _state(session)
    assert state["xpsr"] & 0x1FF == 11, state
    assert state["xpsr"] & _T_BIT, state
    if prof.svc_symbol == "SVC_Handler":
        assert state["lr"] == 0xFFFFFFF9, state
    assert state["psp"] == 0, state
    first = state["tcbs"]["gdr_sched_first_handle"]
    data = _backtrace(session, "gdr_sched_first")
    assert data["source"] == "unstarted", data
    assert data["complete"] and data["stop_reason"] == "task-entry", data
    assert data["frames"][0]["pc"] == prof.svc_entry, data
    assert data["frames"][1]["pc"] == first["frame"][6] & ~1, data
    assert data["frames"][1]["method"] == "exception", data
    assert "stage=svc-entry" in " ".join(data["messages"]), data

    for stage_name, addr, check in (
        (
            "svc-saved-sp",
            prof.svc_saved_sp,
            lambda st: st["psp"] == 0 and st["regs"]["r0"] == first["top"],
        ),
        (
            "svc-restored-pending-psp",
            prof.svc_restored_pending_psp,
            lambda st: st["psp"] == 0 and st["regs"]["r0"] == first["top"] + _SW_SIZE,
        ),
        (
            "svc-psp-written",
            prof.svc_psp_written,
            lambda st: st["psp"] == first["top"] + _SW_SIZE,
        ),
        (
            "svc-exception-return",
            prof.svc_exception_return,
            lambda st: (st["regs"]["r2"] if _EXC_RET_IN_R3 else st["lr"]) == 0xFFFFFFFD,
        ),
    ):
        _, output = _stop_at_addr(addr)
        assert "breakpoint" in output.lower(), output
        cur = _state(session)
        assert check(cur), cur
        data = _backtrace(session, "gdr_sched_first")
        assert data["complete"] and data["source"] == "unstarted", data
        assert f"stage={stage_name}" in " ".join(data["messages"]), data

    session.run("stepi", timeout=10)
    returned = _state(session)
    assert (
        first["top"] + _SW_SIZE <= returned["psp"] <= first["top"] + _SW_SIZE + 0x20
    ), returned
    assert returned["current"] == returned["handles"]["gdr_sched_first_handle"], (
        returned
    )
    assert returned["pc"] != prof.svc_exception_return, returned

    # First PendSV after gdr_sched_first suspends switches into an unstarted task.
    if prof.switch_after_update is not None:
        _, output = _stop_at_addr(prof.switch_after_update)
        assert "breakpoint" in output.lower(), output
        incoming_name = current_task_name(session)
        assert incoming_name and incoming_name != "gdr_sched_first", incoming_name
        data = _backtrace(session, incoming_name)
        assert data["source"] == "unstarted", (incoming_name, data)
        assert data["complete"] and data["stop_reason"] == "task-entry", (
            incoming_name,
            data,
        )
        assert "stage=switch-after-update" in " ".join(data["messages"]), data

    _, output = _stop_at_addr(prof.pendsv_after_switch)
    assert "breakpoint" in output.lower(), output
    incoming_name = current_task_name(session)
    assert incoming_name and incoming_name != "gdr_sched_first", incoming_name
    data = _backtrace(session, incoming_name)
    assert data["source"] == "unstarted", (incoming_name, data)
    assert data["complete"] and data["stop_reason"] == "task-entry", (
        incoming_name,
        data,
    )
    assert data["frames"][0]["pc"] == prof.pendsv_after_switch, data
    assert data["frames"][1]["method"] == "exception", data
    entry_pc = data["frames"][1]["pc"]
    assert "stage=pendsv-after-switch" in " ".join(data["messages"]), data

    pendsv_stages: list[tuple[str, int]] = []
    if prof.pendsv_select is not None:
        pendsv_stages.append(("pendsv-select", prof.pendsv_select))
    pendsv_stages.extend(
        [
            ("pendsv-saved-sp", prof.pendsv_saved_sp),
            ("pendsv-restored-pending-psp", prof.pendsv_restored_pending_psp),
        ]
    )
    if prof.pendsv_psp_written is not None:
        pendsv_stages.append(("pendsv-psp-written", prof.pendsv_psp_written))
    pendsv_stages.append(("pendsv-exception-return", prof.pendsv_exception_return))
    for stage_name, addr in pendsv_stages:
        _, output = _stop_at_addr(addr)
        assert "breakpoint" in output.lower(), output
        data = _backtrace(session, incoming_name)
        assert data["source"] == "unstarted", (stage_name, incoming_name, data)
        assert data["complete"] and data["stop_reason"] == "task-entry", (
            stage_name,
            incoming_name,
            data,
        )
        assert data["frames"][0]["pc"] == addr, (stage_name, data)
        assert data["frames"][1]["pc"] == entry_pc, (stage_name, data)
        assert data["frames"][1]["method"] == "exception", (stage_name, data)
        assert f"stage={stage_name}" in " ".join(data["messages"]), (stage_name, data)


@_fault_skip(precise=True)
def test_precise_usagefault_escalation_is_synchronous(gdb_session):
    with _with_gate(gdb_session, "gdr_fault_gate"):
        _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
        raw = raw_fault_frame(gdb_session)
        assert raw["frame_xpsr"] & _T_BIT
        assert raw["frame_xpsr"] & 0x1FF == 0, raw
        assert raw["xpsr"] & 0x1FF == 3, raw
        assert "gdr_fault_undefinstr_leaf" in raw["frame_pc_symbol"], raw
        assert raw["stack_base"] <= raw["psp"] < raw["stack_high"], raw
        assert raw["cfsr"] & 0x00010000 and raw["hfsr"] & 0x40000000, raw

        data = _trace_active(gdb_session, "gdr_fault")
        assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
        evidence = " ".join(data["messages"])
        assert "synchronous fault" in evidence and "UNDEFINSTR" in evidence, data
        assert "CFSR=" in evidence and "HFSR=" in evidence, data
        transitions = _exception_frames(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["frame_pc"], data


@_fault_skip(mm=True)
def test_precise_memmanage_fault_reports_the_blocked_region(gdb_session):
    with _with_gate(gdb_session, "gdr_fault_gate"):
        _stop_at(gdb_session, "gdr_fault_mm_stop")
        _stop_at(gdb_session, "MemManage_Handler")
        raw = raw_fault_frame(gdb_session)
        mmfar = int(
            gdb_session.run("p/x *(unsigned int *)0xE000ED34").split("=")[1].strip(),
            16,
        )
        assert raw["frame_xpsr"] & _T_BIT and raw["frame_xpsr"] & 0x1FF == 0, raw
        assert raw["xpsr"] & 0x1FF == 4, raw
        assert "gdr_fault_memmanage_leaf" in raw["frame_pc_symbol"], raw
        assert raw["cfsr"] & 0x00000002 and raw["cfsr"] & 0x00000080, raw
        assert mmfar == 0x60000000, hex(mmfar)

        data = _trace_active(gdb_session, "gdr_fault")
        assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
        evidence = " ".join(data["messages"])
        assert "MemManage" in evidence and "DACCVIOL" in evidence, data
        assert "MMFAR=0x60000000" in evidence, data
        transitions = _exception_frames(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["frame_pc"], data


@_fault_skip(precise=True)
def test_precise_bus_fault_reports_the_faulting_data_address(gdb_session):
    with _with_gate(gdb_session, "gdr_fault_gate"):
        _stop_at_fault(gdb_session, "gdr_fault_bus_stop")
        raw = raw_fault_frame(gdb_session)
        bfar = int(
            gdb_session.run("p/x *(unsigned int *)0xE000ED38").split("=")[1].strip(),
            16,
        )
        assert raw["frame_xpsr"] & _T_BIT and raw["frame_xpsr"] & 0x1FF == 0, raw
        assert raw["xpsr"] & 0x1FF == 3, raw
        assert "gdr_fault_bus_leaf" in raw["frame_pc_symbol"], raw
        assert raw["cfsr"] & 0x00000200 and raw["cfsr"] & 0x00008000, raw
        assert bfar == _BUS_FAULT_ADDR, hex(bfar)

        data = _trace_active(gdb_session, "gdr_fault")
        assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
        evidence = " ".join(data["messages"])
        assert "PRECISERR" in evidence and f"BFAR={_BUS_FAULT_ADDR:#x}" in evidence, (
            data
        )
        transitions = _exception_frames(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["frame_pc"], data


@_fault_skip(m0=True)
def test_m0_hardfault_without_cfsr_is_imprecise(gdb_session):
    with _with_gate(gdb_session, "gdr_fault_gate"):
        _stop_at(gdb_session, "HardFault_Handler")
        data = _backtrace(gdb_session, "gdr_fault")
        assert data["task"] == "gdr_fault" and data["architecture"] == _ISA, data
        assert not data["complete"], data
        assert data["stop_reason"] == "fault-frame-imprecise", data
        assert data["frames"] == [], data


@_fault_skip(precise=True)
def test_fault_backtrace_preserves_target_registers_and_stack_memory(gdb_session):
    with _with_gate(gdb_session, "gdr_fault_gate"):
        _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
        result = verify_readonly_backtrace(gdb_session, "gdr_fault", include_tick=False)
        assert result["unchanged"] is True, result
        assert result["stop"] == "synchronous-fault", result
        assert result["events"] == [], result


@_fault_skip(precise=True)
def test_fault_backtrace_inside_handler_callee_unwinds_msp_and_psp_frames(gdb_session):
    with _with_gate(gdb_session, "gdr_fault_gate"):
        _stop_at(gdb_session, "gdr_fault_ud_stop")
        _stop_at(gdb_session, "gdr_fault_frame")
        hf_data = _trace_active(gdb_session, "gdr_fault")
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
            mm_data = _trace_active(gdb_session, "gdr_fault")
            assert (
                mm_data["complete"] and mm_data["stop_reason"] == "synchronous-fault"
            ), mm_data
            mm_symbols = [frame["symbol"] or "" for frame in mm_data["frames"]]
            assert "gdr_fault_frame" in mm_symbols[0], mm_symbols
            assert "MemManage_Handler" in mm_symbols[1], mm_symbols
            assert mm_data["frames"][2]["method"] == "exception", mm_data
            assert "gdr_fault_memmanage_leaf" in mm_symbols[2], mm_symbols
            assert any("gdr_fault_task" in sym for sym in mm_symbols), mm_symbols


@_irq_skip()
def test_ehabi_only_isr_entry_leaf_fallback_and_elf_vector_fallback(gdb_session):
    """At ISR entry, EXIDX-only metadata falls back to leaf+exception and ELF vectors."""
    with _with_gate(gdb_session, "gdr_irq_gate"):
        _stop_at(gdb_session, "Timer0_Handler if gdr_irq_scenario == 0")
        raw = raw_irq_state(gdb_session, "psp", _ISA)
        assert (raw["xpsr"] & 0x1FF) == _TIMER0_EXC, raw
        probe = json_line(
            gdb_session.run_python(
                """
import dataclasses, json
from core.adapter import active
import freertos.backtrace.unwind_gdb as bt

orig_meta = bt._unwind_metadata
def ehabi_only(adapter):
    meta = orig_meta(adapter)
    if meta is None:
        return None
    return dataclasses.replace(meta, cfi_tables=())

bt._unwind_metadata = ehabi_only
try:
    exidx_rep = bt.backtrace(active(), "gdr_irq")
finally:
    bt._unwind_metadata = orig_meta

orig_read = bt.read_bytes
def mask_vector_mmio(address, size):
    if address < 0x200 or 0xE000E000 <= address <= 0xE000EFFF:
        if address in (0xE000E004, 0xE000ED08) or address < 0x200:
            return None
    return orig_read(address, size)

bt.read_bytes = mask_vector_mmio
try:
    vec_rep = bt.backtrace(active(), "gdr_irq")
finally:
    bt.read_bytes = orig_read

print(json.dumps({
    "exidx_complete": exidx_rep.complete,
    "exidx_stop": exidx_rep.stop_reason,
    "exidx_methods": [f.method for f in exidx_rep.frames],
    "exidx_symbols": [f.symbol or "" for f in exidx_rep.frames],
    "exidx_pcs": [f.pc for f in exidx_rep.frames],
    "vec_complete": vec_rep.complete,
    "vec_stop": vec_rep.stop_reason,
    "vec_bounds": vec_rep.bounds,
    "vec_methods": [f.method for f in vec_rep.frames],
    "vec_pcs": [f.pc for f in vec_rep.frames],
}))
"""
            )
        )
        assert probe["exidx_complete"] is True, probe
        assert probe["exidx_stop"] == "task-exit", probe
        assert probe["exidx_methods"][:2] == ["seed", "exception"], probe
        assert probe["exidx_pcs"][1] == raw["frame_pc"] & ~1, (probe, raw)
        assert any("gdr_irq_task" in sym for sym in probe["exidx_symbols"]), probe
        assert probe["vec_complete"] is True, probe
        assert probe["vec_stop"] == "task-exit", probe
        assert "exception" in probe["vec_methods"], probe
        assert probe["vec_pcs"][probe["vec_methods"].index("exception")] == (
            raw["frame_pc"] & ~1
        ), (probe, raw)


@contextlib.contextmanager
def _paused_session():
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


@_irq_skip()
def test_mprofile_nmi_and_debug_monitor_cross_stack():
    """Hardware NMI (IPSR=2) and DebugMonitor (IPSR=12) unwind across MSP->PSP."""
    with _paused_session() as gdb:
        gdb.run("tbreak gdr_ready_spin")
        hit = gdb.run("continue", timeout=30)
        assert "Temporary breakpoint" in hit, hit
        gdb.run("stepi 2")
        pre = json_line(
            gdb.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
print(json.dumps({
    "pc": int(frame.read_register("pc")) & ~1,
    "psp": int(frame.read_register("psp")),
}))
"""
            )
        )
        gdb.run("tbreak Default_Handler")
        gdb.run("set *(unsigned int *)0xE000ED04 = 0x80000000")
        nmi_hit = gdb.run("continue", timeout=15)
        assert "Temporary breakpoint" in nmi_hit, nmi_hit
        nmi_raw = json_line(
            gdb.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
xpsr = int(frame.read_register("xpsr"))
print(json.dumps({"xpsr": xpsr, "ipsr": xpsr & 0x1FF}))
"""
            )
        )
        assert nmi_raw["ipsr"] == 2, nmi_raw
        nmi_bt = _backtrace(gdb, "gdr_spin")
        assert nmi_bt["complete"] is True and nmi_bt["stop_reason"] == "task-exit", (
            nmi_bt
        )
        assert any(f["method"] == "exception" for f in nmi_bt["frames"]), nmi_bt
        exc_frame = next(f for f in nmi_bt["frames"] if f["method"] == "exception")
        assert exc_frame["pc"] == pre["pc"], (exc_frame, pre)
        assert any("gdr_ready_spin" in (f["symbol"] or "") for f in nmi_bt["frames"]), (
            nmi_bt
        )

        if _ISA in _PRECISE_ISAS:
            dm_xpsr = (nmi_raw["xpsr"] & ~0x1FF) | 12
            gdb.run(f"set $xpsr = {dm_xpsr:#x}")
            dm_bt = _backtrace(gdb, "gdr_spin")
            assert dm_bt["complete"] is True and dm_bt["stop_reason"] == "task-exit", (
                dm_bt
            )
            exc_dm = next(f for f in dm_bt["frames"] if f["method"] == "exception")
            assert exc_dm["pc"] == pre["pc"], (exc_dm, pre)
            assert any(
                "gdr_ready_spin" in (f["symbol"] or "") for f in dm_bt["frames"]
            ), dm_bt


def _rv_profile() -> RiscvSchedulerProfile:
    global _RV_PROFILE_CACHE
    if _RV_PROFILE_CACHE is None:
        profile, reason = build_riscv_profile(ElfImage.from_path(SPEC.elf_path()))
        assert profile is not None, reason
        _RV_PROFILE_CACHE = profile
    return _RV_PROFILE_CACHE


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


@pytest.fixture(scope="module")
def first_start_rv_session():
    if not SPEC.target.startswith("qemu-virt-rv"):
        pytest.skip("requires a FreeRTOS QEMU RISC-V fixture")
    with _paused_session() as session:
        yield session


@_rv_skip()
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

        assert riscv_isr_yield_owner_probe(gdb_session, raw["tcb"]) == {
            "current_task": "gdr_spin",
            "current_source": "current",
            "current_complete": True,
            "switched_source": "saved",
            "switched_complete": True,
        }
        trap_entry = _symbol_address(gdb_session, "freertos_risc_v_trap_handler")
        assert riscv_vectored_mtvec_probe(
            gdb_session,
            task="gdr_spin",
            trap_entry=trap_entry,
            xlen=_XLEN,
        ) == {"complete": True, "stop_reason": "task-exit"}
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


@_rv_skip()
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


@_rv_skip()
def test_riscv_trap_prologue_stages_preserve_interrupted_task(gdb_session):
    prof = _rv_profile()
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


@_rv_skip()
def test_riscv_ecall_yield_and_switch_stages(gdb_session):
    prof = _rv_profile()
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


@_rv_skip()
def test_riscv_first_start_stages_unwind_first_task(first_start_rv_session):
    prof = _rv_profile()
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

    with _paused_session() as popped_session:
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


@_rv_skip()
def test_riscv_scheduler_profile_and_sync_fault_fail_closed(gdb_session):
    prof = _rv_profile()
    gdb_session.run("set var gdr_irq_hold = 0")
    try:
        _stop_at(gdb_session, f"*{prof.switch_start:#x}")
        switch_task = current_task_name(gdb_session)
        mismatch = json_line(
            gdb_session.run_python(
                f"""
import json
from core.adapter import active
import freertos.backtrace.unwind_gdb as bt

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
import freertos.backtrace.unwind_gdb as bt

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
import freertos.backtrace.unwind_gdb as bt

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


@pytest.mark.skipif(
    SPEC.target != "mps2-an505",
    reason="requires FreeRTOS Armv8-M Mainline PSPLIM fixture",
)
def test_armv8m_nonzero_psplim_admits_valid_and_refuses_invalid_bounds(gdb_session):
    """Valid 8B-aligned PSPLIM passes saved and live unwinds; invalid bounds refuse."""
    saved_probe = json_line(
        gdb_session.run_python(
            """
import gdb, json
from core.adapter import active
from freertos.adapter import find_task
import freertos.backtrace.unwind_gdb as bt

tcb = find_task("gdr_ready", active().layout)
sp = int(tcb["pxTopOfStack"])
low = int(tcb["pxStack"])
inf = gdb.selected_inferior()
orig_psplim = bytes(inf.read_memory(sp, 4))
valid_psplim = (low + 7) & ~7

def _write_psplim(val):
    inf.write_memory(sp, val.to_bytes(4, "little"))

try:
    _write_psplim(valid_psplim)
    ok_rep = bt.backtrace(active(), "gdr_ready")
    _write_psplim(valid_psplim + 1)
    unaligned_rep = bt.backtrace(active(), "gdr_ready")
    exc_ret = int.from_bytes(inf.read_memory(sp + 4, 4), "little")
    caller_sp = sp + (208 if exc_ret == 0xFFFFFFED else 72)
    _write_psplim(caller_sp + 8)
    above_rep = bt.backtrace(active(), "gdr_ready")
finally:
    inf.write_memory(sp, orig_psplim)

print(json.dumps({
    "ok_complete": ok_rep.complete,
    "ok_stop": ok_rep.stop_reason,
    "unaligned_reason": unaligned_rep.stop_reason,
    "above_reason": above_rep.stop_reason,
}))
"""
        )
    )
    assert saved_probe == {
        "ok_complete": True,
        "ok_stop": "task-exit",
        "unaligned_reason": "unsupported-extension-context",
        "above_reason": "unsupported-extension-context",
    }, saved_probe

    _halt(gdb_session)
    _write_u32(gdb_session, "gdr_irq_hold", 1)
    try:
        _stop_at(gdb_session, "gdr_rv_spin_leaf")
        live_info = json_line(
            gdb_session.run_python(
                """
import gdb, json
from core.adapter import active
from freertos.adapter import find_task
frame = gdb.newest_frame()
tcb = find_task("gdr_spin", active().layout)
print(json.dumps({
    "psplim": int(frame.read_register("psplim")),
    "spin_low": int(tcb["pxStack"]),
    "control": int(frame.read_register("control")),
}))
"""
            )
        )
        assert live_info["psplim"] == live_info["spin_low"], live_info
        assert (live_info["control"] & 2) == 2 and (live_info["psplim"] & 0x7) == 0, (
            live_info
        )
        ok_live = _backtrace(gdb_session, "gdr_spin")
        assert ok_live["complete"] is True and ok_live["stop_reason"] == "task-exit", (
            ok_live
        )
        live_neg = json_line(
            gdb_session.run_python(
                f"""
import gdb, json
from core.adapter import active
import freertos.backtrace.unwind_gdb as bt

sp_now = int(gdb.newest_frame().read_register("psp"))
orig = bt._read_armv8m_regs
try:
    bt._read_armv8m_regs = lambda: ((sp_now + 0x100) & ~7, {live_info["control"]})
    above_rep = bt.backtrace(active(), "gdr_spin")
    bt._read_armv8m_regs = lambda: ({live_info["psplim"]}, {live_info["control"] | 1})
    unpriv_rep = bt.backtrace(active(), "gdr_spin")
finally:
    bt._read_armv8m_regs = orig
print(json.dumps({{
    "above_complete": above_rep.complete,
    "above_reason": above_rep.stop_reason,
    "unpriv_complete": unpriv_rep.complete,
    "unpriv_reason": unpriv_rep.stop_reason,
}}))
"""
            )
        )
        assert live_neg == {
            "above_complete": False,
            "above_reason": "unsupported-extension-context",
            "unpriv_complete": False,
            "unpriv_reason": "unsupported-extension-context",
        }, live_neg
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")
