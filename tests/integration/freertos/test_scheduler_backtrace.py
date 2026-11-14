"""Live scheduler-transition ownership on the deterministic AN385 fixture.

Every stop is a real instruction breakpoint inside the compiled V10.3.1
``portable/GCC/ARM_CM3`` PendSV/SVC path.  The oracle reads raw TCBs, saved
buffers, PSP/MSP, live registers and stacked frames through GDB; it never
derives the expected task from GDR's report.  Synthetic seed mutations are not
used here: the gate only selects a real cooperative, tick or IRQ path.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.unwind_meta import ElfImage
from freertos.scheduler_switch import SchedulerProfile, build_profile
from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile
from tests.support.gdb_process import has_embedded_python
from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession, check_tools

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos"
    or (SPEC.variant, SPEC.target, SPEC.version)
    not in (
        ("sched", "mps2-an385", "10.3.1"),
        ("sched", "mps2-an386", "11.3.1"),
        ("sched", "microbit", "11.1.0"),
        ("sched", "mps2-an505", "11.3.1"),
    ),
    reason="requires a FreeRTOS M-profile scheduler fixture lane",
)

GDR_ROOT = Path(__file__).resolve().parents[3]
_PORT_SPECS = {
    "mps2-an385": ("arm-cm3", 0x20, 0x00, False, 24, 25),
    "mps2-an386": ("arm-cm4f", 0x24, 0x00, False, 24, 25),
    "microbit": ("arm-cm0", 0x24, 0x04, True, 24, 25),
    "mps2-an505": ("arm-cm33", 0x28, 0x08, True, 19, 20),
}
_PORT, _SW_SIZE, _R4_OFF, _EXC_RET_IN_R3, _TIMER0_EXC, _TIMER1_EXC = _PORT_SPECS.get(
    SPEC.target, _PORT_SPECS["mps2-an385"]
)
_PROFILE_CACHE: SchedulerProfile | None = None


def _profile() -> SchedulerProfile:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        profile, reason = build_profile(ElfImage.from_path(SPEC.elf_path()))
        assert profile is not None, reason
        _PROFILE_CACHE = profile
    return _PROFILE_CACHE


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


def _backtrace(session, task: str) -> dict:
    return _envelope(session.run(f"gdr api backtrace {task} --json", timeout=30))


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


def _write_u32(session, name: str, value: int) -> None:
    session.run_python(
        f"""
import gdb
address = int(gdb.parse_and_eval("&{name}"))
gdb.selected_inferior().write_memory(address, ({value}).to_bytes(4, "little"))
"""
    )


def _state(session) -> dict:
    prof = _profile()
    return _json_line(
        session.run_python(
            f"""
import gdb, json

