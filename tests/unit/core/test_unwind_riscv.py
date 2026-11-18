"""Unit tests for generic RISC-V live seed, CSR validation, and trap transitions."""

from __future__ import annotations

import struct
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from core.unwind import UnwindMetadata, unwind_chain
from core.unwind_cfi import CfaRule, CfiRow, CfiTable, RegisterRule
from core.unwind_meta import CodeSymbol, ElfImage, ElfSection
from core.unwind_riscv import (
    MCAUSE_EXC_ECALL_M,
    MSTATUS_MPP_MACHINE,
    RiscvSnapshot,
    RiscvTrapFrameLayout,
    RiscvTrapSpan,
    decode_riscv_mcause,
    prepare_riscv_handler_unwind,
    resolve_riscv_irq_stack_bounds_detail,
    riscv_exception_guard,
    riscv_fault_precision,
    riscv_live_seed_detail,
    riscv_mtvec_targets_entry,
    riscv_snapshot,
    riscv_trap_transition_factory,
    u_xlen_mask,
)
from core.unwind_segments import ExceptionTransitionContext, StackSegment


class _Memory:
    def __init__(self, chunks: dict[int, bytes]) -> None:
        self.chunks = dict(chunks)

    def __call__(self, address: int, size: int) -> bytes | None:
        out = bytearray()
        cursor = address
        while len(out) < size:
            matched = None
            for base, blob in self.chunks.items():
                if base <= cursor < base + len(blob):
                    offset = cursor - base
                    take = min(size - len(out), len(blob) - offset)
                    out.extend(blob[offset : offset + take])
                    cursor += take
                    matched = take
                    break
            if matched is None:
                return None
        return bytes(out)


class _FakeGdbError(Exception):
    pass


def _fake_gdb(regs: dict[str, int], *, fail_on: set[str] | None = None):
    fail = fail_on or set()

    class _Frame:
        def pc(self):
            if "pc" in fail:
                raise _FakeGdbError("pc unavailable")
            return regs["pc"]

        def read_register(self, name: str):
            if name in fail or name not in regs:
                raise _FakeGdbError(f"{name} unavailable")
            return regs[name]

    return SimpleNamespace(error=_FakeGdbError, newest_frame=lambda: _Frame())


def _base_rv32_regs() -> dict[str, int]:
    return {
        "pc": 0x80001010,
        "sp": 0x8001FFE0,
        "ra": 0x80001008,
        "gp": 0x80008000,
        "tp": 0,
        "t0": 1,
        "t1": 2,
        "t2": 3,
        "s0": 0x80020000,
        "s1": 0x11,
        "a0": 0,
        "a1": 1,
        "a2": 2,
        "a3": 3,
        "a4": 4,
        "a5": 5,
        "a6": 6,
        "a7": 7,
        "s2": 0x22,
        "s3": 0x33,
        "s4": 0x44,
        "s5": 0x55,
        "s6": 0x66,
        "s7": 0x77,
        "s8": 0x88,
        "s9": 0x99,
        "s10": 0xAA,
        "s11": 0xBB,
        "t3": 4,
        "t4": 5,
        "t5": 6,
        "t6": 7,
        "mstatus": MSTATUS_MPP_MACHINE | 0x80,
        "mcause": -0x7FFFFFF9,  # signed 0x80000007
        "mepc": -0x7FFFEFE0,  # signed 0x80001020
        "mtvec": -0x7FFFF000,  # signed 0x80001000
    }


def _pack_words(base: int, values: list[int], *, word: int = 4) -> dict[int, bytes]:
    fmt = "<I" if word == 4 else "<Q"
    return {
        base + index * word: struct.pack(fmt, value & ((1 << (word * 8)) - 1))
        for index, value in enumerate(values)
    }


