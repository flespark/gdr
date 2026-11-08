"""Arm M-profile exception frames, SCB/vector readers and handler unwind setup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from .gdb_bridge import linker_symbol_address, read_bytes
from .unwind import SeedFrame
from .unwind_meta import Reader
from .unwind_segments import (
    ExceptionDetector,
    ExceptionHook,
    ExceptionTransitionContext,
    ExceptionTransitionResult,
    StackSegment,
    TransitionGuard,
    segment_for_address,
)

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

# Common Arm M-profile frame model

EXCEPTION_FRAME_SIZE = 0x20

EXTENDED_FRAME_SIZE = 0x68

FPCCR_ADDRESS = 0xE000EF34

FPCCR_LSPACT_BIT = 0x1

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
    extended: bool = False


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
    """Restore the shared basic or FPU-extended frame under a selected ISA policy."""
    value = decoded.value
    frame_size = EXTENDED_FRAME_SIZE if decoded.extended else EXCEPTION_FRAME_SIZE
    if context.current_segment.kind != "exception":
        return "exception-context-unproven"
    if context.current_segment.stack != "main":
        return "exception-context-unproven"
    if context.current_segment.low is None or context.current_segment.high is None:
        return "exception-frame-bounds-unavailable"
    main_size = frame_size if decoded.stack == "main" else 0
    if not context.current_segment.contains(context.frame_sp, main_size) or not (
        context.current_segment.low < context.frame_sp <= context.current_segment.high
    ):
        return "exception-frame-outside-main-stack"
    if context.remaining_exception_bytes < frame_size:
        return "exception-read-budget-exceeded"
    lspact = False
    if decoded.extended:
        fpccr_raw = context.read(FPCCR_ADDRESS, 4)
        if fpccr_raw is None or len(fpccr_raw) != 4:
            return "exception-fp-state-unavailable"
        lspact = bool(int.from_bytes(fpccr_raw, "little") & FPCCR_LSPACT_BIT)

    target: StackSegment | None = None
    if decoded.stack == "process":
        frame_base = context.process_sp
        if frame_base is None:
            return "exception-process-sp-unavailable"
    else:
        frame_base = context.frame_sp
    if frame_base <= 0:
        return "exception-frame-invalid"
    if decoded.extended or context.stkalign:
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
                and candidate.contains(frame_base, frame_size)
            ]
            target = matches[0] if len(matches) == 1 else None
        else:
            target = segment_for_address(
                context.segments,
                frame_base,
                size=frame_size,
                stack=decoded.stack,
            )
        if target is None:
            return "exception-context-unproven"

    raw = context.read(frame_base, frame_size)
    if raw is None or len(raw) != frame_size:
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

    restored_sp = frame_base + frame_size + (4 if alignment else 0)
    if decoded.extended or context.stkalign:
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
    if not target.contains(frame_base, frame_size):
        return "exception-frame-outside-bounds"
    if not target.contains(restored_sp, 0) or restored_sp > (target.high or 0):
        return "exception-context-outside-bounds"

    evidence = (
        f"EXC_RETURN={value:#010x} {decoded.mode}/{decoded.stack} "
        f"frame={frame_base:#x} pc={word_pc:#x} xpsr={xpsr:#010x} "
        f"ipsr={ipsr}" + (f" extended lspact={int(lspact)}" if decoded.extended else "")
    )
    return ExceptionTransitionResult(
        segment=target,
        pc=word_pc,
        sp=restored_sp,
        registers=registers,
        method="exception",
        source=source,
        value=value,
        bytes_read=frame_size,
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

EXC_RETURN_EXTENDED_HANDLER_MAIN = 0xFFFFFFE1

EXC_RETURN_EXTENDED_THREAD_MAIN = 0xFFFFFFE9

EXC_RETURN_EXTENDED_THREAD_PROCESS = 0xFFFFFFED

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
    """Decode an Armv7-M basic or non-secure FPU-extended return value."""
    if value in EXTENDED_EXC_RETURNS:
        return ExceptionReturn(
            value,
            "handler" if value == EXC_RETURN_EXTENDED_HANDLER_MAIN else "thread",
            "process" if value == EXC_RETURN_EXTENDED_THREAD_PROCESS else "main",
            extended=True,
        )
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
    """Decode an Armv7-M basic or FPU-extended frame using CCR.STKALIGN."""
    value = context.value & 0xFFFFFFFF
    if not armv7m_exception_return_candidate(value):
        return None
    decoded = decode_armv7m_exception_return(value)
    if isinstance(decoded, str):
        return decoded
    source = (
        "armv7m-extended-exception-frame"
        if decoded.extended
        else "armv7m-basic-exception-frame"
    )
    return _basic_exception_transition(
        context,
        decoded,
        exception_number_kind=armv7m_exception_number_kind,
        ipsr_mask=ARMV7M_IPSR_MASK,
        source=source,
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


# ---------------------------------------------------------------------------
# GDB-facing Arm live registers, SCB/VTOR/vector readers and handler setup
# ---------------------------------------------------------------------------

SCB_ICTR_ADDRESS = 0xE000E004
SCB_CPUID_ADDRESS = 0xE000ED00
SCB_VTOR_ADDRESS = 0xE000ED08
SCB_CCR_ADDRESS = 0xE000ED14

MAX_INFERRED_IRQ_STACK_BYTES = 256 * 1024
_STANDARD_STACK_TOP_SYMBOLS = (
    "__StackTop",
    "_estack",
    "__initial_sp",
    "__stack",
    "_stack_top",
)
_STANDARD_STACK_LOW_SYMBOLS = (
    "__StackLimit",
    "_sstack",
    "__stack_limit",
    "_stack_bottom",
)
_STANDARD_STACK_SECTIONS = (
    ".irq_stack",
    ".isr_stack",
    ".stack",
    ".main_stack",
    "STACK",
)
_STANDARD_VECTOR_BASE_SYMBOLS = (
    "g_pfnVectors",
    "__Vectors",
    "_vectors",
    "__vector_table",
    "vector_table",
)
_STANDARD_VECTOR_END_SYMBOLS = (
    "__Vectors_End",
    "_evectors",
    "__vector_table_end",
    "__isr_vector_end",
)
_STANDARD_VECTOR_SECTIONS = (
    ".isr_vector",
    ".vectors",
    ".vector_table",
)


@dataclass(frozen=True)
class ArmSnapshot:
    """Raw live Arm registers; scheduler stages read the same snapshot."""

    pc: int
    sp: int
    msp: int
    psp: int
    xpsr: int
    registers: dict[int, int]


def arm_snapshot(*, gdb_module: Any = None) -> tuple[ArmSnapshot | None, str]:
    """Read Arm live registers without rejecting an unset PSP.

    The ordinary live seed still refuses a zero PSP; the scheduler path needs
    the raw value so it can prove the SVC first-task restore itself.
    """
    active_gdb = gdb if gdb_module is None else gdb_module
    if active_gdb is None:
        return None, "live-registers-unavailable"
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc()) & 0xFFFFFFFF
        sp = int(frame.read_register("sp")) & 0xFFFFFFFF
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers: dict[int, int] = {}
    for register in range(13):
        try:
            registers[register] = int(frame.read_register(f"r{register}")) & 0xFFFFFFFF
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
            return None, "live-registers-unavailable"
    registers[13] = sp
    try:
        registers[14] = int(frame.read_register("lr")) & 0xFFFFFFFF
        registers[15] = pc
        xpsr = int(frame.read_register("xpsr")) & 0xFFFFFFFF
        msp = int(frame.read_register("msp")) & 0xFFFFFFFF
        psp = int(frame.read_register("psp")) & 0xFFFFFFFF
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "exception-registers-unavailable"
    return ArmSnapshot(pc, sp, msp, psp, xpsr, registers), "ok"


def arm_live_seed_detail(
    isa: str | None = None,
    *,
    root_pc: int | None = None,
    require_exception_registers: bool = True,
    gdb_module: Any = None,
    snapshot_reader: Callable[[], tuple[ArmSnapshot | None, str]] | None = None,
) -> tuple[SeedFrame | None, str]:
    """Read the selected Arm core's live registers without driving the target."""
    active_gdb = gdb if gdb_module is None else gdb_module
    if require_exception_registers:
        snapshot, reason = (
            snapshot_reader()
            if snapshot_reader is not None
            else arm_snapshot(gdb_module=active_gdb)
        )
        if snapshot is None:
            return None, reason
        exception_number = snapshot.xpsr & XPSR_IPSR_MAX_MASK
        if snapshot.msp <= 0 or snapshot.psp <= 0:
            return None, "live-registers-unavailable"
        if exception_number:
            if not snapshot.xpsr & XPSR_T_BIT:
                return None, "exception-frame-not-thumb"
            if isa is None:
                return None, "unsupported-exception-model"
            classifier = (
                armv6m_exception_number_kind
                if isa == "armv6-m"
                else armv7m_exception_number_kind
            )
            if (
                isa in ("armv6-m", "armv7-m", "armv7e-m")
                and classifier(exception_number) == "invalid"
            ):
                return None, "exception-number-invalid"
            if snapshot.sp != snapshot.msp:
                return None, "live-stack-selection-invalid"
        return (
            SeedFrame(
                pc=snapshot.pc & ~1,
                sp=snapshot.sp,
                regs=dict(snapshot.registers),
                source="current",
                root_pc=root_pc,
                process_sp=snapshot.psp,
                exception_number=exception_number,
            ),
            "ok",
        )
    if active_gdb is None:
        return None, "live-registers-unavailable"
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc())
        sp = int(frame.read_register("sp"))
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers: dict[int, int] = {}
    for register in range(13):
        try:
            registers[register] = int(frame.read_register(f"r{register}"))
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
            return None, "live-registers-unavailable"
    registers[13] = sp
    try:
        registers[14] = int(frame.read_register("lr"))
        registers[15] = pc
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    return (
        SeedFrame(
            pc=pc & ~1,
            sp=sp,
            regs=registers,
            source="current",
            root_pc=root_pc,
        ),
        "ok",
    )


