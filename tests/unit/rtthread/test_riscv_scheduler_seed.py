"""Unit tests for RT-Thread RISC-V scheduler/trap seeds and GDB bridge."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

import rtthread.backtrace.backtrace_gdb as bridge
from core.backtrace.unwind import UnwindMetadata
from core.backtrace.unwind_cfi import CfaRule, CfiRow, CfiTable, RegisterRule
from core.backtrace.unwind_riscv import MSTATUS_MPP_MACHINE, RiscvSnapshot
from core.elf import (
    EM_RISCV,
    RISCV_CONVENTION,
    SHF_EXECINSTR,
    CodeSymbol,
    ElfImage,
    ElfSection,
)
from core.stack_frame_parser import StackContextSelection
from rtthread.adapter import Thread
from rtthread.backtrace.backtrace import RISCV32_INTEGER, RISCV64_INTEGER
from rtthread.backtrace.riscv_scheduler_switch import (
    build_riscv_profile,
    classify_riscv_stage,
)
from rtthread.backtrace.scheduler_seed import (
    SchedulerSeed,
    build_riscv_stage_seed,
)
from rtthread.layout import RT_THREAD_STACK_FILL, RtConfig, build_layouts
from tests.unit.core.oracle_support import attributes_blob

_TEXT_BASE = 0x80000000
_FLAG_ADDR = 0x80013000
_FROM_ADDR = 0x80013004
_TO_ADDR = 0x80013008
_CUR_ADDR = 0x8001300C
_IRQ_LOW = 0x8001E000
_IRQ_HIGH = 0x8001F000
_IRQ_SP = 0x8001EFE0

_ENTER_ADDR = 0x80000800
_HANDLE_ADDR = 0x80000840
_LEAVE_ADDR = 0x80000880
_THREAD_EXIT = 0x80000900

_OLD_ENTRY = 0x80001000
_OLD_LEAF = 0x80001100
_NEW_ENTRY = 0x80001200
_NEW_LEAF = 0x80001300
_OTHER_ENTRY = 0x80001400

_SP_OFFSET = 12
_OLD_ADDR = 0x80010000
_OLD_LOW = 0x80010100
_OLD_HIGH = 0x80010900
_OLD_SP = 0x80010700

_NEW_ADDR = 0x80011000
_NEW_LOW = 0x80011100
_NEW_HIGH = 0x80011900
_NEW_SP = 0x80011700

_OTHER_ADDR = 0x80012000
_OTHER_LOW = 0x80012100
_OTHER_HIGH = 0x80012900
_OTHER_SP = 0x80012700


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


def _enc_r(fn7: int, rs2: int, rs1: int, fn3: int, rd: int, opcode: int) -> int:
    return (
        ((fn7 & 0x7F) << 25)
        | ((rs2 & 31) << 20)
        | ((rs1 & 31) << 15)
        | ((fn3 & 7) << 12)
        | ((rd & 31) << 7)
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


def _asm_la(words: list[int], base: int, reg: int, target: int) -> None:
    pc = base + len(words) * 4
    delta = target - pc
    hi = (delta + 0x800) & ~0xFFF
    lo = delta - hi
    words.append(_enc_u(23, reg, hi))
    words.append(_enc_i(19, reg, 0, reg, lo))


def _build_rtt_elf(*, xlen: int, epc_mode: str) -> ElfImage:
    word = xlen // 8
    load_fn = 3 if word == 8 else 2
    store_fn = 3 if word == 8 else 2
    frame_bytes = 32 * word
    text = bytearray(0x2000)

    def _emit(offset: int, words: list[int]) -> None:
        for idx, insn in enumerate(words):
            text[offset + idx * 4 : offset + (idx + 1) * 4] = (
                insn & 0xFFFFFFFF
            ).to_bytes(4, "little")

    sw_off = 0x080
    sw_base = _TEXT_BASE + sw_off
    sw = [
        _enc_i(19, 2, 0, 2, -frame_bytes),
        _enc_s(35, store_fn, 10, 2, 0),
        _enc_s(35, store_fn, 2, 1, 0),
        _enc_s(35, store_fn, 2, 1, word),
        _enc_i(115, 10, 2, 0, 0x300),
        _enc_i(19, 10, 7, 10, 8),
    ]
    beq_idx = len(sw)
    sw.append(0)
    sw.append(_enc_i(19, 10, 0, 0, 0x80))
    save_mpie_pc = sw_base + len(sw) * 4
    sw[beq_idx] = _enc_b(0, 10, 0, save_mpie_pc - (sw_base + beq_idx * 4))
    sw.append(_enc_s(35, store_fn, 2, 10, 2 * word))
    sw.extend(_enc_s(35, store_fn, 2, reg, reg * word) for reg in range(4, 32))
    sw.append(_enc_i(3, 2, load_fn, 11, 0))
    exit_off = sw_off + (len(sw) + 1) * 4
    exit_base = _TEXT_BASE + exit_off
    sw.append(_enc_j(0, exit_base - (sw_base + len(sw) * 4)))
    _emit(sw_off, sw)

    st_off = 0x040
    st_base = _TEXT_BASE + st_off
    st = [
        _enc_i(3, 2, load_fn, 10, 0),
        _enc_i(3, 10, load_fn, 2, 2 * word),
        _enc_i(115, 0, 1, 10, 0x300),
        _enc_j(0, exit_base - (st_base + 3 * 4)),
    ]
    _emit(st_off, st)

    ex = [
        _enc_i(3, 10, load_fn, 2, 0),
        _enc_i(115, 0, 1, 10, 0x341),
        _enc_i(3, 1, load_fn, 2, word),
        _enc_i(3, 10, load_fn, 2, 2 * word),
        _enc_i(115, 0, 2, 10, 0x300),
    ]
    ex.extend(_enc_i(3, reg, load_fn, 2, reg * word) for reg in range(4, 32))
    ex.append(_enc_i(19, 2, 0, 2, frame_bytes))
    ex.append(0x30200073)
    _emit(exit_off, ex)

    tr_off = 0x400
    tr_base = _TEXT_BASE + tr_off
    tr = [_enc_i(19, 2, 0, 2, -frame_bytes), _enc_s(35, store_fn, 2, 1, word)]
    if epc_mode == "stacked-epc":
        tr.extend(
            (
                _enc_i(115, 1, 2, 0, 0x300),
                _enc_s(35, store_fn, 2, 1, 2 * word),
                _enc_i(115, 1, 2, 0, 0x341),
                _enc_s(35, store_fn, 2, 1, 0),
            )
        )
    else:
        tr.extend(
            (
                _enc_i(19, 5, 0, 0, 0x80),
                _enc_s(35, store_fn, 2, 5, 2 * word),
            )
        )
    tr.extend(_enc_s(35, store_fn, 2, reg, reg * word) for reg in range(4, 32))
    tr.append(_enc_r(0, 2, 0, 0, 8, 51))
    _asm_la(tr, tr_base, 2, _IRQ_HIGH)
    tr.append(_enc_j(1, _ENTER_ADDR - (tr_base + len(tr) * 4)))
    tr.append(_enc_j(1, _HANDLE_ADDR - (tr_base + len(tr) * 4)))
    tr.append(_enc_j(1, _LEAVE_ADDR - (tr_base + len(tr) * 4)))
    tr.append(_enc_r(0, 8, 0, 0, 2, 51))
    _asm_la(tr, tr_base, 8, _FLAG_ADDR)
    tr.append(_enc_i(3, 18, 2, 8, 0))
    beq_spur_idx = len(tr)
    tr.append(0)
    tr.append(_enc_s(35, 2, 8, 0, 0))
    if epc_mode == "deferred-mepc":
        tr.append(_enc_i(115, 10, 2, 0, 0x341))
        tr.append(_enc_s(35, store_fn, 2, 10, 0))
    _asm_la(tr, tr_base, 8, _FROM_ADDR)
    tr.append(_enc_i(3, 9, load_fn, 8, 0))
    tr.append(_enc_s(35, store_fn, 9, 2, 0))
    _asm_la(tr, tr_base, 8, _TO_ADDR)
    tr.append(_enc_i(3, 9, load_fn, 8, 0))
    tr.append(_enc_i(3, 2, load_fn, 9, 0))
    if epc_mode == "deferred-mepc":
        tr.append(_enc_i(3, 10, load_fn, 2, 0))
        tr.append(_enc_i(115, 0, 1, 10, 0x341))

    spurious_pc = tr_base + len(tr) * 4
    tr[beq_spur_idx] = _enc_b(0, 18, 0, spurious_pc - (tr_base + beq_spur_idx * 4))
    if epc_mode == "stacked-epc":
        tr.append(_enc_j(0, exit_base - (tr_base + len(tr) * 4)))
    else:
        tr.append(_enc_i(3, 1, load_fn, 2, word))
        tr.append(_enc_i(3, 5, load_fn, 2, 2 * word))
        tr.append(_enc_i(115, 0, 2, 5, 0x300))
        tr.extend(_enc_i(3, reg, load_fn, 2, reg * word) for reg in range(4, 32))
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
            CodeSymbol("trap_entry", tr_base, len(tr) * 4),
            CodeSymbol("handle_trap", _HANDLE_ADDR, 0x40),
            CodeSymbol("old_entry", _OLD_ENTRY, 0x40),
            CodeSymbol("old_leaf", _OLD_LEAF, 0x40),
            CodeSymbol("new_entry", _NEW_ENTRY, 0x40),
            CodeSymbol("new_leaf", _NEW_LEAF, 0x40),
            CodeSymbol("other_entry", _OTHER_ENTRY, 0x40),
            CodeSymbol("rt_thread_exit", _THREAD_EXIT, 0x20),
        ),
        anchors=(
            ("rt_hw_context_switch_to", st_base),
            ("rt_hw_context_switch", sw_base),
            ("save_mpie", save_mpie_pc),
            ("rt_hw_context_switch_exit", exit_base),
            ("trap_entry", tr_base),
            ("spurious_interrupt", spurious_pc),
            ("rt_thread_switch_interrupt_flag", _FLAG_ADDR),
            ("rt_interrupt_from_thread", _FROM_ADDR),
            ("rt_interrupt_to_thread", _TO_ADDR),
            ("rt_current_thread", _CUR_ADDR),
        ),
    )


class _Mem:
    def __init__(self, word: int) -> None:
        self.word = word
        self.fmt = "<Q" if word == 8 else "<I"
        self.words: dict[int, int] = {}

    def write_frame(
        self,
        top: int,
        *,
        epc: int | None,
        ra: int,
        fill: int,
        unstarted: bool = False,
    ) -> None:
        vals = [fill] * 32
        if epc is not None:
            vals[0] = epc
        vals[1] = ra
        vals[2] = MSTATUS_MPP_MACHINE | 0x80
        if not unstarted:
            for reg in range(4, 32):
                vals[reg] = 0x1000 + reg
        for idx, val in enumerate(vals):
            self.words[top + idx * self.word] = val

    def write_stack_growth(self, low: int, high: int) -> None:
        fill = RT_THREAD_STACK_FILL
        fill_word = int.from_bytes(bytes([fill]) * self.word, "little")
        for off in range(0, 16, self.word):
            self.words[low + off] = fill_word
            self.words[high - 16 + off] = 0x1234

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
    mepc: int = _OLD_LEAF + 8,
    mcause: int | None = None,
    mstatus: int = MSTATUS_MPP_MACHINE | 0x80,
    regs: dict[int, int] | None = None,
    subset_only: bool = False,
) -> RiscvSnapshot:
    allowed = {reg for reg, _ in RISCV_CONVENTION.live_registers}
    base = {reg: 0x2000 + reg for reg in (allowed if subset_only else range(1, 32))}
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
        mtvec=profile.trap_start,
        registers=base,
    )


def _selection(xlen: int) -> StackContextSelection:
    if xlen == 64:
        return StackContextSelection("riscv64", "riscv", RISCV64_INTEGER, "ok")
    return StackContextSelection("riscv32", "riscv", RISCV32_INTEGER, "ok")


@pytest.mark.parametrize(
    ("xlen", "epc_mode"),
    [
        (32, "deferred-mepc"),
        (64, "stacked-epc"),
    ],
)
def test_build_riscv_stage_seed_covers_all_rtthread_stages(xlen, epc_mode):
    elf = _build_rtt_elf(xlen=xlen, epc_mode=epc_mode)
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None
    sel = _selection(xlen)
    ctx = sel.context
    assert ctx is not None
    mem = _Mem(xlen // 8)
    mem.write_frame(_OLD_SP, epc=_OLD_LEAF + 8, ra=_OLD_ENTRY + 12, fill=ctx.fill_word)
    mem.write_frame(
        _NEW_SP, epc=_NEW_ENTRY, ra=_THREAD_EXIT, fill=ctx.fill_word, unstarted=True
    )
    mem.write_frame(
        _OTHER_SP, epc=_OTHER_ENTRY + 8, ra=_THREAD_EXIT, fill=ctx.fill_word
    )

    # 1. rt_hw_context_switch_to
    for pc, sp_val in (
        (profile.switch_to_start, _IRQ_SP),
        (profile.switch_to_start + 4, _NEW_SP),
    ):
        st = classify_riscv_stage(profile, pc)
        assert not isinstance(st, str) and st is not None
        out = build_riscv_stage_seed(
            profile,
            sel,
            st,
            flag_value=0,
            from_ptr=0,
            to_ptr=0,
            sp_offset=_SP_OFFSET,
            thread_addr=_NEW_ADDR,
            thread_sp=_NEW_SP,
            thread_entry=_NEW_ENTRY,
            current_addr=_NEW_ADDR,
            low=_NEW_LOW,
            high=_NEW_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(profile, pc, sp=sp_val, regs={10: _NEW_ADDR + _SP_OFFSET}),
        )
        assert isinstance(out, SchedulerSeed), out
        assert out.owner == "new" and out.seed.source == "unstarted"
        assert out.seed.pc == _NEW_ENTRY

    # 2. rt_hw_context_switch (rt_current_thread == _NEW_ADDR throughout)
    # 2a. context-switch-entry: old uses live ra/sp, new reads saved _NEW_SP.
    st_entry = classify_riscv_stage(profile, profile.switch_start)
    assert not isinstance(st_entry, str) and st_entry is not None
    entry_snap = _snap(
        profile,
        profile.switch_start,
        sp=_OLD_SP + profile.frame_bytes,
        regs={1: _OLD_LEAF + 8, 10: _OLD_ADDR + _SP_OFFSET, 11: _NEW_ADDR + _SP_OFFSET},
    )
    res_old = build_riscv_stage_seed(
        profile,
        sel,
        st_entry,
        flag_value=0,
        from_ptr=_OLD_ADDR + _SP_OFFSET,
        to_ptr=_NEW_ADDR + _SP_OFFSET,
        sp_offset=_SP_OFFSET,
        thread_addr=_OLD_ADDR,
        thread_sp=_OLD_SP + profile.frame_bytes,
        thread_entry=_OLD_ENTRY,
        current_addr=_NEW_ADDR,
        low=_OLD_LOW,
        high=_OLD_HIGH,
        endian="little",
        read=mem.read,
        snapshot=entry_snap,
    )
    assert isinstance(res_old, SchedulerSeed)
    assert res_old.owner == "old" and res_old.seed.pc == _OLD_LEAF + 8
    assert res_old.seed.sp == _OLD_SP + profile.frame_bytes

    res_new = build_riscv_stage_seed(
        profile,
        sel,
        st_entry,
        flag_value=0,
        from_ptr=_OLD_ADDR + _SP_OFFSET,
        to_ptr=_NEW_ADDR + _SP_OFFSET,
        sp_offset=_SP_OFFSET,
        thread_addr=_NEW_ADDR,
        thread_sp=_NEW_SP,
        thread_entry=_NEW_ENTRY,
        current_addr=_NEW_ADDR,
        low=_NEW_LOW,
        high=_NEW_HIGH,
        endian="little",
        read=mem.read,
        snapshot=entry_snap,
    )
    assert isinstance(res_new, SchedulerSeed)
    assert res_new.owner == "new" and res_new.seed.source == "unstarted"
    assert res_new.seed.pc == _NEW_ENTRY

    # 2b. context-switch-saving after a0 clobber (csrr a0, mstatus), tp absent live
    st_sav = classify_riscv_stage(profile, profile.switch_load_to_pc)
    assert not isinstance(st_sav, str) and st_sav is not None
    res_sav = build_riscv_stage_seed(
        profile,
        sel,
        st_sav,
        flag_value=0,
        from_ptr=0,
        to_ptr=0,
        sp_offset=_SP_OFFSET,
        thread_addr=_OLD_ADDR,
        thread_sp=_OLD_SP,
        thread_entry=_OLD_ENTRY,
        current_addr=_NEW_ADDR,
        low=_OLD_LOW,
        high=_OLD_HIGH,
        endian="little",
        read=mem.read,
        snapshot=_snap(
            profile, profile.switch_load_to_pc, sp=_OLD_SP, subset_only=True
        ),
        live_a0=0x80,
        live_a1=_NEW_ADDR + _SP_OFFSET,
    )
    assert isinstance(res_sav, SchedulerSeed), res_sav
    assert res_sav.owner == "old" and res_sav.seed.pc == _OLD_ENTRY + 12

    # 2c. context-switch-sp-loaded -> new owns cpu, old reads saved frame
    st_loaded = classify_riscv_stage(profile, profile.switch_load_to_pc + 4)
    assert not isinstance(st_loaded, str) and st_loaded is not None
    res_loaded_new = build_riscv_stage_seed(
        profile,
        sel,
        st_loaded,
        flag_value=0,
        from_ptr=0,
        to_ptr=0,
        sp_offset=_SP_OFFSET,
        thread_addr=_NEW_ADDR,
        thread_sp=_NEW_SP,
        thread_entry=_NEW_ENTRY,
        current_addr=_NEW_ADDR,
        low=_NEW_LOW,
        high=_NEW_HIGH,
        endian="little",
        read=mem.read,
        snapshot=_snap(profile, profile.switch_load_to_pc + 4, sp=_NEW_SP),
    )
    assert isinstance(res_loaded_new, SchedulerSeed)
    assert res_loaded_new.owner == "new" and res_loaded_new.seed.pc == _NEW_ENTRY

    # 3. trap-prologue-saving before and after mv s0, sp (tp missing live OK)
    for pc, sp_val, extra in (
        (profile.trap_start + 8, _OLD_SP, None),
        (profile.trap_call_start - 4, _IRQ_HIGH, {8: _OLD_SP}),
    ):
        st_pr = classify_riscv_stage(profile, pc)
        assert not isinstance(st_pr, str) and st_pr is not None
        res_pr = build_riscv_stage_seed(
            profile,
            sel,
            st_pr,
            flag_value=0,
            from_ptr=0,
            to_ptr=0,
            sp_offset=_SP_OFFSET,
            thread_addr=_OLD_ADDR,
            thread_sp=_OLD_SP + profile.frame_bytes,
            thread_entry=_OLD_ENTRY,
            current_addr=_OLD_ADDR,
            low=_OLD_LOW,
            high=_OLD_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(profile, pc, sp=sp_val, regs=extra, subset_only=True),
            nest_count=0,
        )
        assert isinstance(res_pr, SchedulerSeed), res_pr
        assert res_pr.owner == "old" and res_pr.seed.pc == _OLD_LEAF + 8
        assert res_pr.seed.sp == _OLD_SP + profile.frame_bytes

    # 4. trap-epilogue switch before store, after flag clear, and before load
    for pc, flag_v, sp_v, s0_v in (
        (profile.trap_mv_sp_pc, 1, _IRQ_HIGH, _OLD_SP),
        (profile.trap_store_from_pc, 0, _OLD_SP, _FLAG_ADDR),
        (profile.trap_load_to_pc, 0, _OLD_SP, _TO_ADDR),
    ):
        st_ep = classify_riscv_stage(profile, pc)
        assert not isinstance(st_ep, str) and st_ep is not None
        res_ep_old = build_riscv_stage_seed(
            profile,
            sel,
            st_ep,
            flag_value=flag_v,
            from_ptr=_OLD_ADDR + _SP_OFFSET,
            to_ptr=_NEW_ADDR + _SP_OFFSET,
            sp_offset=_SP_OFFSET,
            thread_addr=_OLD_ADDR,
            thread_sp=_OLD_SP,
            thread_entry=_OLD_ENTRY,
            current_addr=_NEW_ADDR,
            low=_OLD_LOW,
            high=_OLD_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(profile, pc, sp=sp_v, regs={8: s0_v}),
            nest_count=0,
        )
        assert isinstance(res_ep_old, SchedulerSeed), res_ep_old
        assert res_ep_old.owner == "old" and res_ep_old.seed.pc == _OLD_LEAF + 8
        res_ep_new = build_riscv_stage_seed(
            profile,
            sel,
            st_ep,
            flag_value=flag_v,
            from_ptr=_OLD_ADDR + _SP_OFFSET,
            to_ptr=_NEW_ADDR + _SP_OFFSET,
            sp_offset=_SP_OFFSET,
            thread_addr=_NEW_ADDR,
            thread_sp=_NEW_SP,
            thread_entry=_NEW_ENTRY,
            current_addr=_NEW_ADDR,
            low=_NEW_LOW,
            high=_NEW_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(profile, pc, sp=sp_v, regs={8: s0_v}),
            nest_count=0,
        )
        assert isinstance(res_ep_new, SchedulerSeed)
        assert res_ep_new.owner == "new" and res_ep_new.seed.pc == _NEW_ENTRY

    # 5. trap-epilogue-after-switch-load / restore-window / restore-sp-popped
    popped_pc = (
        profile.spurious_mret_pc
        if epc_mode == "deferred-mepc"
        else profile.exit_mret_pc
    )
    assert popped_pc is not None
    restore_cases = [
        (profile.trap_load_to_pc + 4, _NEW_SP, True),
        (profile.exit_start + 4, _NEW_SP, True),
        (profile.spurious_start, _NEW_SP, True),
        (popped_pc, _NEW_SP + profile.frame_bytes, True),
    ]
    for pc, sp_val, is_unstarted in restore_cases:
        st_rst = classify_riscv_stage(profile, pc)
        assert not isinstance(st_rst, str) and st_rst is not None
        res_rst = build_riscv_stage_seed(
            profile,
            sel,
            st_rst,
            flag_value=0,
            from_ptr=_OLD_ADDR + _SP_OFFSET,
            to_ptr=_NEW_ADDR + _SP_OFFSET,
            sp_offset=_SP_OFFSET,
            thread_addr=_NEW_ADDR,
            thread_sp=_NEW_SP,
            thread_entry=_NEW_ENTRY,
            current_addr=_NEW_ADDR,
            low=_NEW_LOW,
            high=_NEW_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(
                profile,
                pc,
                sp=sp_val,
                mepc=_NEW_ENTRY,
                regs={reg: ctx.fill_word for reg in (5, 6, 7, 8, 9, *range(18, 28))}
                if is_unstarted
                else None,
            ),
            nest_count=0,
        )
        assert isinstance(res_rst, SchedulerSeed), res_rst
        assert res_rst.owner == "new" and res_rst.seed.source == "unstarted"
        assert res_rst.seed.pc == _NEW_ENTRY


def test_build_riscv_stage_seed_fails_closed_on_conflicts():
    elf = _build_rtt_elf(xlen=32, epc_mode="deferred-mepc")
    profile, _ = build_riscv_profile(elf)
    assert profile is not None
    sel = _selection(32)
    mem = _Mem(4)
    mem.write_frame(_OLD_SP, epc=_OLD_LEAF + 8, ra=_OLD_ENTRY + 12, fill=0x23)

    st_ep = classify_riscv_stage(profile, profile.trap_store_from_pc)
    assert not isinstance(st_ep, str) and st_ep is not None
    assert (
        build_riscv_stage_seed(
            profile,
            sel,
            st_ep,
            flag_value=0,
            from_ptr=_OLD_ADDR + _SP_OFFSET,
            to_ptr=_NEW_ADDR + _SP_OFFSET,
            sp_offset=_SP_OFFSET,
            thread_addr=_OLD_ADDR,
            thread_sp=_OLD_SP,
            thread_entry=_OLD_ENTRY,
            current_addr=_NEW_ADDR,
            low=_OLD_LOW,
            high=_OLD_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(profile, profile.trap_store_from_pc, sp=_OLD_SP),
            nest_count=1,
        )
        == "nested-trap-epc-unproven"
    )
    assert (
        build_riscv_stage_seed(
            profile,
            sel,
            st_ep,
            flag_value=0,
            from_ptr=_OLD_ADDR + _SP_OFFSET,
            to_ptr=_NEW_ADDR + _SP_OFFSET,
            sp_offset=_SP_OFFSET,
            thread_addr=_OLD_ADDR,
            thread_sp=_OLD_SP,
            thread_entry=_OLD_ENTRY,
            current_addr=_NEW_ADDR,
            low=_OLD_LOW,
            high=_OLD_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(profile, profile.trap_store_from_pc, sp=_OLD_SP),
            nest_count=None,
        )
        == "exception-context-unproven"
    )

    st_pop = classify_riscv_stage(profile, profile.exit_mret_pc)
    assert not isinstance(st_pop, str) and st_pop is not None
    assert (
        build_riscv_stage_seed(
            profile,
            sel,
            st_pop,
            flag_value=0,
            from_ptr=0,
            to_ptr=0,
            sp_offset=_SP_OFFSET,
            thread_addr=_OLD_ADDR,
            thread_sp=_OLD_SP,
            thread_entry=_OLD_ENTRY,
            current_addr=_OLD_ADDR,
            low=_OLD_LOW,
            high=_OLD_HIGH,
            endian="little",
            read=mem.read,
            snapshot=_snap(profile, profile.exit_mret_pc, sp=_OLD_SP + 128, mstatus=0),
            nest_count=0,
        )
        == "unsupported-exception-model"
    )

    st_pr = classify_riscv_stage(profile, profile.trap_start + 8)
    assert not isinstance(st_pr, str) and st_pr is not None
    snap_missing_s2 = _snap(profile, st_pr.pc, sp=_OLD_SP, subset_only=True)
    snap_missing_s2.registers.pop(18)
    assert (
        build_riscv_stage_seed(
            profile,
            sel,
            st_pr,
            flag_value=0,
            from_ptr=0,
            to_ptr=0,
            sp_offset=_SP_OFFSET,
            thread_addr=_OLD_ADDR,
            thread_sp=_OLD_SP + profile.frame_bytes,
            thread_entry=_OLD_ENTRY,
            current_addr=_OLD_ADDR,
            low=_OLD_LOW,
            high=_OLD_HIGH,
            endian="little",
            read=mem.read,
            snapshot=snap_missing_s2,
            nest_count=0,
        )
        == "live-registers-unavailable"
    )


def test_rtthread_riscv_bridge_routes_current_and_pending_trap_owner(monkeypatch):
    elf = _build_rtt_elf(xlen=64, epc_mode="stacked-epc")
    profile, _ = build_riscv_profile(elf)
    assert profile is not None
    mem = _Mem(8)
    mem.write_stack_growth(_OLD_LOW, _OLD_HIGH)
    mem.write_stack_growth(_NEW_LOW, _NEW_HIGH)
    old_call_sp = _OLD_SP + profile.frame_bytes
    # Old thread trap frame at _OLD_SP: leaf -> old_entry + 12 -> _THREAD_EXIT
    mem.write_frame(_OLD_SP, epc=_OLD_LEAF + 8, ra=_OLD_ENTRY + 12, fill=0x23)
    mem.words[old_call_sp + 32 - 8] = _OLD_ENTRY + 12
    mem.words[old_call_sp + 32 + 32 - 8] = _THREAD_EXIT
    # New thread saved frame at _NEW_SP: _NEW_LEAF + 8 -> _NEW_ENTRY + 12 -> _THREAD_EXIT
    mem.write_frame(_NEW_SP, epc=_NEW_LEAF + 8, ra=_NEW_ENTRY + 12, fill=0x23)
    mem.words[_NEW_SP + profile.frame_bytes + 32 - 8] = _NEW_ENTRY + 12
    mem.words[_NEW_SP + profile.frame_bytes + 32 + 32 - 8] = _THREAD_EXIT
    # Active ISR C frame on IRQ stack for handle_trap -> epilogue inside trap_entry
    mem.words[_IRQ_SP + 16 - 8] = profile.trap_mv_sp_pc - 4

    cfi = CfiTable.from_rows(
        [
            (
                _HANDLE_ADDR,
                _HANDLE_ADDR + 0x40,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -8)}),
            ),
            (
                _OLD_LEAF,
                _OLD_LEAF + 0x40,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -8)}),
            ),
            (
                _OLD_ENTRY,
                _OLD_ENTRY + 0x40,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -8)}),
            ),
            (
                _NEW_LEAF,
                _NEW_LEAF + 0x40,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -8)}),
            ),
            (
                _NEW_ENTRY,
                _NEW_ENTRY + 0x40,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -8)}),
            ),
        ],
        return_register=1,
        address_size=8,
    )
    meta = UnwindMetadata(
        elf=elf,
        arch="riscv",
        cfi_tables=(cfi,),
        ehabi=None,
        return_register=1,
        frame_pointer_register=8,
    )
    threads = {
        _OLD_ADDR: Thread(
            name="old_t",
            address=_OLD_ADDR,
            sp=_OLD_SP,
            stack_addr=_OLD_LOW,
            stack_size=_OLD_HIGH - _OLD_LOW,
            stack_grows_up=False,
            entry=_OLD_ENTRY,
            oncpu=0,
        ),
        _NEW_ADDR: Thread(
            name="new_t",
            address=_NEW_ADDR,
            sp=_NEW_SP,
            stack_addr=_NEW_LOW,
            stack_size=_NEW_HIGH - _NEW_LOW,
            stack_grows_up=False,
            entry=_NEW_ENTRY,
            oncpu=None,
        ),
    }
    tokens = {_OLD_ADDR: object(), _NEW_ADDR: object()}
    state = {
        "flag": 0,
        "from_ptr": _OLD_ADDR + _SP_OFFSET,
        "to_ptr": _NEW_ADDR + _SP_OFFSET,
        "current": _NEW_ADDR,
        "nest": 0,
        "snap": _snap(
            profile,
            profile.switch_start,
            sp=old_call_sp,
            regs={
                1: _OLD_LEAF + 8,
                10: _OLD_ADDR + _SP_OFFSET,
                11: _NEW_ADDR + _SP_OFFSET,
            },
        ),
        "a0": _OLD_ADDR + _SP_OFFSET,
        "a1": _NEW_ADDR + _SP_OFFSET,
    }
    adapter = SimpleNamespace(layout=build_layouts(RtConfig()), version=(4, 0, 5))

    def _find(spec, _layout):
        key = str(spec)
        if key == "old_t" or key == f"{_OLD_ADDR:#x}":
            return tokens[_OLD_ADDR]
        if key == "new_t" or key == f"{_NEW_ADDR:#x}":
            return tokens[_NEW_ADDR]
        return None

    monkeypatch.setattr(bridge, "find_thread", _find)
    monkeypatch.setattr(bridge, "iter_threads", lambda _layout: list(tokens.values()))
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda val, _layout, **_kwargs: threads[
            _OLD_ADDR if val is tokens[_OLD_ADDR] else _NEW_ADDR
        ],
    )
    monkeypatch.setattr(
        bridge,
        "get_current_thread",
        lambda: None if state["current"] is None else tokens[state["current"]],
    )
    monkeypatch.setattr(
        bridge,
        "value_address",
        lambda val: (
            _THREAD_EXIT
            if val == "rt_thread_exit"
            else (_OLD_ADDR if val is tokens[_OLD_ADDR] else _NEW_ADDR)
        ),
    )
    monkeypatch.setattr(bridge, "_backtrace_context", lambda: _selection(64))
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: meta)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (8, "little"))
    monkeypatch.setattr(bridge, "member_offset", lambda *_args: _SP_OFFSET)
    monkeypatch.setattr(bridge, "eval_identifier", lambda name: name)
    monkeypatch.setattr(
        bridge,
        "read_int",
        lambda expr: {
            "rt_thread_switch_interrupt_flag": state["flag"],
            "rt_interrupt_from_thread": state["from_ptr"],
            "rt_interrupt_to_thread": state["to_ptr"],
            "rt_interrupt_nest": state["nest"],
        }.get(expr),
    )
    monkeypatch.setattr(bridge, "read_bytes", mem.read)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda addr: f"sym_{addr:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _addr: None)
    monkeypatch.setattr(
        bridge,
        "_riscv_irq_stack_bounds",
        lambda _elf, *, hart_id=0: (_IRQ_LOW, _IRQ_HIGH, "symbol:irq"),  # noqa: ARG005
    )
    monkeypatch.setattr(
        bridge,
        "_riscv_snapshot",
        lambda *, xlen=64: (state["snap"], "ok"),  # noqa: ARG005
    )
    monkeypatch.setattr(
        bridge, "_riscv_call_args", lambda _xlen: (state["a0"], state["a1"])
    )

    # 1. rtt bt inside rt_hw_context_switch entry picks old_t despite rt_current_thread == new_t.
    rep_sw = bridge.backtrace(adapter)
    assert rep_sw.complete and rep_sw.task == "old_t"
    assert [f.pc for f in rep_sw.frames] == [
        _OLD_LEAF + 8,
        _OLD_ENTRY + 12,
        _THREAD_EXIT,
    ]
    rep_sw_new = bridge.backtrace(adapter, "new_t")
    assert rep_sw_new.complete and rep_sw_new.task == "new_t"
    assert [f.pc for f in rep_sw_new.frames] == [
        _NEW_LEAF + 8,
        _NEW_ENTRY + 12,
        _THREAD_EXIT,
    ]

    # 2. rtt bt inside handle_trap while flag=1 routes from_thread across ISR stack!
    state["flag"] = 1
    state["nest"] = 1
    state["snap"] = _snap(
        profile,
        _HANDLE_ADDR + 8,
        sp=_IRQ_SP,
        regs={1: profile.trap_mv_sp_pc - 4, 8: _OLD_SP},
    )
    rep_isr = bridge.backtrace(adapter)
    assert rep_isr.complete and rep_isr.task == "old_t"
    assert any(
        f.method == "exception" and f.pc == _OLD_LEAF + 8 for f in rep_isr.frames
    )
    rep_isr_new = bridge.backtrace(adapter, "new_t")
    assert rep_isr_new.complete and rep_isr_new.source == "saved"

    # 3. rtt bt past clear_flag_pc (flag=0, still pre-load) keeps picking old_t!
    state["flag"] = 0
    state["nest"] = 0
    state["snap"] = _snap(profile, profile.trap_store_from_pc, sp=_OLD_SP)
    rep_ep = bridge.backtrace(adapter)
    assert rep_ep.complete and rep_ep.task == "old_t"
    assert [f.pc for f in rep_ep.frames] == [
        _OLD_LEAF + 8,
        _OLD_ENTRY + 12,
        _THREAD_EXIT,
    ]

    # 4. rtt bt after LOAD sp, (to_thread) picks new_t!
    state["snap"] = _snap(profile, profile.trap_load_to_pc + 4, sp=_NEW_SP)
    rep_to = bridge.backtrace(adapter)
    assert rep_to.complete and rep_to.task == "new_t"
    assert [f.pc for f in rep_to.frames] == [
        _NEW_LEAF + 8,
        _NEW_ENTRY + 12,
        _THREAD_EXIT,
    ]

    # 5. rt_current_thread is None inside trap_entry refuses scheduler-current-tcb-unavailable.
    state["current"] = None
    rep_boot = bridge.backtrace(adapter)
    assert not rep_boot.complete
    assert rep_boot.stop_reason == "scheduler-current-tcb-unavailable"


def test_rtthread_riscv_sync_fault_in_trap_prologue_saving_attaches_fault():
    elf = _build_rtt_elf(xlen=32, epc_mode="deferred-mepc")
    profile, _ = build_riscv_profile(elf)
    assert profile is not None
    sel = _selection(32)
    assert sel.context is not None
    mem = _Mem(4)
    saving_sp = _OLD_SP - profile.frame_bytes
    mem.write_frame(
        saving_sp,
        epc=_OLD_LEAF + 8,
        ra=_OLD_ENTRY + 12,
        fill=sel.context.fill_word,
    )
    st = classify_riscv_stage(profile, profile.trap_start + 8)
    assert not isinstance(st, str) and st is not None
    assert st.name == "trap-prologue-saving"

    out = build_riscv_stage_seed(
        profile,
        sel,
        st,
        flag_value=0,
        from_ptr=_OLD_ADDR + _SP_OFFSET,
        to_ptr=_NEW_ADDR + _SP_OFFSET,
        sp_offset=_SP_OFFSET,
        thread_addr=_OLD_ADDR,
        thread_sp=_OLD_SP,
        thread_entry=_OLD_ENTRY,
        current_addr=_OLD_ADDR,
        low=_OLD_LOW,
        high=_OLD_HIGH,
        endian="little",
        read=mem.read,
        snapshot=_snap(
            profile,
            profile.trap_start + 8,
            sp=saving_sp,
            mcause=2,
            regs={8: saving_sp},
        ),
        nest_count=0,
    )
    assert isinstance(out, SchedulerSeed)
    assert out.seed.pc == _OLD_LEAF + 8
    assert out.fault is not None and out.fault.fault == "Exception:IllegalInstruction"

    bad = build_riscv_stage_seed(
        profile,
        sel,
        st,
        flag_value=0,
        from_ptr=_OLD_ADDR + _SP_OFFSET,
        to_ptr=_NEW_ADDR + _SP_OFFSET,
        sp_offset=_SP_OFFSET,
        thread_addr=_OLD_ADDR,
        thread_sp=_OLD_SP,
        thread_entry=_OLD_ENTRY,
        current_addr=_OLD_ADDR,
        low=_OLD_LOW,
        high=_OLD_HIGH,
        endian="little",
        read=mem.read,
        snapshot=_snap(
            profile,
            profile.trap_start + 8,
            sp=saving_sp,
            mcause=24,
            regs={8: saving_sp},
        ),
        nest_count=0,
    )
    assert bad == "exception-frame-type-unproven"
