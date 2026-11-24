"""FreeRTOS scheduler-transition seeds for FPU task layouts (synthetic)."""

from __future__ import annotations

from freertos.backtrace.arm_scheduler_switch import (
    SchedulerProfile,
    SchedulerStage,
)
from freertos.backtrace.scheduler_seed import build_stage_seed
from freertos.backtrace.unwind import (
    ARMV7M_CM4F_BASIC,
    ARMV7M_CM4F_FPU,
    ARMV7M_INTEGER,
)

TASK_TCB = 0x20001000
TASK_TOP = 0x20002004
TASK_LOW = 0x20001000
TASK_HIGH = 0x20003000
EXIT_PC = 0x2000
ENTRY_PC = 0x3000
FD = 0xFFFFFFFD
ED = 0xFFFFFFED


class _Memory:
    """Sparse word memory; unmapped reads fail closed."""

    def __init__(self) -> None:
        self.words: dict[int, bytes] = {}

    def word(self, address: int, value: int) -> None:
        self.words[address] = value.to_bytes(4, "little")

    def __call__(self, address: int, size: int) -> bytes | None:
        if size == 4 and address in self.words:
            return self.words[address]
        return None


def _profile() -> SchedulerProfile:
    return SchedulerProfile(
        profile_id="test-fpu",
        fingerprint="test",
        pendsv_start=0x1000,
        pendsv_end=0x1100,
        svc_start=0x1200,
        svc_end=0x1300,
        start_first_start=0x1400,
        start_first_end=0x1500,
        switch_start=0x1600,
        switch_end=0x1700,
        pendsv_entry=0x1000,
        pendsv_save_pending=0x1020,
        pendsv_save_written=0x1040,
        pendsv_writeback=0x1060,
        pendsv_switch_call=0x1080,
        pendsv_after_switch=0x1090,
        pendsv_select=0x10A0,
        pendsv_saved_sp=0x10B0,
        pendsv_restored_pending_psp=0x10C0,
        pendsv_psp_written=0x10D0,
        pendsv_exception_return=0x10E0,
        svc_entry=0x1200,
        svc_saved_sp=0x1220,
        svc_restored_pending_psp=0x1240,
        svc_psp_written=0x1260,
        svc_exception_return=0x1280,
        start_first_svc=0x1400,
        switch_entry=0x1600,
        switch_update=0x1620,
        switch_after_update=0x1640,
        switch_return=0x1650,
        px_current_tcb=0x2000,
    )


def _registers(overrides: dict[int, int] | None = None) -> dict[int, int]:
    registers = {register: 0x40 + register - 4 for register in range(4, 12)}
    registers.update({13: TASK_HIGH, 14: FD, 15: 0x1000})
    if overrides:
        registers.update(overrides)
    return registers


def _write_fpu_block(
    memory: _Memory, top: int, *, pc: int = 0x1000, lr: int = EXIT_PC
) -> None:
    for index in range(8):
        memory.word(top + index * 4, 0x40 + index)
    memory.word(top + 32, ED)
    for index in range(16):
        memory.word(top + 36 + index * 4, 0xFFFFFFFF)
    memory.word(top + 100, 1)
    memory.word(top + 104, 2)
    memory.word(top + 108, 3)
    memory.word(top + 112, 4)
    memory.word(top + 116, 12)
    memory.word(top + 120, lr)
    memory.word(top + 124, pc)
    memory.word(top + 128, 0x61000000)


def _write_cm4f_basic_block(
    memory: _Memory, top: int, *, pc: int = 0x1000, lr: int = EXIT_PC
) -> None:
    for index in range(8):
        memory.word(top + index * 4, 0x40 + index)
    memory.word(top + 32, FD)
    memory.word(top + 36, 0)
    memory.word(top + 40, 0x2000)
    memory.word(top + 44, 0x100)
    memory.word(top + 48, 0x200)
    memory.word(top + 52, 0x300)
    memory.word(top + 56, lr)
    memory.word(top + 60, pc)
    memory.word(top + 64, 0x61000000)


def _write_cm4f_initial(
    memory: _Memory, top: int, *, pc: int = ENTRY_PC, lr: int = EXIT_PC
) -> None:
    for index in range(8):
        memory.word(top + index * 4, 0xA5A5A5A5)
    memory.word(top + 32, FD)
    memory.word(top + 36, 0)
    memory.word(top + 40, 0)
    memory.word(top + 44, 0)
    memory.word(top + 48, 0)
    memory.word(top + 52, 0)
    memory.word(top + 56, lr)
    memory.word(top + 60, pc)
    memory.word(top + 64, 0x01000000)


