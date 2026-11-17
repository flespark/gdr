"""Unit tests for FreeRTOS RISC-V scheduler/trap seed construction and GDB bridge."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

import freertos.backtrace_gdb as bridge
from core.stack_frame_parser import StackContextSelection
from core.unwind import SeedFrame, UnwindMetadata
from core.unwind_cfi import CfaRule, CfiRow, CfiTable, RegisterRule
from core.unwind_meta import EM_RISCV, SHF_EXECINSTR, CodeSymbol, ElfImage, ElfSection
from core.unwind_riscv import (
    MCAUSE_EXC_ECALL_M,
    MSTATUS_MPP_MACHINE,
    RiscvSnapshot,
)
from freertos.adapter import FreeRtosTask
from freertos.backtrace import RISCV32_INTEGER, RISCV64_INTEGER
from freertos.layout import FreeRtosConfig, build_layout
from freertos.riscv_scheduler_switch import build_riscv_profile, classify_riscv_stage
from freertos.scheduler_seed import SchedulerSeed, build_riscv_stage_seed
from tests.unit.core.oracle_support import attributes_blob

_TEXT_BASE = 0x80001000
_TCB_PTR = 0x80010000
_CRIT_PTR = 0x80010004
_ISR_TOP = 0x80010008
_CRIT_VAL = 0x8001000C
_READY_LISTS = 0x80010020
_APP_EXC_ADDR = 0x80000F00
_TASK_ENTRY_PC = 0x80001020
_TASK_LEAF_PC = 0x80001060
_EXIT_PC = 0x800010A0

_OLD_TCB = 0x80011000
_OLD_LOW = 0x80011100
_OLD_HIGH = 0x80011900
_OLD_TOP = 0x80011500

_NEW_TCB = 0x80012000
_NEW_LOW = 0x80012100
_NEW_HIGH = 0x80012900
_NEW_TOP = 0x80012500

_ISR_LOW = 0x8001E000
_ISR_HIGH = 0x8001F000
_ISR_SP = 0x8001EFE0


def _enc_i(opcode: int, rd: int, fn3: int, rs1: int, imm: int) -> int:
    return (
        ((imm & 0xFFF) << 20)
        | ((rs1 & 31) << 15)
        | ((fn3 & 7) << 12)
        | ((rd & 31) << 7)
        | (opcode & 0x7F)
    )


def _enc_s(opcode: int, fn3: int, rs1: int, rs2: int, imm: int) -> int:
    uimm = imm & 0xFFF
    return (
        (((uimm >> 5) & 0x7F) << 25)
        | ((rs2 & 31) << 20)
        | ((rs1 & 31) << 15)
        | ((fn3 & 7) << 12)
        | ((uimm & 0x1F) << 7)
        | (opcode & 0x7F)
    )


def _enc_u(opcode: int, rd: int, imm20_shifted: int) -> int:
    return (imm20_shifted & 0xFFFFF000) | ((rd & 31) << 7) | (opcode & 0x7F)


def _enc_b(fn3: int, rs1: int, rs2: int, offset: int) -> int:
    return (
        (((offset >> 12) & 1) << 31)
        | (((offset >> 5) & 0x3F) << 25)
        | ((rs2 & 31) << 20)
        | ((rs1 & 31) << 15)
        | ((fn3 & 7) << 12)
        | (((offset >> 1) & 0xF) << 8)
        | (((offset >> 11) & 1) << 7)
        | 99
    )


def _enc_j(rd: int, offset: int) -> int:
    return (
        (((offset >> 20) & 1) << 31)
        | (((offset >> 1) & 0x3FF) << 21)
        | (((offset >> 11) & 1) << 20)
        | (((offset >> 12) & 0xFF) << 12)
        | ((rd & 31) << 7)
        | 111
    )


def _la(
    words: list[int], base: int, reg: int, target: int, load_fn: int | None
) -> None:
    pc = base + len(words) * 4
    delta = target - pc
    hi = (delta + 0x800) & ~0xFFF
    lo = delta - hi
    words.append(_enc_u(23, reg, hi))
    if load_fn is None:
        words.append(_enc_i(19, reg, 0, reg, lo))
    else:
        words.append(_enc_i(3, reg, load_fn, reg, lo))


def _build_rv_elf(xlen: int = 32) -> ElfImage:
    word = xlen // 8
    fn = 3 if word == 8 else 2
    frame_bytes = 31 * word
    text = bytearray(0x2000)

    def _emit(offset: int, words: list[int]) -> int:
        for idx, insn in enumerate(words):
            text[offset + idx * 4 : offset + (idx + 1) * 4] = (
                insn & 0xFFFFFFFF
            ).to_bytes(4, "little")
        return offset + len(words) * 4

    sf_off = 0x100
    sf_base = _TEXT_BASE + sf_off
    sf: list[int] = []
    _la(sf, sf_base, 2, _TCB_PTR, fn)
    sf.append(_enc_i(3, 2, fn, 2, 0))
    sf.append(_enc_i(3, 1, fn, 2, 0))
    sf.extend(_enc_i(3, reg, fn, 2, (reg - 3) * word) for reg in range(7, 32))
    sf.append(_enc_i(3, 5, fn, 2, 29 * word))
    _la(sf, sf_base, 6, _CRIT_PTR, fn)
    sf.append(_enc_s(35, fn, 6, 5, 0))
    sf.append(_enc_i(3, 5, fn, 2, 30 * word))
    sf.append(_enc_i(19, 5, 0, 5, 8))
    sf.append(_enc_i(115, 0, 1, 5, 0x300))
    sf.append(_enc_i(3, 5, fn, 2, 2 * word))
    sf.append(_enc_i(3, 6, fn, 2, 3 * word))
    sf.append(_enc_i(19, 2, 0, 2, frame_bytes))
    sf.append(_enc_i(103, 0, 0, 1, 0))
    _emit(sf_off, sf)

    sw_off = 0x400
    sw_base = _TEXT_BASE + sw_off
    sw: list[int] = [_enc_i(19, 2, 0, 2, -16), _enc_s(35, fn, 2, 1, 8)]
    _la(sw, sw_base, 14, _READY_LISTS, None)
    sw.append(_enc_i(3, 15, fn, 14, 8))
    sw.append(_enc_i(3, 14, fn, 15, 24))
    _la(sw, sw_base, 15, _TCB_PTR, None)
    sw.append(_enc_s(35, fn, 15, 14, 0))
    sw.append(_enc_i(3, 1, fn, 2, 8))
    sw.append(_enc_i(19, 2, 0, 2, 16))
    sw.append(_enc_i(103, 0, 0, 1, 0))
    sw_end = _emit(sw_off, sw)

    tr_off = 0x800
    tr_base = _TEXT_BASE + tr_off
    tr: list[int] = [_enc_i(19, 2, 0, 2, -frame_bytes), _enc_s(35, fn, 2, 1, word)]
    tr.extend(_enc_s(35, fn, 2, reg, (reg - 3) * word) for reg in range(5, 32))
    _la(tr, tr_base, 5, _CRIT_VAL, fn)
    tr.append(_enc_s(35, fn, 2, 5, 29 * word))
    tr.append(_enc_i(115, 5, 2, 0, 0x300))
    tr.append(_enc_s(35, fn, 2, 5, 30 * word))
    _la(tr, tr_base, 5, _TCB_PTR, fn)
    tr.append(_enc_s(35, fn, 5, 2, 0))
    tr.append(_enc_i(115, 10, 2, 0, 0x342))
    tr.append(_enc_i(115, 11, 2, 0, 0x341))
    bge_idx = len(tr)
    tr.append(0)
    async_pc = tr_base + len(tr) * 4
    tr.append(_enc_s(35, fn, 2, 11, 0))
    _la(tr, tr_base, 2, _ISR_TOP, fn)
    j_int_idx = len(tr)
    tr.append(0)
    sync_pc = tr_base + len(tr) * 4
    tr[bge_idx] = _enc_b(5, 10, 0, sync_pc - (tr_base + bge_idx * 4))
    tr.append(_enc_i(19, 11, 0, 11, 4))
    tr.append(_enc_s(35, fn, 2, 11, 0))
    _la(tr, tr_base, 2, _ISR_TOP, fn)
    j_exc_idx = len(tr)
    tr.append(0)
    idle_pc = tr_base + len(tr) * 4
    tr[j_int_idx] = _enc_j(0, idle_pc - (tr_base + j_int_idx * 4))
    tr.append(_enc_j(1, sw_base - (tr_base + len(tr) * 4)))
    j_proc1 = len(tr)
    tr.append(0)
    exc_pc = tr_base + len(tr) * 4
    tr[j_exc_idx] = _enc_j(0, exc_pc - (tr_base + j_exc_idx * 4))
    tr.append(_enc_i(19, 5, 0, 0, 11))
    bne_idx = len(tr)
    tr.append(0)
    tr.append(_enc_j(1, sw_base - (tr_base + len(tr) * 4)))
    j_proc2 = len(tr)
    tr.append(0)
    app_pc = tr_base + len(tr) * 4
    tr[bne_idx] = _enc_b(1, 10, 5, app_pc - (tr_base + bne_idx * 4))
    tr.append(_enc_j(1, _APP_EXC_ADDR - (tr_base + len(tr) * 4)))
    j_proc3 = len(tr)
    tr.append(0)
    proc_pc = tr_base + len(tr) * 4
    tr[j_proc1] = _enc_j(0, proc_pc - (tr_base + j_proc1 * 4))
    tr[j_proc2] = _enc_j(0, proc_pc - (tr_base + j_proc2 * 4))
    tr[j_proc3] = _enc_j(0, proc_pc - (tr_base + j_proc3 * 4))
    _la(tr, tr_base, 6, _TCB_PTR, fn)
    tr.append(_enc_i(3, 2, fn, 6, 0))
    tr.append(_enc_i(3, 5, fn, 2, 0))
    tr.append(_enc_i(115, 0, 1, 5, 0x341))
    tr.append(_enc_i(3, 5, fn, 2, 30 * word))
    tr.append(_enc_i(115, 0, 1, 5, 0x300))
    tr.append(_enc_i(3, 5, fn, 2, 29 * word))
    _la(tr, tr_base, 6, _CRIT_PTR, fn)
    tr.append(_enc_s(35, fn, 6, 5, 0))
    tr.append(_enc_i(3, 1, fn, 2, word))
    tr.extend(_enc_i(3, reg, fn, 2, (reg - 3) * word) for reg in range(5, 32))
    tr.append(_enc_i(19, 2, 0, 2, frame_bytes))
    tr.append(0x30200073)
    _emit(tr_off, tr)

    return ElfImage(
        pointer_size=word,
        endian="little",
        machine=EM_RISCV,
        sections=(
            ElfSection(".text", _TEXT_BASE, bytes(text), SHF_EXECINSTR, 1),
            ElfSection(
                ".riscv.attributes",
                0,
                attributes_blob(
                    b"riscv", ((4, 16), (5, f"rv{xlen}i2p1_m2p0_a2p1_zicsr2p0"))
                ),
                0,
                0x70000003,
            ),
        ),
        symbols=(
            CodeSymbol(
                "freertos_risc_v_application_exception_handler", _APP_EXC_ADDR, 16
            ),
            CodeSymbol("task_entry", _TASK_ENTRY_PC, 0x40),
            CodeSymbol("task_leaf", _TASK_LEAF_PC, 0x40),
            CodeSymbol("vTaskSwitchContext", sw_base, sw_end - sw_off),
        ),
        anchors=(
            ("xPortStartFirstTask", sf_base),
            ("vTaskSwitchContext", sw_base),
            ("freertos_risc_v_trap_handler", tr_base),
            ("asynchronous_interrupt", async_pc),
            ("synchronous_exception", sync_pc),
            ("handle_interrupt", idle_pc),
            ("handle_exception", exc_pc),
            ("application_exception_handler", app_pc),
            ("processed_source", proc_pc),
            ("pxCurrentTCB", _TCB_PTR),
            ("pxCriticalNesting", _CRIT_PTR),
            ("xCriticalNesting", _CRIT_VAL),
            ("xISRStackTop", _ISR_TOP),
            ("pxReadyTasksLists", _READY_LISTS),
        ),
    )


class _Mem:
    def __init__(self, word: int = 4) -> None:
        self.word = word
        self.fmt = "<Q" if word == 8 else "<I"
        self.words: dict[int, int] = {}

    def write_frame(
        self,
        top: int,
        *,
        epc: int,
        ra: int,
        fill: int,
        unstarted: bool = False,
    ) -> None:
        vals = [fill] * 31
        vals[0] = epc
        vals[1] = ra
        vals[29] = 0
        vals[30] = MSTATUS_MPP_MACHINE | 0x80
        if not unstarted:
            for reg in range(5, 32):
                vals[reg - 3] = 0x1000 + reg
        for idx, val in enumerate(vals):
            self.words[top + idx * self.word] = val

    def read(self, addr: int, size: int) -> bytes | None:
        out = bytearray()
        cursor = addr
        while len(out) < size:
            aligned = cursor & ~(self.word - 1)
            val = self.words.get(aligned)
            if val is None:
                return None
            raw = struct.pack(self.fmt, val & ((1 << (self.word * 8)) - 1))
            off = cursor - aligned
            take = min(size - len(out), self.word - off)
            out.extend(raw[off : off + take])
            cursor += take
        return bytes(out)


def _snap(
    profile,
    pc: int,
    *,
    sp: int,
    mcause: int | None = None,
    mepc: int = _TASK_LEAF_PC,
    mtvec: int | None = None,
    mstatus: int = MSTATUS_MPP_MACHINE | 0x80,
    regs: dict[int, int] | None = None,
) -> RiscvSnapshot:
    base = {reg: 0x2000 + reg for reg in range(1, 32)}
    base[0] = 0
    base[1] = _TASK_ENTRY_PC + 8
    base[2] = sp
    if regs:
        base.update(regs)
    return RiscvSnapshot(
        xlen=profile.xlen,
        pc=pc,
        sp=sp,
        mstatus=mstatus,
        mcause=((1 << (profile.xlen - 1)) | 7) if mcause is None else mcause,
        mepc=mepc,
        mtvec=profile.trap_start if mtvec is None else mtvec,
        registers=base,
    )


@pytest.mark.parametrize("xlen", [32, 64])
def test_build_riscv_stage_seed_covers_all_stages(xlen):
    elf = _build_rv_elf(xlen)
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None
    word = xlen // 8
    ctx = RISCV64_INTEGER if xlen == 64 else RISCV32_INTEGER
    fill = ctx.fill_word
    mem = _Mem(word)
    mem.write_frame(_OLD_TOP, epc=_TASK_LEAF_PC, ra=_TASK_ENTRY_PC + 8, fill=fill)
    mem.write_frame(
        _NEW_TOP, epc=_TASK_ENTRY_PC, ra=_EXIT_PC, fill=fill, unstarted=True
    )

    def _is_entry(addr: int) -> bool:
        sym = elf.function_containing(addr)
        return sym is not None and sym.start == addr

    # 1. start-first-*
    for pc, sp_val, extra_regs in (
        (profile.start_first_start, _ISR_SP, None),
        (profile.start_first_sp_load_pc + 4, _NEW_TOP, None),
        (
            profile.start_first_ret_pc,
            _NEW_TOP + profile.frame_bytes,
            {1: _TASK_ENTRY_PC},
        ),
    ):
        stage = classify_riscv_stage(profile, pc)
        assert not isinstance(stage, str) and stage is not None
        out = build_riscv_stage_seed(
            profile,
            stage,
            snapshot=_snap(profile, pc, sp=sp_val, regs=extra_regs),
            read=mem.read,
            context=ctx,
            endian="little",
            task_tcb=_NEW_TCB,
            task_top=_NEW_TOP,
            task_low=_NEW_LOW,
            task_high=_NEW_HIGH,
            current_tcb=_NEW_TCB,
            exit_pc=_EXIT_PC,
            is_entry=_is_entry,
        )
        assert isinstance(out, SchedulerSeed), out
        assert out.seed.source == "unstarted"
        assert out.seed.pc == _TASK_ENTRY_PC
        assert out.seed.root_reason == "task-entry"

    # 2. trap-entry with ecall -> mepc + 4
    ecall_stage = classify_riscv_stage(profile, profile.trap_start)
    assert not isinstance(ecall_stage, str) and ecall_stage is not None
    ecall_seed = build_riscv_stage_seed(
        profile,
        ecall_stage,
        snapshot=_snap(
            profile,
            profile.trap_start,
            sp=_OLD_TOP + profile.frame_bytes,
            mcause=MCAUSE_EXC_ECALL_M,
            mepc=_TASK_LEAF_PC,
        ),
        read=mem.read,
        context=ctx,
        endian="little",
        task_tcb=_OLD_TCB,
        task_top=_OLD_TOP,
        task_low=_OLD_LOW,
        task_high=_OLD_HIGH,
        current_tcb=_OLD_TCB,
        exit_pc=_EXIT_PC,
        is_entry=_is_entry,
    )
    assert isinstance(ecall_seed, SchedulerSeed)
    assert ecall_seed.seed.pc == _TASK_LEAF_PC + 4
    assert ecall_seed.seed.source == "current"

    # 3. trap-prologue-saving: t0 (reg 5) read from stack once saved, even if live t0 is clobbered
    t0_done_pc = next(done for reg, _off, done in profile.trap_saved_slots if reg == 5)
    saving_stage = classify_riscv_stage(profile, t0_done_pc)
    assert not isinstance(saving_stage, str) and saving_stage is not None
    assert saving_stage.name == "trap-prologue-saving"
    saving_out = build_riscv_stage_seed(
        profile,
        saving_stage,
        snapshot=_snap(
            profile,
            t0_done_pc,
            sp=_OLD_TOP,
            mcause=(1 << (xlen - 1)) | 7,
            mepc=_TASK_LEAF_PC,
            regs={5: 0xDEAD, 8: 0xBEEF},
        ),
        read=mem.read,
        context=ctx,
        endian="little",
        task_tcb=_OLD_TCB,
        task_top=_OLD_TOP,
        task_low=_OLD_LOW,
        task_high=_OLD_HIGH,
        current_tcb=_OLD_TCB,
        exit_pc=_EXIT_PC,
        is_entry=_is_entry,
    )
    assert isinstance(saving_out, SchedulerSeed)
    assert saving_out.seed.regs[5] == 0x1005  # stacked, not clobbered 0xDEAD
    assert saving_out.seed.regs[8] == 0xBEEF  # not yet saved, read from live GPR

    # 4. trap-prologue-tcb-written (sync branch with ecall)
    tcb_written = classify_riscv_stage(profile, profile.sync_epc_store_pc)
    assert not isinstance(tcb_written, str) and tcb_written is not None
    assert tcb_written.name == "trap-prologue-tcb-written"
    written_out = build_riscv_stage_seed(
        profile,
        tcb_written,
        snapshot=_snap(
            profile,
            profile.sync_epc_store_pc,
            sp=_OLD_TOP,
            mcause=MCAUSE_EXC_ECALL_M,
            mepc=_TASK_LEAF_PC,
        ),
        read=mem.read,
        context=ctx,
        endian="little",
        task_tcb=_OLD_TCB,
        task_top=_OLD_TOP,
        task_low=_OLD_LOW,
        task_high=_OLD_HIGH,
        current_tcb=_OLD_TCB,
        exit_pc=_EXIT_PC,
        is_entry=_is_entry,
    )
    assert isinstance(written_out, SchedulerSeed)
    assert written_out.seed.pc == _TASK_LEAF_PC + 4

    # 5. trap-prologue-epc-saved & switch-before-update
    for pc, expected_name in (
        (profile.async_epc_store_pc + 4, "trap-prologue-epc-saved"),
        (profile.switch_update_pc, "switch-before-update"),
    ):
        st = classify_riscv_stage(profile, pc)
        assert not isinstance(st, str) and st is not None
        assert st.name == expected_name
        res = build_riscv_stage_seed(
            profile,
            st,
            snapshot=_snap(profile, pc, sp=_ISR_SP, mepc=_TASK_LEAF_PC),
            read=mem.read,
            context=ctx,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_OLD_TCB,
            exit_pc=_EXIT_PC,
            is_entry=_is_entry,
            irq_bounds=(_ISR_LOW, _ISR_HIGH),
        )
        assert isinstance(res, SchedulerSeed)
        assert res.owner == "old"
        assert res.seed.source == "saved"
        assert res.seed.pc == _TASK_LEAF_PC

    # 6. switch-after-update, restore-sp-loaded, restore-sp-popped (unstarted incoming task)
    for pc, sp_val, extra in (
        (profile.switch_after_update_pc, _ISR_SP, None),
        (profile.restore_load_sp_pc, _ISR_SP, None),
        (profile.restore_pop_sp_pc, _NEW_TOP, None),
        (
            profile.restore_mret_pc,
            _NEW_TOP + profile.frame_bytes,
            {1: _EXIT_PC},
        ),
    ):
        st = classify_riscv_stage(profile, pc)
        assert not isinstance(st, str) and st is not None
        res = build_riscv_stage_seed(
            profile,
            st,
            snapshot=_snap(profile, pc, sp=sp_val, mepc=_TASK_ENTRY_PC, regs=extra),
            read=mem.read,
            context=ctx,
            endian="little",
            task_tcb=_NEW_TCB,
            task_top=_NEW_TOP,
            task_low=_NEW_LOW,
            task_high=_NEW_HIGH,
            current_tcb=_NEW_TCB,
            exit_pc=_EXIT_PC,
            is_entry=_is_entry,
            irq_bounds=(_ISR_LOW, _ISR_HIGH),
        )
        assert isinstance(res, SchedulerSeed), res
        assert res.owner == "new"
        assert res.seed.source == "unstarted"
        assert res.seed.pc == _TASK_ENTRY_PC


def test_build_riscv_stage_seed_fails_closed_on_conflicts():
    elf = _build_rv_elf(32)
    profile, _ = build_riscv_profile(elf)
    assert profile is not None
    mem = _Mem(4)
    mem.write_frame(_OLD_TOP, epc=_TASK_LEAF_PC, ra=_TASK_ENTRY_PC + 8, fill=0xA5A5A5A5)

    st_switch = classify_riscv_stage(profile, profile.switch_update_pc)
    assert not isinstance(st_switch, str) and st_switch is not None

    # Non-ecall synchronous exception is refused.
    assert (
        build_riscv_stage_seed(
            profile,
            st_switch,
            snapshot=_snap(profile, profile.switch_update_pc, sp=_ISR_SP, mcause=2),
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_OLD_TCB,
            exit_pc=_EXIT_PC,
            is_entry=lambda addr: addr == _TASK_ENTRY_PC,
            irq_bounds=(_ISR_LOW, _ISR_HIGH),
        )
        == "exception-frame-type-unproven"
    )

    # Wrong mtvec, non-Machine MPP, TCB mismatch, missing/bad irq bounds.
    assert (
        build_riscv_stage_seed(
            profile,
            st_switch,
            snapshot=_snap(
                profile, profile.switch_update_pc, sp=_ISR_SP, mtvec=_TEXT_BASE
            ),
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_OLD_TCB,
            exit_pc=_EXIT_PC,
            is_entry=None,
            irq_bounds=(_ISR_LOW, _ISR_HIGH),
        )
        == "exception-vector-unproven"
    )
    assert (
        build_riscv_stage_seed(
            profile,
            st_switch,
            snapshot=_snap(profile, profile.switch_update_pc, sp=_ISR_SP, mstatus=0),
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_OLD_TCB,
            exit_pc=_EXIT_PC,
            is_entry=None,
            irq_bounds=(_ISR_LOW, _ISR_HIGH),
        )
        == "unsupported-exception-model"
    )
    assert (
        build_riscv_stage_seed(
            profile,
            st_switch,
            snapshot=_snap(profile, profile.switch_update_pc, sp=_ISR_SP),
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_NEW_TCB,
            exit_pc=_EXIT_PC,
            is_entry=None,
            irq_bounds=(_ISR_LOW, _ISR_HIGH),
        )
        == "scheduler-current-tcb-mismatch"
    )
    assert (
        build_riscv_stage_seed(
            profile,
            st_switch,
            snapshot=_snap(profile, profile.switch_update_pc, sp=_ISR_SP),
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_OLD_TCB,
            exit_pc=_EXIT_PC,
            is_entry=None,
            irq_bounds=None,
        )
        == "exception-stack-bounds-unavailable"
    )
    assert (
        build_riscv_stage_seed(
            profile,
            st_switch,
            snapshot=_snap(profile, profile.switch_update_pc, sp=_OLD_TOP),
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_OLD_TCB,
            exit_pc=_EXIT_PC,
            is_entry=None,
            irq_bounds=(_ISR_LOW, _ISR_HIGH),
        )
        == "exception-stack-outside-bounds"
    )


def test_gdb_bridge_current_pc_tracks_old_and_new_tasks_across_riscv_switch(
    monkeypatch,
):
    elf = _build_rv_elf(32)
    profile, _ = build_riscv_profile(elf)
    assert profile is not None

    cfi = CfiTable.from_rows(
        [
            (
                _TASK_LEAF_PC,
                _TASK_LEAF_PC + 0x40,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -4)}),
            ),
            (
                _TASK_ENTRY_PC,
                _TASK_ENTRY_PC + 0x40,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -4)}),
            ),
        ],
        return_register=1,
        address_size=4,
    )
    meta = UnwindMetadata(
        elf=elf,
        arch="riscv",
        cfi_tables=(cfi,),
        ehabi=None,
        return_register=1,
        frame_pointer_register=8,
    )

    mem = _Mem(4)
    old_caller_sp = _OLD_TOP + profile.frame_bytes
    mem.write_frame(
        _OLD_TOP, epc=_TASK_LEAF_PC + 8, ra=_TASK_ENTRY_PC + 12, fill=0xA5A5A5A5
    )
    mem.words[old_caller_sp + 12] = _TASK_ENTRY_PC + 12
    mem.words[old_caller_sp + 16 + 12] = _EXIT_PC

    new_caller_sp = _NEW_TOP + profile.frame_bytes
    mem.write_frame(
        _NEW_TOP, epc=_TASK_LEAF_PC + 16, ra=_TASK_ENTRY_PC + 20, fill=0xA5A5A5A5
    )
    mem.words[new_caller_sp + 12] = _TASK_ENTRY_PC + 20
    mem.words[new_caller_sp + 16 + 12] = _EXIT_PC

    old_task = FreeRtosTask(
        name="old_t",
        address=_OLD_TCB,
        top_of_stack=_OLD_TOP,
        stack_base=_OLD_LOW,
        stack_size=_OLD_HIGH - _OLD_LOW,
    )
    new_task = FreeRtosTask(
        name="new_t",
        address=_NEW_TCB,
        top_of_stack=_NEW_TOP,
        stack_base=_NEW_LOW,
        stack_size=_NEW_HIGH - _NEW_LOW,
    )
    tasks_by_addr = {_OLD_TCB: old_task, _NEW_TCB: new_task}
    state = {
        "current": _OLD_TCB,
        "running": True,
        "snap": _snap(
            profile,
            profile.switch_update_pc,
            sp=_ISR_SP,
            mepc=_TASK_LEAF_PC + 8,
        ),
    }

    layout = build_layout(FreeRtosConfig())
    selection = StackContextSelection("riscv32", "riscv", RISCV32_INTEGER, "ok")
    adapter = SimpleNamespace(
        layout=layout, _context_selection=lambda: selection, _unwind_cache=None
    )

    monkeypatch.setattr(bridge, "active", lambda: adapter)
    monkeypatch.setattr(bridge, "_task_value_by_address", lambda _adapter, addr: addr)
    monkeypatch.setattr(bridge, "task_state", lambda _val, _layout: ("Running", 0))
    monkeypatch.setattr(
        bridge,
        "value_to_task",
        lambda val, *_args, **_kwargs: tasks_by_addr[val],
    )
    monkeypatch.setattr(
        bridge,
        "current_tasks",
        lambda _layout: [(0, state["current"])] if state["current"] else [],
    )
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: meta)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (4, "little"))
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: state["running"])
    monkeypatch.setattr(
        bridge,
        "read_int",
        lambda expr: state["current"] if expr == "pxCurrentTCB" else None,
    )
    monkeypatch.setattr(bridge, "eval_identifier", lambda name: name)
    monkeypatch.setattr(bridge, "read_bytes", mem.read)
    monkeypatch.setattr(bridge, "_exit_pc", lambda: _EXIT_PC)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda addr: f"sym_{addr:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _addr: None)
    monkeypatch.setattr(
        bridge,
        "_riscv_isr_stack_bounds",
        lambda _elf: (_ISR_LOW, _ISR_HIGH, "symbol:isr_stack"),
    )
    monkeypatch.setattr(
        bridge,
        "_riscv_snapshot",
        lambda *, xlen=32: (state["snap"], "ok"),  # noqa: ARG005
    )
    monkeypatch.setattr(
        bridge,
        "_live_seed_detail",
        lambda _arch, _isa: (
            SeedFrame(
                state["snap"].pc,
                state["snap"].sp,
                dict(state["snap"].registers),
                source="current",
            ),
            "ok",
        ),
    )

    # 1. frt bt before pxCurrentTCB update traces old_t.
    rep_before = bridge.backtrace(adapter)
    assert rep_before.complete and rep_before.stop_reason == "task-exit"
    assert rep_before.task == "old_t"
    assert [f.pc for f in rep_before.frames] == [
        _TASK_LEAF_PC + 8,
        _TASK_ENTRY_PC + 12,
        _EXIT_PC,
    ]
    assert "stage=switch-before-update" in " ".join(rep_before.messages)

    # 2. frt bt after pxCurrentTCB update traces new_t (not old_t!).
    state["current"] = _NEW_TCB
    state["snap"] = _snap(
        profile,
        profile.switch_after_update_pc,
        sp=_ISR_SP,
        mepc=_TASK_LEAF_PC + 8,
    )
    rep_after = bridge.backtrace(adapter)
    assert rep_after.complete and rep_after.stop_reason == "task-exit"
    assert rep_after.task == "new_t"
    assert [f.pc for f in rep_after.frames] == [
        _TASK_LEAF_PC + 16,
        _TASK_ENTRY_PC + 20,
        _EXIT_PC,
    ]
    assert "stage=switch-after-update" in " ".join(rep_after.messages)

    # 3. Non-ecall sync exception inside application_exception_handler fails closed.
    state["snap"] = _snap(
        profile,
        _APP_EXC_ADDR + 4,
        sp=_ISR_SP,
        mcause=2,
        mepc=_TASK_LEAF_PC + 8,
    )
    rep_fault = bridge.backtrace(adapter)
    assert not rep_fault.complete
    assert rep_fault.stop_reason == "exception-frame-type-unproven"

    # 4. Halted in scheduler symbol before xSchedulerRunning fails closed.
    state["current"] = 0
    state["running"] = False
    state["snap"] = _snap(profile, profile.start_first_start, sp=_ISR_SP)
    rep_boot = bridge.backtrace(adapter)
    assert not rep_boot.complete
    assert rep_boot.stop_reason == "scheduler-not-running"


def test_riscv_prologue_saving_live_reg_subset_and_popped_strict_top():
    from core.cpus import RISCV_CONVENTION

    elf = _build_rv_elf(32)
    profile, _ = build_riscv_profile(elf)
    assert profile is not None
    mem = _Mem(4)
    saving_sp = _OLD_TOP - profile.frame_bytes
    mem.write_frame(
        saving_sp, epc=_TASK_LEAF_PC, ra=_TASK_ENTRY_PC + 8, fill=0xA5A5A5A5
    )

    def _is_entry(addr: int) -> bool:
        sym = elf.function_containing(addr)
        return sym is not None and sym.start == addr

    required_regs = {
        reg: (0x3000 + reg if reg != 2 else saving_sp)
        for reg, _name in RISCV_CONVENTION.live_registers
    }
    saving_stage = classify_riscv_stage(profile, profile.trap_start + 8)
    assert not isinstance(saving_stage, str) and saving_stage is not None
    assert saving_stage.name == "trap-prologue-saving"
    # Absent caller-saved GPRs (e.g. t0..t6) do not trigger false failure.
    snap_subset = RiscvSnapshot(
        xlen=32,
        pc=saving_stage.pc,
        sp=saving_sp,
        mstatus=MSTATUS_MPP_MACHINE | 0x80,
        mcause=(1 << 31) | 7,
        mepc=_TASK_LEAF_PC,
        mtvec=profile.trap_start,
        registers=required_regs,
    )
    out = build_riscv_stage_seed(
        profile,
        saving_stage,
        snapshot=snap_subset,
        read=mem.read,
        context=RISCV32_INTEGER,
        endian="little",
        task_tcb=_OLD_TCB,
        task_top=_OLD_TOP,
        task_low=_OLD_LOW,
        task_high=_OLD_HIGH,
        current_tcb=_OLD_TCB,
        exit_pc=_EXIT_PC,
        is_entry=_is_entry,
    )
    assert isinstance(out, SchedulerSeed), out

    # Missing a required ABI unwind register before its save slot fails closed.
    missing_abi = dict(required_regs)
    missing_abi.pop(18)
    # Also construct snapshot without s2 (x18) directly:
    snap_missing_abi = RiscvSnapshot(
        xlen=32,
        pc=saving_stage.pc,
        sp=saving_sp,
        mstatus=MSTATUS_MPP_MACHINE | 0x80,
        mcause=(1 << 31) | 7,
        mepc=_TASK_LEAF_PC,
        mtvec=profile.trap_start,
        registers=missing_abi,
    )
    assert (
        build_riscv_stage_seed(
            profile,
            saving_stage,
            snapshot=snap_missing_abi,
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_OLD_TCB,
            task_top=_OLD_TOP,
            task_low=_OLD_LOW,
            task_high=_OLD_HIGH,
            current_tcb=_OLD_TCB,
            exit_pc=_EXIT_PC,
            is_entry=_is_entry,
        )
        == "live-registers-unavailable"
    )

    # Strict contract: if an interrupt overwrites pxTopOfStack before start-first ret,
    # start-first-sp-popped fails closed with scheduler-first-start-entry-unproven.
    preempted_top = _NEW_TOP - profile.frame_bytes
    mem.write_frame(
        preempted_top,
        epc=profile.start_first_ret_pc - 4,
        ra=_TASK_ENTRY_PC,
        fill=0xA5A5A5A5,
    )
    popped_stage = classify_riscv_stage(profile, profile.start_first_ret_pc)
    assert not isinstance(popped_stage, str) and popped_stage is not None
    assert (
        build_riscv_stage_seed(
            profile,
            popped_stage,
            snapshot=_snap(
                profile,
                profile.start_first_ret_pc,
                sp=_NEW_TOP + profile.frame_bytes,
                regs={1: _TASK_ENTRY_PC},
            ),
            read=mem.read,
            context=RISCV32_INTEGER,
            endian="little",
            task_tcb=_NEW_TCB,
            task_top=preempted_top,
            task_low=_NEW_LOW,
            task_high=_NEW_HIGH,
            current_tcb=_NEW_TCB,
            exit_pc=_EXIT_PC,
            is_entry=_is_entry,
        )
        == "scheduler-first-start-entry-unproven"
    )
