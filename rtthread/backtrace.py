"""RT-Thread saved-context decoding for task stack backtraces (pure).

``struct rt_thread::sp`` points at the context frame left by
``rt_hw_context_switch``/the port's exception entry.  The frame shape is a
property of ``libcpu`` and, on Cortex-M33, of the TrustZone/FPU branch; the
tables here cover the integer-only paths exercised by the fixtures.  A port
without a closed-loop fixture returns an explicit unverified reason rather than
a guessed context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.unwind import Endian, Reader, SeedFrame, read_slot

Port = Literal["cortex-m0", "cortex-m3", "cortex-m33", "riscv32", "riscv64"]


@dataclass(frozen=True)
class SavedContext:
    """Offsets in one ``rt_thread`` switch frame, relative to ``thread->sp``."""

    port: Port
    pointer_size: int
    registers: tuple[tuple[int, int], ...]
    pc_offset: int
    lr_offset: int
    xpsr_offset: int | None
    sp_offset: int
    fill_word: int
    verified: bool


def context_for(port: Port) -> SavedContext | None:
    """Return the verified port layout, or ``None`` for an unverified one."""
    if port == "cortex-m0":
        return _cortex_m(port, verified=False)
    if port == "cortex-m3":
        return _cortex_m(port, verified=False)
    if port == "cortex-m33":
        return _cortex_m33()
    if port == "riscv64":
        return _riscv(port, 8, verified=True)
    if port == "riscv32":
        return _riscv(port, 4, verified=False)
    return None


def _cortex_m(port: Port, *, verified: bool) -> SavedContext:
    return SavedContext(
        port=port,
        pointer_size=4,
        registers=tuple((register, (register - 4) * 4) for register in range(4, 12))
        + ((0, 32), (1, 36), (2, 40), (3, 44), (12, 48)),
        pc_offset=56,
        lr_offset=52,
        xpsr_offset=60,
        sp_offset=64,
        fill_word=0xDEADBEEF,
        verified=verified,
    )


def _cortex_m33() -> SavedContext:
    return SavedContext(
        port="cortex-m33",
        pointer_size=4,
        registers=tuple(
            (register, 16 + (register - 4) * 4) for register in range(4, 12)
        )
        + ((0, 48), (1, 52), (2, 56), (3, 60), (12, 64)),
        pc_offset=72,
        lr_offset=4,
        xpsr_offset=76,
        sp_offset=80,
        fill_word=0xDEADBEEF,
        verified=False,
    )


def _riscv(port: Port, word: int, *, verified: bool) -> SavedContext:
    registers: list[tuple[int, int]] = [(1, word)]
    registers.extend((register, register * word) for register in range(5, 32))
    return SavedContext(
        port=port,
        pointer_size=word,
        registers=tuple(registers),
        pc_offset=0,
        lr_offset=word,
        xpsr_offset=None,
        sp_offset=32 * word,
        fill_word=0xDEADBEEF,
        verified=verified,
    )


def decode_saved_context(
    context: SavedContext,
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
        registers[14 if context.xpsr_offset is not None else 1] = lr
    caller_sp = sp + context.sp_offset
    registers[13 if context.xpsr_offset is not None else 2] = caller_sp
    if context.xpsr_offset is not None:
        registers[15] = pc
        xpsr = read_slot(
            read,
            sp + context.xpsr_offset,
            4,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        if xpsr is None:
            return None, "saved-context-unreadable"
        if not xpsr & (1 << 24):
            return None, "saved-frame-not-thumb"
    code_pc = pc & ~1 if context.xpsr_offset is not None else pc
    if code_pc == 0:
        return None, "saved-context-unreadable"
    unstarted = False
    if entry:
        core = [registers.get(register, 0) for register in range(4, 12)]
        if context.xpsr_offset is not None:
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