def _build(
    profile,
    stage,
    memory,
    registers,
    *,
    psp,
    xpsr,
    context,
    task_top=TASK_TOP,
    fpu_context=None,
):
    return build_stage_seed(
        profile,
        stage,
        read=memory,
        context=context,
        task_tcb=TASK_TCB,
        task_top=task_top,
        task_low=TASK_LOW,
        task_high=TASK_HIGH,
        psp=psp,
        msp=TASK_HIGH,
        registers=registers,
        xpsr=xpsr,
        stkalign=True,
        current_tcb=TASK_TCB,
        exit_pc=EXIT_PC,
        is_entry=lambda address: address == ENTRY_PC,
        fpu_context=fpu_context,
    )


def test_basic_context_upgrades_through_supplied_fpu_pair():
    profile = _profile()
    memory = _Memory()
    _write_fpu_block(memory, TASK_TOP)
    stage = SchedulerStage("pendsv-entry", "old", "live-frame", 0x1000)
    seed = _build(
        profile,
        stage,
        memory,
        _registers({14: ED}),
        psp=TASK_TOP + 100,
        xpsr=0x0100000E,
        context=ARMV7M_CM4F_BASIC,
        fpu_context=ARMV7M_CM4F_FPU,
    )
    assert not isinstance(seed, str)
    assert seed.seed.sp == TASK_TOP + 204


def test_basic_context_without_pair_refuses_extended_frame():
    profile = _profile()
    memory = _Memory()
    _write_fpu_block(memory, TASK_TOP)
    stage = SchedulerStage("pendsv-entry", "old", "live-frame", 0x1000)
    assert (
        _build(
            profile,
            stage,
            memory,
            _registers({14: ED}),
            psp=TASK_TOP + 100,
            xpsr=0x0100000E,
            context=ARMV7M_CM4F_BASIC,
        )
        == "scheduler-fpu-context-unproven"
    )


def test_fpu_pendsv_entry_live_frame_restores_caller_sp():
    profile = _profile()
    memory = _Memory()
    _write_fpu_block(memory, TASK_TOP)
    stage = SchedulerStage("pendsv-entry", "old", "live-frame", 0x1000)
    seed = _build(
        profile,
        stage,
        memory,
        _registers({14: ED}),
        psp=TASK_TOP + 100,
        xpsr=0x0100000E,
        context=ARMV7M_CM4F_FPU,
    )
    assert not isinstance(seed, str)
    assert seed.seed.pc == 0x1000
    assert seed.seed.sp == TASK_TOP + 204


def test_fpu_context_refuses_basic_live_frame():
    profile = _profile()
    memory = _Memory()
    _write_cm4f_basic_block(memory, TASK_TOP)
    stage = SchedulerStage("pendsv-entry", "old", "live-frame", 0x1000)
    assert (
        _build(
            profile,
            stage,
            memory,
            _registers({14: FD}),
            psp=TASK_TOP + 36,
            xpsr=0x0100000E,
            context=ARMV7M_CM4F_FPU,
        )
        == "scheduler-fpu-context-unproven"
    )


def test_saved_tcb_decodes_fpu_target_after_basic_interrupt():
    profile = _profile()
    memory = _Memory()
    _write_fpu_block(memory, TASK_TOP, lr=EXIT_PC)
    stage = SchedulerStage("pendsv-writeback", "old", "saved-tcb", 0x1060)
    seed = _build(
        profile,
        stage,
        memory,
        _registers({14: FD}),
        psp=TASK_TOP + 100,
        xpsr=0x0100000E,
        context=ARMV7M_CM4F_FPU,
    )
    assert not isinstance(seed, str)
    assert seed.seed.sp == TASK_TOP + 204


def test_basic_context_refuses_fpu_saved_slot():
    profile = _profile()
    memory = _Memory()
    _write_fpu_block(memory, TASK_TOP)
    stage = SchedulerStage("pendsv-writeback", "old", "saved-tcb", 0x1060)
    assert (
        _build(
            profile,
            stage,
            memory,
            _registers({14: FD}),
            psp=TASK_TOP + 100,
            xpsr=0x0100000E,
            context=ARMV7M_INTEGER,
        )
        == "scheduler-fpu-context-unproven"
    )