def _span_with_layout(
    *, epc_offset: int | None = 0, sync_addend: int = 4
) -> RiscvTrapSpan:
    layout = RiscvTrapFrameLayout(
        frame_bytes=128,
        total_frame_bytes=128,
        reg_offsets={1: 4, 8: 32, 9: 36},
        epc_offset=epc_offset,
        mstatus_offset=120,
        epc_sync_addend=sync_addend,
        frame_pointer_register=8 if epc_offset is None else None,
    )
    return RiscvTrapSpan(
        entry=0x80001000,
        save_complete=0x80001040,
        call_start=0x80001040,
        call_end=0x80001050,
        exit_end=0x80001090,
        layout=layout,
        refused_spans=(
            (0x80001004, 0x80001040, "exception-frame-incomplete"),
            (0x80001050, 0x80001090, "scheduler-transition-unproven"),
        ),
        evidence=("test-span",),
    )


def test_mcause_decode_and_guard():
    assert u_xlen_mask(32) == 0xFFFFFFFF
    assert decode_riscv_mcause(-0x7FFFFFF9, 32) == (True, 7)
    assert decode_riscv_mcause(MCAUSE_EXC_ECALL_M, 32) == (False, 11)
    assert riscv_exception_guard(0x80000007, 0x80001000, xlen=32) is None
    assert riscv_exception_guard((1 << 63) | 16, 0x80001000, xlen=64) is None
    assert (
        riscv_exception_guard(0x80000002, 0x80001000, xlen=32)
        == "exception-number-unverified"
    )
    assert (
        riscv_exception_guard(MCAUSE_EXC_ECALL_M, 0x80001000, xlen=32)
        == "scheduler-transition-unproven"
    )
    assert (
        riscv_exception_guard(MCAUSE_EXC_ECALL_M, 0x80001000, xlen=32, allow_ecall=True)
        is None
    )
    assert (
        riscv_exception_guard(2, 0x80001000, xlen=32, allow_sync=False)
        == "exception-frame-type-unproven"
    )
    assert riscv_exception_guard(64, 0x80001000, xlen=32) == "exception-number-invalid"
    assert riscv_exception_guard(2, 0x80001000, xlen=32) is None
    for code in (0, 1, 2, 4, 5, 6, 7):
        assert riscv_exception_guard(code, 0x80001000, xlen=32) is None
    for unmodeled in (3, 8, 9, 10, 12, 13, 15, 24):
        assert (
            riscv_exception_guard(unmodeled, 0x80001000, xlen=32)
            == "exception-frame-type-unproven"
        )

    assert riscv_fault_precision(0x80000007, 32, mepc=0x80001020) is None
    assert riscv_fault_precision(MCAUSE_EXC_ECALL_M, 32, mepc=0x80001020) is None
    assert riscv_fault_precision(24, 32, mepc=0x80001020) is None
    ill = riscv_fault_precision(2, 32, mepc=0x80001020, mtval=0x1234)
    assert ill is not None
    assert ill.precise is True
    assert ill.fault == "Exception:IllegalInstruction"
    assert "mcause=0x2" in ill.evidence
    assert "mepc=0x80001020" in ill.evidence
    assert "mtval=0x1234" in ill.evidence
    load_fault = riscv_fault_precision(5, 64, mepc=0x80001020, mtval=None)
    assert load_fault is not None
    assert load_fault.fault == "Exception:LoadAccessFault"
    assert "mtval=" not in load_fault.evidence