def arm_ccr_stkalign(*, read: Reader = read_bytes) -> bool | None:
    """Read CCR.STKALIGN without writing target state."""
    raw = read(SCB_CCR_ADDRESS, 4)
    if raw is None or len(raw) != 4:
        return None
    return bool(int.from_bytes(raw, "little") & XPSR_ALIGN_BIT)


def arm_external_exception_limit(*, read: Reader = read_bytes) -> int | None:
    """Read ICTR and return the highest implemented external IPSR, or ``None``."""
    raw = read(SCB_ICTR_ADDRESS, 4)
    if raw is None or len(raw) != 4:
        return None
    return armv7m_max_external_exception(int.from_bytes(raw, "little"))


def arm_cpuid(*, read: Reader = read_bytes) -> int | None:
    """Read SCB.CPUID without writing target state."""
    raw = read(SCB_CPUID_ADDRESS, 4)
    if raw is None or len(raw) != 4:
        return None
    return int.from_bytes(raw, "little")


def armv6m_vector_entry(
    exception_number: int, *, read: Reader = read_bytes
) -> int | None:
    """Read a vector from an Armv6-M fixed-zero vector map, never VTOR."""
    if not 0 < exception_number <= 47 or armv6m_exception_number_kind(
        exception_number
    ) in ("thread", "invalid"):
        return None
    raw = read(4 * exception_number, 4)
    if raw is None or len(raw) != 4:
        return None
    value = int.from_bytes(raw, "little")
    return value & ~1 if value & 1 else None


