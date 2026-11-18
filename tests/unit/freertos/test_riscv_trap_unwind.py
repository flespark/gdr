"""Unit tests for FreeRTOS RISC-V trap profile and GDB backtrace orchestration."""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import freertos.backtrace_gdb as bridge
from core.stack_frame_parser import StackContextSelection
from core.unwind import SeedFrame, UnwindMetadata
from core.unwind_cfi import CfaRule, CfiRow, CfiTable, RegisterRule
from core.unwind_meta import CodeSymbol, ElfImage, ElfSection
from core.unwind_riscv import (
    MCAUSE_EXC_ECALL_M,
    MSTATUS_MPP_MACHINE,
    RiscvSnapshot,
    RiscvTrapFrameLayout,
    RiscvTrapSpan,
)
from freertos.adapter import FreeRtosTask
from freertos.backtrace import RISCV32_INTEGER
from freertos.layout import FreeRtosConfig, build_layout
from freertos.stack_frame_profile import analyze_riscv_trap_profile


def _trap_span() -> RiscvTrapSpan:
    layout = RiscvTrapFrameLayout(
        frame_bytes=124,
        total_frame_bytes=124,
        reg_offsets={1: 4, **{reg: (reg - 3) * 4 for reg in range(5, 32)}},
        epc_offset=0,
        mstatus_offset=120,
        epc_sync_addend=4,
    )
    return RiscvTrapSpan(
        entry=0x80001000,
        save_complete=0x80001080,
        call_start=0x80001080,
        call_end=0x800010E0,
        exit_end=0x80001160,
        layout=layout,
        refused_spans=(
            (0x80001004, 0x80001080, "exception-frame-incomplete"),
            (0x800010E0, 0x80001160, "scheduler-transition-unproven"),
            (0x80002800, 0x80002880, "scheduler-transition-unproven"),
            (0x800010C4, 0x800010C8, "scheduler-transition-unproven"),
        ),
        evidence=("trap=freertos_risc_v_trap_handler@0x80001000",),
    )