def test_riscv_snapshot_and_live_seed_mask_signed_gdb_ints():
    regs = _base_rv32_regs()
    fake_gdb = _fake_gdb(regs)
    snap, reason = riscv_snapshot(xlen=32, gdb_module=fake_gdb)
    assert reason == "ok"
    assert snap is not None
    assert snap.mcause == 0x80000007
    assert snap.mepc == 0x80001020
    assert snap.mtvec == 0x80001000
    assert snap.mtval is None
    assert snap.registers[2] == 0x8001FFE0

    snap_mtval, reason_mtval = riscv_snapshot(
        xlen=32, gdb_module=_fake_gdb({**regs, "mtval": 0xDEAD0001})
    )
    assert reason_mtval == "ok"
    assert snap_mtval is not None and snap_mtval.mtval == 0xDEAD0001

    seed_no_csr, reason = riscv_live_seed_detail(
        "riscv32", gdb_module=_fake_gdb(regs, fail_on={"mcause"})
    )
    assert reason == "ok"
    assert seed_no_csr.exception_number is None

    seed_with_csr, reason = riscv_live_seed_detail(
        "riscv32", require_exception_registers=True, gdb_module=fake_gdb
    )
    assert reason == "ok"
    assert seed_with_csr.exception_number == 0x80000007

    _, fail_gpr = riscv_snapshot(xlen=32, gdb_module=_fake_gdb(regs, fail_on={"sp"}))
    assert fail_gpr == "live-registers-unavailable"
    _, fail_csr = riscv_snapshot(xlen=32, gdb_module=_fake_gdb(regs, fail_on={"mtvec"}))
    assert fail_csr == "exception-registers-unavailable"
    assert (
        riscv_live_seed_detail("riscv32", gdb_module=None)[1]
        == "live-registers-unavailable"
    )


def test_resolve_irq_stack_bounds_precedence():
    elf = ElfImage(
        pointer_size=4,
        endian="little",
        machine=243,
        sections=(ElfSection(".text", 0x80000000, b"\x00" * 0x100, 0x4, 1),),
        nobits_sections=((".irq_stack", 0x80020000, 0x1000),),
    )
    assert resolve_riscv_irq_stack_bounds_detail(
        stack_object_bounds=lambda: (0x80030000, 0x80031000),
        elf=elf,
    ) == (0x80030000, 0x80031000, "symbol:isr_stack")
    assert resolve_riscv_irq_stack_bounds_detail(
        symbol_lookup={"__StackLimit": 0x8001E000, "_sp": 0x80020000}.get,
        elf=elf,
    ) == (0x8001E000, 0x80020000, "linker:__StackLimit.._sp")
    assert resolve_riscv_irq_stack_bounds_detail(
        symbol_lookup={"__stack_start__": 0x80040000, "__STACKSIZE__": 0x1000}.get,
        hart_id=1,
    ) == (0x80041000, 0x80042000, "linker:__stack_start__+__STACKSIZE__")
    assert resolve_riscv_irq_stack_bounds_detail(elf=elf) == (
        0x80020000,
        0x80021000,
        "section:.irq_stack",
    )
    assert resolve_riscv_irq_stack_bounds_detail() is None


def test_prepare_handler_refuses_bad_csrs_bounds_and_regions():
    span = _span_with_layout()
    task_seg = StackSegment(
        ident="task:0x80010000",
        owner="task:0x80010000",
        low=0x80011000,
        high=0x80012000,
        source="task-stack",
        stack="process",
        kind="task",
    )

    def irq_bounds():
        return (0x8001F000, 0x80020000, "symbol:isr_stack")

    base_regs = {2: 0x8001FFE0, 8: 0x80011E00, 1: 0x80001048}

    def _snap(**overrides) -> RiscvSnapshot:
        defaults: dict[str, Any] = dict(
            xlen=32,
            pc=0x80002000,
            sp=0x8001FFE0,
            mstatus=MSTATUS_MPP_MACHINE,
            mcause=0x80000007,
            mepc=0x80003000,
            mtvec=0x80001000,
            registers=base_regs,
        )
        defaults.update(overrides)
        return RiscvSnapshot(**defaults)

    def _reject_reason(snap: RiscvSnapshot, target_span=span) -> str:
        res = prepare_riscv_handler_unwind(
            snap,
            root_pc=snap.pc,
            span=target_span,
            task_segment=task_seg,
            irq_stack_bounds=irq_bounds,
            nest_count=1,
            endian="little",
            task_frame_resolver=lambda _ctx: 0x80011E00,
        )
        assert isinstance(res, tuple)
        return res[0]

    assert _reject_reason(_snap(pc=0x80001010)) == "exception-frame-incomplete"
    assert _reject_reason(_snap(pc=0x80001054)) == "scheduler-transition-unproven"
    assert _reject_reason(_snap(mtvec=0x80009001)) == "exception-vector-unproven"
    assert _reject_reason(_snap(mstatus=0)) == "unsupported-exception-model"
    assert _reject_reason(_snap(sp=0x80011E00)) == "exception-stack-outside-bounds"
    assert (
        _reject_reason(_snap(pc=span.entry, sp=0x8001FFE0))
        == "exception-task-context-conflict"
    )
    deferred_span = _span_with_layout(epc_offset=None)
    res_nested = prepare_riscv_handler_unwind(
        _snap(),
        root_pc=0x80002000,
        span=deferred_span,
        task_segment=task_seg,
        irq_stack_bounds=irq_bounds,
        nest_count=2,
        endian="little",
        task_frame_resolver=lambda _ctx: 0x80011E00,
    )
    assert isinstance(res_nested, tuple) and res_nested[0] == "nested-trap-epc-unproven"


