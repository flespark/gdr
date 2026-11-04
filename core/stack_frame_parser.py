"""Saved task-frame profile descriptions and common slot decoding (pure).

The module is RTOS-agnostic. It defines the shared saved-frame representation,
validates its construction, and decodes common register/SP/PC/status slots.
Instruction decoders live in :mod:`core.insn_riscv` and :mod:`core.insn_thumb`;
RTOS symbol names, configuration policy and profile selection live in the
adapter packages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .cpus import (
    ARM_CONVENTION,
    RISCV_CONVENTION,
    CpuFamily,
    RegisterConvention,
    convention_for_arch,
)
from .insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_all_store_offsets,
    riscv_code_decoder,
    riscv_frame_accesses,
    riscv_next_pc,
    riscv_slot_map,
    riscv_stack_frame_size,
    riscv_target_loads,
)
from .unwind import read_slot
from .unwind_meta import Endian, Reader

ValidationLevel = Literal["synthetic", "file-only", "live"]
StatusKind = Literal["xpsr", "cpsr"]


@dataclass(frozen=True)
class SavedStackContext:
    """Describe byte offsets for one RTOS saved stack context profile.

    Attributes:
        profile_id: Stable identifier for the port/profile, never empty.
        pointer_size: Saved slot width in bytes; 4 or 8.
        registers: ``(register number, byte offset)`` pairs relative to the
            frame top; register numbers must be unique and non-negative.
        pc_offset: Offset of the saved program counter (or trap return PC).
        lr_offset: Offset of the saved link/return-address register.
        status_offset: Offset of the architecture status word (Arm xPSR/CPSR);
            ``None`` when the port has no such slot (for example RISC-V).
        status_kind: Interpretation of a present status slot; xPSR by default,
            CPSR for Arm A-profile. Ignored when no status slot is present.
        sp_offset: Offset from the frame top to the caller's SP; usually the
            frame size consumed by the save path.
        sp_slot_offset: Offset of a slot that stores the task SP instead of
            recovering it from the frame top; ``None`` for direct layouts.
        sp_slot_bias: Bias added to the value read from ``sp_slot_offset`` to
            recover the real task SP.
        fill_word: Initial stack fill word used to recognize untouched slots.
        initial_registers: Eight fixed register values written by the port's
            initial-context builder, or ``None`` when no such proof exists.
        context_in_tcb: Whether the saved context lives in the TCB rather than
            on the task stack.
        context_span: Half-open saved-buffer offsets relative to frame top.
        slot_aliases: Explicit pairs of slot names (``rN``, ``pc``, ``lr``,
            ``status``, ``sp``) that describe the same saved value.
        validation: Evidence tier; only ``live`` profiles pass runtime gates.
    """

    profile_id: str
    pointer_size: int
    registers: tuple[tuple[int, int], ...]
    pc_offset: int
    lr_offset: int
    status_offset: int | None
    sp_offset: int
    context_span: tuple[int, int]
    sp_slot_offset: int | None = None
    sp_slot_bias: int = 0
    fill_word: int = 0
    initial_registers: tuple[int, ...] | None = None
    context_in_tcb: bool = False
    slot_aliases: tuple[tuple[str, str], ...] = ()
    validation: ValidationLevel = "synthetic"
    status_kind: StatusKind = "xpsr"

    @property
    def verified(self) -> bool:
        """Whether this profile has a live closed-loop validation lane."""
        return self.validation == "live"

    @property
    def convention(self) -> RegisterConvention:
        """Return the register convention for this profile's architecture."""
        return ARM_CONVENTION if self.status_offset is not None else RISCV_CONVENTION

    def __post_init__(self) -> None:
        if self.pointer_size not in (4, 8) or not self.profile_id:
            raise ValueError("invalid saved-context profile")
        if self.validation not in ("synthetic", "file-only", "live"):
            raise ValueError("invalid saved-context validation")
        if self.status_kind not in ("xpsr", "cpsr"):
            raise ValueError("invalid saved-context status kind")
        if self.status_kind == "cpsr" and self.status_offset is None:
            raise ValueError("CPSR requires a saved status slot")
        numbers = [register for register, _offset in self.registers]
        if len(set(numbers)) != len(numbers):
            raise ValueError("duplicate saved-context register")
        if any(register < 0 for register in numbers):
            raise ValueError("invalid saved-context register map")
        offsets = [self.pc_offset, self.lr_offset, self.sp_offset, self.sp_slot_bias]
        offsets.extend(offset for _register, offset in self.registers)
        if self.sp_slot_offset is not None:
            offsets.append(self.sp_slot_offset)
        if any(offset % self.pointer_size for offset in offsets):
            raise ValueError("unaligned saved-context slot")
        if self.status_offset is not None and (
            self.pointer_size != 4 or self.status_offset % 4
        ):
            raise ValueError("invalid Arm status slot")
        if self.context_in_tcb and self.sp_slot_offset is None:
            raise ValueError("TCB context requires a saved task SP")
        if self.initial_registers is not None and len(self.initial_registers) != 8:
            raise ValueError("invalid initial-register proof")
        values = (self.fill_word, *(self.initial_registers or ()))
        if any(not 0 <= value < 1 << (8 * self.pointer_size) for value in values):
            raise ValueError("saved-context value exceeds slot width")
        low, high = self.context_span
        if low >= high or any(value % self.pointer_size for value in (low, high)):
            raise ValueError("invalid saved-context span")
        slots = {f"r{register}": offset for register, offset in self.registers}
        slots.update(pc=self.pc_offset, lr=self.lr_offset)
        if self.status_offset is not None:
            slots["status"] = self.status_offset
        if self.sp_slot_offset is not None:
            slots["sp"] = self.sp_slot_offset
        if any(
            offset < low or offset + self.pointer_size > high
            for offset in slots.values()
        ):
            raise ValueError("saved-context slot outside span")
        aliases: set[frozenset[str]] = set()
        for left, right in self.slot_aliases:
            pair = frozenset((left, right))
            if (
                len(pair) != 2
                or pair in aliases
                or left not in slots
                or right not in slots
                or slots[left] != slots[right]
            ):
                raise ValueError("invalid saved-context slot alias")
            aliases.add(pair)
        names = list(slots)
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                if (
                    slots[left] == slots[right]
                    and frozenset((left, right)) not in aliases
                ):
                    raise ValueError("overlapping saved-context slots require an alias")


