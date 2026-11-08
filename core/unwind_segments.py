"""Typed stack segments and exception-transition evidence for unwinding (pure).

The driver in :mod:`core.unwind` normally advances inside one stack segment.
Crossing to another stack requires an architecture hook that proves an
exception-return boundary and supplies the restored context.  This module owns
the neutral data types shared by that driver and the architecture decoder; it
contains no RTOS names or policy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .unwind_meta import Method, Reader

MAX_EXCEPTION_DEPTH = 8
MAX_EXCEPTION_READ_BYTES = 256


@dataclass(frozen=True)
class StackSegment:
    """One physical stack range and the context that owns it.

    ``ident`` is the loop identity; ``owner`` is the display/evidence owner.
    ``stack`` distinguishes a process/task stack from the main/handler stack.
    ``kind`` is ``task``, ``exception`` or ``unknown``.
    """

    ident: str
    owner: str
    low: int | None
    high: int | None
    source: str
    stack: str = "unknown"
    kind: str = "unknown"

    def contains(self, address: int, size: int = 1) -> bool:
        """Whether the inclusive-exclusive span fits inside known bounds."""
        if size < 0:
            return False
        if self.low is None or self.high is None:
            return False
        return self.low <= address and address + size <= self.high


@dataclass(frozen=True)
class SegmentTransition:
    """One proven cross-segment step, retained independently of frame text."""

    from_segment: str
    to_segment: str
    method: Method
    source: str
    pc: int
    sp: int
    value: int
    exact_pc: bool = True
    evidence: str = ""


@dataclass(frozen=True)
class ExceptionTransitionContext:
    """Inputs for one architecture-owned exception-return proof.

    ``value`` is the raw value the ordinary unwind produced in the return
    register/scalar.  ``frame_sp`` is its recovered stack pointer (the main
    stack at the handler entry for handler-mode frames).  ``process_sp`` is the
    live process stack pointer carried from the seed, or ``None`` when the
    architecture has no such register.
    """

    value: int
    registers: Mapping[int, int]
    frame_sp: int
    current_segment: StackSegment
    segments: Mapping[str, StackSegment]
    process_sp: int | None
    read: Reader
    stkalign: bool
    remaining_exception_bytes: int
    guard: Callable[[int, int], str | None] | None = None


@dataclass(frozen=True)
class ExceptionTransitionResult:
    """A proven transition and the context restored from its hardware frame."""

    segment: StackSegment
    pc: int
    sp: int
    registers: Mapping[int, int]
    method: Method
    source: str
    value: int
    bytes_read: int
    evidence: str = ""
    exact_pc: bool = True


@dataclass
class ExceptionBudget:
    """Independent depth and byte budgets for exception-frame decoding."""

    max_depth: int = MAX_EXCEPTION_DEPTH
    max_bytes: int = MAX_EXCEPTION_READ_BYTES
    depth: int = 0
    bytes: int = 0

    @property
    def remaining_bytes(self) -> int:
        """Bytes still available to exception-frame reads."""
        return max(self.max_bytes - self.bytes, 0)


ExceptionDetector = Callable[[int], bool]
ExceptionHook = Callable[
    [ExceptionTransitionContext], ExceptionTransitionResult | str | None
]
TransitionGuard = Callable[[int, int], str | None]


def segment_for_address(
    segments: Mapping[str, StackSegment],
    address: int,
    *,
    size: int = 1,
    stack: str | None = None,
    kind: str | None = None,
) -> StackSegment | None:
    """Return the unique matching segment, or ``None`` when absent/ambiguous."""
    matches = [
        segment
        for segment in segments.values()
        if segment.contains(address, size)
        and (stack is None or segment.stack == stack)
        and (kind is None or segment.kind == kind)
    ]
    return matches[0] if len(matches) == 1 else None
