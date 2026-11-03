"""Arm M-profile basic exception frames with Armv6-M and Armv7-M policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .unwind_segments import (
    ExceptionTransitionContext,
    ExceptionTransitionResult,
    StackSegment,
    TransitionGuard,
    segment_for_address,
)

# Common Arm M-profile frame model

EXCEPTION_FRAME_SIZE = 0x20

XPSR_T_BIT = 1 << 24

XPSR_ALIGN_BIT = 1 << 9

XPSR_IPSR_MAX_MASK = 0x1FF

EXC_RETURN_HANDLER_MAIN = 0xFFFFFFF1

EXC_RETURN_THREAD_MAIN = 0xFFFFFFF9

EXC_RETURN_THREAD_PROCESS = 0xFFFFFFFD

BASIC_EXC_RETURNS = frozenset(
    (EXC_RETURN_HANDLER_MAIN, EXC_RETURN_THREAD_MAIN, EXC_RETURN_THREAD_PROCESS)
)


@dataclass(frozen=True)
class ExceptionReturn:
    """An ISA-validated exception return mode and stack selection."""

    value: int
    mode: str
    stack: str


ExceptionNumberKind = Literal[
    "thread", "external", "system", "fault", "scheduler", "invalid"
]


def _read_word(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def _basic_exception_transition(
    context: ExceptionTransitionContext,
    decoded: ExceptionReturn,
    *,
    exception_number_kind: Callable[[int], ExceptionNumberKind],
    ipsr_mask: int,
    source: str,
) -> ExceptionTransitionResult | str:
    """Restore the shared eight-word frame under a selected ISA policy."""
    value = decoded.value
    if context.current_segment.kind != "exception":
        return "exception-context-unproven"
    if context.current_segment.stack != "main":
        return "exception-context-unproven"
    if context.current_segment.low is None or context.current_segment.high is None:
        return "exception-frame-bounds-unavailable"
    main_size = EXCEPTION_FRAME_SIZE if decoded.stack == "main" else 0
    if not context.current_segment.contains(context.frame_sp, main_size) or not (
        context.current_segment.low < context.frame_sp <= context.current_segment.high
    ):
        return "exception-frame-outside-main-stack"
    if context.remaining_exception_bytes < EXCEPTION_FRAME_SIZE:
        return "exception-read-budget-exceeded"

    target: StackSegment | None = None
    if decoded.stack == "process":
        frame_base = context.process_sp
        if frame_base is None:
            return "exception-process-sp-unavailable"
    else:
        frame_base = context.frame_sp
    if frame_base <= 0:
        return "exception-frame-invalid"
    if context.stkalign:
        if frame_base & 7:
            return "exception-frame-misaligned"
    elif frame_base & 3:
        return "exception-frame-misaligned"
    if decoded.mode == "thread":
        if decoded.stack == "main":
            matches = [
                candidate
                for candidate in context.segments.values()
                if candidate.stack == "main"
                and candidate.kind != "exception"
                and candidate.contains(frame_base, EXCEPTION_FRAME_SIZE)
            ]
            target = matches[0] if len(matches) == 1 else None
        else:
            target = segment_for_address(
                context.segments,
                frame_base,
                size=EXCEPTION_FRAME_SIZE,
                stack=decoded.stack,
            )
        if target is None:
            return "exception-context-unproven"

    raw = context.read(frame_base, EXCEPTION_FRAME_SIZE)
    if raw is None or len(raw) != EXCEPTION_FRAME_SIZE:
        return "exception-frame-unreadable"
    word0 = _read_word(raw, 0x00)
    word1 = _read_word(raw, 0x04)
    word2 = _read_word(raw, 0x08)
    word3 = _read_word(raw, 0x0C)
    word12 = _read_word(raw, 0x10)
    word_lr = _read_word(raw, 0x14)
    word_pc = _read_word(raw, 0x18)
    xpsr = _read_word(raw, 0x1C)

    if not xpsr & XPSR_T_BIT:
        return "exception-frame-not-thumb"
    alignment = bool(xpsr & XPSR_ALIGN_BIT)
    if alignment and not context.stkalign:
        return "exception-frame-alignment-invalid"
    if word_pc == 0 or word_pc & 1:
        return "exception-frame-pc-invalid"
    ipsr = xpsr & XPSR_IPSR_MAX_MASK
    if ipsr & ~ipsr_mask:
        return "exception-number-invalid"
    if decoded.mode == "thread":
        if ipsr != 0:
            return "exception-frame-ipsr-invalid"
    elif ipsr == 0:
        return "exception-frame-ipsr-invalid"
    elif exception_number_kind(ipsr) == "invalid":
        return "exception-number-invalid"

    restored_sp = frame_base + EXCEPTION_FRAME_SIZE + (4 if alignment else 0)
    if context.stkalign:
        expected_rest = 4 if alignment else 0
        if restored_sp & 7 != expected_rest:
            return "exception-frame-alignment-invalid"
    elif restored_sp & 3:
        return "exception-frame-alignment-invalid"
    registers = dict(context.registers)
    registers.update(
        {
            0: word0,
            1: word1,
            2: word2,
            3: word3,
            12: word12,
            14: word_lr,
            15: word_pc,
            13: restored_sp,
        }
    )

    if context.guard is not None:
        reason = context.guard(ipsr, word_pc)
        if reason is not None:
            return reason

    if decoded.mode == "handler":
        main = context.current_segment
        target = StackSegment(
            ident=f"exception:{ipsr}",
            owner=f"exception:{ipsr}",
            low=main.low,
            high=main.high,
            source=f"{main.source}:exception-frame",
            stack="main",
            kind="exception",
        )
    elif target is None:
        return "exception-context-unproven"
    if not target.contains(frame_base, EXCEPTION_FRAME_SIZE):
        return "exception-frame-outside-bounds"
    if not target.contains(restored_sp, 0) or restored_sp > (target.high or 0):
        return "exception-context-outside-bounds"

    evidence = (
        f"EXC_RETURN={value:#010x} {decoded.mode}/{decoded.stack} "
        f"frame={frame_base:#x} pc={word_pc:#x} xpsr={xpsr:#010x} "
        f"ipsr={ipsr}"
    )
    return ExceptionTransitionResult(
        segment=target,
        pc=word_pc,
        sp=restored_sp,
        registers=registers,
        method="exception",
        source=source,
        value=value,
        bytes_read=EXCEPTION_FRAME_SIZE,
        evidence=evidence,
    )


# Armv6-M exception rules

ARMV6M_MAX_EXCEPTION_NUMBER = 47

ARMV6M_IPSR_MASK = 0x3F


def armv6m_exception_number_kind(number: int) -> ExceptionNumberKind:
    """Classify an Armv6-M IPSR number without inferring implemented IRQs."""
    if number == 0:
        return "thread"
    if number in (2, 15):
        return "system"
    if number == 3:
        return "fault"
    if number in (11, 14):
        return "scheduler"
    if 16 <= number <= ARMV6M_MAX_EXCEPTION_NUMBER:
        return "external"
    return "invalid"


def armv6m_exception_return_candidate(value: int) -> bool:
    """Whether an Armv6-M value has the EXC_RETURN high-byte pattern."""
    return (value & 0xFFFFFF00) == 0xFFFFFF00


def decode_armv6m_exception_return(value: int) -> ExceptionReturn | str:
    """Decode one of Armv6-M's three basic exception-return values."""
    if value not in BASIC_EXC_RETURNS:
        return "exception-return-illegal"
    return ExceptionReturn(
        value,
        "handler" if value == EXC_RETURN_HANDLER_MAIN else "thread",
        "process" if value == EXC_RETURN_THREAD_PROCESS else "main",
    )


