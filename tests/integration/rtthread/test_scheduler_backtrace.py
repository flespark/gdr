"""Live scheduler-transition ownership tests for RT-Thread Arm M-profile ports."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.insn_thumb import decode_thumb
from core.unwind_meta import ElfImage
from rtthread.scheduler_switch import SchedulerProfile, build_profile
from tests.support.gdb_process import has_embedded_python
from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession, check_tools
from tests.support.rtthread_qemu_profiles import get_rtthread_qemu_profile

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread"
    or SPEC.version != "4.0.5"
    or SPEC.variant not in {"", "fpu"}
    or SPEC.target not in {"microbit", "mps2-an385", "mps2-an386", "mps2-an505"}
    or (SPEC.target == "mps2-an386" and SPEC.variant != "fpu"),
    reason="requires an RT-Thread M-profile fixture lane",
)

GDR_ROOT = Path(__file__).resolve().parents[3]
_ISA_BY_TARGET = {
    "microbit": "armv6-m",
    "mps2-an385": "armv7-m",
    "mps2-an386": "armv7e-m",
    "mps2-an505": "armv8-m.mainline",
}
_EXPECTED_ISA = _ISA_BY_TARGET.get(SPEC.target, "")
_PROFILE_CACHE: SchedulerProfile | None = None


def _profile() -> SchedulerProfile:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        profile = build_profile(ElfImage.from_path(SPEC.elf_path()))
        assert profile is not None, (
            f"unverified RT-Thread scheduler profile: {SPEC.target}"
        )
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
    assert envelope["schema_version"] == 3, json.dumps(envelope)
    assert envelope["ok"] is True, json.dumps(envelope)
    return envelope["data"]


def _backtrace(session, thread_name: str) -> dict:
    return _envelope(session.run(f"gdr api backtrace {thread_name} --json", timeout=30))


def _state(session) -> dict:
    return _json_line(
        session.run_python(
            """
import gdb, json

def u32(addr):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(addr, 4)), "little")

def read_name(tcb_val):
    name_field = tcb_val["name"]
    raw = bytes(gdb.selected_inferior().read_memory(int(name_field.address), int(name_field.type.sizeof)))
    return raw.split(b"\\x00", 1)[0].decode("ascii", errors="replace")

