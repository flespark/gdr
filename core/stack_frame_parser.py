"""Saved task-frame descriptions and bounded RISC-V code evidence.

The module is RTOS-agnostic. It defines the shared saved-frame representation,
validates its construction, and provides the RISC-V instruction decoder and
ELF code-range primitives that adapters use to prove a compiled port layout.
RTOS symbol names, configuration policy and profile selection live in the
adapter packages.
"""

from __future__ import annotations

from dataclasses import dataclass

from .unwind_meta import ElfImage, _sign_extend


class EvidenceError(ValueError):
    """An instruction or symbol does not prove the requested convention."""


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
        status_offset: Offset of the architecture status word (Arm xPSR);
            ``None`` when the port has no such slot (for example RISC-V).
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
        verified: Whether this profile has compiled/closed-loop evidence;
            selectors must not enable an unverified profile.
    """

    profile_id: str
    pointer_size: int
    registers: tuple[tuple[int, int], ...]
    pc_offset: int
    lr_offset: int
    status_offset: int | None
    sp_offset: int
    sp_slot_offset: int | None = None
    sp_slot_bias: int = 0
    fill_word: int = 0
    initial_registers: tuple[int, ...] | None = None
    context_in_tcb: bool = False
    verified: bool = False

    def __post_init__(self) -> None:
        if self.pointer_size not in (4, 8) or not self.profile_id:
            raise ValueError("invalid saved-context profile")
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
    family: str
    context: SavedStackContext | None
    reason: str
    evidence: tuple[str, ...] = ()
    abi: str | None = None


def riscv_code_decoder(word: int, size: int, pc: int, xlen: int) -> tuple:
    """Normalize the scalar RISC-V instructions used by port evidence checks."""
    if size == 4:
        op = word & 127
        rd, rs, rt = (word >> 7) & 31, (word >> 15) & 31, (word >> 20) & 31
        fn = (word >> 12) & 7
        imm = _sign_extend(word >> 20, 12)
        if op == 3 and fn in (2, 3) and (fn == 2 or xlen == 64):
            return ("lw" if fn == 2 else "ld", rd, rs, imm)
        if op == 35 and fn in (2, 3) and (fn == 2 or xlen == 64):
            offset = _sign_extend(((word >> 7) & 31) | ((word >> 25) << 5), 12)
            return ("sw" if fn == 2 else "sd", rt, rs, offset)
        if op == 19 and fn in (0, 7):
            return ("addi" if fn == 0 else "andi", rd, rs, imm)
        if op == 19 and fn == 4:
            return ("xori", rd, rs, imm)
        if op == 19 and fn == 1 and (word >> 20) < xlen:
            return ("slli", rd, rs, word >> 20)
        if op == 19 and fn == 5 and (word >> 25) == 0 and rt < xlen:
            return ("srli", rd, rs, rt)
        if op == 27 and fn == 0 and xlen == 64:
            return ("addiw", rd, rs, imm)
        if op == 51 and fn in (0, 4, 6, 7) and word >> 25 == 0:
            return ({0: "add", 4: "xor", 6: "or", 7: "and"}[fn], rd, rs, rt)
        if op in (23, 55):
            return ("auipc" if op == 23 else "lui", rd, _sign_extend(word & ~4095, 32))
        if word == 0x30200073:
            return ("mret",)
        if op == 115 and fn == 2 and rs == 0:
            return ("csrr", rd, word >> 20)
        if op == 115 and rd == 0 and fn in (1, 2):
            return ("csrw" if fn == 1 else "csrs", word >> 20, rs)
        if word == 0x00008067:
            return ("ret",)
        if op == 99 and fn in (0, 1, 4, 5, 6, 7):
            offset = ((word >> 8) & 15) << 1 | ((word >> 25) & 63) << 5
            offset |= ((word >> 7) & 1) << 11 | (word >> 31) << 12
            mnemonic = {0: "beq", 1: "bne", 4: "blt", 5: "bge", 6: "bltu", 7: "bgeu"}[
                fn
            ]
            return (mnemonic, rs, rt, pc + _sign_extend(offset, 13))
        if op == 111:
            offset = ((word >> 21) & 1023) << 1 | ((word >> 20) & 1) << 11
            offset |= ((word >> 12) & 255) << 12 | (word >> 31) << 20
            return ("jal", rd, pc + _sign_extend(offset, 21))
        if op == 103 and fn == 0:
            return ("jalr", rd, rs, imm)
        raise EvidenceError("unsupported instruction")
    if size != 2:
        raise EvidenceError("invalid instruction size")
    quadrant, fn = word & 3, word >> 13
    rd, rt = (word >> 7) & 31, (word >> 2) & 31
    imm = _sign_extend(((word >> 2) & 31) | ((word >> 12) & 1) << 5, 6)
    if quadrant == 1:
        if fn == 0:
            return ("addi", rd, rd, imm) if rd and imm else ("nop",)
        if fn == 2 and rd:
            return ("addi", rd, 0, imm)
        if fn == 1 and rd and xlen == 64:
            return ("addiw", rd, rd, imm)
        if fn == 3 and rd == 2:
            offset = ((word >> 6) & 1) << 4 | ((word >> 2) & 1) << 5
            offset |= ((word >> 5) & 1) << 6 | ((word >> 3) & 3) << 7
            offset |= ((word >> 12) & 1) << 9
            if offset:
                return ("addi", 2, 2, _sign_extend(offset, 10))
        if fn == 3 and rd not in (0, 2):
            value = _sign_extend(((word >> 2) & 31) | ((word >> 12) & 1) << 5, 6)
            if value:
                return ("lui", rd, _sign_extend(value << 12, 32))
        if fn == 4:
            subop = (word >> 10) & 3
            compact = 8 + ((word >> 7) & 7)
            if subop == 0:
                return (
                    "srli",
                    compact,
                    compact,
                    ((word >> 2) & 31) | ((word >> 12) & 1) << 5,
                )
            if subop == 1 and xlen == 64:
                return (
                    "srai",
                    compact,
                    compact,
                    ((word >> 2) & 31) | ((word >> 12) & 1) << 5,
                )
            if subop == 2:
                return ("andi", compact, compact, imm)
            if not (word & 0x1000):
                operation = {0: "sub", 1: "xor", 2: "or", 3: "and"}[(word >> 5) & 3]
                return (operation, compact, compact, 8 + ((word >> 2) & 7))
        if fn in (6, 7):
            offset = ((word >> 3) & 3) << 1 | ((word >> 10) & 3) << 3
            offset |= ((word >> 2) & 1) << 5 | ((word >> 5) & 3) << 6
            offset |= ((word >> 12) & 1) << 8
            return (
                "beq" if fn == 6 else "bne",
                8 + ((word >> 7) & 7),
                0,
                pc + _sign_extend(offset, 9),
            )
        if fn == 5:
            offset = ((word >> 3) & 7) << 1 | ((word >> 11) & 1) << 4
            offset |= ((word >> 2) & 1) << 5 | ((word >> 7) & 1) << 6
            offset |= ((word >> 6) & 1) << 7 | ((word >> 9) & 3) << 8
            offset |= ((word >> 8) & 1) << 10 | ((word >> 12) & 1) << 11
            return ("jal", 0, pc + _sign_extend(offset, 12))
    if quadrant == 2:
        if fn == 0 and rd == 0:
            return ("nop",)
        if word == 0x8082:
            return ("ret",)
        if fn == 0 and rd and 0 < (imm & 63) < xlen:
            return ("slli", rd, rd, imm & 63)
        if fn == 2 and rd:
            offset = (
                ((word >> 4) & 7) << 2
                | ((word >> 12) & 1) << 5
                | ((word >> 2) & 3) << 6
            )
            return ("lw", rd, 2, offset)
        if fn == 3 and rd and xlen == 64:
            offset = (
                ((word >> 5) & 3) << 3
                | ((word >> 12) & 1) << 5
                | ((word >> 2) & 7) << 6
            )
            return ("ld", rd, 2, offset)
        if fn == 4 and rd:
            rs2 = (word >> 2) & 31
            if word & 0x1000:
                if rs2 == 0:
                    return ("jalr", 1, rd, 0)
                return ("add", rd, rd, rs2)
            if rs2 == 0:
                return ("jalr", 0, rd, 0)
            return ("add", rd, 0, rs2)
        if fn == 6:
            return ("sw", rt, 2, ((word >> 9) & 15) << 2 | ((word >> 7) & 3) << 6)
        if fn == 7 and xlen == 64:
            return ("sd", rt, 2, ((word >> 10) & 7) << 3 | ((word >> 7) & 7) << 6)
    if quadrant == 0 and fn in (2, 3, 6, 7):
        wide = fn in (3, 7)
        if wide and xlen != 64:
            raise EvidenceError("non-integer compressed instruction")
        offset = ((word >> 10) & 7) << 3
        offset |= (
            ((word >> 5) & 3) << 6
            if wide
            else ((word >> 6) & 1) << 2 | ((word >> 5) & 1) << 6
        )
        mnemonic = {2: "lw", 3: "ld", 6: "sw", 7: "sd"}[fn]
        return (mnemonic, 8 + ((word >> 2) & 7), 8 + ((word >> 7) & 7), offset)
    raise EvidenceError("unsupported compressed instruction")


class RiscvCodeParser:
    """A bounded view of file-backed instructions between ELF anchors."""

    def __init__(
        self, elf: ElfImage, symbol: str, end_symbol: str | None = None
    ) -> None:
        start = elf.symbol_address(symbol)
        end = elf.symbol_address(end_symbol) if end_symbol else None
        section = next(
            (
                item
                for item in elf.sections
                if item.flags & 4
                and start is not None
                and item.address <= start < item.address + len(item.data)
            ),
            None,
        )
        sized = next(
            (
                item
                for item in elf.symbols
                if item.name == symbol and item.start == start
            ),
            None,
        )
        if start is None or section is None or start % 2:
            raise EvidenceError("code anchor unavailable")
        if end is None and sized is not None:
            end = sized.end
        if end is None:
            end = min(
                (
                    address
                    for _name, address in elf.anchors
                    if start < address < section.address + len(section.data)
                ),
                default=section.address + len(section.data),
            )
        if end <= start or end > section.address + len(section.data):
            raise EvidenceError("invalid code range")
        self.elf = elf
        self.start = start
        self.end = end
        self.xlen = elf.pointer_size * 8

    def instructions(self, *, terminal: tuple[str, ...] = ()) -> dict[int, tuple]:
        """Decode sequential instructions through the range or a terminal opcode."""
        result: dict[int, tuple] = {}
        pc = self.start
        while pc < self.end and len(result) < 2048:
            raw = self.elf.read_at(pc, 2)
            if raw is None:
                raise EvidenceError("instruction unavailable")
            halfword = int.from_bytes(raw, "little")
            size = 4 if halfword & 3 == 3 else 2
            if pc + size > self.end:
                raise EvidenceError("instruction outside code range")
            raw = self.elf.read_at(pc, size)
            if raw is None:
                raise EvidenceError("instruction unavailable")
            op = riscv_code_decoder(int.from_bytes(raw, "little"), size, pc, self.xlen)
            result[pc] = op
            pc += size
            if op[0] in terminal:
                return result
        if pc != self.end and len(result) >= 2048:
            raise EvidenceError("instruction budget exceeded")
        return result

    def take(self) -> tuple:
        """Decode the instruction at the current cursor and advance it."""
        if not hasattr(self, "_cursor"):
            self._cursor = self.start
        if self._cursor >= self.end:
            raise EvidenceError("instruction unavailable")
        raw = self.elf.read_at(self._cursor, 2)
        if raw is None:
            raise EvidenceError("instruction unavailable")
        halfword = int.from_bytes(raw, "little")
        size = 4 if halfword & 3 == 3 else 2
        raw = self.elf.read_at(self._cursor, size)
        if raw is None or self._cursor + size > self.end:
            raise EvidenceError("instruction unavailable")
        op = riscv_code_decoder(
            int.from_bytes(raw, "little"), size, self._cursor, self.xlen
        )
        self._cursor += size
        return op

    def expect(self, *expected) -> None:
        """Require a decoded instruction tuple at the current cursor."""
        if self.take() != expected:
            raise EvidenceError("instruction mismatch")

    def load_symbol(self, register: int, name: str) -> None:
        """Require an adjacent PC-relative load resolving to *name*."""
        pc = self._cursor if hasattr(self, "_cursor") else self.start
        upper, lower = self.take(), self.take()
        if upper[0] != "auipc" or lower[0] not in ("lw", "ld"):
            raise EvidenceError("symbol load mismatch")
        if upper[1] != register or lower[1] != register or lower[2] != register:
            raise EvidenceError("symbol load mismatch")
        address = (pc + upper[2] + lower[3]) % (1 << (self.elf.pointer_size * 8))
        if name not in {
            symbol for symbol, value in self.elf.anchors if value == address
        }:
            raise EvidenceError("symbol load mismatch")

    @property
    def store(self) -> str:
        return "sd" if self.elf.pointer_size == 8 else "sw"

    @property
    def load(self) -> str:
        return "ld" if self.elf.pointer_size == 8 else "lw"

    def next_address(self, address: int) -> int:
        """Return the next instruction address for a decoded address."""
        raw = self.elf.read_at(address, 2)
        if raw is None:
            raise EvidenceError("instruction unavailable")
        return address + (4 if int.from_bytes(raw, "little") & 3 == 3 else 2)


def riscv_next_pc(code: dict[int, tuple], pc: int) -> int | None:
    """Return the next decoded instruction address, or ``None`` at the end."""
    return next((candidate for candidate in code if candidate > pc), None)


def riscv_frame_accesses(
    code: dict[int, tuple], operation: str, base: int
) -> dict[int, set[int]]:
    """Map frame offsets to all registers saved or restored there."""
    accesses: dict[int, set[int]] = {}
    allowed = ("sw", "sd") if operation == "store" else ("lw", "ld")
    for op in code.values():
        if op[0] in allowed and op[2] == base:
            accesses.setdefault(op[3], set()).add(op[1])
    return accesses


def riscv_all_store_offsets(code: dict[int, tuple], base: int) -> set[int]:
    """Return every frame offset stored through *base*."""
    return {op[3] for op in code.values() if op[0] in ("sw", "sd") and op[2] == base}


def riscv_slot_map(accesses: dict[int, set[int]]) -> dict[int, set[int]]:
    """Invert frame-offset accesses into register-to-offsets slots."""
    return (
        {
            register: {
                offset for offset, values in accesses.items() if register in values
            }
            for register in set().union(*accesses.values())
        }
        if accesses
        else {}
    )


def riscv_stack_frame_size(code: dict[int, tuple]) -> int | None:
    """Return the single stack decrement in a frame prologue, or ``None``."""
    sizes = {
        -op[3]
        for op in code.values()
        if op[0] == "addi" and op[1:3] == (2, 2) and op[3] < 0
    }
    return next(iter(sizes)) if len(sizes) == 1 else None


def riscv_target_loads(elf: ElfImage, code: dict[int, tuple]) -> dict[int, str]:
    """Resolve AUIPC/load pairs to symbols, independent of temporary registers."""
    loads: dict[int, str] = {}
    ordered = sorted(code)
    for index, pc in enumerate(ordered[:-1]):
        upper, lower = code[pc], code[ordered[index + 1]]
        if upper[0] != "auipc" or lower[0] not in ("lw", "ld"):
            continue
        if upper[1] != lower[2]:
            continue
        address = (pc + upper[2] + lower[3]) % (1 << (elf.pointer_size * 8))
        names = {name for name, candidate in elf.anchors if candidate == address}
        if len(names) == 1:
            loads[lower[1]] = next(iter(names))
    return loads
