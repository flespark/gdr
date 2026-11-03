"""FreeRTOS saved-context decoding for task stack backtraces (pure).

``struct tskTaskControlBlock::pxTopOfStack`` points at the frame the port saved
when the task last left the CPU.  The frame shape is a property of the port
(and, on Cortex-M33, of the MPU/non-MPU save path), not of the kernel version,
so the tables below are keyed by port evidence.  The module only decodes; the
unwind itself lives in :mod:`core.unwind`.
"""

from __future__ import annotations

from core.cpus import Isa
from core.stack_frame_parser import SavedStackContext
from core.unwind import Endian, Reader, SeedFrame, read_block, read_slot

_ARM_FILL = 0xA5A5A5A5


# Cortex-M3/M0: pxTopOfStack points at r4; the hardware frame is above it.
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

# CM33 non-MPU (SMP lane): pxTopOfStack = psp-40; the saved hardware frame
# begins at +40 and the original SP is one hardware frame above it.
ARMV8M_MAINLINE_SMP_INTEGER = SavedStackContext(
    profile_id="freertos/armv8-m.mainline/non-mpu-smp",
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

# CM33 MPU wrappers v2: the port's context array ends at pxTopOfStack.  The
# hardware frame copy is at -48..-21, PC at -24, and the original PSP slot at
# -16; that PSP names the hardware frame, so the interrupted SP is PSP+32.
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


def _riscv_context(isa: Isa, word: int) -> SavedStackContext:
    return SavedStackContext(
        profile_id=f"freertos/{isa}/integer-gpr",
        pointer_size=word,
        registers=((1, word),)
        + tuple((register, (register - 3) * word) for register in range(5, 32)),
        pc_offset=0,
        lr_offset=word,
        status_offset=None,
        sp_offset=31 * word,
        context_span=(0, 31 * word),
        slot_aliases=(("r1", "lr"),),
        fill_word=int.from_bytes(bytes([0xA5]) * word, "little"),
        validation="live",
    )


RISCV32_INTEGER = _riscv_context("riscv32", 4)
RISCV64_INTEGER = _riscv_context("riscv64", 8)


def _registers(
    context: SavedStackContext,
    top: int,
    read: Reader,
    *,
    endian: Endian,
    stack_low: int | None,
    stack_high: int | None,
    context_low: int | None,
    context_high: int | None,
) -> dict[int, int] | None:
    result: dict[int, int] = {}
    for register, offset in context.registers:
        value = read_slot(
            read,
            top + offset,
            context.pointer_size,
            endian,
            stack_low=context_low if context_low is not None else stack_low,
            stack_high=context_high if context_high is not None else stack_high,
        )
        if value is None:
            return None
        result[register] = value
    return result


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
    if top <= 0:
        return None, "no-saved-context"
    if context.context_in_tcb and (context_low is None or context_high is None):
        return None, "context-bounds-unavailable"
    read_low = context_low if context_low is not None else stack_low
    read_high = context_high if context_high is not None else stack_high
    pc = read_slot(
        read,
        top + context.pc_offset,
        context.pointer_size,
        endian,
        stack_low=context_low if context_low is not None else stack_low,
        stack_high=context_high if context_high is not None else stack_high,
    )
    if pc is None or pc == 0:
        return None, "no-saved-context"
    registers = _registers(
        context,
        top,
        read,
        endian=endian,
        stack_low=stack_low,
        stack_high=stack_high,
        context_low=context_low,
        context_high=context_high,
    )
    if registers is None:
        return None, "saved-context-unreadable"
    if context.sp_slot_offset is not None:
        psp = read_slot(
            read,
            top + context.sp_slot_offset,
            context.pointer_size,
            endian,
            stack_low=read_low,
            stack_high=read_high,
        )
        if psp is None:
            return None, "saved-context-unreadable"
        sp = psp + context.sp_slot_bias
    else:
        sp = top + context.sp_offset
    if stack_low is not None and sp < stack_low:
        return None, "saved-stack-underflow"
    if stack_high is not None and sp > stack_high:
        return None, "saved-stack-overflow"
    lr = read_slot(
        read,
        top + context.lr_offset,
        context.pointer_size,
        endian,
        stack_low=read_low,
        stack_high=read_high,
    )
    if lr is not None:
        registers[
            14 if context.pointer_size == 4 and context.status_offset is not None else 1
        ] = lr
    registers[
        13 if context.pointer_size == 4 and context.status_offset is not None else 2
    ] = sp
    if context.status_offset is not None:
        registers[15] = pc
        xpsr = read_slot(
            read,
            top + context.status_offset,
            4,
            endian,
            stack_low=read_low,
            stack_high=read_high,
        )
        if xpsr is None:
            return None, "saved-context-unreadable"
        if not xpsr & (1 << 24):
            return None, "saved-frame-not-thumb"
        if xpsr & 0x1FF:
            return None, "saved-frame-ipsr-invalid"
        if xpsr & (1 << 9):
            sp += 4
            registers[13] = sp
            if stack_high is not None and sp > stack_high:
                return None, "saved-stack-overflow"
    code_pc = (
        pc & ~1
        if context.pointer_size == 4 and context.status_offset is not None
        else pc
    )
    if code_pc == 0:
        return None, "saved-context-unreadable"
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
        if xpsr != 0x01000000:
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