def armv6m_exception_transition(
    context: ExceptionTransitionContext,
) -> ExceptionTransitionResult | str | None:
    """Decode an Armv6-M basic frame with mandatory eight-byte alignment."""
    value = context.value & 0xFFFFFFFF
    if not armv6m_exception_return_candidate(value):
        return None
    decoded = decode_armv6m_exception_return(value)
    if isinstance(decoded, str):
        return decoded
    if not context.stkalign:
        return "exception-frame-alignment-invalid"
    return _basic_exception_transition(
        context,
        decoded,
        exception_number_kind=armv6m_exception_number_kind,
        ipsr_mask=ARMV6M_IPSR_MASK,
        source="armv6m-basic-exception-frame",
    )


# Armv7-M exception rules

EXTENDED_EXC_RETURNS = frozenset((0xFFFFFFE1, 0xFFFFFFE9, 0xFFFFFFED))

ARMV7M_MAX_EXCEPTION_NUMBER = 511

ARMV7M_IPSR_MASK = 0x1FF

ARMV7M_MAX_EXTERNAL_INTERRUPTS = 496

ARMV7M_FIRST_EXTERNAL_EXCEPTION = 16

ICTR_INTLINESNUM_MASK = 0xF

ARMV7M_INVALID_EXCEPTION_NUMBERS = frozenset((1, 7, 8, 9, 10, 13))

