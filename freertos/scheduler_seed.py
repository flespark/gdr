"""Owner-consistent scheduler seeds from classified transition stages (pure).

Proves one task seed from a verified scheduler stop: the compiled stage, the
saved core and hardware frame, and the software-block geometry of the port's
basic or FPU layout.  It never reads target memory itself: the adapter supplies
a reader and the live registers, and every returned seed is bounded by the
task's recorded stack span.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from core.stack_frame_parser import SavedStackContext
from core.unwind import SeedFrame, read_slot
from core.unwind_arm import (
    BASIC_EXC_RETURNS,
    EXC_RETURN_EXTENDED_THREAD_PROCESS,
    EXTENDED_EXC_RETURNS,
    ExceptionReturn,
    decode_armv7m_exception_return,
)
from core.unwind_meta import Reader

from .backtrace import CM4F_EXC_RETURN_SLOT, decode_saved_context
from .scheduler_switch import SchedulerProfile, SchedulerStage

EXC_RETURN_THREAD_PROCESS = 0xFFFFFFFD
XPSR_T_BIT = 1 << 24
XPSR_ALIGN_BIT = 1 << 9

_CORE_REGS = tuple(range(4, 12))


@dataclass(frozen=True)
class SchedulerSeed:
    """An owner-consistent seed plus the evidence that selected it."""

    seed: SeedFrame
    stage: str
    owner: str
    evidence: str


def _in_bounds(low: int, high: int, address: int, size: int) -> bool:
    return low <= address and address + size <= high


_HARDWARE_XPSR_OFFSET = 0x1C
_HARDWARE_FRAME_SIZE = 0x20

_EXTENDED_HARDWARE_FRAME_SIZE = 0x68


def _frame_restored_sp(
    *,
    top: int,
    context: SavedStackContext,
    frame_xpsr: int,
    stkalign: bool,
    hardware_frame: bool,
    low: int,
    high: int,
) -> tuple[int | None, str]:
    """Validate one frame's geometry and return the caller SP it restores.

    A hardware-stacked frame is placed by exception entry: with CCR.STKALIGN=1
    its base is 8-byte aligned and the stacked xPSR.ALIGN bit names the padding
    word that follows the basic frame; with CCR.STKALIGN=0 the base is only
    4-byte aligned and xPSR.ALIGN must be clear.  A first-start frame is built
    in software by ``pxPortInitialiseStack`` and is not placed by exception
    entry, so CCR.STKALIGN does not describe it; its builder always clears
    xPSR.ALIGN and there is no padding word.
    """
    if context.status_offset is None:
        return None, "scheduler-frame-layout-unproven"
    hw_size = _EXTENDED_HARDWARE_FRAME_SIZE if context.fpu else _HARDWARE_FRAME_SIZE
    if context.sp_offset != context.status_offset + (hw_size - _HARDWARE_XPSR_OFFSET):
        return None, "scheduler-frame-layout-unproven"
    frame_base = top + context.status_offset - _HARDWARE_XPSR_OFFSET
    if not _in_bounds(low, high, frame_base, hw_size):
        return None, "scheduler-frame-outside-task-stack"
    alignment = bool(frame_xpsr & XPSR_ALIGN_BIT)
    restored = top + context.sp_offset
    if not hardware_frame:
        if alignment or frame_base & 3:
            return None, "exception-frame-alignment-invalid"
        if not low <= restored <= high:
            return None, "scheduler-frame-outside-task-stack"
        return restored, "ok"
    if stkalign is None:
        return None, "exception-alignment-unavailable"
    if context.fpu or stkalign:
        if frame_base & 7:
            return None, "exception-frame-alignment-invalid"
    elif frame_base & 3:
        return None, "exception-frame-alignment-invalid"
    if alignment and not (context.fpu or stkalign):
        return None, "exception-frame-alignment-invalid"
    sp = restored + (4 if alignment else 0)
    if context.fpu or stkalign:
        if sp & 7 != (4 if alignment else 0):
            return None, "exception-frame-alignment-invalid"
    elif sp & 3:
        return None, "exception-frame-alignment-invalid"
    if not low <= sp <= high:
        return None, "scheduler-frame-outside-task-stack"
    return sp, "ok"


def _decode_frame(
    *,
    top: int,
    core: Mapping[int, int],
    context: SavedStackContext,
    read: Reader,
    low: int,
    high: int,
    source: str,
    stkalign: bool,
    hardware_frame: bool,
) -> tuple[SeedFrame | None, int | None, str]:
    """Decode a saved core block plus the hardware frame above it."""
    span = context.context_span
    if span is None or not _in_bounds(low, high, top, span[1]):
        return None, None, "scheduler-frame-outside-task-stack"
    registers: dict[int, int] = {}
    for register, offset in context.registers:
        if register in core:
            registers[register] = core[register]
            continue
        value = read_slot(
            read,
            top + offset,
            context.pointer_size,
            "little",
            stack_low=low,
            stack_high=high,
        )
        if value is None:
            return None, None, "scheduler-context-unreadable"
        registers[register] = value
    if context.pc_offset is None or context.lr_offset is None:
        return None, None, "scheduler-context-unreadable"
    pc = read_slot(
        read,
        top + context.pc_offset,
        context.pointer_size,
        "little",
        stack_low=low,
        stack_high=high,
    )
    lr = read_slot(
        read,
        top + context.lr_offset,
        context.pointer_size,
        "little",
        stack_low=low,
        stack_high=high,
    )
    if pc is None or lr is None or context.status_offset is None:
        return None, None, "scheduler-context-unreadable"
    xpsr = read_slot(
        read,
        top + context.status_offset,
        4,
        "little",
        stack_low=low,
        stack_high=high,
    )
    if xpsr is None:
        return None, None, "scheduler-context-unreadable"
    if not xpsr & XPSR_T_BIT:
        return None, xpsr, "scheduler-frame-not-thumb"
    if xpsr & 0x1FF:
        return None, xpsr, "scheduler-frame-ipsr-invalid"
    sp, reason = _frame_restored_sp(
        top=top,
        context=context,
        frame_xpsr=xpsr,
        stkalign=stkalign,
        hardware_frame=hardware_frame,
        low=low,
        high=high,
    )
    if sp is None:
        return None, xpsr, reason
    if (pc & ~1) == 0 or (hardware_frame and (pc & 1)):
        return None, xpsr, "scheduler-frame-pc-invalid"
    registers.update({13: sp, 14: lr, 15: pc & ~1})
    return (
        SeedFrame(pc=pc & ~1, sp=sp, regs=registers, source=source),
        xpsr,
        "ok",
    )


def _saved_core_matches(
    *,
    read: Reader,
    top: int,
    context: SavedStackContext,
    registers: Mapping[int, int],
    low: int,
    high: int,
    core_regs: tuple[int, ...] = _CORE_REGS,
) -> bool:
    offsets = dict(context.registers)
    for register in core_regs:
        saved = read_slot(
            read,
            top + offsets[register],
            context.pointer_size,
            "little",
            stack_low=low,
            stack_high=high,
        )
        if saved is None or saved != registers.get(register):
            return False
    return True


def _saved_seed(
    *,
    context: SavedStackContext,
    top: int,
    read: Reader,
    low: int,
    high: int,
    exit_pc: int | None,
    first_start: bool,
    is_entry: Callable[[int], bool] | None,
    stkalign: bool,
    refuse_initial: bool = False,
) -> tuple[SeedFrame | None, str]:
    if context.status_offset is None:
        return None, "scheduler-frame-layout-unproven"
    frame_xpsr = read_slot(
        read,
        top + context.status_offset,
        4,
        "little",
        stack_low=low,
        stack_high=high,
    )
    if frame_xpsr is None:
        return None, "scheduler-context-unreadable"
    expected_sp, reason = _frame_restored_sp(
        top=top,
        context=context,
        frame_xpsr=frame_xpsr,
        stkalign=stkalign,
        hardware_frame=not first_start,
        low=low,
        high=high,
    )
    if expected_sp is None:
        return None, reason
    entry_pc = None
    if first_start:
        pc = read_slot(read, top + context.pc_offset, context.pointer_size, "little")
        if pc is None or is_entry is None or not is_entry(pc & ~1):
            return None, "scheduler-first-start-entry-unproven"
        entry_pc = pc & ~1
    seed, reason = decode_saved_context(
        context,
        top,
        read,
        endian="little",
        stack_low=low,
        stack_high=high,
        exit_pc=exit_pc,
        entry_pc=entry_pc,
    )
    if seed is None:
        return None, reason
    if seed.sp != expected_sp:
        return None, "scheduler-frame-layout-unproven"
    if first_start:
        if seed.source != "unstarted":
            return None, "scheduler-first-start-context-unproven"
        return (
            SeedFrame(
                pc=seed.pc,
                sp=seed.sp,
                regs=seed.regs,
                source="unstarted",
                root_pc=seed.pc,
                root_reason="task-entry",
            ),
            "ok",
        )
    if refuse_initial and _initial_pattern(
        seed,
        frame_xpsr=frame_xpsr,
        exit_pc=exit_pc,
        fill_word=context.fill_word,
        initial_registers=context.initial_registers,
    ):
        return None, "scheduler-unstarted-selection-unproven"
    return seed, "ok"


def _initial_pattern(
    seed: SeedFrame,
    *,
    frame_xpsr: int | None,
    exit_pc: int | None,
    fill_word: int,
    initial_registers: tuple[int, ...] | None = None,
) -> bool:
    if frame_xpsr != 0x01000000:
        return False
    if exit_pc is None or (seed.regs.get(14, 0) & ~1) != (exit_pc & ~1):
        return False
    core = tuple(seed.regs.get(register, 0) for register in _CORE_REGS)
    if initial_registers is not None:
        return core == initial_registers
    return all(value == fill_word for value in core)


def _looks_unstarted(
    seed: SeedFrame,
    *,
    frame_xpsr: int | None,
    exit_pc: int | None,
    fill_word: int,
    initial_registers: tuple[int, ...] | None,
    is_entry: Callable[[int], bool] | None,
) -> bool:
    if not _initial_pattern(
        seed,
        frame_xpsr=frame_xpsr,
        exit_pc=exit_pc,
        fill_word=fill_word,
        initial_registers=initial_registers,
    ):
        return False
    return is_entry is not None and is_entry(seed.pc)


def _initial_seed_ok(
    *,
    seed: SeedFrame,
    frame_xpsr: int | None,
    exit_pc: int | None,
    fill_word: int,
    initial_registers: tuple[int, ...] | None,
    is_entry: Callable[[int], bool] | None,
) -> bool:
    return _looks_unstarted(
        seed,
        frame_xpsr=frame_xpsr,
        exit_pc=exit_pc,
        fill_word=fill_word,
        initial_registers=initial_registers,
        is_entry=is_entry,
    )


def _require_exception_return(value: int, mode: str, stack: str) -> str | None:
    """Prove one basic or FPU-extended EXC_RETURN against the expected mode/stack."""
    decoded = decode_armv7m_exception_return(value)
    if isinstance(decoded, str):
        return decoded
    if decoded.mode != mode:
        return "scheduler-handler-mode-unproven"
    if decoded.stack != stack:
        return "scheduler-stack-selection-unproven"
    return None


def _live_extended(lr_value: int) -> bool | None:
    """Extended-ness of a live EXC_RETURN, or ``None`` for ordinary code LR."""
    decoded = decode_armv7m_exception_return(lr_value)
    return decoded.extended if isinstance(decoded, ExceptionReturn) else None


def _saved_slot_extended(
    *, top: int, read: Reader, low: int, high: int, fpu: bool
) -> bool | str:
    """Extended-ness of a saved task from its EXC_RETURN slot.

    The slot is exact EXC_RETURN only for CM4F-family layouts; basic
    layouts keep task R0 there, so a non-magic word falls through to
    basic and downstream xPSR/PC guards.  An FPU layout requires its
    exact word because its geometry derives from it.
    """
    word = read_slot(
        read,
        top + CM4F_EXC_RETURN_SLOT,
        4,
        "little",
        stack_low=low,
        stack_high=high,
    )
    if word in EXTENDED_EXC_RETURNS:
        return True
    if word in BASIC_EXC_RETURNS:
        return False
    if fpu:
        return "scheduler-fpu-context-unproven"
    return False


def _resolve_pair_profile(
    context: SavedStackContext,
    extended: bool,
    fpu_context: SavedStackContext | None,
) -> SavedStackContext | str:
    """Select the basic or FPU profile for proven extended-ness.

    A basic context carrying an extended frame upgrades to the supplied FPU
    profile; any other extended/basic mismatch refuses without decoding.
    The pair comes from the adapter selection so future ports need no new
    branches here.
    """
    if extended and not context.fpu and fpu_context is not None and fpu_context.fpu:
        return fpu_context
    if extended != context.fpu:
        return "scheduler-fpu-context-unproven"
    return context


def _check_pendsv_exc_return(
    port: str,
    stage_name: str,
    registers: Mapping[int, int],
    *,
    read: Reader,
    task_top: int,
    task_low: int,
    task_high: int,
) -> str | None:
    """Validate EXC_RETURN for the active PendSV stage and port."""
    lr = registers.get(14, 0) & 0xFFFFFFFF
    if stage_name in (
        "pendsv-entry",
        "pendsv-save-pending",
        "pendsv-save-written",
        "pendsv-writeback",
        "pendsv-switch-call",
    ):
        return _require_exception_return(lr, "thread", "process")
    if stage_name == "pendsv-after-switch":
        return None
    if stage_name in ("pendsv-select", "pendsv-saved-sp"):
        if port == "arm-cm3":
            return _require_exception_return(lr, "thread", "process")
        return None
    if stage_name in ("pendsv-restored-pending-psp", "pendsv-psp-written"):
        if port == "arm-cm33":
            return _require_exception_return(
                registers.get(3, 0) & 0xFFFFFFFF, "thread", "process"
            )
        if port == "arm-cm0":
            slot = read_slot(
                read, task_top, 4, "little", stack_low=task_low, stack_high=task_high
            )
            if slot is None:
                return "scheduler-context-unreadable"
            return _require_exception_return(slot, "thread", "process")
        return _require_exception_return(lr, "thread", "process")
    if stage_name == "pendsv-exception-return":
        reg = 3 if port in ("arm-cm0", "arm-cm33") else 14
        return _require_exception_return(
            registers.get(reg, 0) & 0xFFFFFFFF, "thread", "process"
        )
    return None


def _check_svc_exc_return(
    port: str,
    stage_name: str,
    registers: Mapping[int, int],
) -> str | None:
    """Validate EXC_RETURN for the active first-start restore stage and port."""
    lr = registers.get(14, 0) & 0xFFFFFFFF
    if port in ("arm-cm0", "arm-cm33"):
        if stage_name in (
            "svc-restored-pending-psp",
            "svc-psp-written",
            "svc-exception-return",
        ):
            return _require_exception_return(
                registers.get(2, 0) & 0xFFFFFFFF, "thread", "process"
            )
        return None
    if port == "arm-cm4f":
        if stage_name in (
            "svc-restored-pending-psp",
            "svc-psp-written",
            "svc-exception-return",
        ):
            return _require_exception_return(lr, "thread", "process")
        return _require_exception_return(lr, "thread", "main")
    if stage_name == "svc-exception-return":
        return _require_exception_return(lr, "thread", "process")
    return _require_exception_return(lr, "thread", "main")


def _stage_live_core_regs(port: str, seed_kind: str) -> tuple[int, ...]:
    """Return callee-saved core registers already live in CPU regs at ``seed_kind``."""
    if seed_kind in ("first-start-buffer", "first-start-live"):
        if port in ("arm-cm0", "arm-cm33"):
            return ()
        return _CORE_REGS
    if seed_kind in ("restore-buffer", "live-frame-new"):
        if port == "arm-cm0":
            return (8, 9, 10, 11)
        return _CORE_REGS
    return _CORE_REGS


def _live_core_mapping(
    port: str, stage_name: str, seed_kind: str, registers: Mapping[int, int]
) -> dict[int, int]:
    """Build CPU-register core mapping for ``_decode_frame`` on restore stages."""
    if seed_kind in ("restore-buffer", "live-frame-new"):
        if port == "arm-cm0" and stage_name != "pendsv-exception-return":
            return {r: registers[r] for r in (8, 9, 10, 11)}
        return {r: registers[r] for r in _CORE_REGS}
    if seed_kind in ("first-start-buffer", "first-start-live"):
        if port in ("arm-cm0", "arm-cm33"):
            return {}
        return {r: registers[r] for r in _CORE_REGS}
    return {r: registers[r] for r in _CORE_REGS}


def build_stage_seed(
    profile: SchedulerProfile,
    stage: SchedulerStage,
    *,
    read: Reader,
    context: SavedStackContext,
    task_tcb: int,
    task_top: int,
    task_low: int,
    task_high: int,
    psp: int,
    msp: int,
    registers: Mapping[int, int],
    xpsr: int,
    stkalign: bool,
    current_tcb: int,
    exit_pc: int | None,
    is_entry: Callable[[int], bool] | None,
    fpu_context: SavedStackContext | None = None,
) -> SchedulerSeed | str:
    """Build one owner-consistent seed from the stage's proven context.

    Returns the exact refusal reason when a register, buffer, bounds or owner
    invariant does not match the compiled sequence.  The live PSP is never
    used for a stage whose registers still belong to the previous task.
    """
    ipsr = xpsr & 0x1FF
    if stage.pc == profile.start_first_svc:
        return "scheduler-first-start-pending"
    if not xpsr & XPSR_T_BIT:
        return "scheduler-frame-not-thumb"
    if xpsr & XPSR_ALIGN_BIT and not stkalign:
        return "exception-frame-alignment-invalid"
    lr = registers.get(14, 0) & 0xFFFFFFFF
    live_extended = _live_extended(lr)
    port = getattr(profile, "port", "arm-cm3")
    if stage.pc in range(profile.pendsv_start, profile.pendsv_end):
        if ipsr != 14 or registers.get(13) != msp:
            return "scheduler-handler-mode-unproven"
        reason = _check_pendsv_exc_return(
            port,
            stage.name,
            registers,
            read=read,
            task_top=task_top,
            task_low=task_low,
            task_high=task_high,
        )
        if reason is not None:
            return reason
    elif stage.pc in range(profile.switch_start, profile.switch_end):
        if ipsr != 14 or registers.get(13) != msp:
            return "scheduler-handler-mode-unproven"
        if stage.name == "switch-entry" and (lr & ~1) != (profile.switch_return & ~1):
            return "scheduler-switch-call-unproven"
    elif stage.pc in range(profile.svc_start, profile.svc_end):
        if ipsr != 11 or registers.get(13) != msp:
            return "scheduler-handler-mode-unproven"
        reason = _check_svc_exc_return(port, stage.name, registers)
        if reason is not None:
            return reason
    else:
        return "scheduler-stage-ambiguous"
    if current_tcb != task_tcb:
        return "scheduler-current-tcb-mismatch"
    if stage.seed in ("live-frame", "saved-buffer"):
        if live_extended is None:
            return "scheduler-fpu-context-unproven"
        evidenced: bool | str = live_extended
    elif port in ("arm-cm0", "arm-cm33") and not context.fpu and fpu_context is None:
        evidenced = False
    else:
        evidenced = _saved_slot_extended(
            top=task_top, read=read, low=task_low, high=task_high, fpu=context.fpu
        )
        if isinstance(evidenced, str):
            return evidenced
    effective = _resolve_pair_profile(context, evidenced, fpu_context)
    if isinstance(effective, str):
        return effective
    if effective.status_offset is None:
        return "scheduler-frame-layout-unproven"
    sw_size = effective.status_offset - _HARDWARE_XPSR_OFFSET
    hw_size = _EXTENDED_HARDWARE_FRAME_SIZE if effective.fpu else _HARDWARE_FRAME_SIZE

    evidence = (
        f"stage={stage.name} seed={stage.seed} pc={stage.pc:#x}"
        f" profile={profile.profile_id}@{profile.fingerprint}"
        f" owner={stage.owner} current={current_tcb:#x} tcb={task_tcb:#x}"
        f" psp={psp:#x} msp={msp:#x} xpsr={xpsr:#010x}"
    )
    core = {register: registers[register] for register in _CORE_REGS}
    live_core = _live_core_mapping(port, stage.name, stage.seed, registers)
    match_regs = (
        _CORE_REGS
        if stage.name == "pendsv-exception-return"
        else _stage_live_core_regs(port, stage.seed)
    )

    if stage.seed == "live-frame":
        if not _in_bounds(task_low, task_high, psp, hw_size):
            return "scheduler-psp-outside-task-stack"
        seed, _frame_xpsr, reason = _decode_frame(
            top=psp - sw_size,
            core=core,
            context=effective,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "saved-buffer":
        saved_top = registers.get(0)
        if saved_top is None or saved_top != psp - sw_size:
            return "scheduler-saved-sp-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=saved_top,
            context=effective,
            registers=registers,
            low=task_low,
            high=task_high,
        ):
            return "scheduler-saved-buffer-inconsistent"
        seed, _frame_xpsr, reason = _decode_frame(
            top=saved_top,
            core=core,
            context=effective,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "saved-tcb":
        if task_top <= 0:
            return "scheduler-saved-context-unavailable"
        if stage.owner == "old" and task_top != psp - sw_size:
            return "scheduler-saved-sp-inconsistent"
        if stage.name == "pendsv-select" and registers.get(1) != task_tcb:
            return "scheduler-new-tcb-inconsistent"
        if stage.name == "pendsv-saved-sp" and registers.get(0) != task_top:
            return "scheduler-new-tcb-inconsistent"
        seed, reason = _saved_seed(
            context=effective,
            top=task_top,
            read=read,
            low=task_low,
            high=task_high,
            exit_pc=exit_pc,
            first_start=False,
            is_entry=is_entry,
            stkalign=stkalign,
            refuse_initial=stage.owner == "new",
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "restore-buffer":
        if registers.get(0) != task_top + sw_size:
            return "scheduler-restore-window-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=effective,
            registers=registers,
            low=task_low,
            high=task_high,
            core_regs=match_regs,
        ):
            return "scheduler-restore-window-inconsistent"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=live_core,
            context=effective,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        if _initial_pattern(
            seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=effective.fill_word,
            initial_registers=effective.initial_registers,
        ):
            return "scheduler-unstarted-selection-unproven"
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "live-frame-new":
        if psp != task_top + sw_size:
            return "scheduler-psp-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=effective,
            registers=registers,
            low=task_low,
            high=task_high,
            core_regs=match_regs,
        ):
            return "scheduler-restore-window-inconsistent"
        exc_reg = 3 if port in ("arm-cm0", "arm-cm33") else 14
        if stage.name == "pendsv-exception-return" and registers.get(exc_reg) not in (
            EXC_RETURN_THREAD_PROCESS,
            EXC_RETURN_EXTENDED_THREAD_PROCESS,
        ):
            return "scheduler-exception-return-unproven"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=live_core,
            context=effective,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        if _initial_pattern(
            seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=effective.fill_word,
            initial_registers=effective.initial_registers,
        ):
            return "scheduler-unstarted-selection-unproven"
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "first-start-saved":
        seed, reason = _saved_seed(
            context=effective,
            top=task_top,
            read=read,
            low=task_low,
            high=task_high,
            exit_pc=exit_pc,
            first_start=True,
            is_entry=is_entry,
            stkalign=stkalign,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "first-start-buffer":
        if registers.get(0) != task_top + sw_size:
            return "scheduler-restore-window-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=effective,
            registers=registers,
            low=task_low,
            high=task_high,
            core_regs=match_regs,
        ):
            return "scheduler-saved-buffer-inconsistent"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=live_core,
            context=effective,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=False,
        )
        if seed is None:
            return reason
        if not _initial_seed_ok(
            seed=seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=effective.fill_word,
            initial_registers=effective.initial_registers,
            is_entry=is_entry,
        ):
            if is_entry is None or not is_entry(seed.pc):
                return "scheduler-first-start-entry-unproven"
            return "scheduler-first-start-context-unproven"
        return SchedulerSeed(
            SeedFrame(
                pc=seed.pc,
                sp=seed.sp,
                regs=seed.regs,
                source="unstarted",
                root_pc=seed.pc,
                root_reason="task-entry",
            ),
            stage.name,
            stage.owner,
            evidence,
        )

    if stage.seed == "first-start-live":
        if psp != task_top + sw_size:
            return "scheduler-psp-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=effective,
            registers=registers,
            low=task_low,
            high=task_high,
            core_regs=match_regs,
        ):
            return "scheduler-saved-buffer-inconsistent"
        exc_reg = 2 if port in ("arm-cm0", "arm-cm33") else 14
        if (
            stage.name == "svc-exception-return"
            and registers.get(exc_reg) != EXC_RETURN_THREAD_PROCESS
        ):
            return "scheduler-exception-return-unproven"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=live_core,
            context=effective,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=False,
        )
        if seed is None:
            return reason
        if not _initial_seed_ok(
            seed=seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=effective.fill_word,
            initial_registers=effective.initial_registers,
            is_entry=is_entry,
        ):
            if is_entry is None or not is_entry(seed.pc):
                return "scheduler-first-start-entry-unproven"
            return "scheduler-first-start-context-unproven"
        return SchedulerSeed(
            SeedFrame(
                pc=seed.pc,
                sp=seed.sp,
                regs=seed.regs,
                source="unstarted",
                root_pc=seed.pc,
                root_reason="task-entry",
            ),
            stage.name,
            stage.owner,
            evidence,
        )

    return "scheduler-stage-ambiguous"