def armv7m_vector_entry(
    exception_number: int, *, read: Reader = read_bytes
) -> int | None:
    """Return the current VTOR-indexed vector target for one exception number."""
    raw_vtor = read(SCB_VTOR_ADDRESS, 4)
    if raw_vtor is None or len(raw_vtor) != 4 or exception_number <= 0:
        return None
    vtor = int.from_bytes(raw_vtor, "little")
    raw_entry = read(vtor + 4 * exception_number, 4)
    if raw_entry is None or len(raw_entry) != 4:
        return None
    entry = int.from_bytes(raw_entry, "little")
    return entry & ~1 if entry else None


def arm_vector_initial_msp(
    isa: str | None = "armv7-m", *, read: Reader = read_bytes
) -> int | None:
    """Read word 0 (initial MSP) from the active Arm M-profile vector table."""
    if isa == "armv6-m":
        vtor = 0
    elif isa in ("armv7-m", "armv7e-m", "armv8-m.mainline"):
        raw_vtor = read(SCB_VTOR_ADDRESS, 4)
        if raw_vtor is None or len(raw_vtor) != 4:
            return None
        vtor = int.from_bytes(raw_vtor, "little")
        if vtor & 0x7F:
            return None
    else:
        return None
    raw_msp = read(vtor, 4)
    if raw_msp is None or len(raw_msp) != 4:
        return None
    initial_msp = int.from_bytes(raw_msp, "little")
    if initial_msp <= 0 or initial_msp & 0x3:
        return None
    return initial_msp