ARMV7M_FAULT_EXCEPTION_NUMBERS = frozenset((3, 4, 5, 6))

ARMV7M_SCHEDULER_EXCEPTION_NUMBERS = frozenset((11, 14))

ARMV7M_SYSTEM_EXCEPTION_NUMBERS = frozenset((2, 12, 15))


def armv7m_exception_number_kind(number: int) -> ExceptionNumberKind:
    """Classify an Armv7-M IPSR number by its architecture-defined table."""
    if number == 0:
        return "thread"
    if number < 0 or number > ARMV7M_MAX_EXCEPTION_NUMBER:
        return "invalid"
    if number in ARMV7M_INVALID_EXCEPTION_NUMBERS:
        return "invalid"
    if number in ARMV7M_FAULT_EXCEPTION_NUMBERS:
        return "fault"
    if number in ARMV7M_SCHEDULER_EXCEPTION_NUMBERS:
        return "scheduler"
    if number in ARMV7M_SYSTEM_EXCEPTION_NUMBERS:
        return "system"
    if number >= ARMV7M_FIRST_EXTERNAL_EXCEPTION:
        return "external"
    return "invalid"


def armv7m_max_external_exception(ictr: int) -> int | None:
    """Return the highest external IPSR value described by a valid ICTR."""
    if not isinstance(ictr, int) or ictr < 0 or ictr > 0xFFFFFFFF:
        return None
    if ictr & ~ICTR_INTLINESNUM_MASK:
        return None
    lines = min(
        32 * ((ictr & ICTR_INTLINESNUM_MASK) + 1), ARMV7M_MAX_EXTERNAL_INTERRUPTS
    )
    return ARMV7M_FIRST_EXTERNAL_EXCEPTION + lines - 1


def armv7m_exception_return_candidate(value: int) -> bool:
    """Whether an Armv7-M value has the EXC_RETURN high-byte pattern."""
    return (value & 0xFFFFFF00) == 0xFFFFFF00


def decode_armv7m_exception_return(value: int) -> ExceptionReturn | str:
    """Decode an Armv7-M basic return, refusing FP and other extended frames."""
    if value in EXTENDED_EXC_RETURNS:
        return "unsupported-extension-context"
    if value not in BASIC_EXC_RETURNS:
        return "exception-return-illegal"
    return ExceptionReturn(
        value,
        "handler" if value == EXC_RETURN_HANDLER_MAIN else "thread",
        "process" if value == EXC_RETURN_THREAD_PROCESS else "main",
    )


def armv7m_exception_transition(
    context: ExceptionTransitionContext,
) -> ExceptionTransitionResult | str | None:
    """Decode an Armv7-M basic frame using the supplied CCR.STKALIGN value."""
    value = context.value & 0xFFFFFFFF
    if not armv7m_exception_return_candidate(value):
        return None
    decoded = decode_armv7m_exception_return(value)
    if isinstance(decoded, str):
        return decoded
    return _basic_exception_transition(
        context,
        decoded,
        exception_number_kind=armv7m_exception_number_kind,
        ipsr_mask=ARMV7M_IPSR_MASK,
        source="armv7m-basic-exception-frame",
    )


def arm_exception_guard(
    exception_number: int, pc: int, isa: str = "armv7-m"
) -> str | None:
    """Refuse exception frames whose interrupted PC is not a proven resume PC."""
    del pc
    classifier = (
        armv6m_exception_number_kind
        if isa == "armv6-m"
        else armv7m_exception_number_kind
    )
    kind = classifier(exception_number)
    if kind == "invalid":
        return "exception-number-invalid"
    if kind == "scheduler":
        return "scheduler-transition-unproven"
    if kind == "fault":
        return "exception-frame-type-unproven"
    if kind == "system":
        return "exception-number-unverified"
    return None


def arm_capacity_exception_guard(
    max_exception: int, isa: str = "armv7-m"
) -> TransitionGuard:
    """Return a guard that refuses external IDs beyond target ICTR capacity."""

    def guard(exception_number: int, pc: int) -> str | None:
        reason = arm_exception_guard(exception_number, pc, isa)
        if reason is not None:
            return reason
        if exception_number >= ARMV7M_FIRST_EXTERNAL_EXCEPTION and (
            exception_number > max_exception
        ):
            return "exception-number-unimplemented"
        return None

    return guard
