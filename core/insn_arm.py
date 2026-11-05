"""Bounded ARM/A32 instruction decoding for code-evidence checks (pure)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .unwind_meta import ElfImage, _sign_extend


@dataclass(frozen=True)
class ArmInstruction:
    """One decoded 32-bit ARM instruction with normalized operands."""

    address: int
    size: int
    mnemonic: str
    operands: Mapping[str, int | bool | str]


_CONDITIONS = (
    "eq",
    "ne",
    "cs",
    "cc",
    "mi",
    "pl",
    "vs",
    "vc",
    "hi",
    "ls",
    "ge",
    "lt",
    "gt",
    "le",
    "al",
    "nv",
)
_DATA_PROCESSING = (
    "and",
    "eor",
    "sub",
    "rsb",
    "add",
    "adc",
    "sbc",
    "rsc",
    "tst",
    "teq",
    "cmp",
    "cmn",
    "orr",
    "mov",
    "bic",
    "mvn",
)


def decode_arm(elf: ElfImage, address: int, end: int) -> ArmInstruction | str:
    """Decode one aligned 32-bit A32 instruction inside ``[address, end)``."""
    if address < 0 or address & 3 or address + 4 > end:
        return "instruction-outside-function"
    raw = elf.read_at(address, 4)
    if raw is None or len(raw) != 4:
        return "instruction-unreadable"
    if elf.endian not in ("little", "big"):
        return "instruction-endian-unsupported"
    return decode_arm_word(address, int.from_bytes(raw, elf.endian))


def decode_arm_word(address: int, word: int) -> ArmInstruction:
    """Decode the bounded A32 instruction subset used for runtime proofs."""
    word &= 0xFFFFFFFF
    condition = (word >> 28) & 0xF
    cond = _CONDITIONS[condition]

    if word & 0x0FFFFFF0 == 0x012FFF10:
        return _instruction(address, "bx", {"rm": word & 0xF, "cond": cond})

    if word & 0x0E000000 == 0x0A000000:
        if condition == 0xF:
            mnemonic = "blx"
            offset = _sign_extend(((word & 0xFFFFFF) << 2) | ((word >> 23) & 2), 26)
        else:
            mnemonic = "bl" if word & 0x01000000 else "b"
            offset = _sign_extend(word & 0xFFFFFF, 24) << 2
        return _instruction(
            address, mnemonic, {"target": address + 8 + offset, "cond": cond}
        )

    if word & 0x0FBF0FFF == 0x010F0000:
        return _instruction(
            address,
            "mrs",
            {
                "rd": (word >> 12) & 0xF,
                "psr": "spsr" if word & (1 << 22) else "cpsr",
                "cond": cond,
            },
        )

    if condition == 0xF and word & 0xFFF00000 == 0xF1000000:
        interrupt_mode = (word >> 18) & 3
        flags = (word >> 6) & 7
        mode = word & 0x1F
        return _instruction(
            address,
            "cps",
            {
                "interrupt_mode": interrupt_mode,
                "flags": flags,
                "mode": mode,
                "cond": "unconditional",
            },
        )

    if word & 0x0FB00000 in (0x03000000, 0x03400000):
        top = word & 0x00400000
        immediate = (((word >> 16) & 0xF) << 12) | (word & 0xFFF)
        return _instruction(
            address,
            "movt" if top else "movw",
            {"rd": (word >> 12) & 0xF, "imm": immediate, "cond": cond},
        )

    if word & 0x0FFFFFFF == 0x0320F000:
        return _instruction(address, "nop", {"cond": cond})

    if word & 0x0FB0FFF0 == 0x0120F000:
        return _instruction(
            address,
            "msr",
            {
                "psr": "spsr" if word & (1 << 22) else "cpsr",
                "fields": (word >> 16) & 0xF,
                "rm": word & 0xF,
                "cond": cond,
            },
        )

    if word & 0x0DB0F000 == 0x0320F000:
        return _instruction(
            address,
            "msr",
            {
                "psr": "spsr" if word & (1 << 22) else "cpsr",
                "fields": (word >> 16) & 0xF,
                "immediate": _ror(word & 0xFF, ((word >> 8) & 0xF) * 2),
                "cond": cond,
            },
        )

    if word & 0x0E000000 == 0x08000000:
        load = bool(word & (1 << 20))
        pre = bool(word & (1 << 24))
        up = bool(word & (1 << 23))
        mode = ("i" if up else "d") + ("b" if pre else "a")
        return _instruction(
            address,
            ("ldm" if load else "stm") + mode,
            {
                "rn": (word >> 16) & 0xF,
                "wback": bool(word & (1 << 21)),
                "user_bank": bool(word & (1 << 22)),
                "reglist": word & 0xFFFF,
                "cond": cond,
            },
        )

    if word & 0x0C000000 == 0x04000000 and not word & (1 << 25):
        load = bool(word & (1 << 20))
        return _instruction(
            address,
            "ldr" if load else "str",
            {
                "rd": (word >> 12) & 0xF,
                "rn": (word >> 16) & 0xF,
                "offset": word & 0xFFF,
                "pre": bool(word & (1 << 24)),
                "up": bool(word & (1 << 23)),
                "byte": bool(word & (1 << 22)),
                "wback": bool(word & (1 << 21)),
                "cond": cond,
            },
        )

    if word & 0x0E000000 == 0x02000000:
        operation = (word >> 21) & 0xF
        mnemonic = _DATA_PROCESSING[operation]
        immediate = _ror(word & 0xFF, ((word >> 8) & 0xF) * 2)
        operands: dict[str, int | bool | str] = {
            "immediate": immediate,
            "set_flags": bool(word & (1 << 20))
            or mnemonic in {"tst", "teq", "cmp", "cmn"},
            "cond": cond,
        }
        if mnemonic in {"tst", "teq", "cmp", "cmn"}:
            operands["rn"] = (word >> 16) & 0xF
        elif mnemonic in {"mov", "mvn"}:
            operands["rd"] = (word >> 12) & 0xF
        else:
            operands["rd"] = (word >> 12) & 0xF
            operands["rn"] = (word >> 16) & 0xF
        return _instruction(address, mnemonic, operands)

    if word & 0x0C000000 == 0 and not word & (1 << 25):
        if word & 0x0FC000F0 == 0x00000090:
            return _instruction(address, "other", {"word": word})
        operation = (word >> 21) & 0xF
        if operation in (8, 9, 10, 11) and word & (1 << 20):
            mnemonic = _DATA_PROCESSING[operation]
            operands: dict[str, int | bool | str] = {
                "rn": (word >> 16) & 0xF,
                "rm": word & 0xF,
                "set_flags": True,
                "cond": cond,
            }
            if word & (1 << 4):
                operands["shift"] = (word >> 5) & 3
                operands["shift_register"] = (word >> 8) & 0xF
            else:
                operands["shift"] = (word >> 5) & 0x1F
            return _instruction(address, mnemonic, operands)
        if operation in (8, 9, 10, 11):
            return _instruction(address, "other", {"word": word})
        mnemonic = _DATA_PROCESSING[operation]
        operands = {
            "rm": word & 0xF,
            "set_flags": bool(word & (1 << 20)),
            "cond": cond,
        }
        if word & (1 << 4):
            operands["shift"] = (word >> 5) & 3
            operands["shift_register"] = (word >> 8) & 0xF
        else:
            operands["shift"] = (word >> 5) & 0x1F
        if mnemonic in {"mov", "mvn"}:
            operands["rd"] = (word >> 12) & 0xF
        else:
            operands["rd"] = (word >> 12) & 0xF
            operands["rn"] = (word >> 16) & 0xF
        return _instruction(address, mnemonic, operands)

    return _instruction(address, "other", {"word": word})


def decode_arm_stream(
    elf: ElfImage, start: int, end: int, limit: int
) -> tuple[ArmInstruction, ...] | None:
    """Decode every instruction in a bounded A32 code span or refuse it."""
    if start < 0 or start & 3 or end <= start or end & 3 or limit <= 0:
        return None
    instructions: list[ArmInstruction] = []
    address = start
    while address < end and len(instructions) < limit:
        instruction = decode_arm(elf, address, end)
        if isinstance(instruction, str) or instruction.mnemonic == "other":
            return None
        instructions.append(instruction)
        address += instruction.size
    return tuple(instructions) if address == end else None


def decode_arm_prefix(
    elf: ElfImage, start: int, end: int, count: int
) -> tuple[ArmInstruction, ...] | None:
    """Decode exactly *count* sequential A32 instructions within a code span."""
    if start < 0 or start & 3 or end <= start or count <= 0:
        return None
    instructions: list[ArmInstruction] = []
    address = start
    while len(instructions) < count:
        instruction = decode_arm(elf, address, end)
        if isinstance(instruction, str) or instruction.mnemonic == "other":
            return None
        instructions.append(instruction)
        address += instruction.size
    return tuple(instructions)


def expect_arm(
    instruction: ArmInstruction,
    mnemonic: str,
    expected: Mapping[str, int | bool | str],
) -> bool:
    """Match a decoded mnemonic and its required semantic operands."""
    return instruction.mnemonic == mnemonic and all(
        instruction.operands.get(key) == value for key, value in expected.items()
    )


def _instruction(
    address: int, mnemonic: str, operands: Mapping[str, int | bool | str]
) -> ArmInstruction:
    return ArmInstruction(address, 4, mnemonic, operands)


def _ror(value: int, amount: int) -> int:
    amount %= 32
    value &= 0xFFFFFFFF
    return (
        ((value >> amount) | (value << (32 - amount))) & 0xFFFFFFFF if amount else value
    )
