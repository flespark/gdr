"""Owner-consistent M-Profile scheduler-transition seeds for RT-Thread (pure).

RT-Thread updates ``rt_current_thread`` to the incoming thread before calling
``rt_hw_context_switch`` / ``rt_hw_context_switch_to`` and leaves
``rt_interrupt_from_thread`` / ``rt_interrupt_to_thread`` populated after
PendSV finishes.  A switch is active only while
``rt_thread_switch_interrupt_flag != 0`` or the stopped PC falls inside verified
switch handlers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.backtrace.unwind import SeedFrame
from core.backtrace.unwind_arm import FaultPrecision
from core.backtrace.unwind_riscv import (
    MSTATUS_MPP_MACHINE,
    MSTATUS_MPP_MASK,
    RiscvSnapshot,
    riscv_exception_guard,
    riscv_fault_precision,
    u_xlen_mask,
)
from core.elf import RISCV_CONVENTION, Endian, Reader
from core.stack_frame_parser import (
    SavedStackContext,
    StackContextSelection,
    decode_profile_slots,
    read_slot,
)

from .arm_scheduler_switch import SchedulerProfile, SchedulerStage, classify_stage
from .backtrace import (
    ARMV7EM_EXC_RETURN_SLOT,
    ARMV8M_MAINLINE_BASIC_EXC_RETURN,
    ARMV8M_MAINLINE_EXTENDED_EXC_RETURN,
    _armv8m_psplim_valid,
    decode_armv7em_saved_context,
    decode_armv8m_mainline_saved_context,
    decode_saved_context,
)
from .riscv_scheduler_switch import (
    RiscvSchedulerProfile,
    RiscvSchedulerStage,
)

_XPSR_T_BIT = 1 << 24
_XPSR_STKALIGN_BIT = 1 << 9
_XPSR_IPSR_MASK = 0x1FF
_EXC_RETURN_THREAD_PSP = 0xFFFFFFFD
_EXC_RETURN_THREAD_PSP_FPU = 0xFFFFFFED
_CORE_REGS = tuple(range(4, 12))
_HW_FRAME_SLOTS = (
    (0, 0),
    (1, 4),
    (2, 8),
    (3, 12),
    (12, 16),
    (14, 20),
)
_HW_BASIC_BYTES = 32
_HW_FPU_BYTES = 104
_REQUIRED_RISCV_LIVE_REGS = frozenset(
    reg for reg, _name in RISCV_CONVENTION.live_registers
)
_RISCV_CALLEE_FILL_REGS = (5, 6, 7, 8, 9, *range(18, 28))


@dataclass(frozen=True)
class SchedulerSeed:
    """Validated seed resolved at one proven scheduler transition stage."""

    seed: SeedFrame
    stage: str
    owner: str
    evidence: str
    fault: FaultPrecision | None = None


def _in_bounds(low: int, high: int, start: int, end: int) -> bool:
    return low <= start <= end <= high


def _sw_size(context: SavedStackContext) -> int:
    return dict(context.registers)[0]


def _select_effective_context(
    profile: SchedulerProfile,
    selection: StackContextSelection,
    *,
    top: int,
    read: Reader,
    low: int,
    high: int,
    endian: Endian,
) -> tuple[SavedStackContext | None, int | None, str]:
    if selection.context is None:
        return None, None, "unsupported-port"
    if profile.isa in ("armv6-m", "armv7-m") or (
        profile.isa == "armv7e-m" and not profile.fpu
    ):
        return selection.context, None, "ok"
    if profile.isa == "armv7e-m":
        flag = read_slot(
            read,
            top + ARMV7EM_EXC_RETURN_SLOT,
            4,
            endian,
            stack_low=low,
            stack_high=high,
        )
        if flag is None:
            return None, None, "saved-context-unreadable"
        if flag == 0:
            return selection.context, 0, "ok"
        if flag == 1 and selection.alt_context is not None:
            return selection.alt_context, 1, "ok"
        return None, None, "unsupported-extension-context"
    header = tuple(
        read_slot(read, top + offset, 4, endian, stack_low=low, stack_high=high)
        for offset in range(0, 16, 4)
    )
    if None in header:
        return None, None, "saved-context-unreadable"
    exc_return = header[1]
    ctx = (
        selection.context
        if exc_return == ARMV8M_MAINLINE_BASIC_EXC_RETURN
        else selection.alt_context
        if exc_return == ARMV8M_MAINLINE_EXTENDED_EXC_RETURN
        else None
    )
    if (
        ctx is None
        or header[0] != 0
        or not _armv8m_psplim_valid(header[2], stack_low=low, sp=top + ctx.sp_offset)
        or header[3] is None
        or header[3] & 1
    ):
        return None, None, "unsupported-extension-context"
    return ctx, exc_return, "ok"


def _saved_core_matches(
    *,
    read: Reader,
    top: int,
    context: SavedStackContext,
    registers: Mapping[int, int],
    low: int,
    high: int,
    endian: Endian,
    check_regs: tuple[int, ...] = _CORE_REGS,
) -> bool:
    offsets = dict(context.registers)
    for register in check_regs:
        saved = read_slot(
            read,
            top + offsets[register],
            context.pointer_size,
            endian,
            stack_low=low,
            stack_high=high,
        )
        if saved is None or saved != registers.get(register):
            return False
    return True


def _save_written_core_matches(
    *,
    profile: SchedulerProfile,
    context: SavedStackContext,
    sw_meta: int | None,
    top: int,
    read: Reader,
    registers: Mapping[int, int],
    low: int,
    high: int,
    endian: Endian,
) -> bool:
    if profile.isa == "armv7e-m" and profile.fpu:
        if registers.get(4) != sw_meta:
            return False
        check_regs: tuple[int, ...] = tuple(range(5, 12))
    elif profile.isa == "armv8-m.mainline":
        saved_psplim = read_slot(
            read, top + 8, context.pointer_size, endian, stack_low=low, stack_high=high
        )
        saved_ctrl = read_slot(
            read, top + 12, context.pointer_size, endian, stack_low=low, stack_high=high
        )
        if (
            registers.get(2) != 0
            or registers.get(3) != sw_meta
            or not _armv8m_psplim_valid(
                saved_psplim, stack_low=low, sp=top + context.sp_offset
            )
            or registers.get(4) != saved_psplim
            or saved_ctrl is None
            or (saved_ctrl & ~0x6) != 0
            or registers.get(5) != saved_ctrl
        ):
            return False
        check_regs = tuple(range(6, 12))
    else:
        check_regs = _CORE_REGS
    return _saved_core_matches(
        read=read,
        top=top,
        context=context,
        registers=registers,
        low=low,
        high=high,
        endian=endian,
        check_regs=check_regs,
    )


def _decode_hw_frame(
    *,
    hw_sp: int,
    core_regs: Mapping[int, int],
    fpu_extended: bool,
    read: Reader,
    low: int,
    high: int,
    endian: Endian,
    stkalign: bool,
    entry: int | None,
    fill_word: int,
    source: str,
) -> tuple[SeedFrame | None, str]:
    frame_bytes = _HW_FPU_BYTES if fpu_extended else _HW_BASIC_BYTES
    if not _in_bounds(low, high, hw_sp, hw_sp + frame_bytes):
        return None, "scheduler-frame-outside-task-stack"
    registers: dict[int, int] = {
        register: core_regs[register]
        for register in _CORE_REGS
        if register in core_regs
    }
    if len(registers) != len(_CORE_REGS):
        return None, "live-registers-unavailable"
    for register, offset in _HW_FRAME_SLOTS:
        value = read_slot(
            read, hw_sp + offset, 4, endian, stack_low=low, stack_high=high
        )
        if value is None:
            return None, "scheduler-context-unreadable"
        registers[register] = value
    pc = read_slot(read, hw_sp + 24, 4, endian, stack_low=low, stack_high=high)
    xpsr = read_slot(read, hw_sp + 28, 4, endian, stack_low=low, stack_high=high)
    if pc is None or xpsr is None:
        return None, "scheduler-context-unreadable"
    if not xpsr & _XPSR_T_BIT:
        return None, "scheduler-frame-not-thumb"
    if xpsr & _XPSR_IPSR_MASK:
        return None, "scheduler-frame-ipsr-invalid"
    code_pc = pc & ~1
    unstarted = bool(
        entry
        and source == "unstarted-candidate"
        and code_pc == (entry & ~1)
        and all(registers[reg] == fill_word for reg in _CORE_REGS)
    )
    if code_pc == 0 or ((pc & 1) and not unstarted):
        return None, "scheduler-frame-pc-invalid"
    caller_sp = (
        hw_sp + frame_bytes + (4 if stkalign and (xpsr & _XPSR_STKALIGN_BIT) else 0)
    )
    if caller_sp > high:
        return None, "scheduler-frame-outside-task-stack"
    registers[13] = caller_sp
    registers[15] = code_pc
    resolved_source = (
        "unstarted"
        if unstarted
        else ("current" if source == "unstarted-candidate" else source)
    )
    root_pc = (entry & ~1) if entry else None
    return (
        SeedFrame(
            pc=code_pc,
            sp=caller_sp,
            regs=registers,
            source=resolved_source,
            root_pc=code_pc if unstarted else root_pc,
        ),
        "ok",
    )


def _decode_saved_thread(
    selection: StackContextSelection,
    *,
    top: int,
    read: Reader,
    low: int,
    high: int,
    endian: Endian,
    entry: int | None,
) -> tuple[SeedFrame | None, str]:
    context = selection.context
    if context is None:
        return None, "unsupported-port"
    if selection.alt_context is not None and selection.isa in (
        "armv7e-m",
        "armv8-m.mainline",
    ):
        decode_pair = (
            decode_armv7em_saved_context
            if selection.isa == "armv7e-m"
            else decode_armv8m_mainline_saved_context
        )
        return decode_pair(
            top,
            read,
            basic=context,
            fpu=selection.alt_context,
            endian=endian,
            entry=entry or None,
            stack_low=low,
            stack_high=high,
        )
    return decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        entry=entry or None,
        stack_low=low,
        stack_high=high,
    )


def _stage_detail(stage: SchedulerStage | None, default: str) -> tuple[str, str]:
    if stage is None:
        return default, "interrupt active prior to PendSV entry"
    return stage.name, stage.detail


def _check_restored_state(
    profile: SchedulerProfile,
    context: SavedStackContext,
    sw_meta: int | None,
    stage: SchedulerStage,
    live_regs: Mapping[int, int],
    live_psplim: int | None,
    live_control: int | None,
    *,
    expected_psplim: int | None = None,
    stack_low: int | None = None,
    expected_sp: int | None = None,
) -> str | None:
    if profile.isa == "armv7e-m" and profile.fpu:
        if live_regs.get(3) != sw_meta:
            return "scheduler-restore-window-inconsistent"
        if stage.name == "pendsv-exception-return":
            want_lr = (
                _EXC_RETURN_THREAD_PSP_FPU if context.fpu else _EXC_RETURN_THREAD_PSP
            )
            if live_regs.get(14) != want_lr:
                return "scheduler-exc-return-inconsistent"
        return None
    if profile.isa == "armv8-m.mainline":
        # NOTE: In Handler mode ARMv8-M reads CONTROL.SPSEL (bit 1) as 0; allow
        # bit 1 (SPSEL) and bit 2 (FPCA) while rejecting nPRIV (bit 0) or
        # extension bits outside 0..2.
        if (
            not _armv8m_psplim_valid(live_psplim, stack_low=stack_low, sp=expected_sp)
            or (expected_psplim is not None and live_psplim != expected_psplim)
            or live_control is None
            or (live_control & ~0x6) != 0
        ):
            return "unsupported-extension-context"
        if live_regs.get(14) != sw_meta:
            return "scheduler-exc-return-inconsistent"
        return None
    if stage.name == "pendsv-exception-return":
        if profile.isa == "armv6-m":
            if live_regs.get(0) != _EXC_RETURN_THREAD_PSP:
                return "scheduler-exc-return-inconsistent"
        elif live_regs.get(14) != _EXC_RETURN_THREAD_PSP:
            return "scheduler-exc-return-inconsistent"
    return None


def _resolve_old_thread(
    profile: SchedulerProfile,
    selection: StackContextSelection,
    stage: SchedulerStage | None,
    *,
    flag_value: int,
    from_ptr: int,
    thread_sp: int,
    thread_entry: int | None,
    low: int,
    high: int,
    endian: Endian,
    stkalign: bool,
    read: Reader,
    live_pc: int,
    live_sp: int,
    live_psp: int | None,
    live_exc: int,
    live_regs: Mapping[int, int],
) -> SchedulerSeed | str:
    stage_name, stage_detail = _stage_detail(stage, "pending-before-pendsv")
    if stage is None or stage.name == "switch-return":
        if flag_value != 1:
            return "scheduler-transition-unproven"
        if live_exc == 14:
            return "scheduler-stage-ambiguous:PendSV_Handler"
        if live_exc == 0:
            if not _in_bounds(low, high, live_sp, live_sp) or live_sp != live_psp:
                return "scheduler-psp-inconsistent"
            seed = SeedFrame(
                pc=live_pc & ~1,
                sp=live_sp,
                regs=dict(live_regs),
                source="current",
                root_pc=(thread_entry & ~1) if thread_entry else None,
            )
            return SchedulerSeed(seed, stage_name, "old", stage_detail)
        if live_psp is None or not _in_bounds(low, high, live_psp, live_psp):
            return "exception-task-context-conflict"
        regs = dict(live_regs)
        regs[13] = live_sp
        seed = SeedFrame(
            pc=live_pc & ~1,
            sp=live_sp,
            regs=regs,
            source="current",
            exception_number=live_exc,
            process_sp=live_psp,
            root_pc=(thread_entry & ~1) if thread_entry else None,
        )
        return SchedulerSeed(seed, stage_name, "old", stage_detail)

    if live_exc != 14:
        return "scheduler-transition-unproven"

    if stage.name in ("pendsv-entry", "pendsv-save-pending"):
        want_flag = 1 if stage.name == "pendsv-entry" else 0
        if flag_value != want_flag:
            return "scheduler-transition-unproven"
        exc_lr = live_regs.get(14, 0)
        allowed_lr = (
            (_EXC_RETURN_THREAD_PSP, _EXC_RETURN_THREAD_PSP_FPU)
            if (profile.isa == "armv7e-m" and profile.fpu)
            or profile.isa == "armv8-m.mainline"
            else (_EXC_RETURN_THREAD_PSP,)
        )
        if exc_lr not in allowed_lr:
            return "scheduler-exc-return-inconsistent"
        fpu_ext = exc_lr == _EXC_RETURN_THREAD_PSP_FPU
        if live_psp is None:
            return "scheduler-psp-inconsistent"
        seed, reason = _decode_hw_frame(
            hw_sp=live_psp,
            core_regs=live_regs,
            fpu_extended=fpu_ext,
            read=read,
            low=low,
            high=high,
            endian=endian,
            stkalign=stkalign,
            entry=thread_entry,
            fill_word=selection.context.fill_word if selection.context else 0xDEADBEEF,
            source="current",
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage_name, "old", stage_detail)

    if stage.name == "pendsv-save-written":
        if flag_value != 0 or live_regs.get(0) != from_ptr:
            return "scheduler-save-window-inconsistent"
        top = live_regs.get(1)
        if top is None or live_psp is None:
            return "scheduler-save-window-inconsistent"
        context, sw_meta, ctx_reason = _select_effective_context(
            profile, selection, top=top, read=read, low=low, high=high, endian=endian
        )
        if context is None:
            return ctx_reason
        if top + _sw_size(context) != live_psp:
            return "scheduler-save-window-inconsistent"
        if not _save_written_core_matches(
            profile=profile,
            context=context,
            sw_meta=sw_meta,
            top=top,
            read=read,
            registers=live_regs,
            low=low,
            high=high,
            endian=endian,
        ):
            return "scheduler-save-window-inconsistent"
        seed, reason = _decode_saved_thread(
            selection,
            top=top,
            read=read,
            low=low,
            high=high,
            endian=endian,
            entry=thread_entry,
        )
        if seed is None:
            return reason
        seed = SeedFrame(
            pc=seed.pc,
            sp=seed.sp,
            regs=seed.regs,
            source="saved-buffer",
            root_pc=seed.root_pc,
        )
        return SchedulerSeed(seed, stage_name, "old", stage_detail)

    # pendsv-switch-to, pendsv-saved-sp, pendsv-restored-pending-psp, pendsv-psp-written, pendsv-exception-return
    if flag_value != 0:
        return "scheduler-transition-unproven"
    context, _meta, ctx_reason = _select_effective_context(
        profile, selection, top=thread_sp, read=read, low=low, high=high, endian=endian
    )
    if context is None:
        return ctx_reason
    if stage.name in (
        "pendsv-switch-to",
        "pendsv-saved-sp",
        "pendsv-restored-pending-psp",
    ) and live_psp != thread_sp + _sw_size(context):
        return "scheduler-saved-sp-inconsistent"
    seed, reason = _decode_saved_thread(
        selection,
        top=thread_sp,
        read=read,
        low=low,
        high=high,
        endian=endian,
        entry=thread_entry,
    )
    if seed is None:
        return reason
    return SchedulerSeed(seed, stage_name, "old", stage_detail)


def _resolve_new_thread(
    profile: SchedulerProfile,
    selection: StackContextSelection,
    stage: SchedulerStage | None,
    *,
    flag_value: int,
    thread_sp: int,
    thread_entry: int | None,
    low: int,
    high: int,
    endian: Endian,
    stkalign: bool,
    read: Reader,
    live_psp: int | None,
    live_exc: int,
    live_regs: Mapping[int, int],
    live_psplim: int | None,
    live_control: int | None,
) -> SchedulerSeed | str:
    stage_name, stage_detail = _stage_detail(stage, "pending-before-pendsv")
    if stage is None or stage.name in (
        "switch-return",
        "pendsv-entry",
        "pendsv-save-pending",
        "pendsv-save-written",
        "pendsv-switch-to",
        "pendsv-saved-sp",
    ):
        want_flag = (
            1
            if (stage is None or stage.name in ("switch-return", "pendsv-entry"))
            else 0
        )
        if flag_value != want_flag:
            return "scheduler-transition-unproven"
        if stage is not None and stage.name != "switch-return" and live_exc != 14:
            return "scheduler-transition-unproven"
        if (
            stage is not None
            and stage.name == "pendsv-saved-sp"
            and live_regs.get(1) != thread_sp
        ):
            return "scheduler-restore-window-inconsistent"
        seed, reason = _decode_saved_thread(
            selection,
            top=thread_sp,
            read=read,
            low=low,
            high=high,
            endian=endian,
            entry=thread_entry,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage_name, "new", stage_detail)

    if flag_value != 0 or live_exc != 14:
        return "scheduler-transition-unproven"
    context, sw_meta, ctx_reason = _select_effective_context(
        profile, selection, top=thread_sp, read=read, low=low, high=high, endian=endian
    )
    if context is None:
        return ctx_reason
    expected_hw_sp = thread_sp + _sw_size(context)
    if stage.name == "pendsv-restored-pending-psp":
        if live_regs.get(1) != expected_hw_sp:
            return "scheduler-restore-window-inconsistent"
    else:
        if live_psp != expected_hw_sp:
            return "scheduler-psp-inconsistent"
    if not _saved_core_matches(
        read=read,
        top=thread_sp,
        context=context,
        registers=live_regs,
        low=low,
        high=high,
        endian=endian,
    ):
        return "scheduler-restore-window-inconsistent"
    expected_psplim = (
        read_slot(read, thread_sp + 8, 4, endian, stack_low=low, stack_high=high)
        if profile.isa == "armv8-m.mainline"
        else None
    )
    reg_reason = _check_restored_state(
        profile,
        context,
        sw_meta,
        stage,
        live_regs,
        live_psplim,
        live_control,
        expected_psplim=expected_psplim,
        stack_low=low,
        expected_sp=thread_sp + context.sp_offset,
    )
    if reg_reason is not None:
        return reg_reason
    seed, reason = _decode_hw_frame(
        hw_sp=expected_hw_sp,
        core_regs=live_regs,
        fpu_extended=context.fpu,
        read=read,
        low=low,
        high=high,
        endian=endian,
        stkalign=stkalign,
        entry=thread_entry,
        fill_word=context.fill_word,
        source="unstarted-candidate",
    )
    if seed is None:
        return reason
    return SchedulerSeed(seed, stage_name, "new", stage_detail)


def build_stage_seed(
    profile: SchedulerProfile,
    selection: StackContextSelection,
    *,
    flag_value: int | None,
    from_ptr: int | None,
    to_ptr: int | None,
    sp_offset: int | None,
    thread_addr: int,
    thread_sp: int,
    thread_entry: int | None,
    current_addr: int | None,
    low: int,
    high: int,
    endian: Endian,
    stkalign: bool,
    read: Reader,
    live_pc: int,
    live_sp: int,
    live_psp: int | None,
    live_exc: int,
    live_regs: Mapping[int, int],
    live_psplim: int | None = None,
    live_control: int | None = None,
) -> SchedulerSeed | None | str:
    """Resolve one scheduler-transition seed or return an explicit failure reason.

    Returns ``None`` only when both the pending-switch flag is zero and ``live_pc``
    lies outside verified scheduler handlers.
    """
    if flag_value is None or from_ptr is None or to_ptr is None or sp_offset is None:
        return "scheduler-state-unavailable"
    stage = classify_stage(profile, live_pc)
    if flag_value == 0 and stage is None:
        return None
    if profile.trustzone_symbol is not None:
        tz_val = read_slot(read, profile.trustzone_symbol, 4, endian)
        if tz_val is None:
            return "scheduler-state-unavailable"
        if tz_val != 0:
            return "scheduler-trustzone-unsupported"
    if stage is not None and stage.name == "ambiguous":
        return f"scheduler-stage-ambiguous:{stage.function}"
    if to_ptr == 0:
        return "scheduler-transition-unproven"
    if current_addr is None or current_addr + sp_offset != to_ptr:
        return "scheduler-owner-inconsistent"

    target_sp_addr = thread_addr + sp_offset
    from_match = from_ptr != 0 and target_sp_addr == from_ptr
    to_match = target_sp_addr == to_ptr
    if from_match and to_match:
        return "scheduler-owner-inconsistent"

    if not from_match and not to_match:
        seed, reason = _decode_saved_thread(
            selection,
            top=thread_sp,
            read=read,
            low=low,
            high=high,
            endian=endian,
            entry=thread_entry,
        )
        if seed is None:
            return reason
        stage_name, stage_detail = _stage_detail(stage, "pending-before-pendsv")
        return SchedulerSeed(seed, stage_name, "other", stage_detail)

    if from_match:
        return _resolve_old_thread(
            profile,
            selection,
            stage,
            flag_value=flag_value,
            from_ptr=from_ptr,
            thread_sp=thread_sp,
            thread_entry=thread_entry,
            low=low,
            high=high,
            endian=endian,
            stkalign=stkalign,
            read=read,
            live_pc=live_pc,
            live_sp=live_sp,
            live_psp=live_psp,
            live_exc=live_exc,
            live_regs=live_regs,
        )

    return _resolve_new_thread(
        profile,
        selection,
        stage,
        flag_value=flag_value,
        thread_sp=thread_sp,
        thread_entry=thread_entry,
        low=low,
        high=high,
        endian=endian,
        stkalign=stkalign,
        read=read,
        live_psp=live_psp,
        live_exc=live_exc,
        live_regs=live_regs,
        live_psplim=live_psplim,
        live_control=live_control,
    )


def _riscv_saved_seed(
    context: SavedStackContext,
    *,
    top: int,
    read: Reader,
    low: int,
    high: int,
    endian: Endian,
    entry: int | None,
    stage: str,
    owner: str,
    evidence: str,
) -> SchedulerSeed | str:
    seed, reason = decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        entry=entry or None,
        stack_low=low,
        stack_high=high,
    )
    if seed is None:
        return reason
    return SchedulerSeed(seed, stage, owner, evidence)


def _riscv_decode_saved(
    context: SavedStackContext,
    *,
    top: int,
    read: Reader,
    low: int,
    high: int,
    endian: Endian,
    entry: int | None,
) -> tuple[SeedFrame | None, str]:
    return decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        entry=entry or None,
        stack_low=low,
        stack_high=high,
    )


def _riscv_read_slots(
    slots: tuple[tuple[int, int, int], ...],
    *,
    frame_base: int,
    stage_pc: int | None,
    word: int,
    mask: int,
    low: int,
    high: int,
    endian: Endian,
    read: Reader,
    live_regs: Mapping[int, int],
) -> dict[int, int] | str:
    regs: dict[int, int] = {}
    for reg, offset, done_pc in slots:
        if stage_pc is None or stage_pc >= done_pc:
            val = read_slot(
                read,
                frame_base + offset,
                word,
                endian,
                stack_low=low,
                stack_high=high,
            )
            if val is None:
                return "saved-context-unreadable"
            regs[reg] = val & mask
        elif reg in live_regs:
            regs[reg] = live_regs[reg] & mask
        elif reg in _REQUIRED_RISCV_LIVE_REGS:
            return "live-registers-unavailable"
    return regs


def _riscv_restore_from_frame(
    profile: RiscvSchedulerProfile,
    context: SavedStackContext,
    *,
    frame_base: int,
    read: Reader,
    low: int,
    high: int,
    endian: Endian,
    entry: int | None,
    epc_override: int | None = None,
) -> tuple[SeedFrame | None, str]:
    mask = u_xlen_mask(profile.xlen)
    if not _in_bounds(low, high, frame_base, frame_base + profile.frame_bytes):
        return None, "scheduler-frame-outside-task-stack"
    decoded, reason = decode_profile_slots(
        context,
        frame_base,
        read,
        endian=endian,
        stack_low=low,
        stack_high=high,
    )
    if decoded is None:
        return None, reason
    pc = epc_override if epc_override is not None else decoded.code_pc
    if pc <= 0 or (pc & 1):
        return None, "exception-context-unproven"
    registers = decoded.registers
    unstarted = bool(
        entry
        and (pc & ~1) == (entry & ~1)
        and all(
            registers.get(reg, 0) == context.fill_word
            for reg in _RISCV_CALLEE_FILL_REGS
        )
    )
    return (
        SeedFrame(
            pc=pc & ~1,
            sp=(frame_base + profile.frame_bytes) & mask,
            regs=registers,
            source="unstarted" if unstarted else "saved",
            root_pc=(pc & ~1) if unstarted else ((entry & ~1) if entry else None),
        ),
        "ok",
    )


def _riscv_check_nest(
    profile: RiscvSchedulerProfile,
    stage_name: str,
    nest_count: int | None,
) -> str | None:
    if profile.epc_mode != "deferred-mepc":
        return None
    if nest_count is None:
        return "exception-context-unproven"
    limit = (
        0
        if stage_name in ("trap-entry", "trap-prologue-saving")
        or stage_name.startswith(("trap-epilogue-", "restore-"))
        else 1
    )
    if nest_count > limit:
        return "nested-trap-epc-unproven"
    return None


def _riscv_resolve_context_switch(
    profile: RiscvSchedulerProfile,
    context: SavedStackContext,
    stage: RiscvSchedulerStage,
    *,
    flag_value: int,
    sp_offset: int,
    thread_addr: int,
    thread_sp: int,
    thread_entry: int | None,
    current_addr: int | None,
    low: int,
    high: int,
    endian: Endian,
    read: Reader,
    snapshot: RiscvSnapshot,
    live_a0: int | None,
    live_a1: int | None,
    evidence: str,
) -> SchedulerSeed | str:
    if flag_value != 0 or current_addr is None:
        return (
            "scheduler-transition-unproven"
            if flag_value != 0
            else "scheduler-owner-inconsistent"
        )
    mask = u_xlen_mask(profile.xlen)
    if stage.name == "context-switch-sp-loaded":
        if thread_addr == current_addr:
            if snapshot.sp != thread_sp:
                return "scheduler-saved-sp-inconsistent"
            seed, reason = _riscv_decode_saved(
                context,
                top=thread_sp,
                read=read,
                low=low,
                high=high,
                endian=endian,
                entry=thread_entry,
            )
            if seed is None:
                return reason
            return SchedulerSeed(seed, stage.name, "new", evidence)
        seed, reason = _riscv_decode_saved(
            context,
            top=thread_sp,
            read=read,
            low=low,
            high=high,
            endian=endian,
            entry=thread_entry,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, "other", evidence)

    a0_val = snapshot.registers.get(10) if live_a0 is None else live_a0
    a1_val = snapshot.registers.get(11) if live_a1 is None else live_a1
    if a1_val is not None and a1_val != current_addr + sp_offset:
        return "scheduler-owner-inconsistent"
    if (
        stage.pc <= profile.switch_a0_clobber_pc
        and a0_val is not None
        and a1_val is not None
        and a0_val == a1_val
    ):
        return "scheduler-owner-inconsistent"

    target_sp_ptr = thread_addr + sp_offset
    need_bytes = 0 if stage.name == "context-switch-entry" else profile.frame_bytes
    stack_ok = _in_bounds(low, high, snapshot.sp, snapshot.sp + need_bytes)
    is_from = False
    if stage.pc <= profile.switch_store_from_pc:
        if a0_val is not None:
            is_from = target_sp_ptr == a0_val
        else:
            is_from = stack_ok and thread_addr != current_addr
    else:
        is_from = stack_ok and thread_sp == snapshot.sp and thread_addr != current_addr
        if (
            is_from
            and stage.pc <= profile.switch_a0_clobber_pc
            and a0_val is not None
            and target_sp_ptr != a0_val
        ):
            return "scheduler-owner-inconsistent"

    if is_from:
        if not stack_ok:
            return "scheduler-frame-outside-task-stack"
        if stage.pc > profile.switch_store_from_pc and thread_sp != snapshot.sp:
            return "scheduler-saved-sp-inconsistent"
        if stage.name == "context-switch-entry":
            if not _REQUIRED_RISCV_LIVE_REGS.issubset(snapshot.registers):
                return "live-registers-unavailable"
            ret_pc = snapshot.registers[1] & ~1
            if ret_pc <= 0:
                return "scheduler-save-window-inconsistent"
            regs = {reg: val & mask for reg, val in snapshot.registers.items()}
            regs[2] = snapshot.sp & mask
            seed = SeedFrame(
                pc=ret_pc,
                sp=regs[2],
                regs=regs,
                source="current",
                root_pc=(thread_entry & ~1) if thread_entry else None,
            )
            return SchedulerSeed(seed, stage.name, "old", evidence)

        read_res = _riscv_read_slots(
            profile.switch_saved_slots,
            frame_base=snapshot.sp,
            stage_pc=stage.pc,
            word=profile.word_size,
            mask=mask,
            low=low,
            high=high,
            endian=endian,
            read=read,
            live_regs=snapshot.registers,
        )
        if isinstance(read_res, str):
            return read_res
        ret_pc = read_res.get(1, 0) & ~1
        if ret_pc <= 0:
            return "scheduler-save-window-inconsistent"
        caller_sp = (snapshot.sp + profile.frame_bytes) & mask
        read_res[2] = caller_sp
        seed = SeedFrame(
            pc=ret_pc,
            sp=caller_sp,
            regs=read_res,
            source="current",
            root_pc=(thread_entry & ~1) if thread_entry else None,
        )
        return SchedulerSeed(seed, stage.name, "old", evidence)

    seed, reason = _riscv_decode_saved(
        context,
        top=thread_sp,
        read=read,
        low=low,
        high=high,
        endian=endian,
        entry=thread_entry,
    )
    if seed is None:
        return reason
    owner = "new" if thread_addr == current_addr else "other"
    return SchedulerSeed(seed, stage.name, owner, evidence)


def build_riscv_stage_seed(
    profile: RiscvSchedulerProfile,
    selection: StackContextSelection,
    stage: RiscvSchedulerStage | None,
    *,
    flag_value: int | None,
    from_ptr: int | None,
    to_ptr: int | None,
    sp_offset: int | None,
    thread_addr: int,
    thread_sp: int,
    thread_entry: int | None,
    current_addr: int | None,
    low: int,
    high: int,
    endian: Endian,
    read: Reader,
    snapshot: RiscvSnapshot,
    nest_count: int | None = None,
    irq_bounds: tuple[int, int] | None = None,
    live_a0: int | None = None,
    live_a1: int | None = None,
) -> SchedulerSeed | None | str:
    """Resolve one RISC-V scheduler/trap transition seed or fail closed."""
    context = selection.context
    if (
        context is None
        or not context.verified
        or snapshot.xlen != profile.xlen
        or context.pointer_size != profile.word_size
    ):
        return "scheduler-profile-unsupported-image"
    if flag_value is None or from_ptr is None or to_ptr is None or sp_offset is None:
        return "scheduler-state-unavailable"
    if flag_value == 0 and stage is None:
        return None
    if isinstance(stage, str):
        return stage

    mask = u_xlen_mask(profile.xlen)
    word = profile.word_size
    stage_name = stage.name if stage is not None else "pending-before-trap-epilogue"
    evidence = (
        f"stage={stage_name}"
        f" seed={stage.seed if stage is not None else 'exception-isr'}"
        f" pc={snapshot.pc:#x} profile={profile.profile_id}@{profile.fingerprint}"
        f" sp={snapshot.sp:#x} mcause={snapshot.mcause:#x} mepc={snapshot.mepc:#x}"
    )

    def saved(owner: str, stage_key: str = stage_name) -> SchedulerSeed | str:
        return _riscv_saved_seed(
            context,
            top=thread_sp,
            read=read,
            low=low,
            high=high,
            endian=endian,
            entry=thread_entry,
            stage=stage_key,
            owner=owner,
            evidence=evidence,
        )

    if stage is None or stage.name in ("trap-entry", "trap-dispatch"):
        if stage is not None and stage.name == "trap-dispatch":
            if irq_bounds is None:
                return "exception-stack-bounds-unavailable"
            if not irq_bounds[0] <= snapshot.sp <= irq_bounds[1]:
                return "exception-stack-outside-bounds"
        nest_err = _riscv_check_nest(profile, stage_name, nest_count)
        if nest_err is not None:
            return nest_err
        guard_err = riscv_exception_guard(
            snapshot.mcause,
            snapshot.pc,
            xlen=snapshot.xlen,
            allow_sync=True,
            allow_ecall=False,
        )
        if guard_err is not None:
            return guard_err
        fault = riscv_fault_precision(
            snapshot.mcause, snapshot.xlen, mepc=snapshot.mepc, mtval=snapshot.mtval
        )
        target_ptr = thread_addr + sp_offset
        if flag_value != 0:
            if (
                from_ptr == 0
                or to_ptr == 0
                or from_ptr == to_ptr
                or current_addr is None
                or current_addr + sp_offset != to_ptr
            ):
                return "scheduler-owner-inconsistent"
            if target_ptr == from_ptr:
                seed = SeedFrame(
                    pc=snapshot.pc,
                    sp=snapshot.sp,
                    regs=dict(snapshot.registers),
                    source="current",
                    root_pc=(thread_entry & ~1) if thread_entry else None,
                )
                return SchedulerSeed(seed, stage_name, "old", evidence, fault=fault)
            return saved("new" if target_ptr == to_ptr else "other")
        if current_addr is None:
            return "scheduler-owner-inconsistent"
        if thread_addr != current_addr:
            return saved("other")
        if not _REQUIRED_RISCV_LIVE_REGS.issubset(snapshot.registers):
            return "live-registers-unavailable"
        if snapshot.mepc <= 0 or (snapshot.mepc & 1):
            return "exception-context-unproven"
        regs = {reg: val & mask for reg, val in snapshot.registers.items()}
        regs[2] = snapshot.sp & mask
        return SchedulerSeed(
            SeedFrame(
                pc=snapshot.mepc & ~1,
                sp=regs[2],
                regs=regs,
                source="current",
                root_pc=(thread_entry & ~1) if thread_entry else None,
            ),
            stage_name,
            "old",
            evidence,
            fault=fault,
        )

    if stage.name in ("switch-to-entry", "switch-to-sp-loaded"):
        if flag_value != 0:
            return "scheduler-transition-unproven"
        if current_addr is None:
            return "scheduler-owner-inconsistent"
        if thread_addr != current_addr:
            return saved("other", stage.name)
        a0_val = snapshot.registers.get(10) if live_a0 is None else live_a0
        if (
            stage.name == "switch-to-entry"
            and a0_val is not None
            and a0_val != thread_addr + sp_offset
        ):
            return "scheduler-owner-inconsistent"
        if stage.name == "switch-to-sp-loaded" and snapshot.sp != thread_sp:
            return "scheduler-saved-sp-inconsistent"
        seed, reason = _riscv_decode_saved(
            context,
            top=thread_sp,
            read=read,
            low=low,
            high=high,
            endian=endian,
            entry=thread_entry,
        )
        if seed is None:
            return reason
        if seed.source != "unstarted":
            return "scheduler-first-start-context-unproven"
        return SchedulerSeed(seed, stage.name, "new", evidence)

    if stage.name in (
        "context-switch-entry",
        "context-switch-saving",
        "context-switch-sp-loaded",
    ):
        return _riscv_resolve_context_switch(
            profile,
            context,
            stage,
            flag_value=flag_value,
            sp_offset=sp_offset,
            thread_addr=thread_addr,
            thread_sp=thread_sp,
            thread_entry=thread_entry,
            current_addr=current_addr,
            low=low,
            high=high,
            endian=endian,
            read=read,
            snapshot=snapshot,
            live_a0=live_a0,
            live_a1=live_a1,
            evidence=evidence,
        )

    if stage.name == "trap-prologue-saving":
        if flag_value != 0:
            return "scheduler-transition-unproven"
        if current_addr is None:
            return "scheduler-owner-inconsistent"
        nest_err = _riscv_check_nest(profile, stage.name, nest_count)
        if nest_err is not None:
            return nest_err
        if thread_addr != current_addr:
            return saved("other", stage.name)
        if stage.pc <= profile.trap_mv_s0_pc:
            frame_base = snapshot.sp & mask
        else:
            s0 = snapshot.registers.get(8)
            if s0 is None:
                return "live-registers-unavailable"
            frame_base = s0 & mask
        if not _in_bounds(low, high, frame_base, frame_base + profile.frame_bytes):
            return "scheduler-frame-outside-task-stack"
        read_res = _riscv_read_slots(
            profile.trap_saved_slots,
            frame_base=frame_base,
            stage_pc=stage.pc,
            word=word,
            mask=mask,
            low=low,
            high=high,
            endian=endian,
            read=read,
            live_regs=snapshot.registers,
        )
        if isinstance(read_res, str):
            return read_res
        if snapshot.mepc <= 0 or (snapshot.mepc & 1):
            return "exception-context-unproven"
        guard_err = riscv_exception_guard(
            snapshot.mcause,
            snapshot.pc,
            xlen=snapshot.xlen,
            allow_sync=True,
            allow_ecall=False,
        )
        if guard_err is not None:
            return guard_err
        fault = riscv_fault_precision(
            snapshot.mcause, snapshot.xlen, mepc=snapshot.mepc, mtval=snapshot.mtval
        )
        caller_sp = (frame_base + profile.frame_bytes) & mask
        read_res[2] = caller_sp
        return SchedulerSeed(
            SeedFrame(
                pc=snapshot.mepc & ~1,
                sp=caller_sp,
                regs=read_res,
                source="current",
                root_pc=(thread_entry & ~1) if thread_entry else None,
            ),
            stage.name,
            "old",
            evidence,
            fault=fault,
        )

    if stage.name in (
        "trap-epilogue-before-switch-store",
        "trap-epilogue-before-switch-load",
    ):
        nest_err = _riscv_check_nest(profile, stage.name, nest_count)
        if nest_err is not None:
            return nest_err
        switch_active = stage.pc > profile.trap_clear_flag_pc or flag_value != 0
        if stage.pc <= profile.trap_clear_flag_pc and flag_value == 0:
            switch_active = False
        if stage.pc > profile.trap_clear_flag_pc and flag_value != 0:
            return "scheduler-transition-unproven"

        if stage.pc == profile.trap_mv_sp_pc:
            s0 = snapshot.registers.get(8)
            if s0 is None:
                return "live-registers-unavailable"
            frame_base = s0 & mask
        else:
            frame_base = snapshot.sp & mask

        if not switch_active:
            if current_addr is None:
                return "scheduler-owner-inconsistent"
            if thread_addr != current_addr:
                return saved("other", stage.name)
            epc_override = (
                None if profile.epc_mode == "stacked-epc" else (snapshot.mepc & ~1)
            )
            seed, reason = _riscv_restore_from_frame(
                profile,
                context,
                frame_base=frame_base,
                read=read,
                low=low,
                high=high,
                endian=endian,
                entry=thread_entry,
                epc_override=epc_override,
            )
            if seed is None:
                return reason
            return SchedulerSeed(seed, stage.name, "old", evidence)

        if (
            from_ptr == 0
            or to_ptr == 0
            or from_ptr == to_ptr
            or current_addr is None
            or current_addr + sp_offset != to_ptr
        ):
            return "scheduler-owner-inconsistent"
        target_ptr = thread_addr + sp_offset
        if target_ptr == from_ptr:
            if (
                stage.name == "trap-epilogue-before-switch-load"
                and thread_sp != frame_base
            ):
                return "scheduler-saved-sp-inconsistent"
            use_stacked = profile.epc_mode == "stacked-epc" or (
                profile.trap_epc_store_pc is not None
                and stage.pc > profile.trap_epc_store_pc
            )
            epc_override = None if use_stacked else (snapshot.mepc & ~1)
            seed, reason = _riscv_restore_from_frame(
                profile,
                context,
                frame_base=frame_base,
                read=read,
                low=low,
                high=high,
                endian=endian,
                entry=thread_entry,
                epc_override=epc_override,
            )
            if seed is None:
                return reason
            return SchedulerSeed(seed, stage.name, "old", evidence)
        return saved("new" if target_ptr == to_ptr else "other", stage.name)

    if stage.name in ("trap-epilogue-after-switch-load", "restore-window"):
        if stage.function == "spurious_interrupt":
            nest_err = _riscv_check_nest(profile, stage.name, nest_count)
            if nest_err is not None:
                return nest_err
        if current_addr is None:
            return "scheduler-owner-inconsistent"
        if thread_addr != current_addr:
            return saved("other", stage.name)
        if stage.name == "trap-epilogue-after-switch-load" and snapshot.sp != thread_sp:
            return "scheduler-saved-sp-inconsistent"
        epc_override = (
            snapshot.mepc & ~1
            if (
                stage.function == "spurious_interrupt"
                and profile.epc_mode == "deferred-mepc"
            )
            else None
        )
        seed, reason = _riscv_restore_from_frame(
            profile,
            context,
            frame_base=snapshot.sp & mask,
            read=read,
            low=low,
            high=high,
            endian=endian,
            entry=thread_entry,
            epc_override=epc_override,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, "new", evidence)

    if stage.name == "restore-sp-popped":
        if stage.function == "spurious_interrupt":
            nest_err = _riscv_check_nest(profile, stage.name, nest_count)
            if nest_err is not None:
                return nest_err
        if current_addr is None:
            return "scheduler-owner-inconsistent"
        if thread_addr != current_addr:
            return saved("other", stage.name)
        if (snapshot.mstatus & MSTATUS_MPP_MASK) != MSTATUS_MPP_MACHINE:
            return "unsupported-exception-model"
        if not _in_bounds(low, high, snapshot.sp, snapshot.sp):
            return "scheduler-psp-inconsistent"
        if snapshot.mepc <= 0 or (snapshot.mepc & 1):
            return "exception-context-unproven"
        if not _REQUIRED_RISCV_LIVE_REGS.issubset(snapshot.registers):
            return "live-registers-unavailable"
        resume_pc = snapshot.mepc & ~1
        regs = {reg: val & mask for reg, val in snapshot.registers.items()}
        regs[2] = snapshot.sp & mask
        unstarted = bool(
            thread_entry
            and resume_pc == (thread_entry & ~1)
            and all(
                regs.get(reg) == context.fill_word
                for reg in _RISCV_CALLEE_FILL_REGS
                if reg in regs
            )
        )
        seed = SeedFrame(
            pc=resume_pc,
            sp=regs[2],
            regs=regs,
            source="unstarted" if unstarted else "current",
            root_pc=resume_pc
            if unstarted
            else ((thread_entry & ~1) if thread_entry else None),
        )
        return SchedulerSeed(seed, stage.name, "new", evidence)

    return "scheduler-transition-unproven"
