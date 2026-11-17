"""Unit tests for RT-Thread RISC-V scheduler and trap stage classification."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.unwind_meta import EM_RISCV, SHF_EXECINSTR, ElfImage, ElfSection
from rtthread.riscv_scheduler_switch import (
    RV32_PROFILE_ID,
    RV64_PROFILE_ID,
    RiscvSchedulerStage,
    build_riscv_profile,
    classify_riscv_stage,
)
from tests.support.loader import cached_elfs, fixture_cache_root
from tests.unit.core.oracle_support import attributes_blob

_TEXT_BASE = 0x80000000
_FLAG_ADDR = 0x80013000
_FROM_ADDR = 0x80013004
_TO_ADDR = 0x80013008
_CUR_ADDR = 0x8001300C
_IRQ_STACK_ADDR = 0x80014000
_ENTER_ADDR = 0x80000800
_HANDLE_ADDR = 0x80000840
_LEAVE_ADDR = 0x80000880


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


def _encode_r(fn7: int, rs2: int, rs1: int, fn3: int, rd: int, opcode: int) -> int:
    return (
        ((fn7 & 0x7F) << 25)
        | ((rs2 & 31) << 20)
        | ((rs1 & 31) << 15)
        | ((fn3 & 7) << 12)
        | ((rd & 31) << 7)
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


def _asm_la(words: list[int], base: int, reg: int, target: int) -> None:
    pc = base + len(words) * 4
    delta = target - pc
    hi = (delta + 0x800) & ~0xFFF
    lo = delta - hi
    words.append(_encode_u(23, reg, hi))
    words.append(_encode_i(19, reg, 0, reg, lo))


def _riscv_attributes(xlen: int) -> bytes:
    return attributes_blob(b"riscv", ((4, 16), (5, f"rv{xlen}i2p1_m2p0_a2p1_zicsr2p0")))


def _synthetic_rtthread_elf(
    *,
    xlen: int = 32,
    epc_mode: str = "deferred-mepc",
    smp_symbol: str | None = None,
    smp_call_in_switch_to: bool = False,
    clobber_s1_before_save: bool = False,
    missing_epc_store: bool = False,
    missing_flag_clear: bool = False,
    bad_spurious_tail: bool = False,
    bad_opcode: bool = False,
) -> ElfImage:
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

    exit_off = 0x200
    exit_base = _TEXT_BASE + exit_off

    # 1. rt_hw_context_switch_to at +0x040
    st_off = 0x040
    st_base = _TEXT_BASE + st_off
    st: list[int] = [
        _encode_i(3, 2, load_fn, 10, 0),  # LOAD sp, (a0)
        _encode_i(3, 10, load_fn, 2, 2 * word),
    ]
    if smp_call_in_switch_to:
        st.append(_encode_j(1, _ENTER_ADDR - (st_base + len(st) * 4)))
    else:
        st.append(_encode_i(115, 0, 1, 10, 0x300))  # csrw mstatus, a0
    st.append(_encode_j(0, exit_base - (st_base + 3 * 4)))
    _emit(st_off, st)

    # 2. rt_hw_context_switch at +0x080
    sw_off = 0x080
    sw_base = _TEXT_BASE + sw_off
    sw: list[int] = [
        _encode_i(19, 2, 0, 2, -frame_bytes),
        _encode_s(
            35, store_fn, 2, 10, 0
        ),  # STORE sp, (a0) -- wait: base=a0(10), src=sp(2)!
    ]
    sw[1] = _encode_s(35, store_fn, 10, 2, 0)
    sw.append(_encode_s(35, store_fn, 2, 1, 0))
    sw.append(_encode_s(35, store_fn, 2, 1, word))
    sw.append(_encode_i(115, 10, 2, 0, 0x300))  # csrr a0, mstatus
    sw.append(_encode_i(19, 10, 7, 10, 8))  # andi a0, a0, 8
    beq_idx = len(sw)
    sw.append(0)
    sw.append(_encode_i(19, 10, 0, 0, 0x80))
    save_mpie_pc = sw_base + len(sw) * 4
    sw[beq_idx] = _encode_b(0, 10, 0, save_mpie_pc - (sw_base + beq_idx * 4))
    sw.append(_encode_s(35, store_fn, 2, 10, 2 * word))
    sw.extend(_encode_s(35, store_fn, 2, reg, reg * word) for reg in range(4, 32))
    sw.append(_encode_i(3, 2, load_fn, 11, 0))  # LOAD sp, (a1)
    sw.append(_encode_j(0, exit_base - (sw_base + len(sw) * 4)))
    _emit(sw_off, sw)

    # 3. rt_hw_context_switch_exit at +0x200
    ex: list[int] = [
        _encode_i(3, 10, load_fn, 2, 0),
        _encode_i(115, 0, 1, 10, 0x341),  # csrw mepc, a0
        _encode_i(3, 1, load_fn, 2, word),
        _encode_i(3, 10, load_fn, 2, 2 * word),
        _encode_i(115, 0, 2, 10, 0x300),  # csrs mstatus, a0
    ]
    ex.extend(_encode_i(3, reg, load_fn, 2, reg * word) for reg in range(4, 32))
    ex.append(_encode_i(19, 2, 0, 2, frame_bytes))
    ex.append(0x30200073)  # mret
    _emit(exit_off, ex)

    # 4. trap_entry at +0x400
    tr_off = 0x400
    tr_base = _TEXT_BASE + tr_off
    tr: list[int] = [_encode_i(19, 2, 0, 2, -frame_bytes)]
    if bad_opcode:
        tr.append(0x00000000)
    if clobber_s1_before_save:
        tr.append(_encode_i(19, 9, 0, 0, 1))
    tr.append(_encode_s(35, store_fn, 2, 1, word))
    if epc_mode == "stacked-epc" and not missing_epc_store:
        tr.append(_encode_i(115, 1, 2, 0, 0x300))
        tr.append(_encode_s(35, store_fn, 2, 1, 2 * word))
        tr.append(_encode_i(115, 1, 2, 0, 0x341))
        tr.append(_encode_s(35, store_fn, 2, 1, 0))
    else:
        tr.append(_encode_i(19, 5, 0, 0, 0x80))
        tr.append(_encode_s(35, store_fn, 2, 5, 2 * word))
    tr.extend(_encode_s(35, store_fn, 2, reg, reg * word) for reg in range(4, 32))
    tr.append(_encode_r(0, 2, 0, 0, 8, 51))  # add s0, x0, sp
    _asm_la(tr, tr_base, 2, _IRQ_STACK_ADDR)
    tr.append(_encode_j(1, _ENTER_ADDR - (tr_base + len(tr) * 4)))
    tr.append(_encode_j(1, _HANDLE_ADDR - (tr_base + len(tr) * 4)))
    tr.append(_encode_j(1, _LEAVE_ADDR - (tr_base + len(tr) * 4)))
    tr.append(_encode_r(0, 8, 0, 0, 2, 51))  # add sp, x0, s0
    _asm_la(tr, tr_base, 8, _FLAG_ADDR)
    tr.append(_encode_i(3, 18, 2, 8, 0))  # lw s2, 0(s0)
    beq_spur_idx = len(tr)
    tr.append(0)
    if not missing_flag_clear:
        tr.append(_encode_s(35, 2, 8, 0, 0))  # sw zero, 0(s0)
    if epc_mode == "deferred-mepc" and not missing_epc_store:
        tr.append(_encode_i(115, 10, 2, 0, 0x341))
        tr.append(_encode_s(35, store_fn, 2, 10, 0))
    _asm_la(tr, tr_base, 8, _FROM_ADDR)
    tr.append(_encode_i(3, 9, load_fn, 8, 0))
    tr.append(_encode_s(35, store_fn, 9, 2, 0))
    _asm_la(tr, tr_base, 8, _TO_ADDR)
    tr.append(_encode_i(3, 9, load_fn, 8, 0))
    tr.append(_encode_i(3, 2, load_fn, 9, 0))
    if epc_mode == "deferred-mepc":
        tr.append(_encode_i(3, 10, load_fn, 2, 0))
        tr.append(_encode_i(115, 0, 1, 10, 0x341))

    spurious_idx = len(tr)
    spurious_pc = tr_base + spurious_idx * 4
    tr[beq_spur_idx] = _encode_b(0, 18, 0, spurious_pc - (tr_base + beq_spur_idx * 4))
    if epc_mode == "stacked-epc":
        target = _TEXT_BASE if bad_spurious_tail else exit_base
        tr.append(_encode_j(0, target - (tr_base + len(tr) * 4)))
    else:
        tr.append(_encode_i(3, 1, load_fn, 2, word))
        tr.append(_encode_i(3, 5, load_fn, 2, 2 * word))
        tr.append(_encode_i(115, 0, 2, 5, 0x300))
        tr.extend(_encode_i(3, reg, load_fn, 2, reg * word) for reg in range(4, 32))
        tr.append(_encode_i(19, 2, 0, 2, frame_bytes))
        tr.append(0x30200073)  # mret
    _emit(tr_off, tr)

    anchors = [
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
    ]
    if smp_symbol is not None:
        anchors.append((smp_symbol, _TEXT_BASE + 0x900))

    return ElfImage(
        pointer_size=word,
        endian="little",
        machine=EM_RISCV,
        sections=(
            ElfSection(".text", _TEXT_BASE, bytes(text), SHF_EXECINSTR, 1),
            ElfSection(".riscv.attributes", 0, _riscv_attributes(xlen), 0, 0x70000003),
        ),
        symbols=(),
        anchors=tuple(anchors),
    )


@pytest.mark.parametrize(
    ("xlen", "epc_mode", "expected_id"),
    [
        (32, "deferred-mepc", RV32_PROFILE_ID),
        (64, "stacked-epc", RV64_PROFILE_ID),
    ],
)
def test_synthetic_rtthread_riscv_profile_classifies_all_stages(
    xlen, epc_mode, expected_id
):
    elf = _synthetic_rtthread_elf(xlen=xlen, epc_mode=epc_mode)
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None
    assert profile.profile_id == expected_id
    assert profile.epc_mode == epc_mode

    expected = {
        "switch-to-entry",
        "switch-to-sp-loaded",
        "context-switch-entry",
        "context-switch-saving",
        "context-switch-sp-loaded",
        "restore-window",
        "restore-sp-popped",
        "trap-entry",
        "trap-prologue-saving",
        "trap-dispatch",
        "trap-epilogue-before-switch-store",
        "trap-epilogue-before-switch-load",
    }
    if epc_mode == "deferred-mepc":
        expected.add("trap-epilogue-after-switch-load")
    assert {stage.name for stage in profile.stages.values()} == expected

    # Verify key stage boundaries.
    assert (
        classify_riscv_stage(profile, profile.switch_to_start).name  # type: ignore[union-attr]
        == "switch-to-entry"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_to_start + 4).name  # type: ignore[union-attr]
        == "switch-to-sp-loaded"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_start).name  # type: ignore[union-attr]
        == "context-switch-entry"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_store_from_pc).name  # type: ignore[union-attr]
        == "context-switch-saving"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_load_to_pc).name  # type: ignore[union-attr]
        == "context-switch-saving"
    )
    assert (
        classify_riscv_stage(profile, profile.switch_load_to_pc + 4).name  # type: ignore[union-attr]
        == "context-switch-sp-loaded"
    )
    assert (
        classify_riscv_stage(profile, profile.exit_pop_sp_pc).name  # type: ignore[union-attr]
        == "restore-window"
    )
    assert (
        classify_riscv_stage(profile, profile.exit_mret_pc).name  # type: ignore[union-attr]
        == "restore-sp-popped"
    )
    assert (
        classify_riscv_stage(profile, profile.trap_start).name  # type: ignore[union-attr]
        == "trap-entry"
    )
    assert (
        classify_riscv_stage(profile, profile.trap_mv_s0_pc).name  # type: ignore[union-attr]
        == "trap-prologue-saving"
    )
    assert (
        classify_riscv_stage(profile, profile.trap_call_start).name  # type: ignore[union-attr]
        == "trap-dispatch"
    )
    assert (
        classify_riscv_stage(profile, profile.trap_store_from_pc).name  # type: ignore[union-attr]
        == "trap-epilogue-before-switch-store"
    )
    assert (
        classify_riscv_stage(profile, profile.trap_load_to_pc).name  # type: ignore[union-attr]
        == "trap-epilogue-before-switch-load"
    )
    if epc_mode == "deferred-mepc":
        assert (
            classify_riscv_stage(profile, profile.trap_load_to_pc + 4).name  # type: ignore[union-attr]
            == "trap-epilogue-after-switch-load"
        )
        assert profile.spurious_mret_pc is not None
        assert (
            classify_riscv_stage(profile, profile.spurious_mret_pc).name  # type: ignore[union-attr]
            == "restore-sp-popped"
        )
    else:
        assert (
            classify_riscv_stage(profile, profile.spurious_start).name  # type: ignore[union-attr]
            == "restore-window"
        )

    assert (
        classify_riscv_stage(profile, profile.trap_start + 2)
        == "scheduler-stage-ambiguous:trap_entry"
    )
    assert isinstance(
        classify_riscv_stage(profile, profile.trap_start), RiscvSchedulerStage
    )
    assert (
        classify_riscv_stage(profile, profile.switch_start + 2)
        == "scheduler-stage-ambiguous:rt_hw_context_switch"
    )
    assert classify_riscv_stage(profile, _TEXT_BASE) is None


def test_rtthread_riscv_profile_fails_closed_on_smp_fpu_and_mutations():
    base = _synthetic_rtthread_elf(xlen=32, epc_mode="deferred-mepc")
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

    for sym in (
        "rt_cpus_lock_status_restore",
        "rt_scheduler_do_irq_switch",
        "rt_signal_check",
    ):
        assert build_riscv_profile(_synthetic_rtthread_elf(smp_symbol=sym))[1] == (
            "scheduler-profile-unsupported-smp"
        )

    assert (
        "call"
        in build_riscv_profile(_synthetic_rtthread_elf(smp_call_in_switch_to=True))[1]
    )
    assert (
        "preserved-slots"
        in build_riscv_profile(_synthetic_rtthread_elf(clobber_s1_before_save=True))[1]
    )
    assert (
        "epc-policy"
        in build_riscv_profile(_synthetic_rtthread_elf(missing_epc_store=True))[1]
    )
    assert (
        "epilogue"
        in build_riscv_profile(_synthetic_rtthread_elf(missing_flag_clear=True))[1]
    )
    assert (
        "tail"
        in build_riscv_profile(
            _synthetic_rtthread_elf(
                xlen=64, epc_mode="stacked-epc", bad_spurious_tail=True
            )
        )[1]
    )
    assert "decode" in build_riscv_profile(_synthetic_rtthread_elf(bad_opcode=True))[1]


_CACHED_RTTHREAD_RISCV_ELFS = tuple(
    path
    for path in cached_elfs()
    if path.name == "rtthread.elf"
    and path.relative_to(fixture_cache_root()).parts[:2]
    in (("rtthread", "rv32"), ("rtthread", "rv64"))
)


@pytest.mark.skipif(
    not _CACHED_RTTHREAD_RISCV_ELFS, reason="no cached RT-Thread RISC-V fixtures"
)
@pytest.mark.parametrize(
    "path",
    _CACHED_RTTHREAD_RISCV_ELFS,
    ids=lambda p: "/".join(p.relative_to(fixture_cache_root()).parts[1:3]),
)
def test_cached_rtthread_riscv_elfs_build_scheduler_profile(path):
    elf = ElfImage.from_path(path)
    assert elf is not None
    profile, reason = build_riscv_profile(elf)
    assert reason == "ok" and profile is not None, reason
    want_mode = "deferred-mepc" if profile.xlen == 32 else "stacked-epc"
    assert profile.epc_mode == want_mode
    assert (
        profile.switch_start < profile.switch_store_from_pc < profile.switch_load_to_pc
    )
    assert profile.trap_start < profile.trap_call_start < profile.trap_load_to_pc
    assert len(profile.switch_saved_slots) == 14
    assert len(profile.trap_saved_slots) == 14
