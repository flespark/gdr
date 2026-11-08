"""RT-Thread saved-context decoding for task stack backtraces (pure).

``struct rt_thread::sp`` points at the context frame left by
``rt_hw_context_switch``/the port's exception entry.  The frame shape is a
property of ``libcpu`` and, on Armv8-M Mainline, of the TrustZone/FPU branch;
the tables here cover the integer-only paths exercised by the fixtures.  A port
without a closed-loop fixture returns an explicit unverified reason rather than
a guessed context.
"""

from __future__ import annotations

from core.cpus import Isa
from core.stack_frame_parser import (
    SavedStackContext,
    ValidationLevel,
    decode_profile_slots,
)
from core.unwind import Endian, Reader, SeedFrame, read_slot


def _arm_integer(
    isa: Isa, *, validation: ValidationLevel = "synthetic"
) -> SavedStackContext:
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
        validation=validation,
    )


def _armv7em(*, validation: ValidationLevel = "synthetic") -> SavedStackContext:
    return SavedStackContext(
        profile_id="rtthread/armv7e-m/integer",
        pointer_size=4,
        registers=tuple((register, 4 + (register - 4) * 4) for register in range(4, 12))
        + ((0, 36), (1, 40), (2, 44), (3, 48), (12, 52)),
        pc_offset=60,
        lr_offset=56,
        status_offset=64,
        sp_offset=68,
        context_span=(0, 68),
        fill_word=0xDEADBEEF,
        validation=validation,
    )


def _armv7em_fpu(*, validation: ValidationLevel = "synthetic") -> SavedStackContext:
    return SavedStackContext(
        profile_id="rtthread/armv7e-m/fpu",
        pointer_size=4,
        registers=tuple((register, 4 + (register - 4) * 4) for register in range(4, 12))
        + ((0, 100), (1, 104), (2, 108), (3, 112), (12, 116)),
        pc_offset=124,
        lr_offset=120,
        status_offset=128,
        sp_offset=204,
        context_span=(0, 204),
        fill_word=0xDEADBEEF,
        validation=validation,
        fpu=True,
    )


def _armv8m_mainline(*, validation: ValidationLevel = "synthetic") -> SavedStackContext:
    return SavedStackContext(
        profile_id="rtthread/armv8-m.mainline/integer",
        pointer_size=4,
        registers=tuple(
            (register, 16 + (register - 4) * 4) for register in range(4, 12)
        )
        + ((0, 48), (1, 52), (2, 56), (3, 60), (12, 64)),
        pc_offset=72,
        lr_offset=68,
        status_offset=76,
        sp_offset=80,
        context_span=(0, 80),
        fill_word=0xDEADBEEF,
        validation=validation,
    )


def _armv8m_mainline_fpu(*, validation: ValidationLevel = "live") -> SavedStackContext:
    return SavedStackContext(
        profile_id="rtthread/armv8-m.mainline/fpu",
        pointer_size=4,
        registers=tuple(
            (register, 16 + (register - 4) * 4) for register in range(4, 12)
        )
        + ((0, 112), (1, 116), (2, 120), (3, 124), (12, 128)),
        pc_offset=136,
        lr_offset=132,
        status_offset=140,
        sp_offset=216,
        context_span=(0, 216),
        fill_word=0xDEADBEEF,
        validation=validation,
        fpu=True,
    )