thread_type = gdb.lookup_type("struct rt_thread")
sp_off = next(int(f.bitpos) // 8 for f in thread_type.fields() if f.name == "sp")
current = int(gdb.parse_and_eval("rt_current_thread"))
from_ptr = int(gdb.parse_and_eval("rt_interrupt_from_thread"))
to_ptr = int(gdb.parse_and_eval("rt_interrupt_to_thread"))
flag = int(gdb.parse_and_eval("rt_thread_switch_interrupt_flag"))

from_tcb = (from_ptr - sp_off) if from_ptr else 0
to_tcb = (to_ptr - sp_off) if to_ptr else 0
from_name = read_name(gdb.Value(from_tcb).cast(thread_type.pointer())) if from_tcb else None
to_name = read_name(gdb.Value(to_tcb).cast(thread_type.pointer())) if to_tcb else None

container_sym = (
    gdb.lookup_static_symbol("rt_object_container")
    or gdb.lookup_static_symbol("_object_container")
    or gdb.lookup_global_symbol("rt_object_container")
    or gdb.lookup_global_symbol("_object_container")
)
thread_info = 0
try:
    thread_info = next(
        int(f.enumval)
        for f in gdb.lookup_type("enum rt_object_info_type").fields()
        if f.name == "RT_Object_Info_Thread"
    )
except gdb.error:
    thread_info = 0
container = container_sym.value()[thread_info]
head = int(container["object_list"].address)
list_off = next(int(f.bitpos) // 8 for f in thread_type.fields() if f.name == "list")
other_name = None
node = u32(head)
seen = 0
while node != head and seen < 64:
    seen += 1
    tcb = node - list_off
    if tcb not in (from_tcb, to_tcb, current):
        name = read_name(gdb.Value(tcb).cast(thread_type.pointer()))
        if name and not name.startswith("gdr_un"):
            other_name = name
            break
    node = u32(node)

psp = int(gdb.parse_and_eval("$psp"))
r1 = int(gdb.parse_and_eval("$r1")) & 0xFFFFFFFF
print(json.dumps({
    "pc": int(gdb.parse_and_eval("$pc")) & ~1,
    "xpsr": int(gdb.parse_and_eval("$xpsr")) & 0xFFFFFFFF,
    "psp": psp,
    "psp_pc": (u32(psp + 24) & ~1) if psp else None,
    "r1": r1,
    "flag": flag,
    "from_ptr": from_ptr,
    "to_ptr": to_ptr,
    "current": current,
    "from_name": from_name,
    "to_name": to_name,
    "other_name": other_name,
}))
"""
        )
    )


def _stop_at(session, address: int) -> dict:
    session.run(f"tbreak *{address:#x}")
    output = session.run("continue", timeout=30)
    assert "breakpoint" in output.lower(), output
    return _state(session)


def _wait_non_first_pendsv(session, address: int, attempts: int = 20) -> dict:
    for _ in range(attempts):
        state = _stop_at(session, address)
        if (
            state["from_ptr"] != 0
            and state["to_ptr"] != 0
            and state["from_name"]
            and state["to_name"]
            and state["from_name"] != state["to_name"]
            and (address != _profile().pendsv_entry or state["flag"] == 1)
        ):
            return state
    raise AssertionError(f"no steady-state PendSV switch observed at {address:#x}")


def _assert_completed_owner(
    data: dict,
    *,
    stage: str,
    sources: set[str],
) -> None:
    dumped = json.dumps(data)
    assert data["architecture"] == _EXPECTED_ISA, dumped
    assert data["complete"] is True, dumped
    assert data["stop_reason"] in {"task-exit", "task-entry"}, dumped
    assert data["source"] in sources, dumped
    assert len(data["frames"]) >= 1, dumped
    assert any(f"scheduler stage {stage} " in msg for msg in data["messages"]), dumped


@pytest.fixture(scope="module")
def first_start_session():
    profile = replace(get_rtthread_qemu_profile(GDR_ROOT, SPEC), init_command=None)
    check_tools(profile, SPEC.gdb)
    if not has_embedded_python(SPEC.gdb):
        pytest.skip("GDB has no embedded Python")
    qemu = QemuSession(profile)
    qemu.start(SPEC.boot_wait, paused=True)
    session = GdbSession(SPEC.gdb, profile, qemu.gdb_port, GDR_ROOT)
    try:
        session.start()
        session.run(f"gdr init rtthread {SPEC.version}", timeout=30)
        yield session
    finally:
        session.stop()
        qemu.stop()


def test_first_thread_start_stages_on_real_boot(first_start_session):
    session = first_start_session
    prof = _profile()

    ambig_state = _stop_at(session, prof.switch_to_start)
    assert ambig_state["from_ptr"] == 0, ambig_state
    ambig = _backtrace(session, ambig_state["to_name"] or "main")
    assert ambig["complete"] is False, ambig
    assert ambig["frames"] == [], ambig
    assert (
        ambig["stop_reason"] == "scheduler-stage-ambiguous:rt_hw_context_switch_to"
    ), ambig

    entry_state = _stop_at(session, prof.pendsv_entry)
    assert entry_state["from_ptr"] == 0, entry_state
    to_name = entry_state["to_name"]
    assert to_name in {"main", "timer", "tidle0"}, entry_state
    entry_data = _backtrace(session, to_name)
    _assert_completed_owner(entry_data, stage="pendsv-entry", sources={"unstarted"})

    for stage_name, addr, expected_sources in (
        ("pendsv-switch-to", prof.pendsv_switch_to, {"unstarted"}),
        ("pendsv-saved-sp", prof.pendsv_saved_sp, {"unstarted"}),
        (
            "pendsv-restored-pending-psp",
            prof.pendsv_restored_pending_psp,
            {"unstarted"},
        ),
        ("pendsv-psp-written", prof.pendsv_psp_written, {"unstarted"}),
        ("pendsv-exception-return", prof.pendsv_exception_return, {"unstarted"}),
    ):
        state = _stop_at(session, addr)
        assert state["from_ptr"] == 0, state
        data = _backtrace(session, to_name)
        _assert_completed_owner(data, stage=stage_name, sources=expected_sources)


def test_pendsv_stages_across_one_switch_cycle(gdb_session):
    prof = _profile()
    entry = _wait_non_first_pendsv(gdb_session, prof.pendsv_entry)
    from_name = entry["from_name"]
    to_name = entry["to_name"]
    other_name = entry["other_name"]
    assert from_name and to_name and other_name, entry

    old_entry = _backtrace(gdb_session, from_name)
    _assert_completed_owner(old_entry, stage="pendsv-entry", sources={"current"})
    assert any(f["pc"] == entry["psp_pc"] for f in old_entry["frames"]), old_entry
    new_entry = _backtrace(gdb_session, to_name)
    _assert_completed_owner(
        new_entry, stage="pendsv-entry", sources={"saved", "unstarted"}
    )
    other_entry = _backtrace(gdb_session, other_name)
    _assert_completed_owner(
        other_entry, stage="pendsv-entry", sources={"saved", "unstarted"}
    )

    pending = _stop_at(gdb_session, prof.pendsv_save_pending)
    assert pending["from_name"] == from_name and pending["to_name"] == to_name
    old_pending = _backtrace(gdb_session, from_name)
    _assert_completed_owner(
        old_pending, stage="pendsv-save-pending", sources={"current"}
    )
    new_pending = _backtrace(gdb_session, to_name)
    _assert_completed_owner(
        new_pending, stage="pendsv-save-pending", sources={"saved", "unstarted"}
    )

    if prof.pendsv_save_written is not None:
        written = _stop_at(gdb_session, prof.pendsv_save_written)
        assert written["from_name"] == from_name and written["to_name"] == to_name
        old_written = _backtrace(gdb_session, from_name)
        _assert_completed_owner(
            old_written, stage="pendsv-save-written", sources={"saved-buffer"}
        )
        new_written = _backtrace(gdb_session, to_name)
        _assert_completed_owner(
            new_written, stage="pendsv-save-written", sources={"saved", "unstarted"}
        )

    for stage_name, addr, old_sources, new_sources in (
        ("pendsv-switch-to", prof.pendsv_switch_to, {"saved"}, {"saved", "unstarted"}),
        ("pendsv-saved-sp", prof.pendsv_saved_sp, {"saved"}, {"saved", "unstarted"}),
        (
            "pendsv-restored-pending-psp",
            prof.pendsv_restored_pending_psp,
            {"saved"},
            {"current", "unstarted"},
        ),
        (
            "pendsv-psp-written",
            prof.pendsv_psp_written,
            {"saved"},
            {"current", "unstarted"},
        ),
        (
            "pendsv-exception-return",
            prof.pendsv_exception_return,
            {"saved"},
            {"current", "unstarted"},
        ),
    ):
        state = _stop_at(gdb_session, addr)
        assert state["from_name"] == from_name and state["to_name"] == to_name
        old_data = _backtrace(gdb_session, from_name)
        _assert_completed_owner(old_data, stage=stage_name, sources=old_sources)
        new_data = _backtrace(gdb_session, to_name)
        _assert_completed_owner(new_data, stage=stage_name, sources=new_sources)


def test_switch_return_and_ambiguous_handler_stages(gdb_session):
    prof = _profile()

    sw_ret = _wait_non_first_pendsv(gdb_session, prof.switch_return)
    old_ret = _backtrace(gdb_session, sw_ret["from_name"])
    _assert_completed_owner(old_ret, stage="switch-return", sources={"current"})
    new_ret = _backtrace(gdb_session, sw_ret["to_name"])
    _assert_completed_owner(
        new_ret, stage="switch-return", sources={"saved", "unstarted"}
    )

    elf = ElfImage.from_path(SPEC.elf_path())
    ambig_pendsv = None
    addr = prof.pendsv_entry
    while addr < prof.pendsv_save_pending:
        insn = decode_thumb(elf, addr, prof.pendsv_save_pending)
        assert not isinstance(insn, str), insn
        if insn.address > prof.pendsv_entry and insn.address not in prof.stages:
            ambig_pendsv = insn.address
            break
        addr += insn.size
    assert ambig_pendsv is not None
    ambig_state = _stop_at(gdb_session, ambig_pendsv)
    target_name = ambig_state["to_name"] or ambig_state["from_name"] or "tidle0"
    ambig_data = _backtrace(gdb_session, target_name)
    assert ambig_data["complete"] is False, ambig_data
    assert ambig_data["frames"] == [], ambig_data
    assert ambig_data["stop_reason"] == "scheduler-stage-ambiguous:PendSV_Handler", (
        ambig_data
    )

    ambig_switch = prof.switch_start
    assert ambig_switch not in prof.stages
    sw_state = _stop_at(gdb_session, ambig_switch)
    sw_target = sw_state["to_name"] or sw_state["from_name"] or "tidle0"
    sw_ambig = _backtrace(gdb_session, sw_target)
    assert sw_ambig["complete"] is False, sw_ambig
    assert sw_ambig["frames"] == [], sw_ambig
    assert (
        sw_ambig["stop_reason"] == "scheduler-stage-ambiguous:rt_hw_context_switch"
    ), sw_ambig