def _make_riscv_cross_stack_env(
    *, epc_offset: int | None = 0, mcause: int = 0x80000007
):
    span = _span_with_layout(epc_offset=epc_offset, sync_addend=4)
    # Functions:
    # - trap dispatch: 0x80001040..0x80001050 (ra = caller inside trap call)
    # - tick_isr:      0x80002000..0x80002040 (uses CFI sp+16, ra at cfa-4)
    # - task_leaf:     0x80003000..0x80003040 (uses CFI sp+32, ra at cfa-4)
    # - task_root:     0x80004000..0x80004040 (stops at task-exit)
    cfi = CfiTable.from_rows(
        [
            (
                0x80002000,
                0x80002040,
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
            CodeSymbol("trap_entry", 0x80001000, 0x90),
            CodeSymbol("tick_isr", 0x80002000, 0x40),
            CodeSymbol("task_leaf", 0x80003000, 0x40),
            CodeSymbol("task_root", 0x80004000, 0x40),
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
    task_seg = StackSegment(
        ident="task:0x80010000",
        owner="task:0x80010000",
        low=0x80011000,
        high=0x80012000,
        source="task-stack",
        stack="process",
        kind="task",
    )
    frame_addr = 0x80011E00
    interrupted_sp = frame_addr + 128  # 0x80011E80
    # ISR stack at 0x8001FFE0: tick_isr frame size 16 -> return ra at 0x8001FFEC = 0x80001044
    # Task stack at frame_addr: 128-byte trap frame
    # Task interrupted frame at 0x80011E80: task_leaf size 32 -> return ra at 0x80011E9C = 0x80004010
    mem_map = {}
    mem_map.update(_pack_words(0x8001FFE0 + 12, [0x80001044]))
    trap_words = [0] * 32
    if epc_offset is not None:
        trap_words[0] = 0x80003014 if (mcause & 0x80000000) == 0 else 0x80003010
    trap_words[1] = 0x80003888
    trap_words[8] = 0x12345678  # interrupted s0
    trap_words[9] = 0x87654321
    trap_words[30] = MSTATUS_MPP_MACHINE | 0x80
    mem_map.update(_pack_words(frame_addr, trap_words))
    mem_map.update(_pack_words(interrupted_sp + 28, [0x80004010]))
    return span, meta, task_seg, frame_addr, _Memory(mem_map)


@pytest.mark.parametrize(
    "epc_offset,mcause,expect_source",
    [
        (0, 0x80000007, "riscv-trap-frame:stacked-epc"),
        (0, 2, "riscv-trap-frame:stacked-epc"),
        (None, 0x80000007, "riscv-trap-frame:mepc-csr"),
    ],
)
def test_unwind_chain_crosses_isr_to_task_stack(epc_offset, mcause, expect_source):
    span, meta, task_seg, frame_addr, mem = _make_riscv_cross_stack_env(
        epc_offset=epc_offset, mcause=mcause
    )
    snap = RiscvSnapshot(
        xlen=32,
        pc=0x80002010,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=mcause,
        mepc=0x80003010,
        mtvec=0x80001000,
        registers={1: 0x80001044, 2: 0x8001FFE0, 8: frame_addr},
    )
    setup = prepare_riscv_handler_unwind(
        snap,
        root_pc=0x80004010,
        span=span,
        task_segment=task_seg,
        irq_stack_bounds=lambda: (0x8001F000, 0x80020000, "symbol:isr_stack"),
        nest_count=1,
        endian="little",
        task_frame_resolver=lambda ctx: ctx.registers[8],
    )
    assert not isinstance(setup, tuple)
    result = unwind_chain(
        setup.seed,
        meta,
        read=mem,
        stack_low=setup.exception_segment.low,
        stack_high=setup.exception_segment.high,
        segments=(setup.exception_segment, task_seg),
        current_segment=setup.exception_segment.ident,
        exception_detector=setup.exception_detector,
        exception_hook=setup.exception_hook,
        exception_guard=setup.exception_guard,
    )
    assert [frame.pc for frame in result.frames] == [0x80002010, 0x80003010, 0x80004010]
    assert result.frames[0].segment == setup.exception_segment.ident
    assert result.frames[1].segment == task_seg.ident
    assert result.frames[2].segment == task_seg.ident
    assert result.stop_reason == "task-entry"
    assert result.complete
    assert len(result.transitions) == 1
    assert result.transitions[0].method == "exception"
    assert result.transitions[0].source == expect_source
    assert result.transitions[0].exact_pc is True
    assert result.transitions[0].sp == frame_addr + 128


def test_unwind_chain_at_raw_trap_entry():
    span, meta, task_seg, frame_addr, mem = _make_riscv_cross_stack_env(epc_offset=0)
    interrupted_sp = frame_addr + 128
    snap = RiscvSnapshot(
        xlen=32,
        pc=span.entry,
        sp=interrupted_sp,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=0x80000007,
        mepc=0x80003010,
        mtvec=0x80001000,
        registers={1: 0x80004010, 2: interrupted_sp, 8: 0x11111111},
    )
    setup = prepare_riscv_handler_unwind(
        snap,
        root_pc=0x80004010,
        span=span,
        task_segment=task_seg,
        irq_stack_bounds=lambda: (0x8001F000, 0x80020000, "symbol:isr_stack"),
        nest_count=1,
        endian="little",
        task_frame_resolver=lambda _ctx: frame_addr,
    )
    assert not isinstance(setup, tuple)
    result = unwind_chain(
        setup.seed,
        meta,
        read=mem,
        stack_low=setup.exception_segment.low,
        stack_high=setup.exception_segment.high,
        segments=(setup.exception_segment, task_seg),
        current_segment=setup.exception_segment.ident,
        exception_detector=setup.exception_detector,
        exception_hook=setup.exception_hook,
        exception_guard=setup.exception_guard,
    )
    assert [frame.pc for frame in result.frames] == [span.entry, 0x80003010, 0x80004010]
    assert result.transitions[0].source == "riscv-trap-entry-live"
    assert result.stop_reason == "task-entry"
    assert result.complete


def test_transition_factory_edge_failures():
    span, _meta, task_seg, frame_addr, mem = _make_riscv_cross_stack_env(epc_offset=0)
    # Corrupt mstatus.MPP inside stacked trap frame
    bad_mstatus_mem = _Memory({**mem.chunks, frame_addr + 120: struct.pack("<I", 0)})
    setup = prepare_riscv_handler_unwind(
        RiscvSnapshot(
            xlen=32,
            pc=0x80002010,
            sp=0x8001FFE0,
            mstatus=MSTATUS_MPP_MACHINE,
            mcause=0x80000007,
            mepc=0x80003010,
            mtvec=0x80001000,
            registers={1: 0x80001044, 2: 0x8001FFE0, 8: frame_addr},
        ),
        root_pc=0x80004010,
        span=span,
        task_segment=task_seg,
        irq_stack_bounds=lambda: (0x8001F000, 0x80020000, "symbol:isr_stack"),
        nest_count=1,
        endian="little",
        task_frame_resolver=lambda _ctx: frame_addr,
    )
    assert not isinstance(setup, tuple)
    ctx = ExceptionTransitionContext(
        value=0x80001044,
        registers={8: frame_addr},
        frame_sp=0x8001FFE0,
        current_segment=setup.exception_segment,
        segments={
            setup.exception_segment.ident: setup.exception_segment,
            task_seg.ident: task_seg,
        },
        process_sp=None,
        read=bad_mstatus_mem,
        stkalign=True,
        remaining_exception_bytes=512,
        guard=setup.exception_guard,
    )
    assert setup.exception_hook(ctx) == "unsupported-exception-model"
    assert (
        setup.exception_hook(replace(ctx, remaining_exception_bytes=16))
        == "exception-read-budget-exceeded"
    )
    assert (
        setup.exception_hook(replace(ctx, read=_Memory({})))
        == "exception-frame-unreadable"
    )
    assert setup.exception_hook(replace(ctx, current_segment=task_seg)) is None
    hook_missing_seg = riscv_trap_transition_factory(
        xlen=32,
        endian="little",
        mcause=0x80000007,
        mepc_csr=0x80003010,
        nest_count=1,
        span=span,
        task_frame_resolver=lambda _c: frame_addr,
    )
    assert (
        hook_missing_seg(replace(ctx, segments={}))
        == "exception-target-segment-missing"
    )
    hook_resolver_err = riscv_trap_transition_factory(
        xlen=32,
        endian="little",
        mcause=0x80000007,
        mepc_csr=0x80003010,
        nest_count=1,
        span=span,
        task_frame_resolver=lambda _c: "exception-frame-missing",
    )
    assert hook_resolver_err(ctx) == "exception-frame-missing"
    hook_oob = riscv_trap_transition_factory(
        xlen=32,
        endian="little",
        mcause=0x80000007,
        mepc_csr=0x80003010,
        nest_count=1,
        span=span,
        task_frame_resolver=lambda _c: 0x80000100,
    )
    assert hook_oob(ctx) == "exception-frame-outside-bounds"


def test_prepare_riscv_handler_unwind_attaches_fault_only_on_sync_faults():
    span = _span_with_layout(epc_offset=0, sync_addend=4)
    task_seg = StackSegment(
        ident="task:1",
        owner="spin",
        low=0x80010000,
        high=0x80012000,
        source="task-recorded",
        stack="process",
        kind="task",
    )
    snap_irq = RiscvSnapshot(
        xlen=32,
        pc=0x80002010,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=0x80000007,
        mepc=0x80003010,
        mtvec=0x80001000,
        registers={1: 0x800010C0, 2: 0x8001FFE0, 8: 0x80011E00},
    )
    setup_irq = prepare_riscv_handler_unwind(
        snap_irq,
        root_pc=0x80004000,
        span=span,
        task_segment=task_seg,
        irq_stack_bounds=lambda: (0x8001F000, 0x80020000, "symbol:irq"),
        nest_count=1,
        endian="little",
        task_frame_resolver=lambda _c: 0x80011E00,
    )
    assert not isinstance(setup_irq, tuple)
    assert setup_irq.fault is None

    snap_fault = RiscvSnapshot(
        xlen=32,
        pc=0x80002010,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=2,
        mepc=0x80003010,
        mtvec=0x80001000,
        registers={1: 0x800010C0, 2: 0x8001FFE0, 8: 0x80011E00},
        mtval=0x1337,
    )
    setup_fault = prepare_riscv_handler_unwind(
        snap_fault,
        root_pc=0x80004000,
        span=span,
        task_segment=task_seg,
        irq_stack_bounds=lambda: (0x8001F000, 0x80020000, "symbol:irq"),
        nest_count=1,
        endian="little",
        task_frame_resolver=lambda _c: 0x80011E00,
    )
    assert not isinstance(setup_fault, tuple)
    assert setup_fault.fault is not None
    assert setup_fault.fault.fault == "Exception:IllegalInstruction"
    assert "mtval=0x1337" in setup_fault.fault.evidence

    snap_bad = RiscvSnapshot(
        xlen=32,
        pc=0x80002010,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=24,
        mepc=0x80003010,
        mtvec=0x80001000,
        registers={1: 0x800010C0, 2: 0x8001FFE0, 8: 0x80011E00},
    )
    refused = prepare_riscv_handler_unwind(
        snap_bad,
        root_pc=0x80004000,
        span=span,
        task_segment=task_seg,
        irq_stack_bounds=lambda: (0x8001F000, 0x80020000, "symbol:irq"),
        nest_count=1,
        endian="little",
        task_frame_resolver=lambda _c: 0x80011E00,
    )
    assert isinstance(refused, tuple) and refused[0] == "exception-frame-type-unproven"


def test_riscv_mtvec_targets_entry_direct_and_vectored():
    from core.unwind_riscv import MTVEC_MODE_VECTORED

    def _jal(rd: int, pc: int, target: int) -> bytes:
        offset = target - pc
        word = (
            (((offset >> 20) & 1) << 31)
            | (((offset >> 1) & 0x3FF) << 21)
            | (((offset >> 11) & 1) << 20)
            | (((offset >> 12) & 0xFF) << 12)
            | ((rd & 31) << 7)
            | 111
        )
        return struct.pack("<I", word & 0xFFFFFFFF)

    trap_entry = 0x80001000
    vec_base = 0x80000800
    # Vector slot 0 (sync/base) and slot 7 (machine timer IRQ) jump via `j trap_entry`.
    slot7 = vec_base + 4 * 7
    slot3 = vec_base + 4 * 3
    vec_elf = ElfImage(
        pointer_size=4,
        endian="little",
        machine=243,
        sections=(ElfSection(".text", vec_base, b"\x00" * 0x100, 0x6, 4),),
        symbols=(),
    )
    mem = _Memory(
        {
            vec_base: _jal(0, vec_base, trap_entry),
            slot7: _jal(0, slot7, trap_entry),
            slot3: _jal(1, slot3, trap_entry),  # jal x1 (wrong rd) is refused
        }
    )

    assert riscv_mtvec_targets_entry(trap_entry, 0x80000007, 32, trap_entry) is True
    assert (
        riscv_mtvec_targets_entry(
            vec_base | MTVEC_MODE_VECTORED,
            0x80000007,
            32,
            trap_entry,
            read=mem,
        )
        is True
    )
    assert (
        riscv_mtvec_targets_entry(
            vec_base | MTVEC_MODE_VECTORED,
            2,
            32,
            trap_entry,
            read=mem,
        )
        is True
    )
    assert (
        riscv_mtvec_targets_entry(
            vec_base | MTVEC_MODE_VECTORED,
            0x80000003,
            32,
            trap_entry,
            read=mem,
        )
        is False
    )
    assert (
        riscv_mtvec_targets_entry(
            vec_base | MTVEC_MODE_VECTORED,
            0x80000007,
            32,
            trap_entry,
            elf=vec_elf,
            read=None,
        )
        is False
    )

    # prepare_riscv_handler_unwind admits vectored mtvec targeting span.entry
    span = _span_with_layout(epc_offset=0, sync_addend=4)
    task_seg = StackSegment(
        "task:1", "spin", 0x80010000, 0x80012000, "t", "process", "task"
    )
    snap_vec = RiscvSnapshot(
        xlen=32,
        pc=0x80002010,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=0x80000007,
        mepc=0x80003010,
        mtvec=vec_base | MTVEC_MODE_VECTORED,
        registers={1: 0x800010C0, 2: 0x8001FFE0, 8: 0x80011E00},
    )
    ok_vec = prepare_riscv_handler_unwind(
        snap_vec,
        root_pc=0x80004000,
        span=span,
        task_segment=task_seg,
        irq_stack_bounds=lambda: (0x8001F000, 0x80020000, "symbol:irq"),
        nest_count=1,
        endian="little",
        task_frame_resolver=lambda _c: 0x80011E00,
        read=mem,
    )
    assert not isinstance(ok_vec, tuple)