def u16(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 2)), "little")

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {{address:#x}}", to_string=True).split(" in section ")[0].strip()

def tcb(name):
    address = int(gdb.parse_and_eval(name))
    value = gdb.parse_and_eval(f"(struct tskTaskControlBlock*){{address:#x}}")
    top = int(value["pxTopOfStack"])
    return {{
        "addr": address,
        "top": top,
        "base": int(value["pxStack"]),
        "end": int(value["pxEndOfStack"]) + 4,
        "saved": [u32(top + {_R4_OFF} + 4 * index) for index in range(8)],
        "frame": [u32(top + {_SW_SIZE} + 4 * index) for index in range(8)],
    }}

handles = {{
    name: int(gdb.parse_and_eval(name))
    for name in ("gdr_sched_task_handle", "gdr_sched_tick_handle",
                 "gdr_sched_irq_handle", "gdr_sched_first_handle")
}}
tcbs = {{name: tcb(name) for name in handles}}
current = int(gdb.parse_and_eval("pxCurrentTCB"))
psp_val = int(gdb.parse_and_eval("$psp"))
print(json.dumps({{
    "pc": int(gdb.parse_and_eval("$pc")),
    "sp": int(gdb.parse_and_eval("$sp")),
    "msp": int(gdb.parse_and_eval("$msp")),
    "psp": psp_val,
    "psp_frame": [u32(psp_val + 4 * index) for index in range(8)] if psp_val else [],
    "saved_buffer": [u32(psp_val - {_SW_SIZE} + {_R4_OFF} + 4 * index) for index in range(8)] if psp_val else [],
    "xpsr": int(gdb.parse_and_eval("$xpsr")),
    "lr": int(gdb.parse_and_eval("$lr")) & 0xFFFFFFFF,
    "regs": {{f"r{{index}}": int(gdb.parse_and_eval(f"$r{{index}}")) & 0xFFFFFFFF for index in range(13)}},
    "current": current,
    "current_symbol": symbol(current),
    "handles": handles,
    "tcbs": tcbs,
    "gate": int(gdb.parse_and_eval("gdr_sched_gate")),
    "phase": int(gdb.parse_and_eval("gdr_sched_phase")),
    "done": int(gdb.parse_and_eval("gdr_sched_done")),
    "irq_requested": int(gdb.parse_and_eval("gdr_sched_irq_requested")),
    "yield_count": int(gdb.parse_and_eval("gdr_sched_yield_count")),
    "first_started": int(gdb.parse_and_eval("gdr_sched_first_started")),
    "tick_count": int(gdb.parse_and_eval("xTickCount")),
    "pendsv_pc_symbol": symbol(int(gdb.parse_and_eval("&PendSV_Handler"))),
    "svc_pc_symbol": symbol(int(gdb.parse_and_eval("&{prof.svc_symbol}"))),
}}))
"""
        )
    )


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
    assert not any(
        "PendSV_Handler" in name or "SVC_Handler" in name for name in symbols
    ), symbols


def _assert_task_frame(state: dict, tcb_name: str) -> None:
    tcb = state["tcbs"][tcb_name]
    assert tcb["base"] <= tcb["top"] < tcb["end"], state
    assert tcb["frame"][7] & (1 << 24), state
    assert tcb["frame"][7] & 0x1FF == 0, state
    assert tcb["frame"][6] & 1 == 0, state


def _assert_psp_frame(state: dict, tcb_name: str) -> None:
    tcb = state["tcbs"][tcb_name]
    frame = state["psp_frame"]
    assert len(frame) == 8, state
    assert tcb["base"] <= state["psp"] <= tcb["end"], state
    assert frame[7] & (1 << 24), state
    assert frame[7] & 0x1FF == 0, state
    assert frame[6] & 1 == 0, state


def _frame_at(session, address: int) -> dict:
    return _json_line(
        session.run_python(
            f"""
import gdb, json

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {{address:#x}}", to_string=True).split(" in section ")[0].strip()

words = [u32({address:#x} + 4 * index) for index in range(8)]
print(json.dumps({{
    "base": {address:#x},
    "words": words,
    "pc": words[6],
    "pc_symbol": symbol(words[6]),
    "xpsr": words[7],
    "ipsr": words[7] & 0x1FF,
}}))
"""
        )
    )


def _compiled_oracle(session) -> dict:
    prof = _profile()
    values = _json_line(
        session.run_python(
            f"""
import gdb, json

def u16(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 2)), "little")

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

pendsv = int(gdb.parse_and_eval("&PendSV_Handler"))
svc = int(gdb.parse_and_eval("&{prof.svc_symbol}"))
start = int(gdb.parse_and_eval("&{prof.start_first_symbol}"))
switch = int(gdb.parse_and_eval("&vTaskSwitchContext"))
print(json.dumps({{
    "pendsv": pendsv, "svc": svc, "start": start, "switch": switch,
    "pendsv_entry": u32(pendsv),
    "pendsv_save": u32({prof.pendsv_save_pending:#x}),
    "pendsv_psp": u32({prof.pendsv_restored_pending_psp:#x}),
    "pendsv_return": u16({prof.pendsv_exception_return:#x}),
    "svc_entry": u32(svc),
    "svc_restored": u32({prof.svc_restored_pending_psp:#x}),
    "svc_return": u16({prof.svc_exception_return:#x}),
    "start_svc": u16({prof.start_first_svc:#x}),
    "switch_update": u16({prof.switch_update:#x}),
    "switch_after": u16({prof.switch_after_update:#x}),
    "sys_tick": int(gdb.parse_and_eval("&SysTick_Handler")),
    "timer0": int(gdb.parse_and_eval("&Timer0_Handler")),
    "timer1": int(gdb.parse_and_eval("&Timer1_Handler")),
}}))
"""
        )
    )
    return values


def test_compiled_transition_bytes_match_the_independent_oracle(gdb_session):
    prof = _profile()
    oracle = _compiled_oracle(gdb_session)
    assert oracle["pendsv"] == prof.pendsv_entry
    assert oracle["svc"] == prof.svc_entry
    assert oracle["start"] == prof.start_first_start
    assert oracle["switch"] == prof.switch_entry
    if _PORT == "arm-cm3":
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


def test_cooperative_yield_marks_old_task_before_and_after_the_tcb_writeback(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 1)
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
    assert state["xpsr"] & (1 << 24), state
    assert state["lr"] == 0xFFFFFFFD, state
    _assert_psp_frame(state, "gdr_sched_task_handle")
    old_pc = state["psp_frame"][6]
    entry_psp = state["psp"]

    data = _backtrace(gdb_session, "gdr_sched")
    assert data["source"] == "current" and data["complete"], data
    assert data["frames"][0]["pc"] == old_pc, data
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
            lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
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
    assert state["tcbs"]["gdr_sched_task_handle"]["top"] == state["psp"] - _SW_SIZE, (
        state
    )
    data = _backtrace(gdb_session, "gdr_sched")
    assert data["source"] == "saved" and data["complete"], data
    assert data["frames"][0]["pc"] == old_pc, data
    evidence = " ".join(data["messages"])
    assert "stage=pendsv-writeback" in evidence and "owner=old" in evidence, data
    _finish_cycle(gdb_session)


def test_tick_preemption_updates_current_tcb_inside_vTaskSwitchContext(gdb_session):
    prof = _profile()
    _set_gate(gdb_session, 2)
    oracle = _compiled_oracle(gdb_session)
    tick = _wait_stage(
        gdb_session,
        oracle["sys_tick"],
        lambda item: (
            item["current"] == item["handles"]["gdr_sched_task_handle"]
            and item["phase"] == 2
        ),
    )
    assert tick["xpsr"] & 0x1FF == 15, tick
    assert tick["xpsr"] & (1 << 24), tick
    tick_frame = _frame_at(gdb_session, tick["psp"])
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
    old = _state(gdb_session)
    _assert_psp_frame(old, "gdr_sched_task_handle")

    call = _wait_stage(
        gdb_session,
        prof.pendsv_switch_call,
        lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
    )
    assert call["lr"] == 0xFFFFFFFD, call
    data = _backtrace(gdb_session, "gdr_sched")
    assert data["source"] == "saved" and data["complete"], data
    assert "stage=pendsv-switch-call" in " ".join(data["messages"]), data
    _assert_no_cross_task(data["frames"], "gdr_sched_task", "gdr_sched_tick_hi_task")

    entry_state = _wait_stage(
        gdb_session,
        prof.switch_entry,
        lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
    )
    assert (entry_state["lr"] & ~1) == prof.pendsv_after_switch, entry_state
    data = _backtrace(gdb_session, "gdr_sched")
    assert data["source"] == "saved" and data["complete"], data
    assert "stage=switch-entry" in " ".join(data["messages"]), data

    update = prof.switch_update
    state = _wait_stage(
        gdb_session,
        update,
        lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
    )
    assert state["tcbs"]["gdr_sched_task_handle"]["top"] == state["psp"] - _SW_SIZE, (
        state
    )
    data = _backtrace(gdb_session, "gdr_sched")
    assert data["source"] == "saved" and data["complete"], data
    assert "stage=switch-before-update" in " ".join(data["messages"]), data

    gdb_session.run("stepi", timeout=10)
    after = _state(gdb_session)
    assert after["current"] == after["handles"]["gdr_sched_tick_handle"], after
    assert after["pc"] == prof.switch_after_update, after
    tick = after["tcbs"]["gdr_sched_tick_handle"]
    _assert_task_frame(after, "gdr_sched_tick_handle")
    data = _backtrace(gdb_session, "gdr_sched_tick")
    assert data["source"] == "saved", data
    assert data["frames"][0]["pc"] == tick["frame"][6] & ~1, (data, after)
    _assert_no_cross_task(data["frames"], "gdr_sched_tick_hi_task", "gdr_sched_task")
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
    tick_top = state["tcbs"]["gdr_sched_tick_handle"]["top"]
    assert state["regs"]["r0"] == tick_top, state
    data = _backtrace(gdb_session, "gdr_sched_tick")
    assert data["source"] == "saved", data
    assert "stage=pendsv-saved-sp" in " ".join(data["messages"]), data
    _finish_cycle(gdb_session)


def test_restore_window_distinguishes_restored_cores_from_the_old_psp(gdb_session):
    prof = _profile()
    _set_gate(gdb_session, 2)
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
        assert [state["regs"][f"r{index}"] for index in range(8, 12)] == tick["saved"][
            4:
        ], state
    else:
        assert [state["regs"][f"r{index}"] for index in range(4, 12)] == tick[
            "saved"
        ], state
    data = _backtrace(gdb_session, "gdr_sched_tick")
    assert data["source"] == "current", data
    assert data["frames"][0]["pc"] == tick["frame"][6] & ~1, data
    _assert_no_cross_task(data["frames"], "gdr_sched_tick_hi_task", "gdr_sched_task")
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
        assert data["frames"][0]["pc"] == tick["frame"][6] & ~1, data
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
    assert tick["top"] + _SW_SIZE <= returned["psp"] <= tick["top"] + _SW_SIZE + 0x20, (
        returned
    )
    assert returned["current"] == returned["handles"]["gdr_sched_tick_handle"], returned
    assert returned["pc"] != prof.pendsv_exception_return, returned
    _finish_cycle(gdb_session)


def test_nested_irq_truly_preempts_timer0_before_requesting_scheduling(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 3)
    oracle = _compiled_oracle(gdb_session)
    timer0 = _wait_stage(
        gdb_session,
        oracle["timer0"],
        lambda item: (
            item["current"] == item["handles"]["gdr_sched_task_handle"]
            and item["phase"] == 3
        ),
    )
    assert timer0["xpsr"] & 0x1FF == _TIMER0_EXC, timer0
    task_frame = _frame_at(gdb_session, timer0["psp"])
    assert task_frame["ipsr"] == 0, task_frame
    assert "gdr_sched_task" in task_frame["pc_symbol"], task_frame

    timer1 = _wait_stage(
        gdb_session,
        oracle["timer1"],
        lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
    )
    assert timer1["xpsr"] & 0x1FF == _TIMER1_EXC, timer1
    nested_frame = _frame_at(gdb_session, timer1["msp"])
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
    assert state["xpsr"] & (1 << 24), state
    assert state["lr"] == 0xFFFFFFFD, state
    _assert_psp_frame(state, "gdr_sched_task_handle")
    data = _backtrace(gdb_session, "gdr_sched")
    assert data["source"] == "current" and data["complete"], data
    _assert_no_cross_task(data["frames"], "gdr_sched_task", "Timer1_Handler")
    _finish_cycle(gdb_session)


def test_nested_irq_requesting_scheduling_keeps_the_interrupted_task_owner(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 3)
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
    assert state["xpsr"] & (1 << 24), state
    assert state["lr"] == 0xFFFFFFFD, state
    _assert_psp_frame(state, "gdr_sched_task_handle")
    data = _backtrace(gdb_session, "gdr_sched")
    assert data["source"] == "current" and data["complete"], data
    _assert_no_cross_task(data["frames"], "gdr_sched_task", "Timer1_Handler")
    symbols = " ".join(frame["symbol"] or "" for frame in data["frames"])
    assert "Timer0_Handler" not in symbols and "Timer1_Handler" not in symbols, data

    update = prof.switch_update
    _wait_stage(
        gdb_session,
        update,
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


def test_unsupported_and_mismatched_scheduler_stops_are_refused(gdb_session):
    """The profile-mismatch half injects the builder in live GDB.

    That half is synthetic instrumentation, not hardware evidence of a
    different compiled body; the ambiguous-PC half is a real live stop.
    """
    prof = _profile()
    _set_gate(gdb_session, 1)
    state = _wait_stage(
        gdb_session,
        prof.pendsv_entry + 4,
        lambda item: item["current"] == item["handles"]["gdr_sched_task_handle"],
    )
    assert state["pc"] == prof.pendsv_entry + 4, state
    data = _backtrace(gdb_session, "gdr_sched")
    assert data["frames"] == [], data
    assert data["stop_reason"].startswith("scheduler-stage-ambiguous"), data

    output = gdb_session.run_python(
        """
import json
import freertos.backtrace_gdb as bt
from core.adapter import active

original = bt.build_scheduler_profile
bt.build_scheduler_profile = lambda _elf: (
    None,
    "scheduler-profile-mismatch:test",
)
try:
    report = bt.backtrace(active(), "gdr_sched")
finally:
    bt.build_scheduler_profile = original
print("DATA=" + json.dumps({
    "stop_reason": report.stop_reason,
    "frames": [frame.pc for frame in report.frames],
}))
"""
    )
    data = json.loads(
        next(
            line.split("DATA=", 1)[1]
            for line in output.splitlines()
            if line.startswith("DATA=")
        )
    )
    assert data["frames"] == [], data
    assert data["stop_reason"] == "scheduler-profile-mismatch:test", data

    _finish_cycle(gdb_session)


def test_irq_saved_paths_still_complete_while_stopped_in_a_scheduler_stage(
    gdb_session,
):
    prof = _profile()
    _set_gate(gdb_session, 1)
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


@pytest.fixture(scope="module")
def first_start_session():
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


def test_first_task_start_uses_the_independent_svc_restore_path(first_start_session):
    session = first_start_session
    prof = _profile()
    start = prof.start_first_start
    session.run(f"tbreak *{start:#x}")
    output = session.run("continue", timeout=20)
    assert "breakpoint" in output.lower(), output
    state = _state(session)
    assert state["current"] == state["handles"]["gdr_sched_first_handle"], state
    data = _backtrace(session, "gdr_sched_first")
    assert data["frames"] == [], data
    assert data["stop_reason"].startswith("scheduler-stage-ambiguous"), data

    session.run(f"tbreak *{prof.start_first_svc:#x}")
    output = session.run("continue", timeout=20)
    assert "breakpoint" in output.lower(), output
    state = _state(session)
    assert state["pc"] == prof.start_first_svc, state
    data = _backtrace(session, "gdr_sched_first")
    assert data["frames"] == [], data
    assert data["stop_reason"] == "scheduler-first-start-pending", data

    session.run(f"tbreak *{prof.svc_entry:#x}")
    output = session.run("continue", timeout=20)
    assert "breakpoint" in output.lower(), output
    state = _state(session)
    assert state["xpsr"] & 0x1FF == 11, state
    assert state["xpsr"] & (1 << 24), state
    if prof.svc_symbol == "SVC_Handler":
        assert state["lr"] == 0xFFFFFFF9, state
    assert state["psp"] == 0, state
    first = state["tcbs"]["gdr_sched_first_handle"]
    data = _backtrace(session, "gdr_sched_first")
    assert data["source"] == "unstarted", data
    assert data["complete"] and data["stop_reason"] == "task-entry", data
    assert data["frames"][0]["pc"] == first["frame"][6] & ~1, data
    assert "stage=svc-entry" in " ".join(data["messages"]), data

    session.run(f"tbreak *{prof.svc_saved_sp:#x}")
    output = session.run("continue", timeout=20)
    assert "breakpoint" in output.lower(), output
    saved = _state(session)
    assert saved["psp"] == 0, saved
    assert saved["regs"]["r0"] == first["top"], saved
    data = _backtrace(session, "gdr_sched_first")
    assert data["complete"] and data["source"] == "unstarted", (
        data["stop_reason"],
        data["messages"],
        data,
    )
    assert "stage=svc-saved-sp" in " ".join(data["messages"]), data

    session.run(f"tbreak *{prof.svc_restored_pending_psp:#x}")
    output = session.run("continue", timeout=20)
    assert "breakpoint" in output.lower(), output
    restored = _state(session)
    assert restored["psp"] == 0, restored
    assert restored["regs"]["r0"] == first["top"] + _SW_SIZE, restored
    data = _backtrace(session, "gdr_sched_first")
    assert data["complete"] and data["source"] == "unstarted", (
        data["stop_reason"],
        data["messages"],
        data,
    )
    assert "stage=svc-restored-pending-psp" in " ".join(data["messages"]), data

    session.run(f"tbreak *{prof.svc_psp_written:#x}")
    output = session.run("continue", timeout=20)
    assert "breakpoint" in output.lower(), output
    written = _state(session)
    assert written["psp"] == first["top"] + _SW_SIZE, written
    data = _backtrace(session, "gdr_sched_first")
    assert data["complete"] and data["source"] == "unstarted", (
        data["stop_reason"],
        data["messages"],
        data,
    )
    assert "stage=svc-psp-written" in " ".join(data["messages"]), data

    session.run(f"tbreak *{prof.svc_exception_return:#x}")
    output = session.run("continue", timeout=20)
    assert "breakpoint" in output.lower(), output
    pending = _state(session)
    exc_ret = pending["regs"]["r2"] if _EXC_RET_IN_R3 else pending["lr"]
    assert exc_ret == 0xFFFFFFFD, pending
    data = _backtrace(session, "gdr_sched_first")
    assert data["complete"] and data["source"] == "unstarted", (
        data["stop_reason"],
        data["messages"],
        data,
    )
    assert "stage=svc-exception-return" in " ".join(data["messages"]), data

    session.run("stepi", timeout=10)
    returned = _state(session)
    assert (
        first["top"] + _SW_SIZE <= returned["psp"] <= first["top"] + _SW_SIZE + 0x20
    ), returned
    assert returned["current"] == returned["handles"]["gdr_sched_first_handle"], (
        returned
    )
    assert returned["pc"] != prof.svc_exception_return, returned