def test_fpu_exception_return_accepts_extended_lr():
    profile = _profile()
    memory = _Memory()
    _write_fpu_block(memory, TASK_TOP)
    stage = SchedulerStage("pendsv-exception-return", "new", "live-frame-new", 0x10E0)
    seed = _build(
        profile,
        stage,
        memory,
        _registers({14: ED}),
        psp=TASK_TOP + 100,
        xpsr=0x0100000E,
        context=ARMV7M_CM4F_FPU,
    )
    assert not isinstance(seed, str)
    assert seed.seed.sp == TASK_TOP + 204


def test_cm4f_basic_geometry_uses_36_byte_software_block():
    profile = _profile()
    memory = _Memory()
    _write_cm4f_basic_block(memory, TASK_TOP)
    stage = SchedulerStage("pendsv-entry", "old", "live-frame", 0x1000)
    seed = _build(
        profile,
        stage,
        memory,
        _registers({14: FD}),
        psp=TASK_TOP + 36,
        xpsr=0x0100000E,
        context=ARMV7M_CM4F_BASIC,
    )
    assert not isinstance(seed, str)
    assert seed.seed.sp == TASK_TOP + 68


def test_first_start_with_fpu_context_is_refused():
    profile = _profile()
    memory = _Memory()
    _write_cm4f_initial(memory, TASK_TOP)
    stage = SchedulerStage("svc-entry", "new", "first-start-saved", 0x1200)
    assert (
        _build(
            profile,
            stage,
            memory,
            _registers({14: 0xFFFFFFF9}),
            psp=TASK_TOP + 68,
            xpsr=0x0100000B,
            context=ARMV7M_CM4F_FPU,
        )
        == "scheduler-fpu-context-unproven"
    )


def test_switch_stage_with_fpu_slot_decodes_saved_tcb():
    profile = _profile()
    memory = _Memory()
    _write_fpu_block(memory, TASK_TOP, lr=EXIT_PC)
    stage = SchedulerStage("switch-entry", "old", "saved-tcb", 0x1600)
    seed = _build(
        profile,
        stage,
        memory,
        _registers({14: 0x1650}),
        psp=TASK_TOP + 100,
        xpsr=0x0100000E,
        context=ARMV7M_CM4F_FPU,
    )
    assert not isinstance(seed, str)
    assert seed.seed.sp == TASK_TOP + 204


def test_cm4f_pendsv_admits_unstarted_basic_task_and_refuses_unproven():
    profile = _profile()
    memory = _Memory()
    _write_cm4f_initial(memory, TASK_TOP)
    fill_regs = dict.fromkeys(range(4, 12), 0xA5A5A5A5)
    for stage_name, psp, extra in (
        ("pendsv-saved-sp", TASK_LOW + 0x100, {0: TASK_TOP}),
        ("pendsv-restored-pending-psp", TASK_LOW + 0x100, {0: TASK_TOP + 36}),
        ("pendsv-exception-return", TASK_TOP + 36, {14: FD}),
    ):
        seed_kind = (
            "saved-tcb"
            if stage_name == "pendsv-saved-sp"
            else "restore-buffer"
            if stage_name == "pendsv-restored-pending-psp"
            else "live-frame-new"
        )
        stage = SchedulerStage(stage_name, "new", seed_kind, 0x10B0)
        seed = _build(
            profile,
            stage,
            memory,
            _registers({**fill_regs, **extra}),
            psp=psp,
            xpsr=0x0100000E,
            context=ARMV7M_CM4F_BASIC,
        )
        assert not isinstance(seed, str), (stage_name, seed)
        assert seed.seed.source == "unstarted"
        assert seed.seed.pc == ENTRY_PC
        assert seed.seed.root_pc == ENTRY_PC
        assert seed.seed.root_reason == "task-entry"

        refusal = build_stage_seed(
            profile,
            stage,
            read=memory,
            context=ARMV7M_CM4F_BASIC,
            task_tcb=TASK_TCB,
            task_top=TASK_TOP,
            task_low=TASK_LOW,
            task_high=TASK_HIGH,
            psp=psp,
            msp=TASK_HIGH,
            registers=_registers({**fill_regs, **extra}),
            xpsr=0x0100000E,
            stkalign=True,
            current_tcb=TASK_TCB,
            exit_pc=EXIT_PC,
            is_entry=lambda _pc: False,
            fpu_context=ARMV7M_CM4F_FPU,
        )
        assert refusal == "scheduler-unstarted-selection-unproven"
