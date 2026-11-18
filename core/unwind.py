"""Bounded, read-only task stack unwinding over ELF unwind metadata (pure).

The driver consumes :mod:`core.unwind_ehabi` and :mod:`core.unwind_cfi` tables and
uses instruction-proven leaf returns or a validated frame-pointer chain only
for clean uncovered PCs. It never calls an inferior function and never writes target
memory.  Every ordinary frame preserves or advances SP inside the current stack
segment; crossing to another segment requires an architecture-owned exception
transition proof.  Each walk is bounded by a maximum depth, and a malformed or
unsupported record becomes a stop reason, never a guessed register value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .cpus import RISCV_CONVENTION, convention_for_arch
from .gdb_bridge import loaded_object_path
from .unwind_cfi import (
    CfiRow,
    CfiTable,
    EncodedPointerContext,
    RegisterRule,
    parse_cfi_section,
)
from .unwind_ehabi import EhabiTable, parse_ehabi, unwind_ehabi
from .unwind_expr import evaluate_cfi_expression
from .unwind_leaf import prove_leaf_return
from .unwind_meta import (
    EM_RISCV,
    MAX_FRAME_READ,
    MAX_FRAMES,
    Arch,
    ElfImage,
    Endian,
    Method,
    Reader,
)
from .unwind_segments import (
    MAX_EXCEPTION_DEPTH,
    MAX_EXCEPTION_READ_BYTES,
    ExceptionBudget,
    ExceptionDetector,
    ExceptionHook,
    ExceptionTransitionContext,
    ExceptionTransitionResult,
    SegmentTransition,
    StackSegment,
    TransitionGuard,
)

# ---------------------------------------------------------------------------
# Unwind driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedFrame:
    """The first frame of a task: registers decoded by the RTOS adapter.

    ``process_sp``, ``exception_number`` and ``at_exception_entry`` describe a
    live handler seed; a saved task seed leaves them unset.
    """

    pc: int
    sp: int
    regs: Mapping[int, int]
    source: str = "saved"
    root_pc: int | None = None
    root_reason: str = "task-entry"
    process_sp: int | None = None
    exception_number: int | None = None
    at_exception_entry: bool = False


@dataclass(frozen=True)
class UnwindFrame:
    """One recovered frame; ``method`` names the evidence that produced it."""

    index: int
    pc: int
    method: Method
    lookup_pc: int | None = None
    segment: str | None = None
    evidence: tuple[str, ...] = ()

    @property
    def address_in_block(self) -> int:
        """Return the metadata/symbol address, retaining the raw resume PC."""
        return self.pc if self.lookup_pc is None else self.lookup_pc


@dataclass(frozen=True)
class UnwindResult:
    """A bounded backtrace and the exact reason it stopped."""

    frames: tuple[UnwindFrame, ...]
    stop_reason: str
    complete: bool
    truncated: bool
    seed_source: str
    method: str
    segments: tuple[StackSegment, ...] = ()
    transitions: tuple[SegmentTransition, ...] = ()


@dataclass(frozen=True)
class UnwindMetadata:
    """All metadata sources for one ELF image, in preference order."""

    elf: ElfImage
    arch: Arch
    cfi_tables: tuple[CfiTable, ...]
    ehabi: EhabiTable | None
    return_register: int
    frame_pointer_register: int | None

    @classmethod
    def from_elf(
        cls,
        elf: ElfImage,
        *,
        pointer_context: EncodedPointerContext | None = None,
    ) -> UnwindMetadata:
        """Build every available source; an absent source stays ``None``.

        ``pointer_context`` is only passed through to CFI parsing; the ELF
        sections do not provide the ABI's ``.got``/``.eh_frame_hdr`` data base,
        so non-PC-relative encodings stay unavailable unless the caller
        supplies explicit bases.
        """
        arch: Arch = "riscv" if elf.machine == EM_RISCV else "arm"
        convention = convention_for_arch(arch)
        cfi_tables: list[CfiTable] = []
        for name, is_eh in ((".eh_frame", True), (".debug_frame", False)):
            section = elf.section(name)
            if section is None or not section.data:
                continue
            hdr_source, hdr_error = elf.eh_frame_hdr_source() if is_eh else (None, None)
            cfi_tables.append(
                parse_cfi_section(
                    section.data,
                    section_address=section.address,
                    address_size=elf.pointer_size,
                    endian=elf.endian,
                    is_eh_frame=is_eh,
                    pointer_context=pointer_context,
                    eh_frame_hdr=hdr_source,
                    eh_frame_hdr_error=hdr_error,
                )
            )
        ehabi = None
        exidx = elf.section(".ARM.exidx")
        if arch == "arm" and exidx is not None and exidx.data:
            ehabi = parse_ehabi(
                exidx.data,
                section_address=exidx.address,
                read_at=elf.read_at,
                text_end=elf.code_end(),
                endian=elf.endian,
            )
        return cls(
            elf=elf,
            arch=arch,
            cfi_tables=tuple(cfi_tables),
            ehabi=ehabi,
            return_register=convention.return_register,
            frame_pointer_register=convention.frame_pointer_register,
        )

    def available(self) -> bool:
        """Whether any table can cover a PC (frame pointers may still work)."""
        return bool(self.ehabi) or any(self.cfi_tables)


def _read_value(
    read: Reader,
    address: int,
    size: int,
    endian: Endian,
    *,
    stack_low: int | None,
    stack_high: int | None,
) -> int | None:
    if size <= 0 or size > 8 or address <= 0:
        return None
    if stack_low is not None and address < stack_low:
        return None
    if stack_high is not None and address + size > stack_high:
        return None
    raw = read(address, size)
    if raw is None or len(raw) != size:
        return None
    return int.from_bytes(raw, endian)


class _RecoveredRegisters(dict[int, int]):
    """Keep unavailable-register reasons outside the readable register values."""

    def __init__(self, regs: Mapping[int, int]) -> None:
        super().__init__(regs)
        self.unavailable: dict[int, str] = (
            dict(regs.unavailable) if isinstance(regs, _RecoveredRegisters) else {}
        )


def _register_failure(regs: Mapping[int, int], register: int, default: str) -> str:
    if isinstance(regs, _RecoveredRegisters):
        return regs.unavailable.get(register, default)
    return default


def _rule_value(
    rule: RegisterRule,
    *,
    cfa: int,
    regs: Mapping[int, int],
    read: Reader,
    ptr_size: int,
    endian: Endian,
    stack_low: int | None,
    stack_high: int | None,
    register_count: int | None = None,
) -> tuple[int | None, str | None]:
    if rule.kind == "same":
        return None, None
    if rule.kind == "undefined":
        return None, "cfi-register-undefined"
    if rule.kind == "register":
        source = rule.register
        if source is None or source not in regs:
            if source is not None:
                return None, _register_failure(
                    regs, source, "cfi-register-source-missing"
                )
            return None, "cfi-register-source-missing"
        return regs[source], None
    if rule.kind in ("expression", "val_expression"):
        if rule.expression is None:
            return None, "cfi-expression-missing"
        result = evaluate_cfi_expression(
            rule.expression,
            registers=regs,
            initial_stack=(cfa,),
            read=read,
            address_size=ptr_size,
            endian=endian,
            purpose="value" if rule.kind == "val_expression" else "address",
            stack_low=stack_low,
            stack_high=stack_high,
            register_limit=register_count,
            register_failure=lambda register: _register_failure(
                regs, register, "cfi-expression-register-missing"
            ),
        )
        if isinstance(result, str):
            return None, result
        if rule.kind == "val_expression":
            return result, None
        value = _read_value(
            read,
            result,
            ptr_size,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        if value is None:
            return None, "cfi-memory-error"
        return value, None
    if rule.kind == "val_offset":
        return cfa + rule.value, None
    value = _read_value(
        read,
        cfa + rule.value,
        ptr_size,
        endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )
    if value is None:
        return None, "cfi-memory-error"
    return value, None


def _unwind_cfi(
    row: CfiRow,
    *,
    regs: Mapping[int, int],
    sp: int,
    read: Reader,
    ptr_size: int,
    endian: Endian,
    return_register: int,
    sp_register: int,
    stack_low: int | None,
    stack_high: int | None,
) -> tuple[dict[int, int], int, int] | str:
    if row.error is not None:
        return row.error
    return_register = (
        row.return_register if row.return_register is not None else return_register
    )
    register_count = 16 if sp_register == 13 else 32
    return_rule = row.registers.get(return_register)
    if (
        return_register < 0
        or return_register == sp_register
        or (
            return_register >= register_count
            and (return_rule is None or return_rule.kind == "same")
        )
    ):
        return "cfi-return-register-unsupported"
    if return_rule is not None and return_rule.kind == "undefined":
        return "cfi-return-address-undefined"
    if row.cfa.expression is None:
        if row.cfa.register is None:
            return "cfi-no-cfa"
        if not 0 <= row.cfa.register < register_count:
            return "cfi-cfa-register-unsupported"
    elif row.cfa.register is not None or row.cfa.offset:
        return "cfi-cfa-rule-invalid"
    for register, rule in row.registers.items():
        if (not 0 <= register < register_count and register != return_register) or (
            rule.register is not None and not 0 <= rule.register < register_count
        ):
            return "cfi-register-unsupported"
    if row.cfa.expression is not None:
        cfa_result = evaluate_cfi_expression(
            row.cfa.expression,
            registers=regs,
            read=read,
            address_size=ptr_size,
            endian=endian,
            purpose="cfa",
            stack_low=stack_low,
            stack_high=stack_high,
            register_limit=register_count,
            register_failure=lambda register: _register_failure(
                regs, register, "cfi-expression-register-missing"
            ),
        )
        if isinstance(cfa_result, str):
            return cfa_result
        cfa = cfa_result
    else:
        cfa_register = row.cfa.register
        if cfa_register is None:
            return "cfi-no-cfa"
        cfa_base = regs.get(cfa_register)
        if cfa_base is None:
            return _register_failure(regs, cfa_register, "cfi-cfa-register-missing")
        cfa = cfa_base + row.cfa.offset
    if cfa < sp:
        return "cfi-no-progress"
    if stack_low is not None and cfa < stack_low:
        return "cfi-stack-underflow"
    if stack_high is not None and cfa > stack_high:
        return "cfi-stack-overflow"
    new_regs = _RecoveredRegisters(regs)
    for register, rule in row.registers.items():
        value, failure = _rule_value(
            rule,
            cfa=cfa,
            regs=regs,
            read=read,
            ptr_size=ptr_size,
            endian=endian,
            stack_low=stack_low,
            stack_high=stack_high,
            register_count=register_count,
        )
        if failure is not None:
            if register == return_register:
                return failure
            new_regs.pop(register, None)
            new_regs.unavailable[register] = failure
            continue
        if value is not None:
            new_regs[register] = value
            new_regs.unavailable.pop(register, None)
    return_rule = row.registers.get(return_register)
    if return_rule is None or return_rule.kind == "same":
        caller_pc = regs.get(return_register)
    else:
        caller_pc, failure = _rule_value(
            return_rule,
            cfa=cfa,
            regs=regs,
            read=read,
            ptr_size=ptr_size,
            endian=endian,
            stack_low=stack_low,
            stack_high=stack_high,
            register_count=register_count,
        )
        if failure is not None:
            return failure
    if caller_pc is None:
        return _register_failure(regs, return_register, "cfi-return-address-missing")
    if return_register < register_count:
        new_regs[return_register] = caller_pc
    else:
        new_regs.pop(return_register, None)
    new_regs[sp_register] = cfa
    new_regs.unavailable.pop(return_register, None)
    new_regs.unavailable.pop(sp_register, None)
    return new_regs, cfa, caller_pc


def _unwind_frame_pointer(
    *,
    arch: Arch,
    regs: Mapping[int, int],
    sp: int,
    read: Reader,
    ptr_size: int,
    endian: Endian,
    frame_pointer_register: int | None,
    stack_low: int | None,
    stack_high: int | None,
) -> tuple[dict[int, int], int, int] | str:
    if frame_pointer_register is None:
        return "fp-register-missing"
    if frame_pointer_register not in regs:
        return _register_failure(regs, frame_pointer_register, "fp-register-missing")
    frame_pointer = regs[frame_pointer_register]
    if frame_pointer <= 0 or frame_pointer % ptr_size != 0:
        return "fp-misaligned"
    if stack_low is not None and frame_pointer < stack_low:
        return "fp-stack-underflow"
    if stack_high is not None and frame_pointer + ptr_size * 2 > stack_high:
        return "fp-stack-overflow"
    if arch == "riscv":
        if frame_pointer <= sp:
            return "fp-no-progress"
        caller_pc = _read_value(
            read,
            frame_pointer - ptr_size,
            ptr_size,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        caller_fp = _read_value(
            read,
            frame_pointer - 2 * ptr_size,
            ptr_size,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        caller_sp = frame_pointer
    else:
        if frame_pointer < sp:
            return "fp-below-sp"
        caller_fp = _read_value(
            read,
            frame_pointer,
            ptr_size,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        caller_pc = _read_value(
            read,
            frame_pointer + ptr_size,
            ptr_size,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        caller_sp = frame_pointer + 2 * ptr_size
    if not caller_pc or caller_fp is None:
        return "fp-memory-error"
    if caller_sp <= sp:
        return "fp-no-progress"
    convention = convention_for_arch(arch)
    new_regs = _RecoveredRegisters(regs)
    new_regs[frame_pointer_register] = caller_fp
    new_regs[convention.sp_register] = caller_sp
    new_regs.unavailable.pop(frame_pointer_register, None)
    new_regs.unavailable.pop(convention.sp_register, None)
    return new_regs, caller_sp, caller_pc


def unwind_chain(
    seed: SeedFrame,
    metadata: UnwindMetadata | None,
    *,
    read: Reader,
    max_depth: int = 32,
    stack_low: int | None = None,
    stack_high: int | None = None,
    allow_frame_pointer: bool = True,
    is_code: Callable[[int], bool] | None = None,
    segments: Sequence[StackSegment] | None = None,
    current_segment: str | None = None,
    exception_detector: ExceptionDetector | None = None,
    exception_hook: ExceptionHook | None = None,
    exception_guard: TransitionGuard | None = None,
    max_exception_depth: int = MAX_EXCEPTION_DEPTH,
    max_exception_bytes: int = MAX_EXCEPTION_READ_BYTES,
    stkalign: bool = True,
) -> UnwindResult:
    """Walk a seed frame through metadata, bounded by *max_depth*.

    The first frame is the seed exactly as the RTOS adapter decoded it.  Each
    further frame must either stay inside the current stack segment or cross
    only through an architecture hook that proves and decodes an exception
    frame.  Ordinary steps must not decrease the stack pointer, must stay
    inside the current segment and be produced by metadata, an instruction
    proof or a validated frame-pointer record. Repeated segment/PC/SP triples are loops.
    ``complete`` requires the adapter's independently supplied root or exit
    sentinel; other termination is partial.

    Args:
        seed: Decoded PC/SP/registers and the optional task entry.
        metadata: Parsed ELF tables; ``None`` permits only a frame-pointer walk.
        read: Bounded target-memory reader ``(address, size) -> bytes|None``.
        max_depth: Maximum returned frames, clamped to ``1..MAX_FRAMES``.
        stack_low: Lowest valid stack byte, or ``None`` when unknown.
        stack_high: One-past-highest valid stack byte, or ``None``.
        allow_frame_pointer: Permit the validated frame-pointer fallback.
        is_code: Executable-address predicate; defaults to the ELF text ranges.
        segments: Optional typed stack segments.  When present, the segment
            bounds replace *stack_low*/*stack_high* for ordinary steps.
        current_segment: Identity of the seed's segment in *segments*.
        exception_detector: Whether a raw return value is an exception-return
            candidate; no candidate is ever decoded without this hook.
        exception_hook: Architecture-owned proof/decoder for one transition.
        exception_guard: Optional policy hook returning a stop reason for a
            proven interrupted exception number (the adapter decides which
            exception contexts the driver must not interpret further).
        max_exception_depth: Independent maximum number of crossed frames.
        max_exception_bytes: Independent budget for exception-frame reads.
        stkalign: Whether the target enforces 8-byte exception stack alignment.

    Returns:
        Frames, the exact stop reason, completeness, truncation state and the
        typed segment/transition evidence used by the walk.
    """
    if max_depth < 1:
        max_depth = 1
    max_depth = min(max_depth, MAX_FRAMES)
    arch: Arch = metadata.arch if metadata is not None else "arm"
    convention = convention_for_arch(arch)
    ptr_size = metadata.elf.pointer_size if metadata is not None else 4
    endian: Endian = metadata.elf.endian if metadata is not None else "little"
    code = is_code or (metadata.elf.is_code if metadata is not None else None)
    if code is None:
        # Without an ELF text range there is no proof that a recovered return
        # address is executable; refuse further frames rather than guessing.
        code = lambda _address: False  # noqa: E731
    fp_register = (
        metadata.frame_pointer_register
        if metadata is not None
        else convention.frame_pointer_register
    )
    return_register = (
        metadata.return_register if metadata is not None else convention.return_register
    )
    sp_register = convention.sp_register

    segment_index: dict[str, StackSegment] = {}
    current: StackSegment | None = None
    if segments is not None:
        for segment in segments:
            if segment.ident in segment_index:
                return UnwindResult(
                    (), "segment-duplicate", False, False, seed.source, seed.source
                )
            segment_index[segment.ident] = segment
        if current_segment is None:
            return UnwindResult(
                (), "segment-unknown", False, False, seed.source, seed.source
            )
        current = segment_index.get(current_segment)
        if current is None:
            return UnwindResult(
                (), "segment-unknown", False, False, seed.source, seed.source
            )
    low = current.low if current is not None else stack_low
    high = current.high if current is not None else stack_high
    budget = ExceptionBudget(
        max_depth=max(0, max_exception_depth),
        max_bytes=max(0, max_exception_bytes),
    )
    transitions: list[SegmentTransition] = []
    process_sp = seed.process_sp

    def _result(
        frames: list[UnwindFrame],
        reason: str,
        complete: bool,
        truncated: bool,
        method: str,
    ) -> UnwindResult:
        return UnwindResult(
            tuple(frames),
            reason,
            complete,
            truncated,
            seed.source,
            method,
            tuple(segment_index.values()),
            tuple(transitions),
        )

    def _apply_transition(
        raw_pc: int,
        source_regs: Mapping[int, int],
        frame_sp: int,
        source_segment: StackSegment | None,
    ) -> ExceptionTransitionResult | str | None:
        if (
            source_segment is None
            or exception_detector is None
            or exception_hook is None
        ):
            return None
        if not exception_detector(raw_pc):
            return None
        if budget.depth >= budget.max_depth:
            return "exception-depth-exceeded"
        context = ExceptionTransitionContext(
            value=raw_pc,
            registers=source_regs,
            frame_sp=frame_sp,
            current_segment=source_segment,
            segments=segment_index,
            process_sp=process_sp,
            read=read,
            stkalign=stkalign,
            remaining_exception_bytes=budget.remaining_bytes,
            guard=exception_guard,
        )
        result = exception_hook(context)
        if result is None:
            return "exception-context-unproven"
        if isinstance(result, str):
            return result
        if result.bytes_read < 0 or result.bytes_read > budget.remaining_bytes:
            return "exception-read-budget-exceeded"
        if result.segment.low is None or result.segment.high is None:
            return "exception-target-bounds-unavailable"
        if not result.segment.contains(result.sp, 0):
            return "exception-target-outside-bounds"
        if result.pc <= 0 or not code(result.pc):
            return "exception-frame-pc-outside-code"
        existing = segment_index.get(result.segment.ident)
        if existing is not None and (
            existing.low != result.segment.low
            or existing.high != result.segment.high
            or existing.owner != result.segment.owner
            or existing.stack != result.segment.stack
            or existing.source != result.segment.source
            or existing.kind != result.segment.kind
        ):
            return "exception-context-conflict"
        budget.depth += 1
        budget.bytes += result.bytes_read
        segment_index[result.segment.ident] = result.segment
        return result

    pc = convention.normalize_pc(seed.pc)
    sp = seed.sp
    regs = dict(seed.regs)
    if pc <= 0:
        return _result([], "seed-pc-invalid", False, False, seed.source)
    if low is not None and sp < low:
        return _result([], "seed-stack-underflow", False, False, seed.source)
    if high is not None and sp > high:
        return _result([], "seed-stack-overflow", False, False, seed.source)
    if metadata is not None and not metadata.elf.is_code(pc):
        return _result([], "seed-pc-outside-code", False, False, seed.source)
    if convention.pc_register is not None:
        regs[convention.pc_register] = pc
    regs[sp_register] = sp
    frames: list[UnwindFrame] = [
        UnwindFrame(
            0,
            pc,
            "seed",
            segment=current.ident if current is not None else None,
        )
    ]
    lookup_pc = pc
    segment_key = current.ident if current is not None else ""
    seen: set[tuple[str, int, int]] = {(segment_key, pc, sp)}
    method = seed.source
    stop_reason = "no-unwind-info"
    complete = False
    truncated = False
    root_pc = (
        convention.normalize_pc(seed.root_pc) if seed.root_pc is not None else None
    )
    if root_pc is not None and pc == root_pc:
        return _result(frames, seed.root_reason, True, False, method)

    next_index = 1
    if seed.at_exception_entry and current is not None and max_depth > 1:
        raw_entry = regs.get(return_register)
        if exception_detector is None or exception_hook is None:
            return _result(frames, "exception-return-unproven", False, False, method)
        if raw_entry is None or not exception_detector(raw_entry):
            if exception_detector(pc):
                raw_entry = pc
            else:
                return _result(
                    frames, "exception-return-unproven", False, False, method
                )
        assert raw_entry is not None
        transition = _apply_transition(raw_entry, regs, sp, current)
        if isinstance(transition, str):
            return _result(frames, transition, False, False, method)
        if transition is not None:
            key = (transition.segment.ident, transition.pc, transition.sp)
            if key in seen:
                return _result(frames, "loop", False, False, method)
            seen.add(key)
            frames.append(
                UnwindFrame(
                    1,
                    transition.pc,
                    transition.method,
                    transition.pc,
                    segment=transition.segment.ident,
                )
            )
            transitions.append(
                SegmentTransition(
                    from_segment=current.ident,
                    to_segment=transition.segment.ident,
                    method=transition.method,
                    source=transition.source,
                    pc=transition.pc,
                    sp=transition.sp,
                    value=transition.value,
                    exact_pc=transition.exact_pc,
                    evidence=transition.evidence,
                )
            )
            recovered_entry = _RecoveredRegisters(transition.registers)
            if isinstance(regs, _RecoveredRegisters):
                for register, reason in regs.unavailable.items():
                    if register not in recovered_entry:
                        recovered_entry.unavailable[register] = reason
            regs = recovered_entry
            regs[sp_register] = transition.sp
            regs.unavailable.pop(sp_register, None)
            if convention.pc_register is not None:
                regs[convention.pc_register] = transition.pc
                regs.unavailable.pop(convention.pc_register, None)
            current = transition.segment
            pc = transition.pc
            lookup_pc = transition.pc
            sp = transition.sp
            method = transition.method
            next_index = 2
            if root_pc is not None and pc == root_pc:
                return _result(frames, seed.root_reason, True, False, method)

    for index in range(next_index, max_depth):
        low = current.low if current is not None else stack_low
        high = current.high if current is not None else stack_high
        attempts: list[str] = []
        has_metadata = False
        ehabi_unexecuted_prologue = False
        parse_failures: list[str] = []
        exact_caller_pc = False
        frame_evidence: tuple[str, ...] = ()
        new_regs: Any = regs
        new_sp = sp
        new_pc = pc
        step_method: Method = "cfi"
        if metadata is not None and metadata.ehabi is not None:
            symbol = metadata.elf.function_containing(lookup_pc)
            entry = metadata.ehabi.entry_for(lookup_pc)
            if (
                entry is not None
                and symbol is not None
                and (entry.start & ~1) == symbol.start
            ):
                has_metadata = True
                outcome = unwind_ehabi(
                    entry,
                    table=metadata.ehabi,
                    sp=sp,
                    regs=regs,
                    read=read,
                    stack_low=low,
                    stack_high=high,
                    endian=endian,
                )
                # NOTE: .ARM.exidx encodes a single post-prologue unwind program
                # per function. At the first instruction (lookup_pc == symbol.start)
                # no prologue push has executed yet, so a non-zero VSP delta from
                # EHABI cannot apply; defer to per-PC DWARF CFI when available.
                if (
                    isinstance(outcome, tuple)
                    and lookup_pc == symbol.start
                    and outcome[1] != sp
                ):
                    ehabi_unexecuted_prologue = True
                    attempts.append("ehabi-prologue-unexecuted")
                    outcome = None
                elif isinstance(outcome, tuple):
                    new_regs, new_sp, new_pc = outcome
                    step_method: Method = "ehabi"
                else:
                    attempts.append(outcome)
                    outcome = None
            else:
                attempts.append("ehabi-no-matching-function")
                outcome = None
        else:
            outcome = None
            step_method = "cfi"
        if outcome is None and metadata is not None:
            row = None
            for table in metadata.cfi_tables:
                row = table.row_for(lookup_pc)
                if row is not None:
                    if row.error is not None:
                        has_metadata = True
                        parse_failures.append(row.error)
                        row = None
                        continue
                    break
                if table.error is not None:
                    parse_failures.append(table.error)
            if row is not None:
                has_metadata = True
                outcome = _unwind_cfi(
                    row,
                    regs=regs,
                    sp=sp,
                    read=read,
                    ptr_size=ptr_size,
                    endian=endian,
                    return_register=return_register,
                    sp_register=sp_register,
                    stack_low=low,
                    stack_high=high,
                )
                if isinstance(outcome, tuple):
                    new_regs, new_sp, new_pc = outcome
                    step_method = "cfi"
                    exact_caller_pc = row.signal_frame
                else:
                    attempts.append(outcome)
                    outcome = None
            else:
                has_metadata = has_metadata or bool(parse_failures)
                attempts.extend(parse_failures or ["cfi-no-fde"])
        if (
            outcome is None
            and (not has_metadata or (ehabi_unexecuted_prologue and not parse_failures))
            and metadata is not None
        ):
            exidx = metadata.elf.section(".ARM.exidx")
            if exidx is not None and len(exidx.data) % 8:
                stop_reason = "ehabi-truncated-table"
                break
            proof = prove_leaf_return(metadata.elf, lookup_pc)
            if proof is not None:
                raw_return = regs.get(proof.return_register)
                if raw_return is None:
                    stop_reason = _register_failure(
                        regs, proof.return_register, "leaf-return-address-missing"
                    )
                    break
                new_regs = _RecoveredRegisters(regs)
                for register in (*proof.clobbered, proof.return_register):
                    new_regs.pop(register, None)
                    new_regs.unavailable[register] = "leaf-register-unavailable"
                new_sp, new_pc = sp, raw_return
                outcome = new_regs, new_sp, new_pc
                step_method = "leaf"
                frame_evidence = proof.evidence
        if outcome is None and allow_frame_pointer and not has_metadata:
            outcome = _unwind_frame_pointer(
                arch=arch,
                regs=regs,
                sp=sp,
                read=read,
                ptr_size=ptr_size,
                endian=endian,
                frame_pointer_register=fp_register,
                stack_low=low,
                stack_high=high,
            )
            if isinstance(outcome, tuple):
                new_regs, new_sp, new_pc = outcome
                step_method = "frame-pointer"
            else:
                attempts.append(outcome)
                outcome = None
        if outcome is None:
            cfi_failures = [
                reason for reason in attempts if reason.startswith(("cfi-", "eh-"))
            ]
            if cfi_failures:
                specific = [reason for reason in cfi_failures if reason != "cfi-no-fde"]
                stop_reason = specific[0] if specific else "no-unwind-info"
            else:
                stop_reason = attempts[0] if attempts else "no-unwind-info"
            break

        raw_caller = new_pc
        transition = _apply_transition(raw_caller, new_regs, new_sp, current)
        if isinstance(transition, str):
            stop_reason = transition
            break
        if transition is not None:
            key = (transition.segment.ident, transition.pc, transition.sp)
            if key in seen:
                stop_reason = "loop"
                break
            seen.add(key)
            frames.append(
                UnwindFrame(
                    index,
                    transition.pc,
                    transition.method,
                    transition.pc,
                    segment=transition.segment.ident,
                )
            )
            transitions.append(
                SegmentTransition(
                    from_segment=current.ident if current is not None else "",
                    to_segment=transition.segment.ident,
                    method=transition.method,
                    source=transition.source,
                    pc=transition.pc,
                    sp=transition.sp,
                    value=transition.value,
                    exact_pc=transition.exact_pc,
                    evidence=transition.evidence,
                )
            )
            recovered_transition = _RecoveredRegisters(transition.registers)
            if isinstance(new_regs, _RecoveredRegisters):
                for register, reason in new_regs.unavailable.items():
                    if register not in recovered_transition:
                        recovered_transition.unavailable[register] = reason
            regs = recovered_transition
            regs[sp_register] = transition.sp
            regs.unavailable.pop(sp_register, None)
            if convention.pc_register is not None:
                regs[convention.pc_register] = transition.pc
                regs.unavailable.pop(convention.pc_register, None)
            current = transition.segment
            pc = transition.pc
            lookup_pc = transition.pc
            sp = transition.sp
            method = transition.method
            if root_pc is not None and pc == root_pc:
                stop_reason = seed.root_reason
                complete = True
                break
            continue

        caller_pc = convention.normalize_pc(raw_caller)
        if caller_pc == 0:
            complete = seed.root_pc == 0
            stop_reason = seed.root_reason if complete else "return-address-zero"
            break
        at_root = root_pc is not None and caller_pc == root_pc
        caller_lookup_pc = caller_pc if exact_caller_pc or at_root else caller_pc - 1
        if not code(caller_lookup_pc):
            stop_reason = "return-address-outside-code"
            break
        if new_sp < sp:
            stop_reason = "no-progress"
            break
        if low is not None and new_sp < low:
            stop_reason = "stack-underflow"
            break
        if high is not None and new_sp > high:
            stop_reason = "stack-overflow"
            break
        segment_id = current.ident if current is not None else ""
        if (segment_id, caller_pc, new_sp) in seen:
            stop_reason = "loop"
            break
        seen.add((segment_id, caller_pc, new_sp))
        frames.append(
            UnwindFrame(
                index,
                caller_pc,
                step_method,
                caller_lookup_pc,
                segment=current.ident if current is not None else None,
                evidence=frame_evidence,
            )
        )
        method = step_method
        pc = caller_pc
        lookup_pc = caller_lookup_pc
        sp = new_sp
        recovered = _RecoveredRegisters(new_regs)
        if isinstance(regs, _RecoveredRegisters):
            for register, reason in regs.unavailable.items():
                if register not in recovered:
                    recovered.unavailable.setdefault(register, reason)
        regs = recovered
        if convention.pc_register is not None:
            regs[convention.pc_register] = pc
            regs.unavailable.pop(convention.pc_register, None)
        if at_root:
            stop_reason = seed.root_reason
            complete = True
            break
    else:
        stop_reason = "max-depth"
        truncated = True
    return _result(frames, stop_reason, complete, truncated, method)


# The following two helpers are used by RTOS adapters to read a frame slot
# without duplicating bounds checks.
def read_slot(
    read: Reader,
    address: int,
    width: int,
    endian: Endian,
    *,
    stack_low: int | None = None,
    stack_high: int | None = None,
) -> int | None:
    """Read one integer slot with optional stack bounds; ``None`` on failure."""
    return _read_value(
        read,
        address,
        width,
        endian,
        stack_low=stack_low,
        stack_high=stack_high,
    )


def read_block(
    read: Reader,
    address: int,
    size: int,
    *,
    stack_low: int | None = None,
    stack_high: int | None = None,
) -> bytes | None:
    """Read a bounded block; ``None`` on a short read or bounds violation."""
    if size <= 0 or size > MAX_FRAME_READ or address <= 0:
        return None
    if stack_low is not None and address < stack_low:
        return None
    if stack_high is not None and address + size > stack_high:
        return None
    raw = read(address, size)
    if raw is None or len(raw) != size:
        return None
    return raw


try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

RISCV_LIVE_REGISTERS: dict[int, str] = dict(RISCV_CONVENTION.live_registers)


def cached_unwind_metadata(
    adapter: Any,
    *,
    path_reader: Callable[[], str | None] = loaded_object_path,
) -> UnwindMetadata | None:
    """Parse the loaded image's unwind tables once per ELF path."""
    path = path_reader()
    if path is None:
        return None
    cache = getattr(adapter, "_unwind_cache", None)
    if cache is not None and cache[0] == path:
        return cache[1]
    elf = ElfImage.from_path(path)
    metadata = UnwindMetadata.from_elf(elf) if elf is not None else None
    adapter._unwind_cache = (path, metadata)
    return metadata


def live_seed_detail(
    arch: str,
    isa: str | None = None,
    *,
    root_pc: int | None = None,
    require_exception_registers: bool = True,
    gdb_module: Any = None,
    snapshot_reader: Callable[[], tuple[Any, str]] | None = None,
) -> tuple[SeedFrame | None, str]:
    """Read the selected core's live registers without driving the target.

    ``isa`` is the selected profile evidence. A live handler seed requires it
    so architecture-specific exception-number rules are applied only to the
    matching profile; an unknown profile is refused rather than assumed.
    """
    active_gdb = gdb if gdb_module is None else gdb_module
    if arch.startswith("arm"):
        from .unwind_arm import arm_live_seed_detail

        return arm_live_seed_detail(
            isa,
            root_pc=root_pc,
            require_exception_registers=require_exception_registers,
            gdb_module=active_gdb,
            snapshot_reader=snapshot_reader,
        )
    if active_gdb is None:
        return None, "live-registers-unavailable"
    mask = (
        (1 << 32) - 1
        if isa == "riscv32"
        else (1 << 64) - 1
        if isa == "riscv64"
        else None
    )
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc())
        sp = int(frame.read_register("sp"))
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    if mask is not None:
        pc &= mask
        sp &= mask
    registers: dict[int, int] = {}
    for register, name in RISCV_LIVE_REGISTERS.items():
        try:
            value = int(frame.read_register(name))
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):
            return None, "live-registers-unavailable"
        registers[register] = value & mask if mask is not None else value
    registers[2] = sp
    return (
        SeedFrame(
            pc=pc,
            sp=sp,
            regs=registers,
            source="current",
            root_pc=root_pc,
        ),
        "ok",
    )


def live_seed(
    arch: str,
    isa: str | None = None,
    *,
    root_pc: int | None = None,
    require_exception_registers: bool = True,
    gdb_module: Any = None,
) -> SeedFrame | None:
    """Compatibility wrapper returning only the decoded live seed."""
    seed, _reason = live_seed_detail(
        arch,
        isa,
        root_pc=root_pc,
        require_exception_registers=require_exception_registers,
        gdb_module=gdb_module,
    )
    return seed


def _resolve_boot_riscv_bounds(
    *,
    seed: SeedFrame,
    elf: ElfImage | None,
    hart_id: int,
    symbol_lookup: Callable[[str], int | None],
) -> tuple[int, int, str] | None:
    from .unwind_arm import MAX_INFERRED_IRQ_STACK_BYTES
    from .unwind_riscv import resolve_riscv_irq_stack_bounds_detail

    bounds = resolve_riscv_irq_stack_bounds_detail(
        symbol_lookup=symbol_lookup,
        elf=elf,
        hart_id=hart_id,
    )
    if bounds is not None:
        return bounds
    for name in (
        "_stack_top",
        "_estack",
        "__stack_end__",
        "__freertos_irq_stack_top",
    ):
        top = symbol_lookup(name)
        if top is None and elf is not None:
            top = elf.symbol_address(name)
        if (
            top is not None
            and 0 < seed.sp <= top
            and top - seed.sp <= MAX_INFERRED_IRQ_STACK_BYTES
        ):
            return seed.sp, top, f"sp:live..{name}"
    return None


def live_boot_unwind(
    arch: Arch,
    isa: str | None,
    metadata: UnwindMetadata | None,
    *,
    max_depth: int = 32,
    hart_id: int = 0,
    gdb_module: Any = None,
    read: Reader | None = None,
    symbol_lookup: Callable[[str], int | None] | None = None,
    arm_snapshot_reader: Callable[[], tuple[Any, str]] | None = None,
) -> tuple[UnwindResult | None, str, str, str]:
    """Unwind the halted core's current pre-scheduler stack within boot bounds."""
    from .gdb_bridge import linker_symbol_address, read_bytes
    from .unwind_arm import (
        XPSR_IPSR_MAX_MASK,
        arm_snapshot,
        resolve_arm_irq_stack_bounds_detail,
    )

    active_gdb = gdb if gdb_module is None else gdb_module
    reader = read_bytes if read is None else read
    lookup = linker_symbol_address if symbol_lookup is None else symbol_lookup
    elf = metadata.elf if metadata is not None else None
    m_profile = isa in (
        "armv6-m",
        "armv7-m",
        "armv7e-m",
        "armv8-m.baseline",
        "armv8-m.mainline",
    )

    if arch == "arm" and m_profile:
        snapshot, snap_reason = (
            arm_snapshot_reader()
            if arm_snapshot_reader is not None
            else arm_snapshot(gdb_module=active_gdb)
        )
        if snapshot is None:
            return (
                None,
                snap_reason,
                "the selected core's exception/stack registers are unavailable",
                "unknown",
            )
        if snapshot.xpsr & XPSR_IPSR_MAX_MASK:
            return (
                None,
                "scheduler-not-running",
                "an M-profile exception handler is active before the scheduler is started",
                "unknown",
            )
        if snapshot.msp <= 0 or snapshot.sp != snapshot.msp:
            return (
                None,
                "live-stack-selection-invalid",
                "the live pre-scheduler SP is not the active MSP",
                "unknown",
            )
        seed = SeedFrame(
            pc=snapshot.pc & ~1,
            sp=snapshot.sp,
            regs=dict(snapshot.registers),
            source="current",
        )
        bounds = resolve_arm_irq_stack_bounds_detail(
            symbol_lookup=lookup,
            read=reader,
            isa=isa,
            elf=elf,
            live_msp=seed.sp,
        )
    elif arch == "arm":
        seed_or_none, seed_reason = live_seed_detail(
            "arm",
            isa,
            require_exception_registers=False,
            gdb_module=active_gdb,
        )
        if seed_or_none is None:
            return (
                None,
                seed_reason,
                "the selected core's live registers are unavailable",
                "unknown",
            )
        if isa == "armv7-a":
            if active_gdb is None:
                return (
                    None,
                    "live-registers-unavailable",
                    "the selected core's CPSR is unavailable",
                    "unknown",
                )
            try:
                cpsr = int(active_gdb.newest_frame().read_register("cpsr"))
            except (active_gdb.error, AttributeError, RuntimeError, TypeError):
                return (
                    None,
                    "live-registers-unavailable",
                    "the selected core's CPSR is unavailable",
                    "unknown",
                )
            if cpsr & 0x1F not in (0x13, 0x1F):
                return (
                    None,
                    "unsupported-exception-model",
                    "pre-scheduler Armv7-A unwind requires SVC or System mode",
                    "unknown",
                )
        seed = seed_or_none
        bounds = resolve_arm_irq_stack_bounds_detail(
            symbol_lookup=lookup,
            read=reader,
            isa=isa,
            elf=elf,
            live_msp=seed.sp,
        )
    elif arch == "riscv":
        seed_or_none, seed_reason = live_seed_detail(
            "riscv",
            isa,
            gdb_module=active_gdb,
        )
        if seed_or_none is None:
            return (
                None,
                seed_reason,
                "the selected core's live registers are unavailable",
                "unknown",
            )
        seed = seed_or_none
        bounds = _resolve_boot_riscv_bounds(
            seed=seed,
            elf=elf,
            hart_id=hart_id,
            symbol_lookup=lookup,
        )
    else:
        return (
            None,
            "unsupported-architecture",
            f"pre-scheduler live unwind is unsupported for {arch!r}",
            "unknown",
        )

    if bounds is None:
        return (
            None,
            "stack-bounds-unavailable",
            "live boot stack bounds could not be resolved",
            "unknown",
        )
    low, high, _ = bounds
    result = unwind_chain(
        seed,
        metadata,
        read=reader,
        max_depth=max_depth,
        stack_low=low,
        stack_high=high,
        allow_frame_pointer=isa != "armv7-a",
    )
    return result, result.stop_reason, "", "recorded"
