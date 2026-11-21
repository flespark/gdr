"""Unit tests for FreeRTOS RISC-V scheduler and trap stage classification."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.elf import EM_RISCV, SHF_EXECINSTR, CodeSymbol, ElfImage, ElfSection
from freertos.backtrace.riscv_scheduler_switch import (
    RV32_PROFILE_ID,
    RV64_PROFILE_ID,
    RiscvSchedulerStage,
    build_riscv_profile,
    classify_riscv_stage,
)
from tests.support.loader import cached_elfs, fixture_cache_root
from tests.unit.core.oracle_support import attributes_blob

_TEXT_BASE = 0x80001000
_TCB_PTR = 0x80010000
_CRIT_PTR = 0x80010004
_ISR_TOP = 0x80010008
_CRIT_VAL = 0x8001000C
_READY_LISTS = 0x80010020
_APP_EXC_ADDR = 0x80000F00


def _encode_i(opcode: int, rd: int, fn3: int, rs1: int, imm: int) -> int:
    return (
        ((imm & 0xFFF) << 20)
        | ((rs1 & 31) << 15)
        | ((fn3 & 7) << 12)
        | ((rd & 31) << 7)
        | (opcode & 0x7F)
    )


def _encode_s(opcode: int, fn3: int, rs1: int, rs2: int, imm: int) -> int:
    uimm = imm & 0xFFF
    return (
        (((uimm >> 5) & 0x7F) << 25)
        | ((rs2 & 31) << 20)
        | ((rs1 & 31) << 15)
        | ((fn3 & 7) << 12)
        | ((uimm & 0x1F) << 7)
        | (opcode & 0x7F)
    )


def _encode_u(opcode: int, rd: int, imm20_shifted: int) -> int:
    return (imm20_shifted & 0xFFFFF000) | ((rd & 31) << 7) | (opcode & 0x7F)


def _encode_b(fn3: int, rs1: int, rs2: int, offset: int) -> int:
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


def _encode_j(rd: int, offset: int) -> int:
    return (
        (((offset >> 20) & 1) << 31)
        | (((offset >> 1) & 0x3FF) << 21)
        | (((offset >> 11) & 1) << 20)
        | (((offset >> 12) & 0xFF) << 12)
        | ((rd & 31) << 7)
        | 111
    )


def _split_pcrel(pc: int, target: int) -> tuple[int, int]:
    delta = target - pc
    hi = (delta + 0x800) & ~0xFFF
    lo = delta - hi
    return hi, lo


def _asm_load_sym(
    words: list[int], base: int, reg: int, target: int, load_fn: int
) -> None:
    pc = base + len(words) * 4
    hi, lo = _split_pcrel(pc, target)
    words.append(_encode_u(23, reg, hi))
    words.append(_encode_i(3, reg, load_fn, reg, lo))


def _asm_la(words: list[int], base: int, reg: int, target: int) -> None:
    pc = base + len(words) * 4
    hi, lo = _split_pcrel(pc, target)
    words.append(_encode_u(23, reg, hi))
    words.append(_encode_i(19, reg, 0, reg, lo))


def _riscv_attributes(xlen: int) -> bytes:
    return attributes_blob(b"riscv", ((4, 16), (5, f"rv{xlen}i2p1_m2p0_a2p1_zicsr2p0")))


def _synthetic_freertos_elf(
    *,
    xlen: int = 32,
    nop_switch_entry: bool = False,
    double_tcb_store: bool = False,
    zero_tcb_store: bool = False,
    bad_source_tcb_store: bool = False,
    clobber_t0_early: bool = False,
    missing_ecall_branch: bool = False,
    cycle_across_store: bool = False,
    bad_insn_in_switch: bool = False,
) -> ElfImage:
    word = xlen // 8
    load_fn = 3 if word == 8 else 2
    store_fn = 3 if word == 8 else 2
    frame_bytes = 31 * word

    text = bytearray(0x2000)

    def _emit(offset: int, words: list[int]) -> int:
        for idx, insn in enumerate(words):
            text[offset + idx * 4 : offset + (idx + 1) * 4] = (
                insn & 0xFFFFFFFF
            ).to_bytes(4, "little")
        return offset + len(words) * 4

    # 1. xPortStartFirstTask at _TEXT_BASE + 0x100
    sf_off = 0x100
    sf_base = _TEXT_BASE + sf_off
    sf: list[int] = []
    _asm_load_sym(sf, sf_base, 2, _TCB_PTR, load_fn)
    sf.append(_encode_i(3, 2, load_fn, 2, 0))  # load_x sp, 0(sp)
    sf.append(_encode_i(3, 1, load_fn, 2, 0))  # load_x x1, 0(sp)
    sf.extend(_encode_i(3, reg, load_fn, 2, (reg - 3) * word) for reg in range(7, 32))
    sf.append(_encode_i(3, 5, load_fn, 2, 29 * word))
    _asm_load_sym(sf, sf_base, 6, _CRIT_PTR, load_fn)
    sf.append(_encode_s(35, store_fn, 6, 5, 0))
    sf.append(_encode_i(3, 5, load_fn, 2, 30 * word))
    sf.append(_encode_i(19, 5, 0, 5, 8))
    sf.append(_encode_i(115, 0, 1, 5, 0x300))  # csrw mstatus, x5
    sf.append(_encode_i(3, 5, load_fn, 2, 2 * word))
    sf.append(_encode_i(3, 6, load_fn, 2, 3 * word))
    sf.append(_encode_i(19, 2, 0, 2, frame_bytes))
    sf.append(_encode_i(103, 0, 0, 1, 0))  # ret
    _emit(sf_off, sf)

    # 2. vTaskSwitchContext at _TEXT_BASE + 0x400
    sw_off = 0x400
    sw_base = _TEXT_BASE + sw_off
    sw: list[int] = []
    if nop_switch_entry:
        sw.append(0x00000013)
    else:
        sw.append(_encode_i(19, 2, 0, 2, -16))
    sw.append(_encode_s(35, store_fn, 2, 1, 8))
    if bad_insn_in_switch:
        sw.append(0x00000000)  # illegal
    _asm_la(sw, sw_base, 14, _READY_LISTS)
    sw.append(_encode_i(3, 15, load_fn, 14, 8))  # list_item
    sw.append(_encode_i(3, 14, load_fn, 15, 24))  # owner
    _asm_la(sw, sw_base, 15, _TCB_PTR)
    if not zero_tcb_store:
        src_reg = 2 if bad_source_tcb_store else 14
        sw.append(_encode_s(35, store_fn, 15, src_reg, 0))
    if double_tcb_store:
        sw.append(_encode_s(35, store_fn, 15, 14, 0))
    if cycle_across_store:
        # Branch back across the store from post-store to entry!
        cur_pc = sw_base + len(sw) * 4
        sw.append(_encode_b(0, 14, 0, sw_base - cur_pc))
    sw.append(_encode_i(3, 1, load_fn, 2, 8))
    sw.append(_encode_i(19, 2, 0, 2, 16))
    sw.append(_encode_i(103, 0, 0, 1, 0))  # ret
    sw_end_off = _emit(sw_off, sw)

    # 3. freertos_risc_v_trap_handler at _TEXT_BASE + 0x800
    tr_off = 0x800
    tr_base = _TEXT_BASE + tr_off
    tr: list[int] = []
    tr.append(_encode_i(19, 2, 0, 2, -frame_bytes))
    if clobber_t0_early:
        tr.append(_encode_i(19, 5, 0, 0, 1))
    tr.append(_encode_s(35, store_fn, 2, 1, word))
    tr.extend(_encode_s(35, store_fn, 2, reg, (reg - 3) * word) for reg in range(5, 32))
    _asm_load_sym(tr, tr_base, 5, _CRIT_VAL, load_fn)
    tr.append(_encode_s(35, store_fn, 2, 5, 29 * word))
    tr.append(_encode_i(115, 5, 2, 0, 0x300))  # csrr t0, mstatus
    tr.append(_encode_s(35, store_fn, 2, 5, 30 * word))
    _asm_load_sym(tr, tr_base, 5, _TCB_PTR, load_fn)
    tr.append(_encode_s(35, store_fn, 5, 2, 0))  # store sp, 0(t0)
    tr.append(_encode_i(115, 10, 2, 0, 0x342))  # csrr a0, mcause
    tr.append(_encode_i(115, 11, 2, 0, 0x341))  # csrr a1, mepc
    bge_idx = len(tr)
    tr.append(0)  # placeholder bge a0, x0, synchronous_exception
    async_idx = len(tr)
    async_pc = tr_base + async_idx * 4
    tr.append(_encode_s(35, store_fn, 2, 11, 0))
    _asm_load_sym(tr, tr_base, 2, _ISR_TOP, load_fn)
    j_handle_int_idx = len(tr)
    tr.append(0)  # jal x0, handle_interrupt
    sync_idx = len(tr)
    sync_pc = tr_base + sync_idx * 4
    tr[bge_idx] = _encode_b(5, 10, 0, sync_pc - (tr_base + bge_idx * 4))
    tr.append(_encode_i(19, 11, 0, 11, 4))
    tr.append(_encode_s(35, store_fn, 2, 11, 0))
    _asm_load_sym(tr, tr_base, 2, _ISR_TOP, load_fn)
    j_handle_exc_idx = len(tr)
    tr.append(0)  # jal x0, handle_exception

    idle_idx = len(tr)
    idle_pc = tr_base + idle_idx * 4
    tr[j_handle_int_idx] = _encode_j(0, idle_pc - (tr_base + j_handle_int_idx * 4))
    tr.append(
        _encode_j(1, sw_base - (tr_base + len(tr) * 4))
    )  # jal ra, vTaskSwitchContext
    j_proc_from_int_idx = len(tr)
    tr.append(0)  # jal x0, processed_source

    exc_idx = len(tr)
    exc_pc = tr_base + exc_idx * 4
    tr[j_handle_exc_idx] = _encode_j(0, exc_pc - (tr_base + j_handle_exc_idx * 4))
    if not missing_ecall_branch:
        tr.append(_encode_i(19, 5, 0, 0, 11))
        bne_app_idx = len(tr)
        tr.append(0)  # bne a0, t0, app_exc
    else:
        bne_app_idx = None
    tr.append(_encode_j(1, sw_base - (tr_base + len(tr) * 4)))
    j_proc_from_exc_idx = len(tr)
    tr.append(0)  # jal x0, processed_source
    app_exc_idx = len(tr)
    app_exc_pc = tr_base + app_exc_idx * 4
    if bne_app_idx is not None:
        tr[bne_app_idx] = _encode_b(1, 10, 5, app_exc_pc - (tr_base + bne_app_idx * 4))
    tr.append(_encode_j(1, _APP_EXC_ADDR - (tr_base + len(tr) * 4)))
    j_proc_from_app_idx = len(tr)
    tr.append(0)

    proc_idx = len(tr)
    proc_pc = tr_base + proc_idx * 4
    tr[j_proc_from_int_idx] = _encode_j(
        0, proc_pc - (tr_base + j_proc_from_int_idx * 4)
    )
    tr[j_proc_from_exc_idx] = _encode_j(
        0, proc_pc - (tr_base + j_proc_from_exc_idx * 4)
    )
    tr[j_proc_from_app_idx] = _encode_j(
        0, proc_pc - (tr_base + j_proc_from_app_idx * 4)
    )
    _asm_load_sym(tr, tr_base, 6, _TCB_PTR, load_fn)
    tr.append(_encode_i(3, 2, load_fn, 6, 0))  # load_x sp, 0(t1)
    tr.append(_encode_i(3, 5, load_fn, 2, 0))
    tr.append(_encode_i(115, 0, 1, 5, 0x341))  # csrw mepc, t0
    tr.append(_encode_i(3, 5, load_fn, 2, 30 * word))
    tr.append(_encode_i(115, 0, 1, 5, 0x300))  # csrw mstatus, t0
    tr.append(_encode_i(3, 5, load_fn, 2, 29 * word))
    _asm_load_sym(tr, tr_base, 6, _CRIT_PTR, load_fn)
    tr.append(_encode_s(35, store_fn, 6, 5, 0))
    tr.append(_encode_i(3, 1, load_fn, 2, word))
    tr.extend(_encode_i(3, reg, load_fn, 2, (reg - 3) * word) for reg in range(5, 32))
    tr.append(_encode_i(19, 2, 0, 2, frame_bytes))
    tr.append(0x30200073)  # mret
    _emit(tr_off, tr)

    return ElfImage(
        pointer_size=word,
        endian="little",
        machine=EM_RISCV,
        sections=(
            ElfSection(".text", _TEXT_BASE, bytes(text), SHF_EXECINSTR, 1),
            ElfSection(".riscv.attributes", 0, _riscv_attributes(xlen), 0, 0x70000003),
        ),
        symbols=(
            CodeSymbol(
                "freertos_risc_v_application_exception_handler", _APP_EXC_ADDR, 16
            ),
            CodeSymbol("vTaskSwitchContext", sw_base, sw_end_off - sw_off),
        ),
        anchors=(
            ("xPortStartFirstTask", sf_base),
            ("vTaskSwitchContext", sw_base),
            ("freertos_risc_v_trap_handler", tr_base),
            ("asynchronous_interrupt", async_pc),
            ("synchronous_exception", sync_pc),
            ("handle_interrupt", idle_pc),
            ("handle_exception", exc_pc),
            ("processed_source", proc_pc),
            ("pxCurrentTCB", _TCB_PTR),
            ("pxCriticalNesting", _CRIT_PTR),
            ("xCriticalNesting", _CRIT_VAL),
            ("xISRStackTop", _ISR_TOP),
            ("pxReadyTasksLists", _READY_LISTS),
        ),
    )


@pytest.mark.parametrize(
    ("xlen", "expected_id"),
    [(32, RV32_PROFILE_ID), (64, RV64_PROFILE_ID)],
)
def test_synthetic_freertos_riscv_profile_classifies_all_stages(xlen, expected_id):
    elf = _synthetic_freertos_elf(xlen=xlen)
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None
    assert profile.profile_id == expected_id
    assert profile.xlen == xlen
    assert profile.word_size == xlen // 8

    expected_stages = {
        "start-first-entry",
        "start-first-sp-loaded",
        "start-first-sp-popped",
        "trap-entry",
        "trap-prologue-saving",
        "trap-prologue-tcb-written",
        "trap-prologue-epc-saved",
        "trap-dispatch",
        "trap-sync-dispatch",
        "trap-sync-unhandled",
        "switch-before-update",
        "switch-after-update",
        "restore-sp-loaded",
        "restore-sp-popped",
    }
    assert {stage.name for stage in profile.stages.values()} == expected_stages

    # Verify exact boundary transitions.
    s_first_load = classify_riscv_stage(profile, profile.start_first_sp_load_pc)
    assert isinstance(s_first_load, RiscvSchedulerStage)
    assert s_first_load.name == "start-first-entry"
    s_first_after = classify_riscv_stage(profile, profile.start_first_sp_load_pc + 4)
    assert isinstance(s_first_after, RiscvSchedulerStage)
    assert s_first_after.name == "start-first-sp-loaded"
    s_first_ret = classify_riscv_stage(profile, profile.start_first_ret_pc)
    assert isinstance(s_first_ret, RiscvSchedulerStage)
    assert s_first_ret.name == "start-first-sp-popped"

    assert (
        classify_riscv_stage(profile, profile.trap_start).name  # type: ignore[union-attr]
        == "trap-entry"
    )
    assert (
        classify_riscv_stage(profile, profile.trap_tcb_store_pc).name  # type: ignore[union-attr]
        == "trap-prologue-saving"
    )
    assert (
        classify_riscv_stage(profile, profile.async_epc_store_pc).name  # type: ignore[union-attr]
        == "trap-prologue-tcb-written"
    )
    assert (
        classify_riscv_stage(profile, profile.async_epc_store_pc + 4).name  # type: ignore[union-attr]
        == "trap-prologue-epc-saved"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_update_pc).name  # type: ignore[union-attr]
        == "switch-before-update"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_after_update_pc).name  # type: ignore[union-attr]
        == "switch-after-update"
    )
    assert (
        classify_riscv_stage(profile, profile.restore_load_sp_pc).name  # type: ignore[union-attr]
        == "switch-after-update"
    )
    assert (
        classify_riscv_stage(profile, profile.restore_pop_sp_pc).name  # type: ignore[union-attr]
        == "restore-sp-loaded"
    )
    assert (
        classify_riscv_stage(profile, profile.restore_mret_pc).name  # type: ignore[union-attr]
        == "restore-sp-popped"
    )

    # Mid-instruction PCs inside transition spans fail closed as ambiguous.
    assert (
        classify_riscv_stage(profile, profile.trap_start + 2)
        == "scheduler-stage-ambiguous:freertos_risc_v_trap_handler"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_start + 2)
        == "scheduler-stage-ambiguous:vTaskSwitchContext"
    )
    assert (
        classify_riscv_stage(profile, profile.start_first_start + 2)
        == "scheduler-stage-ambiguous:xPortStartFirstTask"
    )
    # Unrelated task code returns None.
    assert classify_riscv_stage(profile, _TEXT_BASE) is None


def test_freertos_riscv_profile_fails_closed_on_mutations():
    base = _synthetic_freertos_elf(xlen=32)

    assert build_riscv_profile(None)[1] == "scheduler-profile-unavailable"
    assert build_riscv_profile(replace(base, machine=40))[1] == (
        "scheduler-profile-unsupported-image"
    )
    assert build_riscv_profile(replace(base, endian="big"))[1] == (
        "scheduler-profile-unsupported-endian"
    )
    assert build_riscv_profile(replace(base, anchors=()))[1] == (
        "scheduler-profile-symbol-missing:kernel-globals"
    )
    assert build_riscv_profile(replace(base, symbols=()))[1] == (
        "scheduler-profile-symbol-missing:vTaskSwitchContext"
    )

    assert (
        "entry"
        in build_riscv_profile(_synthetic_freertos_elf(nop_switch_entry=True))[1]
    )
    assert (
        "update-count"
        in build_riscv_profile(_synthetic_freertos_elf(double_tcb_store=True))[1]
    )
    assert (
        "update-count"
        in build_riscv_profile(_synthetic_freertos_elf(zero_tcb_store=True))[1]
    )
    assert (
        "update-source"
        in build_riscv_profile(_synthetic_freertos_elf(bad_source_tcb_store=True))[1]
    )
    assert (
        "cfg-partition"
        in build_riscv_profile(_synthetic_freertos_elf(cycle_across_store=True))[1]
    )
    assert (
        "decode"
        in build_riscv_profile(_synthetic_freertos_elf(bad_insn_in_switch=True))[1]
    )
    assert (
        "gpr-slots"
        in build_riscv_profile(_synthetic_freertos_elf(clobber_t0_early=True))[1]
    )
    assert (
        "structure"
        in build_riscv_profile(_synthetic_freertos_elf(missing_ecall_branch=True))[1]
    )


_CACHED_FREERTOS_RISCV_ELFS = tuple(
    path
    for path in cached_elfs()
    if path.name == "freertos.elf"
    and path.relative_to(fixture_cache_root()).parts[:2]
    in (
        ("freertos", "qemu-virt-rv32"),
        ("freertos", "qemu-virt-rv64"),
    )
)


@pytest.mark.skipif(
    not _CACHED_FREERTOS_RISCV_ELFS, reason="no cached FreeRTOS RISC-V fixtures"
)
@pytest.mark.parametrize(
    "path",
    _CACHED_FREERTOS_RISCV_ELFS,
    ids=lambda p: "/".join(p.relative_to(fixture_cache_root()).parts[1:4]),
)
def test_cached_freertos_riscv_elfs_build_scheduler_profile(path):
    elf = ElfImage.from_path(path)
    assert elf is not None
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None, reason
    assert profile.switch_start < profile.switch_update_pc < profile.switch_end
    assert profile.trap_start < profile.async_epc_store_pc < profile.trap_end
    assert (
        profile.start_first_start
        < profile.start_first_sp_pop_pc
        < profile.start_first_ret_pc
    )
    assert len(profile.trap_saved_slots) == 28
    # Cold branch after ret in vTaskSwitchContext must still classify as pre-store.
    for pc in range(profile.switch_start, profile.switch_end, 2):
        stage = classify_riscv_stage(profile, pc)
        if isinstance(stage, RiscvSchedulerStage):
            assert stage.name in ("switch-before-update", "switch-after-update")


def _synthetic_variant_freertos_elf(
    *, xlen: int = 32, variant: str = "v11_3"
) -> ElfImage:
    """Build synthetic FreeRTOS 11.3.1-style or 10.3.x-style RISC-V ELF images."""
    from freertos.stack_frame_profile import (
        select_context as _,
    )  # keep import validated

    _ = _
    word = xlen // 8
    load_fn = 3 if word == 8 else 2
    store_fn = 3 if word == 8 else 2
    is_v10 = variant == "v10_3"
    frame_words = 30 if is_v10 else 31
    frame_bytes = frame_words * word
    mstatus_off = 29 * word if is_v10 else word
    ra_off = word if is_v10 else 2 * word
    crit_off = None if is_v10 else 30 * word
    gpr_base_shift = 3 if is_v10 else 2

    text = bytearray(0x2000)

    def _emit_words(offset: int, words: list[int]) -> int:
        for idx, insn in enumerate(words):
            text[offset + idx * 4 : offset + (idx + 1) * 4] = (
                insn & 0xFFFFFFFF
            ).to_bytes(4, "little")
        return offset + len(words) * 4

    def _emit_bytes(offset: int, chunks: list[tuple[int, int]]) -> int:
        cursor = offset
        for val, size in chunks:
            mask = (1 << (size * 8)) - 1
            text[cursor : cursor + size] = (val & mask).to_bytes(size, "little")
            cursor += size
        return cursor

    # In v10_3, xPortStartFirstTask sits BEFORE pxPortInitialiseStack and prvTaskExitError exists unused.
    sf_off = 0x100 if is_v10 else 0x200
    init_off = 0x200 if is_v10 else 0x100
    ret_stub_off = 0x080
    ret_stub_addr = _TEXT_BASE + ret_stub_off
    _emit_words(ret_stub_off, [_encode_i(103, 0, 0, 1, 0)])

    # pxPortInitialiseStack
    init_base = _TEXT_BASE + init_off
    init: list[int] = [_encode_i(19, 10, 0, 10, -frame_bytes)]
    init.append(_encode_s(35, store_fn, 10, 11, 0))  # epc arg1
    init.append(
        _encode_s(35, store_fn, 10, 12, (10 - gpr_base_shift) * word)
    )  # a0 arg2
    init.append(_encode_i(115, 5, 2, 0, 0x300))  # csrr t0, mstatus
    init.append(_encode_s(35, store_fn, 10, 5, mstatus_off))
    if is_v10:
        init.append(_encode_s(35, store_fn, 10, 0, ra_off))  # ra = 0
    else:
        _asm_load_sym(init, init_base, 5, ret_stub_addr, load_fn)
        init.append(_encode_s(35, store_fn, 10, 5, ra_off))  # ra = xTaskReturnAddress
        assert crit_off is not None
        init.append(_encode_s(35, store_fn, 10, 0, crit_off))
    init.append(_encode_i(103, 0, 0, 1, 0))  # ret
    init_end = _emit_words(init_off, init)
    if not is_v10:
        # pxPortInitialiseStack precedes xPortStartFirstTask contiguously with no zero gap.
        sf_off = init_end

    # xPortStartFirstTask
    sf_base = _TEXT_BASE + sf_off
    sf: list[int] = []
    _asm_load_sym(sf, sf_base, 2, _TCB_PTR, load_fn)
    sf.append(_encode_i(3, 2, load_fn, 2, 0))  # sp = pxCurrentTCB
    sf.append(_encode_i(3, 1, load_fn, 2, 0))  # x1 = epc (start PC)
    sf.extend(
        _encode_i(3, reg, load_fn, 2, (reg - gpr_base_shift) * word)
        for reg in range(7, 32)
    )
    if not is_v10:
        assert crit_off is not None
        sf.append(_encode_i(3, 5, load_fn, 2, crit_off))
        _asm_load_sym(sf, sf_base, 6, _CRIT_PTR, load_fn)
        sf.append(_encode_s(35, store_fn, 6, 5, 0))
    sf.append(_encode_i(3, 5, load_fn, 2, mstatus_off))
    sf.append(_encode_i(19, 5, 0, 5, 8))
    sf.append(_encode_i(115, 0, 1, 5, 0x300))
    sf.append(_encode_i(3, 5, load_fn, 2, (5 - gpr_base_shift) * word))
    sf.append(_encode_i(3, 6, load_fn, 2, (6 - gpr_base_shift) * word))
    sf.append(_encode_i(19, 2, 0, 2, frame_bytes))
    sf.append(_encode_i(103, 0, 0, 1, 0))
    _emit_words(sf_off, sf)

    # vTaskSwitchContext
    sw_off = 0x400
    sw_base = _TEXT_BASE + sw_off
    sw: list[int] = [
        _encode_i(19, 2, 0, 2, -16),
        _encode_s(35, store_fn, 2, 1, 8),
    ]
    _asm_la(sw, sw_base, 14, _READY_LISTS)
    sw.append(_encode_i(3, 15, load_fn, 14, 8))
    sw.append(_encode_i(3, 14, load_fn, 15, 24))
    _asm_la(sw, sw_base, 15, _TCB_PTR)
    sw.append(_encode_s(35, store_fn, 15, 14, 0))
    sw.append(_encode_i(3, 1, load_fn, 2, 8))
    sw.append(_encode_i(19, 2, 0, 2, 16))
    sw.append(_encode_i(103, 0, 0, 1, 0))
    sw_end_off = _emit_words(sw_off, sw)

    # freertos_risc_v_trap_handler
    tr_off = 0x800
    tr_base = _TEXT_BASE + tr_off
    anchors: list[tuple[str, int]] = [
        ("xPortStartFirstTask", sf_base),
        ("pxPortInitialiseStack", init_base),
        ("vTaskSwitchContext", sw_base),
        ("freertos_risc_v_trap_handler", tr_base),
        ("pxCurrentTCB", _TCB_PTR),
        ("xISRStackTop", _ISR_TOP),
        ("pxReadyTasksLists", _READY_LISTS),
    ]
    symbols: list[CodeSymbol] = [
        CodeSymbol("vTaskSwitchContext", sw_base, sw_end_off - sw_off),
    ]
    if is_v10:
        anchors.append(("prvTaskExitError", ret_stub_addr))
        # Single-block historical portASM layout with compressed c.beqz and is_exception self-loop.
        pre: list[tuple[int, int]] = [
            (_encode_i(19, 2, 0, 2, -frame_bytes), 4),
            (_encode_s(35, store_fn, 2, 1, ra_off), 4),
            *(
                (_encode_s(35, store_fn, 2, reg, (reg - 3) * word), 4)
                for reg in range(5, 32)
            ),
        ]
        pre.append((_encode_i(115, 5, 2, 0, 0x300), 4))  # csrr t0, mstatus
        pre.append((_encode_s(35, store_fn, 2, 5, mstatus_off), 4))
        cur = tr_base + len(pre) * 4
        hi, lo = _split_pcrel(cur, _TCB_PTR)
        pre.extend(
            [
                (_encode_u(23, 5, hi), 4),
                (_encode_i(3, 5, load_fn, 5, lo), 4),
                (_encode_s(35, store_fn, 5, 2, 0), 4),
                (_encode_i(115, 10, 2, 0, 0x342), 4),  # csrr a0, mcause
                (_encode_i(115, 11, 2, 0, 0x341), 4),  # csrr a1, mepc
                # test_if_asynchronous: srli a2, a0, xlen-1; c.beqz a2, handle_synchronous; c.nop
                (_encode_i(0x13, 12, 5, 10, xlen - 1), 4),
                (0xCE01, 2),  # c.beqz a2, +24 (2 + 2 + 20 = 24 bytes)
                (0x0001, 2),  # c.nop
            ]
        )
        # Asynchronous branch (20 bytes): store mepc, load xISRStackTop, call switch, j processed_source
        async_pc = tr_base + sum(sz for _, sz in pre)
        pre.append((_encode_s(35, store_fn, 2, 11, 0), 4))
        cur = async_pc + 4
        hi, lo = _split_pcrel(cur, _ISR_TOP)
        pre.extend(
            [
                (_encode_u(23, 2, hi), 4),
                (_encode_i(3, 2, load_fn, 2, lo), 4),
            ]
        )
        cur = tr_base + sum(sz for _, sz in pre)
        pre.append((_encode_j(1, sw_base - cur), 4))  # jal ra, vTaskSwitchContext
        j_proc1_pos = len(pre)
        pre.append((0, 4))  # jal x0, processed_source
        sync_pc = tr_base + sum(sz for _, sz in pre)
        assert sync_pc == async_pc - 4 + 24
        pre.append((_encode_i(19, 11, 0, 11, 4), 4))  # addi a1, a1, 4
        pre.append((_encode_s(35, store_fn, 2, 11, 0), 4))
        pre.append((_encode_i(19, 5, 0, 0, 11), 4))  # li t0, 11
        bne_unh_pos = len(pre)
        pre.append((0, 4))  # bne a0, t0, as_yet_unhandled
        cur = tr_base + sum(sz for _, sz in pre)
        hi, lo = _split_pcrel(cur, _ISR_TOP)
        pre.extend(
            [
                (_encode_u(23, 2, hi), 4),
                (_encode_i(3, 2, load_fn, 2, lo), 4),
            ]
        )
        cur = tr_base + sum(sz for _, sz in pre)
        pre.append((_encode_j(1, sw_base - cur), 4))  # jal ra, vTaskSwitchContext
        j_proc2_pos = len(pre)
        pre.append((0, 4))  # jal x0, processed_source

        is_exc_pc = tr_base + sum(sz for _, sz in pre)
        pre.append((_encode_j(0, 0), 4))  # j is_exception (self-loop on task_top)

        unh_pc = tr_base + sum(sz for _, sz in pre)
        bne_pc = tr_base + sum(sz for _, sz in pre[:bne_unh_pos])
        pre[bne_unh_pos] = (_encode_b(1, 10, 5, unh_pc - bne_pc), 4)
        j_is_exc_pc = tr_base + sum(sz for _, sz in pre)
        pre.append((_encode_j(0, is_exc_pc - j_is_exc_pc), 4))

        proc_pc = tr_base + sum(sz for _, sz in pre)
        for jpos in (j_proc1_pos, j_proc2_pos):
            jpc = tr_base + sum(sz for _, sz in pre[:jpos])
            pre[jpos] = (_encode_j(0, proc_pc - jpc), 4)
        hi, lo = _split_pcrel(proc_pc, _TCB_PTR)
        pre.extend(
            [
                (_encode_u(23, 6, hi), 4),
                (_encode_i(3, 6, load_fn, 6, lo), 4),
                (_encode_i(3, 2, load_fn, 6, 0), 4),
                (_encode_i(3, 5, load_fn, 2, 0), 4),
                (_encode_i(115, 0, 1, 5, 0x341), 4),
                (_encode_i(3, 5, load_fn, 2, mstatus_off), 4),
                (_encode_i(115, 0, 1, 5, 0x300), 4),
                (_encode_i(3, 1, load_fn, 2, ra_off), 4),
                *(
                    (_encode_i(3, reg, load_fn, 2, (reg - 3) * word), 4)
                    for reg in range(5, 32)
                ),
            ]
        )
        pre.append((_encode_i(19, 2, 0, 2, frame_bytes), 4))
        pre.append((0x30200073, 4))  # mret
        tr_end_off = _emit_bytes(tr_off, pre)
        anchors.append(("processed_source", proc_pc))
        symbols.append(
            CodeSymbol("freertos_risc_v_trap_handler", tr_base, tr_end_off - tr_off)
        )
    else:
        anchors.extend(
            [
                ("xTaskReturnAddress", ret_stub_addr),
                ("pxCriticalNesting", _CRIT_PTR),
                ("xCriticalNesting", _CRIT_VAL),
            ]
        )
        symbols.append(
            CodeSymbol(
                "freertos_risc_v_application_exception_handler", _APP_EXC_ADDR, 16
            )
        )
        tr: list[int] = [
            _encode_i(19, 2, 0, 2, -frame_bytes),
            _encode_s(35, store_fn, 2, 1, ra_off),
            *(
                _encode_s(35, store_fn, 2, reg, (reg - 2) * word)
                for reg in range(5, 32)
            ),
        ]
        tr.append(_encode_i(115, 5, 2, 0, 0x300))  # csrr t0, mstatus
        tr.append(_encode_s(35, store_fn, 2, 5, mstatus_off))
        assert crit_off is not None
        _asm_load_sym(tr, tr_base, 5, _CRIT_VAL, load_fn)
        tr.append(_encode_s(35, store_fn, 2, 5, crit_off))
        _asm_load_sym(tr, tr_base, 5, _TCB_PTR, load_fn)
        tr.append(_encode_s(35, store_fn, 5, 2, 0))
        tr.append(_encode_i(115, 10, 2, 0, 0x342))
        tr.append(_encode_i(115, 11, 2, 0, 0x341))
        bge_idx = len(tr)
        tr.append(0)
        async_pc = tr_base + len(tr) * 4
        tr.append(_encode_s(35, store_fn, 2, 11, 0))
        _asm_load_sym(tr, tr_base, 2, _ISR_TOP, load_fn)
        j_int_idx = len(tr)
        tr.append(0)
        sync_pc = tr_base + len(tr) * 4
        tr[bge_idx] = _encode_b(5, 10, 0, sync_pc - (tr_base + bge_idx * 4))
        tr.append(_encode_i(19, 11, 0, 11, 4))
        tr.append(_encode_s(35, store_fn, 2, 11, 0))
        _asm_load_sym(tr, tr_base, 2, _ISR_TOP, load_fn)
        j_exc_idx = len(tr)
        tr.append(0)
        idle_pc = tr_base + len(tr) * 4
        tr[j_int_idx] = _encode_j(0, idle_pc - (tr_base + j_int_idx * 4))
        tr.append(_encode_j(1, sw_base - (tr_base + len(tr) * 4)))
        j_proc1 = len(tr)
        tr.append(0)
        exc_pc = tr_base + len(tr) * 4
        tr[j_exc_idx] = _encode_j(0, exc_pc - (tr_base + j_exc_idx * 4))
        tr.append(_encode_i(19, 5, 0, 0, 11))
        bne_app = len(tr)
        tr.append(0)
        tr.append(_encode_j(1, sw_base - (tr_base + len(tr) * 4)))
        j_proc2 = len(tr)
        tr.append(0)
        app_pc = tr_base + len(tr) * 4
        tr[bne_app] = _encode_b(1, 10, 5, app_pc - (tr_base + bne_app * 4))
        tr.append(_encode_j(1, _APP_EXC_ADDR - (tr_base + len(tr) * 4)))
        j_proc3 = len(tr)
        tr.append(0)
        proc_pc = tr_base + len(tr) * 4
        for idx in (j_proc1, j_proc2, j_proc3):
            tr[idx] = _encode_j(0, proc_pc - (tr_base + idx * 4))
        _asm_load_sym(tr, tr_base, 6, _TCB_PTR, load_fn)
        tr.append(_encode_i(3, 2, load_fn, 6, 0))
        tr.append(_encode_i(3, 5, load_fn, 2, 0))
        tr.append(_encode_i(115, 0, 1, 5, 0x341))
        tr.append(_encode_i(3, 5, load_fn, 2, mstatus_off))
        tr.append(_encode_i(115, 0, 1, 5, 0x300))
        tr.append(_encode_i(3, 5, load_fn, 2, crit_off))
        _asm_load_sym(tr, tr_base, 6, _CRIT_PTR, load_fn)
        tr.append(_encode_s(35, store_fn, 6, 5, 0))
        tr.append(_encode_i(3, 1, load_fn, 2, ra_off))
        tr.extend(
            _encode_i(3, reg, load_fn, 2, (reg - 2) * word) for reg in range(5, 32)
        )
        tr.append(_encode_i(19, 2, 0, 2, frame_bytes))
        tr.append(0x30200073)
        _emit_words(tr_off, tr)
        anchors.extend(
            [
                ("asynchronous_interrupt", async_pc),
                ("synchronous_exception", sync_pc),
                ("handle_interrupt", idle_pc),
                ("handle_exception", exc_pc),
                ("application_exception_handler", app_pc),
                ("processed_source", proc_pc),
            ]
        )

    return ElfImage(
        pointer_size=word,
        endian="little",
        machine=EM_RISCV,
        sections=(
            ElfSection(".text", _TEXT_BASE, bytes(text), SHF_EXECINSTR, 1),
            ElfSection(".riscv.attributes", 0, _riscv_attributes(xlen), 0, 0x70000003),
        ),
        symbols=tuple(symbols),
        anchors=tuple(anchors),
    )


@pytest.mark.parametrize("xlen", [32, 64])
def test_synthetic_freertos_riscv_11_3_swapped_header_profile_and_context(xlen):
    from freertos.stack_frame_profile import (
        analyze_riscv_trap_profile,
        riscv_zero_lr_sentinel,
        select_context,
    )

    elf = _synthetic_variant_freertos_elf(xlen=xlen, variant="v11_3")
    word = xlen // 8
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None
    assert profile.frame_bytes == 31 * word
    assert profile.mstatus_offset == word
    slots = {reg: off for reg, off, _pc in profile.trap_saved_slots}
    assert slots[1] == 2 * word
    assert slots[31] == 29 * word

    ctx = select_context(elf)
    assert ctx.context is not None and ctx.reason == "ok"
    regs = dict(ctx.context.registers)
    assert ctx.context.sp_offset == 31 * word
    assert ctx.context.lr_offset == 2 * word
    assert regs[1] == 2 * word
    assert regs[5] == 3 * word

    span = analyze_riscv_trap_profile(elf)
    assert span is not None and span.layout.mstatus_offset == word
    assert riscv_zero_lr_sentinel(elf) is False


@pytest.mark.parametrize("xlen", [32, 64])
def test_synthetic_freertos_riscv_historical_10_3_30w_profile_and_context(xlen):
    from freertos.stack_frame_profile import (
        analyze_riscv_trap_profile,
        riscv_zero_lr_sentinel,
        select_context,
    )

    elf = _synthetic_variant_freertos_elf(xlen=xlen, variant="v10_3")
    word = xlen // 8
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None
    assert profile.frame_bytes == 30 * word
    assert profile.mstatus_offset == 29 * word
    slots = {reg: off for reg, off, _pc in profile.trap_saved_slots}
    assert slots[1] == word
    assert slots[31] == 28 * word
    unhandled = [s for s in profile.stages.values() if s.name == "trap-sync-unhandled"]
    assert unhandled and all(stage.seed == "saved-tcb" for stage in unhandled)

    ctx = select_context(elf)
    assert ctx.context is not None and ctx.reason == "ok"
    assert ctx.context.sp_offset == 30 * word
    assert ctx.context.lr_offset == word
    assert len(ctx.context.registers) == 28

    span = analyze_riscv_trap_profile(elf)
    assert span is not None and span.layout.frame_bytes == 30 * word
    assert riscv_zero_lr_sentinel(elf) is True