def _armv7a(*, lwp: bool) -> SavedStackContext:
    bias = 8 if lwp else 0
    return SavedStackContext(
        profile_id="rtthread/armv7-a/integer" + ("-lwp" if lwp else ""),
        pointer_size=4,
        registers=tuple((register, bias + 4 + register * 4) for register in range(13)),
        pc_offset=bias + 60,
        lr_offset=bias + 56,
        status_offset=bias,
        status_kind="cpsr",
        sp_offset=bias + 64,
        context_span=(0, bias + 64),
        fill_word=0xDEADBEEF,
        validation="live" if lwp else "synthetic",
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


ARMV6M_INTEGER = _arm_integer("armv6-m", validation="live")
ARMV7M_INTEGER = _arm_integer("armv7-m", validation="live")
ARMV7EM_INTEGER = _armv7em(validation="live")
ARMV7EM_FPU = _armv7em_fpu(validation="live")
ARMV8M_MAINLINE_INTEGER = _armv8m_mainline(validation="live")
ARMV8M_MAINLINE_FPU = _armv8m_mainline_fpu()
ARMV7A_INTEGER = _armv7a(lwp=False)
ARMV7A_LWP_INTEGER = _armv7a(lwp=True)
RISCV32_INTEGER = _riscv("riscv32", 4, validation="live")
RISCV64_INTEGER = _riscv("riscv64", 8, validation="live")


ARMV7EM_EXC_RETURN_SLOT = 0

ARMV8M_MAINLINE_EXC_RETURN_SLOT = 4

ARMV8M_MAINLINE_BASIC_EXC_RETURN = 0xFFFFFFFD

ARMV8M_MAINLINE_EXTENDED_EXC_RETURN = 0xFFFFFFED


def decode_armv7em_saved_context(
    top: int,
    read: Reader,
    *,
    basic: SavedStackContext,
    fpu: SavedStackContext,
    endian: Endian,
    entry: int | None = None,
    stack_low: int | None = None,
    stack_high: int | None = None,
) -> tuple[SeedFrame | None, str]:
    """Decode one Armv7E-M task by its saved flag.

    The flag carries whether the port stacked S16-S31: only the exact flag
    values select a profile.  Anything else refuses before further reads.
    """
    flag = read_slot(
        read,
        top + ARMV7EM_EXC_RETURN_SLOT,
        4,
        endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )
    if flag is None:
        return None, "saved-context-unreadable"
    if flag == 1:
        context = fpu
    elif flag == 0:
        context = basic
    else:
        return None, "unsupported-extension-context"
    return decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        entry=entry,
        stack_low=stack_low,
        stack_high=stack_high,
    )


def decode_armv8m_mainline_saved_context(
    top: int,
    read: Reader,
    *,
    basic: SavedStackContext,
    fpu: SavedStackContext,
    endian: Endian,
    entry: int | None = None,
    stack_low: int | None = None,
    stack_high: int | None = None,
) -> tuple[SeedFrame | None, str]:
    """Decode one non-secure Armv8-M Mainline task by its saved EXC_RETURN.

    The header carries tz, EXC_RETURN, PSPLIM and CONTROL at fixed slots in
    both layouts; only the exact non-secure basic or extended return selects
    a profile.  Anything else refuses before further reads.
    """
    header = tuple(
        read_slot(
            read, top + offset, 4, endian, stack_low=stack_low, stack_high=stack_high
        )
        for offset in range(0, 16, 4)
    )
    if None in header:
        return None, "saved-context-unreadable"
    if header[0] != 0 or header[2] != 0 or header[3] is None or header[3] & 1:
        return None, "unsupported-extension-context"
    if header[1] == ARMV8M_MAINLINE_EXTENDED_EXC_RETURN:
        context = fpu
    elif header[1] == ARMV8M_MAINLINE_BASIC_EXC_RETURN:
        context = basic
    else:
        return None, "unsupported-extension-context"
    return decode_saved_context(
        context,
        top,
        read,
        endian=endian,
        entry=entry,
        stack_low=stack_low,
        stack_high=stack_high,
    )


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
    if context.profile_id in (
        ARMV7EM_INTEGER.profile_id,
        ARMV7EM_FPU.profile_id,
    ):
        flag = read_slot(
            read, sp, 4, endian, stack_low=stack_low, stack_high=stack_high
        )
        if flag is None:
            return None, "saved-context-unreadable"
        want = 1 if context.profile_id == ARMV7EM_FPU.profile_id else 0
        if flag != want:
            return None, "unsupported-extension-context"
    if context.profile_id in (
        ARMV8M_MAINLINE_INTEGER.profile_id,
        ARMV8M_MAINLINE_FPU.profile_id,
    ):
        header = tuple(
            read_slot(
                read, sp + offset, 4, endian, stack_low=stack_low, stack_high=stack_high
            )
            for offset in range(0, 16, 4)
        )
        if None in header:
            return None, "saved-context-unreadable"
        if header[0] != 0 or header[2] != 0 or header[3] is None or header[3] & 1:
            return None, "unsupported-extension-context"
        want = (
            ARMV8M_MAINLINE_EXTENDED_EXC_RETURN
            if context.profile_id == ARMV8M_MAINLINE_FPU.profile_id
            else ARMV8M_MAINLINE_BASIC_EXC_RETURN
        )
        if header[1] != want:
            return None, "unsupported-extension-context"
    decoded, reason = decode_profile_slots(
        context,
        sp,
        read,
        endian=endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )
    if decoded is None:
        return None, reason
    status = decoded.status
    if context.status_kind == "cpsr" and (
        status is None or status & 0x1F not in (0x13, 0x1F)
    ):
        return None, "unsupported-privilege-context"
    registers = decoded.registers
    code_pc = decoded.code_pc
    caller_sp = decoded.sp
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