def resolve_arm_irq_stack_bounds_detail(
    *,
    symbol_lookup: Callable[[str], int | None] = linker_symbol_address,
    read: Reader = read_bytes,
    isa: str | None = "armv7-m",
    elf: Any = None,
    live_msp: int | None = None,
    task_bounds: tuple[int, int] | None = None,
) -> tuple[int, int, str] | None:
    """Resolve the Arm M-profile interrupt stack ``(low, high, source)``.

    Resolution tiers:
    1. Explicit ``__gdr_irq_stack_low`` and ``__gdr_irq_stack_top`` override
       (fails closed when only one of the pair is present).
    2. Standard CMSIS/toolchain stack-bottom and stack-top linker symbols.
    3. Dedicated ELF stack section (``.irq_stack``, ``.isr_stack``, ``.stack``,
       ``.main_stack``, ``STACK``).
    4. Architectural initial MSP (from a standard top symbol or Vector Table
       Word 0) paired with a standard low symbol or a non-overlapping halted
       ``live_msp`` lower bound.
    """

    def _safe_read(address: int, size: int) -> bytes | None:
        try:
            return read(address, size)
        except RuntimeError:
            return None

    def _lookup(name: str) -> int | None:
        try:
            value = symbol_lookup(name)
        except RuntimeError:
            value = None
        if value is not None:
            return value
        if elf is not None and callable(getattr(elf, "symbol_address", None)):
            return elf.symbol_address(name)
        return None

    gdr_low = _lookup("__gdr_irq_stack_low")
    gdr_high = _lookup("__gdr_irq_stack_top")
    if gdr_low is not None or gdr_high is not None:
        if gdr_low is None or gdr_high is None or gdr_low <= 0 or gdr_high <= gdr_low:
            return None
        return (
            gdr_low,
            gdr_high,
            "linker:__gdr_irq_stack_low..__gdr_irq_stack_top",
        )

    top_name: str | None = None
    top_addr: int | None = None
    for name in _STANDARD_STACK_TOP_SYMBOLS:
        candidate = _lookup(name)
        if candidate is not None and candidate > 0 and candidate & 0x3 == 0:
            top_name, top_addr = name, candidate
            break

    low_name: str | None = None
    low_addr: int | None = None
    for name in _STANDARD_STACK_LOW_SYMBOLS:
        candidate = _lookup(name)
        if candidate is not None and candidate > 0 and candidate & 0x3 == 0:
            low_name, low_addr = name, candidate
            break

    if low_addr is not None and top_addr is not None and low_addr < top_addr:
        return low_addr, top_addr, f"linker:{low_name}..{top_name}"

    if elf is not None and callable(getattr(elf, "section_bounds", None)):
        for sec_name in _STANDARD_STACK_SECTIONS:
            bounds = elf.section_bounds(sec_name)
            if bounds is None:
                continue
            sec_low, sec_high = bounds
            if (
                0 < sec_low < sec_high
                and sec_low & 0x3 == 0
                and sec_high & 0x3 == 0
                and (top_addr is None or sec_high == top_addr)
            ):
                return sec_low, sec_high, f"section:{sec_name}"

    if top_addr is not None and top_name is not None:
        high_addr: int | None = top_addr
        high_label = top_name
    else:
        high_addr = arm_vector_initial_msp(isa or "armv7-m", read=_safe_read)
        high_label = "vector[0]"
    if high_addr is None or high_addr <= 0 or high_addr & 0x3:
        return None

    if low_addr is not None and low_name is not None and 0 < low_addr < high_addr:
        return low_addr, high_addr, f"linker:{low_name}..{high_label}"

    if (
        live_msp is not None
        and 0 < live_msp < high_addr
        and live_msp & 0x3 == 0
        and high_addr - live_msp <= MAX_INFERRED_IRQ_STACK_BYTES
    ):
        if task_bounds is not None:
            task_low, task_high = task_bounds
            if not (task_high <= live_msp or task_low >= high_addr):
                return None
        return live_msp, high_addr, f"msp:live..{high_label}"

    return None