@dataclass(frozen=True)
class StackContextSelection:
    """A profile decision and its file/config evidence, without target access.

    Attributes:
        isa: Canonical ISA name (for example ``armv7-m`` or ``riscv64``), or
            ``None`` when the ELF evidence cannot identify one.
        family: Coarse architecture family, ``arm``, ``riscv`` or ``unknown``.
        context: Selected saved-frame profile, or ``None`` when the image is
            rejected.
        reason: ``ok`` or the machine-readable rejection reason retained for
            the user and the API envelope.
        evidence: Human-readable evidence sources that supported the choice.
        abi: ABI name derived from ELF attributes/flags, when available.
    """

    isa: str | None
    family: CpuFamily
    context: SavedStackContext | None
    reason: str
    evidence: tuple[str, ...] = ()
    abi: str | None = None

    @property
    def convention(self) -> RegisterConvention | None:
        """Return the register convention when the architecture family is known."""
        if self.family == "unknown":
            return None
        return convention_for_arch(self.family)


@dataclass(frozen=True)
class DecodedProfileSlots:
    """Raw register, PC, LR, SP and status values read from a saved context."""

    pc: int
    code_pc: int
    sp: int
    lr: int | None
    status: int | None
    registers: dict[int, int]


def decode_profile_slots(
    context: SavedStackContext,
    top: int,
    read: Reader,
    *,
    endian: Endian,
    stack_low: int | None = None,
    stack_high: int | None = None,
    context_low: int | None = None,
    context_high: int | None = None,
    check_ipsr_and_align: bool = True,
) -> tuple[DecodedProfileSlots | None, str]:
    """Read and validate the common register/SP/PC/status slots of a profile."""
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
        stack_low=read_low,
        stack_high=read_high,
    )
    if pc is None or pc == 0:
        return None, "no-saved-context"
    registers: dict[int, int] = {}
    for register, offset in context.registers:
        value = read_slot(
            read,
            top + offset,
            context.pointer_size,
            endian,
            stack_low=read_low,
            stack_high=read_high,
        )
        if value is None:
            return None, "saved-context-unreadable"
        registers[register] = value
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
    convention = context.convention
    if lr is not None:
        registers[convention.return_register] = lr
    registers[convention.sp_register] = sp
    status: int | None = None
    if context.status_offset is not None:
        if convention.pc_register is not None:
            registers[convention.pc_register] = pc
        status = read_slot(
            read,
            top + context.status_offset,
            4,
            endian,
            stack_low=read_low,
            stack_high=read_high,
        )
        if status is None:
            return None, "saved-context-unreadable"
        if context.status_kind == "cpsr":
            if status & 0x1F not in (
                0x10,
                0x11,
                0x12,
                0x13,
                0x16,
                0x17,
                0x1A,
                0x1B,
                0x1F,
            ):
                return None, "saved-frame-cpsr-mode-invalid"
            if status & (1 << 24):
                return None, "saved-frame-instruction-set-unsupported"
            if not status & (1 << 5) and pc & 3:
                return None, "saved-frame-pc-misaligned"
        elif not status & (1 << 24):
            return None, "saved-frame-not-thumb"
        elif check_ipsr_and_align:
            if status & 0x1FF:
                return None, "saved-frame-ipsr-invalid"
            if status & (1 << 9):
                sp += 4
                registers[convention.sp_register] = sp
                if stack_high is not None and sp > stack_high:
                    return None, "saved-stack-overflow"
    code_pc = convention.normalize_pc(pc)
    if code_pc == 0:
        return None, "saved-context-unreadable"
    return (
        DecodedProfileSlots(
            pc=pc,
            code_pc=code_pc,
            sp=sp,
            lr=lr,
            status=status,
            registers=registers,
        ),
        "ok",
    )


__all__ = [
    "DecodedProfileSlots",
    "EvidenceError",
    "RiscvCodeParser",
    "SavedStackContext",
    "StatusKind",
    "StackContextSelection",
    "ValidationLevel",
    "decode_profile_slots",
    "riscv_all_store_offsets",
    "riscv_code_decoder",
    "riscv_frame_accesses",
    "riscv_next_pc",
    "riscv_slot_map",
    "riscv_stack_frame_size",
    "riscv_target_loads",
]
