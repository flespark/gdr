"""FreeRTOS saved-context decoding for task stack backtraces (pure).

``struct tskTaskControlBlock::pxTopOfStack`` points at the frame the port saved
when the task last left the CPU.  The frame shape is a property of the port
(and, on Armv8-M Mainline, of the MPU/non-MPU save path), not of the kernel
version, so the tables below are keyed by port evidence.  The module only
decodes; the unwind itself lives in :mod:`core.backtrace.unwind`.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.backtrace.unwind import SeedFrame
from core.backtrace.unwind_arm import BASIC_EXC_RETURNS, EXTENDED_EXC_RETURNS
from core.elf import Endian, Isa, Reader
from core.stack_frame_parser import (
    SavedStackContext,
    armv8m_psplim_valid,
    decode_profile_slots,
    read_block,
    read_slot,
)

_ARM_FILL = 0xA5A5A5A5


# Armv7-M / Armv6-M: pxTopOfStack points at r4; the hardware frame is above it.
ARMV7M_INTEGER = SavedStackContext(
    profile_id="freertos/armv7-m/integer",
    validation="live",
    pointer_size=4,
    registers=tuple((register, (register - 4) * 4) for register in range(4, 12))
    + ((0, 32), (1, 36), (2, 40), (3, 44), (12, 48)),
    pc_offset=56,
    lr_offset=52,
    status_offset=60,
    sp_offset=64,
    context_span=(0, 64),
    fill_word=_ARM_FILL,
)

ARMV6M_INTEGER = SavedStackContext(
    profile_id="freertos/armv6-m/integer",
    validation="live",
    pointer_size=4,
    registers=tuple((register, 4 + (register - 4) * 4) for register in range(4, 12))
    + ((0, 36), (1, 40), (2, 44), (3, 48), (12, 52)),
    pc_offset=60,
    lr_offset=56,
    status_offset=64,
    sp_offset=68,
    context_span=(0, 68),
    fill_word=0x04040404,
    initial_registers=(
        0x04040404,
        0x05050505,
        0x06060606,
        0x07070707,
        0x08080808,
        0x09090909,
        0x10101010,
        0x11111111,
    ),
)


# Armv8-M Mainline non-MPU (single-core and SMP): pxTopOfStack = psp-40; the
# saved hardware frame begins at +40 and the original SP is one frame above it.
def _armv8m_mainline_non_mpu(profile_id: str) -> SavedStackContext:
    return SavedStackContext(
        profile_id=profile_id,
        validation="live",
        pointer_size=4,
        registers=tuple((register, 8 + (register - 4) * 4) for register in range(4, 12))
        + ((0, 40), (1, 44), (2, 48), (3, 52), (12, 56)),
        pc_offset=64,
        lr_offset=60,
        status_offset=68,
        sp_offset=72,
        context_span=(0, 72),
        fill_word=_ARM_FILL,
        initial_registers=(
            0x04040404,
            0x05050505,
            0x06060606,
            0x07070707,
            0x08080808,
            0x09090909,
            0x10101010,
            0x11111111,
        ),
    )


ARMV8M_MAINLINE_INTEGER = _armv8m_mainline_non_mpu("freertos/armv8-m.mainline/non-mpu")
ARMV8M_MAINLINE_SMP_INTEGER = _armv8m_mainline_non_mpu(
    "freertos/armv8-m.mainline/non-mpu-smp"
)

# Armv8-M Mainline FPU port (ARM_CM33_NTZ, configENABLE_FPU == 1):
# pxTopOfStack[0]=PSPLIM, [4]=EXC_RETURN, [8..36]=r4-r11, [40..100]=s16-s31,
# followed by the 0x68-byte extended hardware frame at +104..+208.
ARMV8M_MAINLINE_FPU = SavedStackContext(
    profile_id="freertos/armv8-m.mainline/non-mpu-fpu",
    validation="live",
    pointer_size=4,
    registers=tuple((register, 8 + (register - 4) * 4) for register in range(4, 12))
    + ((0, 104), (1, 108), (2, 112), (3, 116), (12, 120)),
    pc_offset=128,
    lr_offset=124,
    status_offset=132,
    sp_offset=208,
    context_span=(0, 208),
    fill_word=_ARM_FILL,
    fpu=True,
    initial_registers=(
        0x04040404,
        0x05050505,
        0x06060606,
        0x07070707,
        0x08080808,
        0x09090909,
        0x10101010,
        0x11111111,
    ),
)

CM33_PSPLIM_SLOT = 0
CM33_EXC_RETURN_SLOT = 4
_CM33_MPU_PSPLIM_OFFSET = -12
_ARMV8M_MAINLINE_PROFILES = frozenset(
    (
        ARMV8M_MAINLINE_INTEGER.profile_id,
        ARMV8M_MAINLINE_SMP_INTEGER.profile_id,
        ARMV8M_MAINLINE_FPU.profile_id,
        "freertos/armv8-m.mainline/mpu-v2",
    )
)

# Armv8-M Mainline MPU wrappers v2: the port's context array ends at
# pxTopOfStack.  The hardware frame copy is at -48..-21, PC at -24, and the
# original PSP slot at -16; that PSP names the hardware frame, so the
# interrupted SP is PSP+32.
ARMV8M_MAINLINE_MPU_INTEGER = SavedStackContext(
    profile_id="freertos/armv8-m.mainline/mpu-v2",
    validation="live",
    pointer_size=4,
    registers=tuple((register, -80 + (register - 4) * 4) for register in range(4, 12))
    + ((0, -48), (1, -44), (2, -40), (3, -36), (12, -32)),
    pc_offset=-24,
    lr_offset=-28,
    status_offset=-20,
    sp_offset=0,
    context_span=(-80, 0),
    sp_slot_offset=-16,
    sp_slot_bias=32,
    fill_word=0x04040404,
    initial_registers=(
        0x04040404,
        0x05050505,
        0x06060606,
        0x07070707,
        0x08080808,
        0x09090909,
        0x10101010,
        0x11111111,
    ),
    context_in_tcb=True,
)


# Armv7-M FPU port (ARM_CM4F): pxTopOfStack points at r4, followed by the
# saved EXC_RETURN, then S16-S31 for FPU-using tasks and the hardware frame.
# A task that never used the FPU keeps the basic 68-byte integer layout;
# an FPU-using task carries the 64-byte S16-S31 block plus the 0x68-byte
# extended hardware frame. Both layouts keep the EXC_RETURN at the same
# slot, so one bounded read selects the profile without guessing offsets.
ARMV7M_CM4F_BASIC = SavedStackContext(
    profile_id="freertos/armv7-m-cm4f/basic",
    pointer_size=4,
    registers=tuple((register, (register - 4) * 4) for register in range(4, 12))
    + ((0, 36), (1, 40), (2, 44), (3, 48), (12, 52)),
    pc_offset=60,
    lr_offset=56,
    status_offset=64,
    sp_offset=68,
    context_span=(0, 68),
    fill_word=_ARM_FILL,
    validation="live",
)

ARMV7M_CM4F_FPU = SavedStackContext(
    profile_id="freertos/armv7-m-cm4f/fpu",
    pointer_size=4,
    registers=tuple((register, (register - 4) * 4) for register in range(4, 12))
    + ((0, 100), (1, 104), (2, 108), (3, 112), (12, 116)),
    pc_offset=124,
    lr_offset=120,
    status_offset=128,
    sp_offset=204,
    context_span=(0, 204),
    fill_word=_ARM_FILL,
    fpu=True,
    validation="live",
)

CM4F_EXC_RETURN_SLOT = 32


def decode_cm4f_saved_context(
    top: int,
    read: Reader,
    *,
    basic: SavedStackContext,
    fpu: SavedStackContext | None = None,
    endian: Endian,
    stack_low: int | None = None,
    stack_high: int | None = None,
    exit_pc: int | None = None,
    entry_pc: int | None = None,
) -> tuple[SeedFrame | None, str]:
    """Decode one ARM_CM4F task, selecting basic or FPU layout by EXC_RETURN.

    The saved EXC_RETURN sits at the same slot in both layouts, so the
    selection is an exact set match, never an offset guess.  A word that
    matches neither set refuses without reading further slots.
    """
    saved_lr = read_slot(
        read,
        top + CM4F_EXC_RETURN_SLOT,
        4,
        endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )
    if saved_lr is None:
        return None, "saved-context-unreadable"
    if saved_lr in EXTENDED_EXC_RETURNS:
        if fpu is None:
            return None, "unsupported-extension-context"
        context = fpu
    elif saved_lr in BASIC_EXC_RETURNS:
        context = basic
    else:
        return None, "saved-context-exc-return-unproven"
    return decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        stack_low=stack_low,
        stack_high=stack_high,
        exit_pc=exit_pc,
        entry_pc=entry_pc,
    )


def build_riscv_saved_context(
    isa: Isa,
    word: int,
    *,
    gpr_offsets: Mapping[int, int] | None = None,
    frame_bytes: int | None = None,
) -> SavedStackContext:
    """Build a FreeRTOS RISC-V integer saved-context layout."""
    offsets = (
        dict(gpr_offsets)
        if gpr_offsets is not None
        else {1: word, **{reg: (reg - 3) * word for reg in range(5, 32)}}
    )
    span_bytes = 31 * word if frame_bytes is None else frame_bytes
    ordered = tuple((reg, offsets[reg]) for reg in sorted(offsets))
    return SavedStackContext(
        profile_id=f"freertos/{isa}/integer-gpr",
        pointer_size=word,
        registers=ordered,
        pc_offset=0,
        lr_offset=offsets[1],
        status_offset=None,
        sp_offset=span_bytes,
        context_span=(0, span_bytes),
        slot_aliases=(("r1", "lr"),),
        fill_word=int.from_bytes(bytes([0xA5]) * word, "little"),
        validation="live",
    )


RISCV32_INTEGER = build_riscv_saved_context("riscv32", 4)
RISCV64_INTEGER = build_riscv_saved_context("riscv64", 8)


def decode_cm33_saved_context(
    top: int,
    read: Reader,
    *,
    basic: SavedStackContext,
    fpu: SavedStackContext | None = None,
    endian: Endian,
    stack_low: int | None = None,
    stack_high: int | None = None,
    context_low: int | None = None,
    context_high: int | None = None,
    exit_pc: int | None = None,
    entry_pc: int | None = None,
) -> tuple[SeedFrame | None, str]:
    """Decode one ARM_CM33_NTZ task, enforcing PSPLIM and EXC_RETURN invariants."""
    saved_lr = read_slot(
        read,
        top + CM33_EXC_RETURN_SLOT,
        4,
        endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )
    if saved_lr is None:
        return None, "saved-context-unreadable"
    if saved_lr in EXTENDED_EXC_RETURNS:
        if fpu is None:
            return None, "unsupported-extension-context"
        context = fpu
    elif saved_lr in BASIC_EXC_RETURNS:
        context = basic
    elif (saved_lr & 0xF0000000) == 0xF0000000:
        return None, "unsupported-extension-context"
    else:
        return None, "saved-context-exc-return-unproven"
    return decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        stack_low=stack_low,
        stack_high=stack_high,
        context_low=context_low,
        context_high=context_high,
        exit_pc=exit_pc,
        entry_pc=entry_pc,
    )


def validate_armv8m_live_registers(
    psplim: int,
    control: int,
    *,
    mpu: bool,
    exception_number: int,
    sp: int,
    process_sp: int | None,
    stack_low: int | None,
    stack_high: int | None,
) -> str | None:
    """Validate Armv8-M Mainline hardware ``PSPLIM`` and ``CONTROL`` for a live seed."""
    control_mask = ~0x7 if mpu else ~0x6
    if (psplim & 0x7) != 0 or psplim < 0 or (control & control_mask) != 0:
        return "unsupported-extension-context"
    if not exception_number:
        if (control & 0x2) != 0x2 or not armv8m_psplim_valid(
            psplim, stack_low=stack_low, sp=sp
        ):
            return "unsupported-extension-context"
        return None
    if (
        stack_low is None
        or stack_high is None
        or (process_sp is not None and stack_low <= process_sp <= stack_high)
    ) and not armv8m_psplim_valid(psplim, stack_low=stack_low, sp=process_sp):
        return "unsupported-extension-context"
    return None


def validate_cm33_stage_psplim(
    port: str,
    seed_kind: str,
    stage_owner: str,
    *,
    read: Reader,
    task_top: int,
    psp: int,
    sw_size: int,
    task_low: int,
    task_high: int,
    seed_sp: int,
    live_psplim: int | None,
) -> str | None:
    """Enforce CM33 hardware and saved PSPLIM invariants across switch stages."""
    if not port.startswith("arm-cm33"):
        return None
    if live_psplim is not None and (live_psplim < 0 or (live_psplim & 0x7) != 0):
        return "unsupported-extension-context"
    if seed_kind == "live-frame":
        if live_psplim is not None and not armv8m_psplim_valid(
            live_psplim, stack_low=task_low, sp=seed_sp
        ):
            return "unsupported-extension-context"
        return None
    if seed_kind == "saved-tcb" and stage_owner == "old":
        if live_psplim is not None and not armv8m_psplim_valid(
            live_psplim, stack_low=task_low, sp=seed_sp
        ):
            return "unsupported-extension-context"
        return None
    if seed_kind in ("saved-tcb", "first-start-saved"):
        return None
    slot_base = psp - sw_size if seed_kind == "saved-buffer" else task_top
    saved_psplim = read_slot(
        read,
        slot_base + CM33_PSPLIM_SLOT,
        4,
        "little",
        stack_low=task_low,
        stack_high=task_high,
    )
    if saved_psplim is None:
        return "scheduler-context-unreadable"
    if not armv8m_psplim_valid(saved_psplim, stack_low=task_low, sp=seed_sp):
        return "unsupported-extension-context"
    if live_psplim is not None and (
        live_psplim != saved_psplim
        or not armv8m_psplim_valid(live_psplim, stack_low=task_low, sp=seed_sp)
    ):
        return "unsupported-extension-context"
    return None


def decode_saved_context(
    context: SavedStackContext,
    top: int,
    read: Reader,
    *,
    endian: Endian,
    stack_low: int | None = None,
    stack_high: int | None = None,
    context_low: int | None = None,
    context_high: int | None = None,
    exit_pc: int | None = None,
    entry_pc: int | None = None,
) -> tuple[SeedFrame | None, str]:
    """Decode one task's saved context; return ``(seed, reason)``.

    ``exit_pc`` is the port's task-exit address (``prvTaskExitError`` or
    ``xTaskReturnAddress``) when the adapter can resolve it.  It is used only
    as evidence for an unstarted task, never as a fabricated frame.
    """
    decoded, reason = decode_profile_slots(
        context,
        top,
        read,
        endian=endian,
        stack_low=stack_low,
        stack_high=stack_high,
        context_low=context_low,
        context_high=context_high,
        check_ipsr_and_align=True,
    )
    if decoded is None:
        return None, reason
    registers = decoded.registers
    lr = decoded.lr
    code_pc = decoded.code_pc
    sp = decoded.sp
    if context.profile_id in _ARMV8M_MAINLINE_PROFILES:
        psplim_off = (
            _CM33_MPU_PSPLIM_OFFSET if context.context_in_tcb else CM33_PSPLIM_SLOT
        )
        psplim_low = context_low if context.context_in_tcb else stack_low
        psplim_high = context_high if context.context_in_tcb else stack_high
        saved_psplim = read_slot(
            read,
            top + psplim_off,
            4,
            endian,
            stack_low=psplim_low,
            stack_high=psplim_high,
        )
        if saved_psplim is None:
            return None, "saved-context-unreadable"
        if not armv8m_psplim_valid(saved_psplim, stack_low=stack_low, sp=sp):
            return None, "unsupported-extension-context"
    unstarted = False
    if context.status_offset is not None:
        core = tuple(registers.get(register, 0) for register in range(4, 12))
        if context.initial_registers is not None:
            unstarted = core == context.initial_registers
        else:
            unstarted = all(value == context.fill_word for value in core)
        unstarted = unstarted and (
            exit_pc is not None and lr is not None and (lr & ~1) == (exit_pc & ~1)
        )
        if decoded.status != 0x01000000:
            unstarted = False
    else:
        # RISC-V initial context: pc is the task entry, ra is the port's
        # return sentinel (often zero), and the callee-saved slots still hold
        # the stack fill.  A running frame has a code return address instead.
        core_registers = (5, 6, 7, 8, 9, *range(18, 28))
        core_values = [registers.get(register, 0) for register in core_registers]
        ra_is_sentinel = lr in (None, 0) or (
            exit_pc is not None and (lr & ~1) == (exit_pc & ~1)
        )
        unstarted = ra_is_sentinel and all(
            value == context.fill_word for value in core_values
        )
    unstarted = unstarted and entry_pc is not None and code_pc == entry_pc
    return (
        SeedFrame(
            pc=code_pc,
            sp=sp,
            regs=registers,
            source="unstarted" if unstarted else "saved",
            root_pc=code_pc if unstarted else None,
        ),
        "ok",
    )


def initial_context(
    context: SavedStackContext,
    top: int,
    read: Reader,
    *,
    endian: Endian,
    stack_low: int | None = None,
    stack_high: int | None = None,
    entry_pc: int | None = None,
    exit_pc: int | None = None,
) -> tuple[SeedFrame | None, str]:
    """Decode a task that has never run, requiring an explicit unstarted proof."""
    seed, reason = decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        stack_low=stack_low,
        stack_high=stack_high,
        entry_pc=entry_pc,
        exit_pc=exit_pc,
    )
    if seed is None:
        return None, reason
    if seed.source != "unstarted":
        return None, "not-unstarted"
    return seed, "ok"


def raw_context_words(
    context: SavedStackContext,
    top: int,
    read: Reader,
    *,
    count: int,
    stack_low: int | None = None,
    stack_high: int | None = None,
) -> bytes | None:
    """Read the raw words of a context; used by diagnostics and tests."""
    size = count * context.pointer_size
    return read_block(
        read,
        top,
        size,
        stack_low=stack_low,
        stack_high=stack_high,
    )
