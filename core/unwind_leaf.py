"""Bounded instruction proofs for stack-neutral leaf returns (no target access).

Mainly handle call to asm entrypoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cpus import Cpu, cpu_of
from .insn_arm import decode_arm
from .insn_riscv import EvidenceError, riscv_code_decoder
from .insn_thumb import decode_thumb
from .unwind_meta import CodeSymbol, ElfImage

MAX_LEAF_INSTRUCTIONS = 32
MAX_LEAF_BYTES = 128
MAX_LEAF_SYMBOLS = 65536


@dataclass(frozen=True)
class LeafProof:
    """A straight-line function preserves SP/LR and ends in a register return."""

    return_register: int
    clobbered: tuple[int, ...]
    evidence: tuple[str, ...]


def _function_span(elf: ElfImage, pc: int) -> tuple[CodeSymbol, int] | None:
    functions = (*elf.symbols, *elf.zero_size_functions)
    if len(functions) > MAX_LEAF_SYMBOLS:
        return None
    covering = [symbol for symbol in elf.symbols if symbol.start <= pc < symbol.end]
    if covering:
        if len({(symbol.start, symbol.end, symbol.thumb) for symbol in covering}) != 1:
            return None
        symbol = covering[0]
        return symbol, symbol.end
    preceding = [symbol for symbol in elf.zero_size_functions if symbol.start <= pc]
    if not preceding:
        return None
    start = max(symbol.start for symbol in preceding)
    candidates = [symbol for symbol in preceding if symbol.start == start]
    if len({symbol.thumb for symbol in candidates}) != 1:
        return None
    section = next(
        (
            section
            for section in elf.sections
            if section.flags & 4
            and section.address <= start < section.address + len(section.data)
        ),
        None,
    )
    if section is None:
        return None
    end = min(
        [section.address + len(section.data)]
        + [
            symbol.start
            for symbol in functions
            if start < symbol.start < section.address + len(section.data)
        ]
        + [
            address
            for name, address in elf.anchors
            if name.startswith("$d")
            and start < address < section.address + len(section.data)
        ]
    )
    return (candidates[0], end) if pc < end else None


def _thumb_step(
    elf: ElfImage, pc: int, end: int
) -> tuple[int, int | None, bool] | None:
    instruction = decode_thumb(elf, pc, end)
    if isinstance(instruction, str):
        return None
    mnemonic, operands = instruction.mnemonic, instruction.operands
    if "cond" in operands and operands.get("cond") not in ("al", "unconditional"):
        return None
    if mnemonic == "bx":
        return (instruction.size, None, True) if operands.get("rm") == 14 else None
    if mnemonic in {"nop", "cpsie", "cpsid", "dsb", "isb", "str_imm"}:
        return instruction.size, None, False
    if mnemonic == "msr":
        return (
            (instruction.size, None, False)
            if operands.get("sysm") in (16, 17, 18, 19)
            else None
        )
    if mnemonic in {"mrs", "mov_imm", "orr_imm", "ldr_literal", "ldr_imm"}:
        register = int(operands["rt"] if "rt" in operands else operands["rd"])
        return (
            (instruction.size, register, False)
            if register not in (13, 14, 15)
            else None
        )
    return None


def _arm_step(elf: ElfImage, pc: int, end: int) -> tuple[int, int | None, bool] | None:
    instruction = decode_arm(elf, pc, end)
    if isinstance(instruction, str):
        return None
    mnemonic, operands = instruction.mnemonic, instruction.operands
    if operands.get("cond") not in ("al", "unconditional"):
        return None
    if mnemonic == "bx":
        return (instruction.size, None, True) if operands.get("rm") == 14 else None
    if mnemonic == "nop":
        return instruction.size, None, False
    if mnemonic == "cps":
        return (
            (instruction.size, None, False)
            if operands.get("mode") == 0 and operands.get("interrupt_mode") in (2, 3)
            else None
        )
    if mnemonic == "mrs":
        register = operands.get("rd")
        return (
            (instruction.size, int(register), False)
            if isinstance(register, int) and register not in (13, 14, 15)
            else None
        )
    if mnemonic in {
        "and",
        "eor",
        "sub",
        "rsb",
        "add",
        "adc",
        "sbc",
        "rsc",
        "orr",
        "mov",
        "bic",
        "mvn",
        "and_imm",
        "bic_imm",
    }:
        register = operands.get("rd")
        return (
            (instruction.size, int(register), False)
            if isinstance(register, int) and register not in (13, 14, 15)
            else None
        )
    if mnemonic in {"cmp", "cmn", "tst", "teq"}:
        return instruction.size, None, False
    return None


def _riscv_step(
    elf: ElfImage, pc: int, end: int
) -> tuple[int, int | None, bool] | None:
    raw = elf.read_at(pc, 2)
    if raw is None:
        return None
    size = 4 if int.from_bytes(raw, "little") & 3 == 3 else 2
    raw = elf.read_at(pc, size) if pc + size <= end else None
    if raw is None:
        return None
    try:
        instruction = riscv_code_decoder(
            int.from_bytes(raw, "little"), size, pc, elf.pointer_size * 8
        )
    except EvidenceError:
        return None
    mnemonic = instruction[0]
    if mnemonic == "ret" or instruction == ("jalr", 0, 1, 0):
        return size, None, True
    if mnemonic in {"nop", "sw", "sd", "csrw", "csrs"}:
        return size, None, False
    if mnemonic in {
        "addi",
        "addiw",
        "andi",
        "xori",
        "slli",
        "srli",
        "srai",
        "lui",
        "auipc",
        "add",
        "sub",
        "or",
        "xor",
        "and",
        "lw",
        "ld",
        "csrr",
    }:
        register = instruction[1]
        return (
            (size, register if register else None, False)
            if register not in (1, 2)
            else None
        )
    return None


def prove_leaf_return(elf: ElfImage, pc: int) -> LeafProof | None:
    """Prove a complete straight-line SP/LR-preserving path containing an exact PC.

    Function types and Arm instruction state come from ELF symbols, not names.
    Only the bounded scalar subset is admitted; branches, calls, SP/LR writes,
    stack-slot changes, missing boundaries and unknown instructions are refused.
    The function reads ELF bytes only and never accesses target memory.
    """
    cpu: Cpu = cpu_of(elf)
    if cpu.reason != "ok" or elf.endian != "little":
        return None
    span = _function_span(elf, pc)
    if span is None:
        return None
    symbol, end = span
    if not elf.is_code(symbol.start) or not elf.is_code(end - 1):
        return None
    if cpu.family == "riscv":
        step, return_register = _riscv_step, 1
    elif cpu.isa == "armv7-a":
        if symbol.thumb is None:
            return None
        step, return_register = (_thumb_step if symbol.thumb else _arm_step), 14
    else:
        if symbol.thumb is False:
            return None
        step, return_register = _thumb_step, 14
    address = symbol.start
    limit = min(end, address + MAX_LEAF_BYTES)
    clobbered = set()
    found_pc = False
    for _ in range(MAX_LEAF_INSTRUCTIONS):
        decoded = step(elf, address, limit)
        if decoded is None:
            return None
        size, register, returning = decoded
        found_pc |= address == pc
        if register is not None:
            clobbered.add(register)
        address += size
        if returning:
            if not found_pc:
                return None
            return LeafProof(
                return_register,
                tuple(sorted(clobbered)),
                (
                    f"instruction leaf {cpu.isa} {symbol.name} [{symbol.start:#x},{address:#x}) preserves SP and returns via r{return_register}",
                ),
            )
        if address >= limit:
            return None
    return None