def resolve_arm_irq_stack_bounds(
    *,
    symbol_lookup: Callable[[str], int | None] = linker_symbol_address,
    read: Reader = read_bytes,
    isa: str | None = "armv7-m",
    elf: Any = None,
    live_msp: int | None = None,
    task_bounds: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    """Return the resolved ``(low, high)`` Arm interrupt stack span, or ``None``."""
    detail = resolve_arm_irq_stack_bounds_detail(
        symbol_lookup=symbol_lookup,
        read=read,
        isa=isa,
        elf=elf,
        live_msp=live_msp,
        task_bounds=task_bounds,
    )
    return (detail[0], detail[1]) if detail is not None else None


def resolve_armv6m_vector_limit(
    *,
    symbol_lookup: Callable[[str], int | None] = linker_symbol_address,
    read: Reader = read_bytes,
    elf: Any = None,
) -> int | None:
    """Verify the Armv6-M fixed-zero vector map and return max external IPSR.

    Resolution tiers:
    1. Explicit ``__gdr_vector_table_end`` and ``__gdr_armv6m_irq_count``
       override (fails closed when partial or inconsistent).
    2. Standard vector-end symbol (``__Vectors_End``, ``_evectors``), dedicated
       vector section size at ``0x0`` (``.isr_vector``, ``.vectors``), or the
       first executable function symbol immediately following the vector table.
    """

    def _safe_read(address: int, size: int) -> bytes | None:
        try:
            return read(address, size)
        except RuntimeError:
            return None

    def _lookup(name: str) -> int | None:
        try:
            value = symbol_lookup(name)
        except RuntimeError:
            value = None
        if value is not None:
            return value
        if elf is not None and callable(getattr(elf, "symbol_address", None)):
            return elf.symbol_address(name)
        return None

    cpuid = arm_cpuid(read=_safe_read)
    if cpuid is None or cpuid & 0xFF0FFFF0 != 0x410CC200:
        return None

    gdr_end = _lookup("__gdr_vector_table_end")
    gdr_count = _lookup("__gdr_armv6m_irq_count")
    if gdr_end is not None or gdr_count is not None:
        base = _lookup("g_pfnVectors")
        if (
            base != 0
            or gdr_count is None
            or not 1 <= gdr_count <= 32
            or gdr_end != (16 + gdr_count) * 4
        ):
            return None
        return 16 + gdr_count - 1

    has_zero_base = False
    for name in _STANDARD_VECTOR_BASE_SYMBOLS:
        addr = _lookup(name)
        if addr is not None:
            if addr != 0:
                return None
            has_zero_base = True

    sec_size: int | None = None
    if elf is not None and callable(getattr(elf, "section_bounds", None)):
        for sec_name in _STANDARD_VECTOR_SECTIONS:
            bounds = elf.section_bounds(sec_name)
            if bounds is None:
                continue
            sec_low, sec_high = bounds
            if sec_low != 0:
                return None
            has_zero_base = True
            sec_size = sec_high
            break

    if not has_zero_base:
        return None

    for name in _STANDARD_VECTOR_END_SYMBOLS:
        end_addr = _lookup(name)
        if end_addr is not None:
            if end_addr & 0x3 or not 17 * 4 <= end_addr <= 48 * 4:
                return None
            return (end_addr // 4) - 1

    if sec_size is not None and sec_size & 0x3 == 0 and 17 * 4 <= sec_size <= 48 * 4:
        return (sec_size // 4) - 1

    symbols = getattr(elf, "symbols", ()) if elf is not None else ()
    first_code = min(
        (sym.start for sym in symbols if getattr(sym, "start", 0) > 0),
        default=None,
    )
    if (
        first_code is not None
        and first_code & 0x3 == 0
        and 17 * 4 <= first_code <= 48 * 4
    ):
        return (first_code // 4) - 1

    return None


@dataclass(frozen=True)
class ArmHandlerSetup:
    """Validated Armv6-M/Armv7-M handler-mode unwind configuration."""

    seed: SeedFrame
    exception_segment: StackSegment
    stkalign: bool
    exception_detector: ExceptionDetector
    exception_hook: ExceptionHook
    exception_guard: TransitionGuard


def prepare_arm_handler_unwind(
    seed: SeedFrame,
    isa: str | None,
    *,
    scheduler_running: Callable[[], bool | None],
    irq_stack_bounds: Callable[[], tuple[int, int] | tuple[int, int, str] | None],
    irq_stack_source: str = "linker:__gdr_irq_stack_low..__gdr_irq_stack_top",
    capacity_reader: Callable[[], int | None],
    stkalign_reader: Callable[[], bool | None],
    vector_entry_reader: Callable[[int], int | None],
    guard_fn: Callable[[int, int, str], str | None] = arm_exception_guard,
    capacity_guard_factory: Callable[
        [int, str], TransitionGuard
    ] = arm_capacity_exception_guard,
) -> ArmHandlerSetup | tuple[str, str]:
    """Validate an Arm handler-mode seed and build its segment and hooks.

    Returns :class:`ArmHandlerSetup` when all architectural and port invariants
    hold, or ``(stop_reason, message)`` when the stop must be refused.
    """
    if isa not in ("armv6-m", "armv7-m", "armv7e-m") or seed.exception_number is None:
        return (
            "unsupported-exception-model",
            "handler-mode seeds require a verified Armv6-M/Armv7-M/Armv7E-M model",
        )
    guard_reason = guard_fn(seed.exception_number, seed.pc, isa)
    if guard_reason == "scheduler-transition-unproven":
        return (
            guard_reason,
            "the stop is inside the scheduler's SVC/PendSV transition path",
        )
    if guard_reason == "exception-frame-type-unproven":
        return (
            guard_reason,
            "a synchronous fault frame does not name a proven resume PC",
        )
    if guard_reason == "exception-number-unverified":
        return (
            guard_reason,
            "the live exception number is architecturally valid but "
            "outside the verified external-IRQ proof scope",
        )
    if guard_reason == "exception-number-invalid":
        return (
            guard_reason,
            "the live IPSR names no architecturally valid exception",
        )
    running = scheduler_running()
    if running is None:
        return (
            "scheduler-state-unavailable",
            "scheduler ownership is not readable; the task owner is unproven",
        )
    if not running:
        return (
            "scheduler-not-running",
            "handler mode without a running scheduler has no task owner",
        )
    bounds = irq_stack_bounds()
    if bounds is None:
        return (
            "exception-stack-bounds-unavailable",
            "the interrupt stack span could not be resolved from symbols, "
            "ELF sections, or vector-table initial MSP",
        )
    if len(bounds) == 3:
        irq_low, irq_high, resolved_source = bounds
    else:
        irq_low, irq_high = bounds
        resolved_source = irq_stack_source
    if not irq_low <= seed.sp <= irq_high:
        return (
            "exception-stack-outside-bounds",
            "the live MSP is outside the resolved interrupt stack span",
        )
    capacity = capacity_reader()
    if capacity is None:
        return (
            "exception-capacity-unavailable",
            "the target's implemented external interrupt capacity is not readable",
        )
    if seed.exception_number >= 16 and seed.exception_number > capacity:
        return (
            "exception-number-unimplemented",
            "the live IPSR names an external exception beyond the target's "
            "implemented interrupt capacity",
        )
    alignment = True if isa == "armv6-m" else stkalign_reader()
    if alignment is None:
        return (
            "exception-alignment-unavailable",
            "CCR.STKALIGN is not readable; the frame layout is unproven",
        )
    exception_segment = StackSegment(
        ident=f"exception:{seed.exception_number}",
        owner=f"exception:{seed.exception_number}",
        low=irq_low,
        high=irq_high,
        source=resolved_source,
        stack="main",
        kind="exception",
    )
    entry = vector_entry_reader(seed.exception_number)
    if entry is not None and entry == seed.pc:
        seed = replace(seed, at_exception_entry=True)
    detector = (
        armv6m_exception_return_candidate
        if isa == "armv6-m"
        else armv7m_exception_return_candidate
    )
    hook = (
        armv6m_exception_transition if isa == "armv6-m" else armv7m_exception_transition
    )
    return ArmHandlerSetup(
        seed=seed,
        exception_segment=exception_segment,
        stkalign=alignment,
        exception_detector=detector,
        exception_hook=hook,
        exception_guard=capacity_guard_factory(capacity, isa),
    )
