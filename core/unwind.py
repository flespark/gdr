"""Bounded, read-only task stack unwinding over ELF unwind metadata (pure).

The driver consumes :mod:`core.unwind_ehabi` and :mod:`core.unwind_cfi` tables and
falls back to a validated frame-pointer chain only when those tables do not
cover a PC.  It never calls an inferior function and never writes target
memory. Every frame preserves or advances SP, stays inside the supplied bounds and
is bounded by a maximum depth; a malformed or unsupported record becomes a
stop reason, never a guessed register value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .unwind_cfi import CfiRow, CfiTable, RegisterRule, parse_cfi_section
from .unwind_ehabi import EhabiTable, parse_ehabi, unwind_ehabi
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

# ---------------------------------------------------------------------------
# Unwind driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedFrame:
    """The first frame of a task: registers decoded by the RTOS adapter."""

    pc: int
    sp: int
    regs: Mapping[int, int]
    source: str = "saved"
    root_pc: int | None = None
    root_reason: str = "task-entry"


@dataclass(frozen=True)
class UnwindFrame:
    """One recovered frame; ``method`` names the evidence that produced it."""

    index: int
    pc: int
    method: Method
    lookup_pc: int | None = None

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
    def from_elf(cls, elf: ElfImage) -> UnwindMetadata:
        """Build every available source; an absent source stays ``None``."""
        arch: Arch = "riscv" if elf.machine == EM_RISCV else "arm"
        cfi_tables: list[CfiTable] = []
        for name, is_eh in ((".eh_frame", True), (".debug_frame", False)):
            section = elf.section(name)
            if section is None or not section.data:
                continue
            cfi_tables.append(
                parse_cfi_section(
                    section.data,
                    section_address=section.address,
                    address_size=elf.pointer_size,
                    endian=elf.endian,
                    is_eh_frame=is_eh,
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
            return_register=1 if arch == "riscv" else 14,
            frame_pointer_register=8 if arch == "riscv" else 7,
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
    if row.cfa.register is None:
        return "cfi-no-cfa"
    if not 0 <= row.cfa.register < register_count:
        return "cfi-cfa-register-unsupported"
    for register, rule in row.registers.items():
        if (not 0 <= register < register_count and register != return_register) or (
            rule.register is not None and not 0 <= rule.register < register_count
        ):
            return "cfi-register-unsupported"
    cfa_base = regs.get(row.cfa.register)
    if cfa_base is None:
        return _register_failure(regs, row.cfa.register, "cfi-cfa-register-missing")
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
    new_regs = _RecoveredRegisters(regs)
    new_regs[frame_pointer_register] = caller_fp
    new_regs[13 if arch == "arm" else 2] = caller_sp
    new_regs.unavailable.pop(frame_pointer_register, None)
    new_regs.unavailable.pop(13 if arch == "arm" else 2, None)
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
) -> UnwindResult:
    """Walk a seed frame through metadata, bounded by *max_depth*.

    The first frame is the seed exactly as the RTOS adapter decoded it.  Each
    further frame must not decrease the stack pointer, must stay inside the supplied
    bounds and be produced by a metadata rule or a validated frame-pointer
    record. Repeated PC/SP pairs are loops. ``complete`` requires the adapter's
    independently supplied root or exit sentinel; other termination is partial.

    Args:
        seed: Decoded PC/SP/registers and the optional task entry.
        metadata: Parsed ELF tables; ``None`` permits only a frame-pointer walk.
        read: Bounded target-memory reader ``(address, size) -> bytes|None``.
        max_depth: Maximum returned frames, clamped to ``1..MAX_FRAMES``.
        stack_low: Lowest valid stack byte, or ``None`` when unknown.
        stack_high: One-past-highest valid stack byte, or ``None``.
        allow_frame_pointer: Permit the validated frame-pointer fallback.
        is_code: Executable-address predicate; defaults to the ELF text ranges.

    Returns:
        Frames, the exact stop reason, completeness and truncation state.
    """
    if max_depth < 1:
        max_depth = 1
    max_depth = min(max_depth, MAX_FRAMES)
    arch: Arch = metadata.arch if metadata is not None else "arm"
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
        else (8 if arch == "riscv" else 7)
    )
    return_register = metadata.return_register if metadata is not None else 14
    sp_register = 13 if arch == "arm" else 2

    pc = seed.pc & ~1 if arch == "arm" else seed.pc
    sp = seed.sp
    regs = dict(seed.regs)
    if pc <= 0:
        return UnwindResult(
            (), "seed-pc-invalid", False, False, seed.source, seed.source
        )
    if stack_low is not None and sp < stack_low:
        return UnwindResult(
            (), "seed-stack-underflow", False, False, seed.source, seed.source
        )
    if stack_high is not None and sp > stack_high:
        return UnwindResult(
            (), "seed-stack-overflow", False, False, seed.source, seed.source
        )
    if metadata is not None and not metadata.elf.is_code(pc):
        return UnwindResult(
            (), "seed-pc-outside-code", False, False, seed.source, seed.source
        )
    if arch == "arm":
        regs[15] = pc
        regs[13] = sp
    else:
        regs[2] = sp
    frames: list[UnwindFrame] = [UnwindFrame(0, pc, "seed")]
    lookup_pc = pc
    seen: set[tuple[int, int]] = {(pc, sp)}
    method = seed.source
    stop_reason = "no-unwind-info"
    complete = False
    truncated = False
    if seed.root_pc is not None and (pc & ~1 if arch == "arm" else pc) == (
        seed.root_pc & ~1 if arch == "arm" else seed.root_pc
    ):
        return UnwindResult(
            frames=tuple(frames),
            stop_reason=seed.root_reason,
            complete=True,
            truncated=False,
            seed_source=seed.source,
            method=method,
        )
    for index in range(1, max_depth):
        attempts: list[str] = []
        has_metadata = False
        exact_caller_pc = False
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
                    stack_low=stack_low,
                    stack_high=stack_high,
                    endian=endian,
                )
                if isinstance(outcome, tuple):
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
            parse_failures: list[str] = []
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
                    stack_low=stack_low,
                    stack_high=stack_high,
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
        if outcome is None and allow_frame_pointer and not has_metadata:
            outcome = _unwind_frame_pointer(
                arch=arch,
                regs=regs,
                sp=sp,
                read=read,
                ptr_size=ptr_size,
                endian=endian,
                frame_pointer_register=fp_register,
                stack_low=stack_low,
                stack_high=stack_high,
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
        caller_pc = new_pc & ~1 if arch == "arm" else new_pc
        if caller_pc == 0:
            complete = seed.root_pc == 0
            stop_reason = seed.root_reason if complete else "return-address-zero"
            break
        at_root = seed.root_pc is not None and caller_pc == (
            seed.root_pc & ~1 if arch == "arm" else seed.root_pc
        )
        caller_lookup_pc = caller_pc if exact_caller_pc or at_root else caller_pc - 1
        if not code(caller_lookup_pc):
            stop_reason = "return-address-outside-code"
            break
        if new_sp < sp:
            stop_reason = "no-progress"
            break
        if stack_low is not None and new_sp < stack_low:
            stop_reason = "stack-underflow"
            break
        if stack_high is not None and new_sp > stack_high:
            stop_reason = "stack-overflow"
            break
        if (caller_pc, new_sp) in seen:
            stop_reason = "loop"
            break
        seen.add((caller_pc, new_sp))
        frames.append(UnwindFrame(index, caller_pc, step_method, caller_lookup_pc))
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
        if arch == "arm":
            regs[15] = pc
            regs.unavailable.pop(15, None)
        if at_root:
            stop_reason = seed.root_reason
            complete = True
            break
    else:
        stop_reason = "max-depth"
        truncated = True
    return UnwindResult(
        frames=tuple(frames),
        stop_reason=stop_reason,
        complete=complete,
        truncated=truncated,
        seed_source=seed.source,
        method=method,
    )


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
