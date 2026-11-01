"""RT-Thread saved-context decoding for task stack backtraces (pure).

``struct rt_thread::sp`` points at the context frame left by
``rt_hw_context_switch``/the port's exception entry.  The frame shape is a
property of ``libcpu`` and, on Cortex-M33, of the TrustZone/FPU branch; the
tables here cover the integer-only paths exercised by the fixtures.  A port
without a closed-loop fixture returns an explicit unverified reason rather than
a guessed context.
"""

from __future__ import annotations

from core.cpus import Isa
from core.stack_frame_parser import SavedStackContext, ValidationLevel
from core.unwind import Endian, Reader, SeedFrame, read_slot


def _arm_integer(isa: Isa) -> SavedStackContext:
    return SavedStackContext(
        profile_id=f"rtthread/{isa}/integer",
        pointer_size=4,
        registers=tuple((register, (register - 4) * 4) for register in range(4, 12))
        + ((0, 32), (1, 36), (2, 40), (3, 44), (12, 48)),
        pc_offset=56,
        lr_offset=52,
        status_offset=60,
        sp_offset=64,
        context_span=(0, 64),
        fill_word=0xDEADBEEF,
    )


def _armv8m_mainline() -> SavedStackContext:
    return SavedStackContext(
        profile_id="rtthread/armv8-m.mainline/integer",
        pointer_size=4,
        registers=tuple(
            (register, 16 + (register - 4) * 4) for register in range(4, 12)
        )
        + ((0, 48), (1, 52), (2, 56), (3, 60), (12, 64)),
        pc_offset=72,
        lr_offset=4,
        status_offset=76,
        sp_offset=80,
        context_span=(0, 80),
        fill_word=0xDEADBEEF,
    )


def _riscv(isa: Isa, word: int, *, validation: ValidationLevel) -> SavedStackContext:
    registers: list[tuple[int, int]] = [(1, word)]
    registers.append((4, 4 * word))
    registers.extend((register, register * word) for register in range(5, 32))
    return SavedStackContext(
        profile_id=f"rtthread/{isa}/integer",
        pointer_size=word,
        registers=tuple(registers),
        pc_offset=0,
        lr_offset=word,
        status_offset=None,
        sp_offset=32 * word,
        context_span=(0, 32 * word),
        slot_aliases=(("r1", "lr"),),
        fill_word=0xDEADBEEF,
        validation=validation,
    )


ARMV6M_INTEGER = _arm_integer("armv6-m")
ARMV7M_INTEGER = _arm_integer("armv7-m")
ARMV8M_MAINLINE_INTEGER = _armv8m_mainline()
RISCV32_INTEGER = _riscv("riscv32", 4, validation="synthetic")
RISCV64_INTEGER = _riscv("riscv64", 8, validation="live")


def decode_saved_context(
    context: SavedStackContext,
    sp: int,
    read: Reader,
    *,
    endian: Endian,
    entry: int | None = None,
    stack_low: int | None = None,
    stack_high: int | None = None,
) -> tuple[SeedFrame | None, str]:
    """Decode one thread's saved frame; return ``(seed, reason)``.

    ``entry`` is the thread entry retained by RT-Thread.  An exact entry match
    with the port's initial fill pattern proves an unstarted thread; a running
    frame with the same entry remains ``saved`` unless the fill is present.
    """
    if sp <= 0:
        return None, "no-saved-context"
    pc = read_slot(
        read,
        sp + context.pc_offset,
        context.pointer_size,
        endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )
    if pc is None or pc == 0:
        return None, "no-saved-context"
    registers: dict[int, int] = {}
    for register, offset in context.registers:
        value = read_slot(
            read,
            sp + offset,
            context.pointer_size,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        if value is None:
            return None, "saved-context-unreadable"
        registers[register] = value
    lr = read_slot(
        read,
        sp + context.lr_offset,
        context.pointer_size,
        endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )
    if lr is not None:
        registers[14 if context.status_offset is not None else 1] = lr
    caller_sp = sp + context.sp_offset
    registers[13 if context.status_offset is not None else 2] = caller_sp
    if context.status_offset is not None:
        registers[15] = pc
        xpsr = read_slot(
            read,
            sp + context.status_offset,
            4,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        if xpsr is None:
            return None, "saved-context-unreadable"
        if not xpsr & (1 << 24):
            return None, "saved-frame-not-thumb"
    code_pc = pc & ~1 if context.status_offset is not None else pc
    if code_pc == 0:
        return None, "saved-context-unreadable"
    unstarted = False
    if entry:
        core = [registers.get(register, 0) for register in range(4, 12)]
        if context.status_offset is not None:
            unstarted = (code_pc & ~1) == (entry & ~1) and all(
                value == context.fill_word for value in core
            )
        else:
            core_registers = (5, 6, 7, 8, 9, *range(18, 28))
            core_values = [registers.get(register, 0) for register in core_registers]
            unstarted = (code_pc & ~1) == (entry & ~1) and all(
                value == context.fill_word for value in core_values
            )
    return (
        SeedFrame(
            pc=code_pc,
            sp=caller_sp,
            regs=registers,
            source="unstarted" if unstarted else "saved",
            root_pc=code_pc if unstarted else (entry & ~1 if entry else None),
        ),
        "ok",
    )