def _setup_frt_session(
    monkeypatch, *, seed_pc=0x80002010, mcause=0x80000007, ra=0x800010C0
):
    layout = build_layout(FreeRtosConfig())
    selection = StackContextSelection("riscv32", "riscv", RISCV32_INTEGER, "ok")
    adapter = SimpleNamespace(
        layout=layout, _context_selection=lambda: selection, _unwind_cache=None
    )
    task = FreeRtosTask(
        name="spin",
        address=0x80010000,
        top_of_stack=0x80011E00,
        stack_base=0x80011000,
        stack_size=0x1000,
    )
    cfi = CfiTable.from_rows(
        [
            (
                0x80002000,
                0x80002040,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -4)}),
            ),
            (
                0x80002800,
                0x80002880,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -4)}),
            ),
            (
                0x80003000,
                0x80003040,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -4)}),
            ),
        ],
        return_register=1,
        address_size=4,
    )
    elf = ElfImage(
        pointer_size=4,
        endian="little",
        machine=243,
        sections=(ElfSection(".text", 0x80001000, b"\x00" * 0x4000, 0x4, 1),),
        symbols=(
            CodeSymbol("freertos_risc_v_trap_handler", 0x80001000, 0x160),
            CodeSymbol("xTaskIncrementTick", 0x80002000, 0x40),
            CodeSymbol("vTaskSwitchContext", 0x80002800, 0x80),
            CodeSymbol("spin_leaf", 0x80003000, 0x40),
            CodeSymbol("prvTaskExitError", 0x80004000, 0x20),
        ),
    )
    meta = UnwindMetadata(
        elf=elf,
        arch="riscv",
        cfi_tables=(cfi,),
        ehabi=None,
        return_register=1,
        frame_pointer_register=8,
    )
    frame_addr = 0x80011E00
    trap_words = [0] * 31
    trap_words[0] = 0x80003010 if (mcause & 0x80000000) else 0x80003014
    trap_words[1] = 0x80003999
    trap_words[30] = MSTATUS_MPP_MACHINE | 0x80
    mem: dict[int, bytes] = {}
    for idx, val in enumerate(trap_words):
        mem[frame_addr + idx * 4] = struct.pack("<I", val)
    mem[0x80010000] = struct.pack("<I", frame_addr)  # pxTopOfStack
    mem[0x8001FFE0 + 12] = struct.pack("<I", ra)  # C ISR ra -> handle_interrupt
    mem[frame_addr + 124 + 28] = struct.pack(
        "<I", 0x80004000
    )  # spin_leaf ra -> prvTaskExitError

    def _read_bytes(addr: int, size: int) -> bytes | None:
        out = bytearray()
        cursor = addr
        while len(out) < size:
            chunk = mem.get(cursor)
            if chunk is None:
                return None
            take = min(size - len(out), len(chunk))
            out.extend(chunk[:take])
            cursor += take
        return bytes(out)

    snap = RiscvSnapshot(
        xlen=32,
        pc=seed_pc,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=mcause,
        mepc=0x80003010,
        mtvec=0x80001000,
        registers={1: ra, 2: 0x8001FFE0, 8: 0x80020000},
    )
    state = {"tcb": 0x80010000, "running": True}
    monkeypatch.setattr(bridge, "active", lambda: adapter)
    monkeypatch.setattr(bridge, "find_task", lambda _target, _layout: object())
    monkeypatch.setattr(bridge, "task_state", lambda _value, _layout: ("Running", None))
    monkeypatch.setattr(bridge, "value_to_task", lambda *_args, **_kwargs: task)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, 0x80010000)])
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: meta)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (4, "little"))
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: state["running"])
    monkeypatch.setattr(
        bridge,
        "read_int",
        lambda expr: state["tcb"] if expr == "pxCurrentTCB" else None,
    )
    monkeypatch.setattr(bridge, "eval_identifier", lambda name: name)
    monkeypatch.setattr(bridge, "read_bytes", _read_bytes)
    monkeypatch.setattr(bridge, "_exit_pc", lambda: 0x80004000)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda addr: f"sym_{addr:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _addr: None)
    monkeypatch.setattr(
        bridge,
        "_riscv_isr_stack_bounds",
        lambda _elf: (0x8001F000, 0x80020000, "symbol:isr_stack"),
    )
    monkeypatch.setattr(bridge, "analyze_riscv_trap_profile", lambda _elf: _trap_span())
    monkeypatch.setattr(
        bridge,
        "_live_seed_detail",
        lambda _arch, _isa: (
            SeedFrame(
                seed_pc,
                0x8001FFE0,
                {1: ra, 2: 0x8001FFE0, 8: 0x80020000},
                source="current",
            ),
            "ok",
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_riscv_snapshot",
        lambda *, xlen=32: (snap, "ok"),  # noqa: ARG005
    )
    monkeypatch.setattr(
        bridge,
        "_live_seed_detail",
        lambda _arch, _isa: (
            SeedFrame(
                0x80002010,
                0x8001FFE0,
                {1: 0x800010C0, 2: 0x8001FFE0, 8: 0x80020000},
                source="current",
            ),
            "ok",
        ),
    )
    return adapter, state


def test_freertos_riscv_isr_unwinds_to_task_stack(monkeypatch):
    adapter, _state = _setup_frt_session(monkeypatch)
    report = bridge.backtrace(adapter, "spin")
    assert report.complete is True
    assert report.stop_reason == "task-exit"
    assert [f.pc for f in report.frames] == [0x80002010, 0x80003010, 0x80004000]
    assert report.frames[0].segment.startswith("exception:")
    assert report.frames[1].segment == "task:0x80010000"
    assert report.frames[2].segment == "task:0x80010000"


def test_freertos_riscv_precise_sync_fault_and_unmodeled_refusal(monkeypatch):
    adapter, _ = _setup_frt_session(monkeypatch, seed_pc=0x80002010, mcause=2)
    report = bridge.backtrace(adapter, "spin")
    assert report.complete is True
    assert report.stop_reason == "synchronous-fault"
    assert [f.pc for f in report.frames] == [0x80002010, 0x80003010, 0x80004000]
    assert report.frames[1].method == "exception"
    assert any("Exception:IllegalInstruction" in msg for msg in report.messages)

    adapter_bad, _ = _setup_frt_session(monkeypatch, seed_pc=0x80002010, mcause=24)
    refused = bridge.backtrace(adapter_bad, "spin")
    assert refused.complete is False
    assert refused.stop_reason == "exception-frame-type-unproven"

    adapter_ebreak, _ = _setup_frt_session(monkeypatch, seed_pc=0x80002010, mcause=3)
    refused_ebreak = bridge.backtrace(adapter_ebreak, "spin")
    assert refused_ebreak.complete is False
    assert refused_ebreak.stop_reason == "exception-frame-type-unproven"


def test_freertos_riscv_refuses_unverified_vtaskswitchcontext_and_ecall(monkeypatch):
    adapter, _ = _setup_frt_session(monkeypatch, seed_pc=0x80002810, ra=0x800010C4)
    report_direct = bridge.backtrace(adapter, "spin")
    assert not report_direct.complete
    assert report_direct.stop_reason == "scheduler-profile-unsupported-image"

    adapter, _ = _setup_frt_session(monkeypatch, seed_pc=0x80002010, ra=0x800010C4)
    report_callee = bridge.backtrace(adapter, "spin")
    assert not report_callee.complete
    assert report_callee.stop_reason == "scheduler-transition-unproven"

    adapter, _ = _setup_frt_session(
        monkeypatch, seed_pc=0x80002010, mcause=MCAUSE_EXC_ECALL_M
    )
    report_ecall = bridge.backtrace(adapter, "spin")
    assert not report_ecall.complete
    assert report_ecall.stop_reason == "scheduler-transition-unproven"


def test_freertos_riscv_refuses_tcb_mismatch_and_unverified_span(monkeypatch):
    adapter, state = _setup_frt_session(monkeypatch)
    state["tcb"] = 0x80099999
    report = bridge.backtrace(adapter, "spin")
    assert not report.complete
    assert report.stop_reason == "exception-task-context-conflict"

    adapter, state = _setup_frt_session(monkeypatch)
    state["running"] = False
    assert bridge.backtrace(adapter, "spin").stop_reason == "scheduler-not-running"

    adapter, _ = _setup_frt_session(monkeypatch)
    monkeypatch.setattr(bridge, "analyze_riscv_trap_profile", lambda _elf: None)
    assert bridge.backtrace(adapter, "spin").stop_reason == "exception-model-unverified"


@pytest.mark.parametrize(
    "elf_path",
    [
        "ci/.cache/fixtures/freertos/qemu-virt-rv32/11.1.0/base/freertos.elf",
        "ci/.cache/fixtures/freertos/qemu-virt-rv64/11.1.0/rv64/freertos.elf",
    ],
)
def test_cached_freertos_riscv_elfs_produce_verified_trap_span(elf_path):
    path = Path(elf_path)
    if not path.is_file():
        pytest.skip(f"{elf_path} not cached")
    elf = ElfImage.from_bytes(path.read_bytes())
    assert elf is not None
    span = analyze_riscv_trap_profile(elf)
    assert span is not None
    assert span.call_start < span.call_end < span.exit_end
    assert span.layout.epc_offset == 0
    assert span.layout.epc_sync_addend == 4
    app_exc_pc = elf.symbol_address("application_exception_handler")
    if app_exc_pc is not None:
        assert span.call_start <= app_exc_pc < span.call_end
        assert span.refused_reason(app_exc_pc) is None


def test_freertos_riscv_port_yield_from_isr_separates_old_and_new_tasks(monkeypatch):
    cfi = CfiTable.from_rows(
        [
            (
                0x80002000,
                0x80002040,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -4)}),
            ),
            (
                0x80001080,
                0x80001100,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -4)}),
            ),
            (
                0x80003000,
                0x80003040,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -4)}),
            ),
            (
                0x80003800,
                0x80003840,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -4)}),
            ),
        ],
        return_register=1,
        address_size=4,
    )
    elf = ElfImage(
        pointer_size=4,
        endian="little",
        machine=243,
        sections=(ElfSection(".text", 0x80001000, b"\x00" * 0x3100, 0x6, 4),),
        symbols=(
            CodeSymbol("freertos_risc_v_trap_handler", 0x80001000, 0x100),
            CodeSymbol("isr_callee", 0x80002000, 0x40),
            CodeSymbol("old_leaf", 0x80003000, 0x40),
            CodeSymbol("new_leaf", 0x80003800, 0x40),
            CodeSymbol("prvTaskExitError", 0x80004000, 0x20),
        ),
    )
    meta = UnwindMetadata(
        elf=elf,
        arch="riscv",
        cfi_tables=(cfi,),
        ehabi=None,
        return_register=1,
        frame_pointer_register=8,
    )
    old_task = FreeRtosTask(
        name="old_t",
        address=0x80010000,
        top_of_stack=0x80011E00,
        stack_base=0x80011000,
        stack_size=0x1000,
    )
    new_task = FreeRtosTask(
        name="new_t",
        address=0x80020000,
        top_of_stack=0x80021E00,
        stack_base=0x80021000,
        stack_size=0x1000,
    )
    tasks_by_token = {"tok_old": old_task, "tok_new": new_task}
    tokens_by_name = {
        "old_t": "tok_old",
        "new_t": "tok_new",
        "0x80010000": "tok_old",
        "0x80020000": "tok_new",
    }
    addr_by_token = {"tok_old": old_task.address, "tok_new": new_task.address}

    mem: dict[int, bytes] = {}
    old_words = [0] * 31
    old_words[0] = 0x80003010
    old_words[1] = 0x80003999
    old_words[30] = MSTATUS_MPP_MACHINE | 0x80
    for idx, val in enumerate(old_words):
        mem[old_task.top_of_stack + idx * 4] = struct.pack("<I", val)
    mem[old_task.top_of_stack + 124 + 28] = struct.pack("<I", 0x80004000)

    new_words = [0] * 31
    new_words[0] = 0x80003810
    new_words[1] = 0x80003777
    new_words[30] = MSTATUS_MPP_MACHINE | 0x80
    for idx, val in enumerate(new_words):
        mem[new_task.top_of_stack + idx * 4] = struct.pack("<I", val)
    mem[new_task.top_of_stack + 124 + 28] = struct.pack("<I", 0x80004000)
    # Peripheral ISR frame on xISRStack returns to 0x800010C0 (inside trap_span)
    mem[0x8001FFE0 + 12] = struct.pack("<I", 0x800010C0)

    def _read(addr: int, size: int) -> bytes | None:
        out = bytearray()
        cursor = addr
        while len(out) < size:
            chunk = mem.get(cursor)
            if chunk is None:
                return None
            take = min(size - len(out), len(chunk))
            out.extend(chunk[:take])
            cursor += take
        return bytes(out)

    snap = RiscvSnapshot(
        xlen=32,
        pc=0x80002010,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=0x80000018,  # peripheral external IRQ
        mepc=0x80003010,  # interrupted old_t at old_leaf
        mtvec=0x80001000,
        registers={1: 0x800010C0, 2: 0x8001FFE0, 8: 0x80020000},
    )
    layout = build_layout(FreeRtosConfig())
    selection = StackContextSelection("riscv32", "riscv", RISCV32_INTEGER, "ok")
    adapter = SimpleNamespace(
        layout=layout, _context_selection=lambda: selection, _unwind_cache=None
    )

    monkeypatch.setattr(bridge, "active", lambda: adapter)
    monkeypatch.setattr(bridge, "find_task", lambda key, _l: tokens_by_name.get(key))
    monkeypatch.setattr(
        bridge,
        "iter_tasks",
        lambda _l: [("tok_old", "Ready", None), ("tok_new", "Running", 0)],
    )
    monkeypatch.setattr(bridge, "value_address", lambda tok: addr_by_token[tok])
    monkeypatch.setattr(
        bridge,
        "task_state",
        lambda tok, _l: ("Running", 0) if tok == "tok_new" else ("Ready", None),
    )
    monkeypatch.setattr(
        bridge, "value_to_task", lambda tok, *_a, **_kw: tasks_by_token[tok]
    )
    monkeypatch.setattr(bridge, "current_tasks", lambda _l: [(0, new_task.address)])
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _a: meta)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (4, "little"))
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(
        bridge,
        "read_int",
        lambda expr: new_task.address if expr == "pxCurrentTCB" else None,
    )
    monkeypatch.setattr(bridge, "eval_identifier", lambda name: name)
    monkeypatch.setattr(bridge, "read_bytes", _read)
    monkeypatch.setattr(bridge, "_exit_pc", lambda: 0x80004000)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda addr: f"sym_{addr:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _addr: None)
    monkeypatch.setattr(
        bridge,
        "_riscv_isr_stack_bounds",
        lambda _elf: (0x8001F000, 0x80020000, "symbol:isr_stack"),
    )
    monkeypatch.setattr(bridge, "analyze_riscv_trap_profile", lambda _elf: _trap_span())
    monkeypatch.setattr(
        bridge, "build_riscv_scheduler_profile", lambda _elf: (None, "none")
    )
    monkeypatch.setattr(
        bridge,
        "_riscv_snapshot",
        lambda *, xlen=32: (snap, "ok"),  # noqa: ARG005
    )
    monkeypatch.setattr(
        bridge,
        "_live_seed_detail",
        lambda _arch, _isa: (
            SeedFrame(
                0x80002010,
                0x8001FFE0,
                {1: 0x800010C0, 2: 0x8001FFE0, 8: 0x80020000},
                source="current",
            ),
            "ok",
        ),
    )

    # 1. Default `frt bt` tracks interrupted old_t across ISR stack -> old_leaf -> prvTaskExitError.
    cur_rep = bridge.backtrace(adapter)
    assert cur_rep.complete is True
    assert cur_rep.task == "old_t"
    assert cur_rep.source == "current"
    assert [f.pc for f in cur_rep.frames] == [0x80002010, 0x80003010, 0x80004000]

    # 2. Explicit `frt bt old_t` also crosses ISR -> old_t.
    old_rep = bridge.backtrace(adapter, "old_t")
    assert old_rep.complete is True
    assert old_rep.source == "current"
    assert [f.pc for f in old_rep.frames] == [0x80002010, 0x80003010, 0x80004000]

    # 3. Explicit `frt bt new_t` uses saved context, NEVER attaching the active ISR!
    new_rep = bridge.backtrace(adapter, "new_t")
    assert new_rep.complete is True
    assert new_rep.source == "saved"
    assert [f.pc for f in new_rep.frames] == [0x80003810, 0x80004000]

    # 4. Two tasks matching snap.mepc refuse with exception-task-context-conflict.
    mem[new_task.top_of_stack] = struct.pack("<I", 0x80003010)
    conflict_rep = bridge.backtrace(adapter)
    assert conflict_rep.complete is False
    assert conflict_rep.stop_reason == "exception-task-context-conflict"
