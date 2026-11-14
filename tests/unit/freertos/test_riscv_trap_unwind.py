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
    trap_words[0] = 0x80003010  # epc inside spin_leaf
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
    monkeypatch.setattr(bridge, "_riscv_snapshot", lambda *, xlen=32: (snap, "ok"))  # noqa: ARG005
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


def test_freertos_riscv_refuses_vtaskswitchcontext_and_ecall(monkeypatch):
    adapter, _ = _setup_frt_session(monkeypatch, seed_pc=0x80002810, ra=0x800010C4)
    report_direct = bridge.backtrace(adapter, "spin")
    assert not report_direct.complete
    assert report_direct.stop_reason == "scheduler-transition-unproven"

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
