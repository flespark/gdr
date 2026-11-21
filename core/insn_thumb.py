"""Bounded Arm Thumb/Thumb-2 instruction decoding and stream helpers (pure).

This module is RTOS-agnostic. It decodes the Thumb-1 and Thumb-2 encodings and
literal-pool symbol tokens used by adapter-side code-evidence checks without
depending on GDB or any RTOS symbol names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .elf import ElfImage, _sign_extend

_THUMB_CONDITIONS = (
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


@dataclass(frozen=True)
class ThumbInstruction:
    """One decoded Arm/Thumb instruction with normalized operands."""

    address: int
    size: int
    mnemonic: str
    operands: Mapping[str, int | bool | str]


def read_thumb_u16(elf: ElfImage, address: int) -> int | None:
    """Read one little-endian 16-bit halfword from *elf* at *address*."""
    raw = elf.read_at(address, 2)
    if raw is None or len(raw) != 2:
        return None
    return int.from_bytes(raw, "little")


def read_thumb_u32(elf: ElfImage, address: int) -> int | None:
    """Read one little-endian 32-bit word from *elf* at *address*."""
    raw = elf.read_at(address, 4)
    if raw is None or len(raw) != 4:
        return None
    return int.from_bytes(raw, "little")


def decode_thumb(elf: ElfImage, address: int, end: int) -> ThumbInstruction | str:
    """Decode one 16-bit or 32-bit Thumb instruction within ``[address, end)``.

    Unknown encodings return ``mnemonic="other"`` with their architectural
    width so a scan can advance; unreadable bytes or an instruction that does
    not fit the verified function span return a refusal reason string.
    """
    if address < 0 or address + 2 > end:
        return "instruction-outside-function"
    first = read_thumb_u16(elf, address)
    if first is None:
        return "instruction-unreadable"
    if first & 0xF800 not in (0xE800, 0xF000, 0xF800):
        return decode_thumb16(address, first)
    if address + 4 > end:
        return "instruction-outside-function"
    second = read_thumb_u16(elf, address + 2)
    if second is None:
        return "instruction-unreadable"
    return decode_thumb32(address, first, second)


def decode_thumb16(address: int, hw: int) -> ThumbInstruction:
    """Decode one 16-bit Thumb instruction halfword."""
    if hw & 0xFF00 == 0xDF00:
        return ThumbInstruction(address, 2, "svc", {"imm": hw & 0xFF})
    if hw & 0xFF87 == 0x4700:
        return ThumbInstruction(address, 2, "bx", {"rm": (hw >> 3) & 0xF})
    if hw == 0xBF00:
        return ThumbInstruction(address, 2, "nop", {})
    if hw & 0xFF00 == 0xBF00 and hw & 0xF != 0 and (hw >> 4) & 0xF != 0xF:
        return ThumbInstruction(
            address,
            2,
            "it",
            {"firstcond": (hw >> 4) & 0xF, "mask": hw & 0xF},
        )
    if hw in (0xB661, 0xB662):
        return ThumbInstruction(
            address, 2, "cpsie", {"flag": "f" if hw == 0xB661 else "i"}
        )
    if hw in (0xB671, 0xB672):
        return ThumbInstruction(
            address, 2, "cpsid", {"flag": "f" if hw == 0xB671 else "i"}
        )
    if hw & 0xFE00 == 0xB400:
        return ThumbInstruction(address, 2, "push", {"reglist": hw & 0x1FF})
    if hw & 0xFE00 == 0xBC00:
        return ThumbInstruction(address, 2, "pop", {"reglist": hw & 0x1FF})
    if hw & 0xF800 == 0x4800:
        literal = ((address + 4) & ~3) + (hw & 0xFF) * 4
        return ThumbInstruction(
            address,
            2,
            "ldr_literal",
            {"rt": (hw >> 8) & 7, "literal": literal},
        )
    if hw & 0xF800 == 0x6800:
        return ThumbInstruction(
            address,
            2,
            "ldr_imm",
            {
                "rt": hw & 7,
                "rn": (hw >> 3) & 7,
                "imm": ((hw >> 6) & 0x1F) * 4,
            },
        )
    if hw & 0xF800 == 0x6000:
        return ThumbInstruction(
            address,
            2,
            "str_imm",
            {
                "rt": hw & 7,
                "rn": (hw >> 3) & 7,
                "imm": ((hw >> 6) & 0x1F) * 4,
            },
        )
    if hw & 0xF800 == 0x2000:
        return ThumbInstruction(
            address, 2, "mov_imm", {"rd": (hw >> 8) & 7, "imm": hw & 0xFF}
        )
    if hw & 0xF800 == 0xA800:
        return ThumbInstruction(
            address,
            2,
            "add_imm",
            {"rd": (hw >> 8) & 7, "rn": 13, "imm": (hw & 0xFF) * 4},
        )
    if hw & 0xF000 == 0xC000:
        load = bool(hw & 0x0800)
        return ThumbInstruction(
            address,
            2,
            "ldmia" if load else "stmia",
            {
                "rn": (hw >> 8) & 7,
                "wback": True,
                "load": load,
                "direction": "ia",
                "reglist": hw & 0xFF,
            },
        )
    if hw & 0xFF00 == 0xB000:
        return ThumbInstruction(
            address,
            2,
            "add_sp" if hw & 0x0080 == 0 else "sub_sp",
            {"imm": (hw & 0x7F) * 4},
        )
    if hw & 0xF800 in (0x0000, 0x0800, 0x1000):
        operation = (hw >> 11) & 3
        return ThumbInstruction(
            address,
            2,
            ("lsls", "lsrs", "asrs")[operation],
            {
                "rd": hw & 7,
                "rm": (hw >> 3) & 7,
                "shift_amount": (hw >> 6) & 0x1F,
                "set_flags": True,
            },
        )
    if hw & 0xF500 == 0xB100:
        offset = (((hw >> 9) & 1) << 6) | (((hw >> 3) & 0x1F) << 1)
        return ThumbInstruction(
            address,
            2,
            "cbnz" if hw & 0x0800 else "cbz",
            {"rn": hw & 7, "target": address + 4 + offset},
        )
    if hw & 0xF000 == 0xD000:
        condition = (hw >> 8) & 0xF
        if condition < 0xE:
            offset = _sign_extend(hw & 0xFF, 8) << 1
            return ThumbInstruction(
                address,
                2,
                "b_cond",
                {"cond": _THUMB_CONDITIONS[condition], "target": address + 4 + offset},
            )
    if hw & 0xF800 == 0xE000:
        offset = _sign_extend(hw & 0x7FF, 11) << 1
        return ThumbInstruction(address, 2, "b", {"target": address + 4 + offset})
    if hw & 0xF800 in (0x1800, 0x1A00, 0x1C00, 0x1E00):
        immediate = bool(hw & 0x0400)
        subtract = bool(hw & 0x0200)
        operands: dict[str, int | bool | str] = {
            "rd": hw & 7,
            "set_flags": True,
        }
        if immediate:
            operands["rn"] = (hw >> 3) & 7
            operands["imm"] = (hw >> 6) & 7
        else:
            operands["rn"] = (hw >> 6) & 7
            operands["rm"] = (hw >> 3) & 7
        return ThumbInstruction(
            address,
            2,
            "subs" if subtract else "adds",
            operands,
        )
    if hw & 0xF800 in (0x2800, 0x3000, 0x3800):
        operation = hw & 0xF800
        register = (hw >> 8) & 7
        immediate = hw & 0xFF
        if operation == 0x2800:
            return ThumbInstruction(
                address, 2, "cmp", {"rn": register, "imm": immediate}
            )
        return ThumbInstruction(
            address,
            2,
            "subs" if operation == 0x3800 else "adds",
            {"rd": register, "rn": register, "imm": immediate, "set_flags": True},
        )
    if hw & 0xFC00 == 0x4000:
        operation = (hw >> 6) & 0xF
        names = (
            "ands",
            "eors",
            "lsls",
            "lsrs",
            "asrs",
            "adcs",
            "sbcs",
            "rors",
            "tst",
            "rsbs",
            "cmp",
            "cmn",
            "orrs",
            "muls",
            "bics",
            "mvns",
        )
        mnemonic = names[operation]
        operands = {"rm": (hw >> 3) & 7, "set_flags": True}
        operands["rn"] = hw & 7
        if mnemonic not in {"tst", "cmp", "cmn"}:
            operands["rd"] = hw & 7
        return ThumbInstruction(address, 2, mnemonic, operands)
    if hw & 0xFC00 == 0x4400:
        operation = (hw >> 8) & 3
        rm = (hw >> 3) & 0xF
        rd = ((hw >> 4) & 8) | (hw & 7)
        if operation == 3:
            return ThumbInstruction(address, 2, "other", {"first": hw})
        operands = {"rm": rm, "set_flags": operation == 1}
        if operation == 1:
            operands["rn"] = rd
        else:
            operands["rd"] = rd
            if operation == 0:
                operands["rn"] = rd
        return ThumbInstruction(
            address,
            2,
            ("add", "cmp", "mov")[operation],
            operands,
        )
    if hw & 0xFE00 == 0xB200:
        mnemonic = {0xB200: "sxth", 0xB240: "sxtb", 0xB280: "uxth", 0xB2C0: "uxtb"}[
            hw & 0xFFC0
        ]
        return ThumbInstruction(
            address,
            2,
            mnemonic,
            {"rd": hw & 7, "rm": (hw >> 3) & 7},
        )
    if hw & 0xFE00 == 0x5800:
        return ThumbInstruction(
            address,
            2,
            "ldr_reg",
            {"rt": hw & 7, "rn": (hw >> 3) & 7, "rm": (hw >> 6) & 7},
        )
    return ThumbInstruction(address, 2, "other", {"first": hw})


def decode_block_transfer(
    address: int, first: int, second: int
) -> ThumbInstruction | None:
    """Decode the Thumb-2 LDM/STM (T1) operation with all semantic bits.

    Bits 15-6 select increment-after/decrement-before, bit 5 is writeback and
    bit 4 is load/store. Keeping the direction in the mnemonic and in explicit
    operands ensures a flipped direction bit cannot alias a required transfer.
    """
    if first & 0xFFC0 == 0xE880:
        direction = "ia"
    elif first & 0xFFC0 == 0xE900:
        direction = "db"
    else:
        return None
    load = bool(first & 0x10)
    mnemonic = ("ldm" if load else "stm") + direction
    return ThumbInstruction(
        address,
        4,
        mnemonic,
        {
            "rn": first & 0xF,
            "wback": bool(first & 0x20),
            "load": load,
            "direction": direction,
            "reglist": second,
        },
    )


def decode_thumb32(address: int, first: int, second: int) -> ThumbInstruction:
    """Decode one 32-bit Thumb-2 instruction from its two 16-bit halfwords."""
    if first == 0xF3EF and second & 0xF000 == 0x8000:
        return ThumbInstruction(
            address,
            4,
            "mrs",
            {"rd": (second >> 8) & 0xF, "sysm": second & 0xFF},
        )
    if first & 0xFFF0 == 0xF380 and second & 0xFF00 == 0x8800:
        return ThumbInstruction(
            address,
            4,
            "msr",
            {"rn": first & 0xF, "sysm": second & 0xFF},
        )
    if first == 0xF3AF and second == 0x8000:
        return ThumbInstruction(address, 4, "nop", {})
    if first & 0xFBE0 == 0xF040 and first & 0x000F == 0x000F and second & 0x8000 == 0:
        imm12 = (
            (((first >> 10) & 1) << 11) | (((second >> 12) & 7) << 8) | (second & 0xFF)
        )
        return ThumbInstruction(
            address,
            4,
            "mov_imm",
            {"rd": (second >> 8) & 0xF, "imm": _thumb_expand_imm(imm12)},
        )
    if first & 0xFBF0 in (0xF000, 0xF020) and second & 0x8000 == 0:
        immediate = (
            (((first >> 10) & 1) << 11) | (((second >> 12) & 7) << 8) | (second & 0xFF)
        )
        mnemonic = "and_imm" if first & 0x0020 == 0 else "bic_imm"
        return ThumbInstruction(
            address,
            4,
            mnemonic,
            {
                "rd": (second >> 8) & 0xF,
                "rn": first & 0xF,
                "imm": _thumb_expand_imm(immediate),
            },
        )
    if first & 0xFBF0 == 0xF010 and second & 0x8F00 == 0x0F00:
        immediate = (
            (((first >> 10) & 1) << 11) | (((second >> 12) & 7) << 8) | (second & 0xFF)
        )
        return ThumbInstruction(
            address,
            4,
            "tst_imm",
            {"rn": first & 0xF, "imm": _thumb_expand_imm(immediate)},
        )
    if first & 0xFBE0 == 0xF040 and second & 0x8000 == 0:
        imm12 = (
            (((first >> 10) & 1) << 11) | (((second >> 12) & 7) << 8) | (second & 0xFF)
        )
        return ThumbInstruction(
            address,
            4,
            "orr_imm",
            {
                "rd": (second >> 8) & 0xF,
                "rn": first & 0xF,
                "imm": _thumb_expand_imm(imm12),
            },
        )
    if first & 0xFFF0 in (0xF8C0, 0xF8D0):
        return ThumbInstruction(
            address,
            4,
            "str_imm" if first & 0xFFF0 == 0xF8C0 else "ldr_imm",
            {
                "rn": first & 0xF,
                "rt": (second >> 12) & 0xF,
                "imm": second & 0xFFF,
            },
        )
    if first & 0xFFF0 == 0xF840 and second & 0x0F00 == 0x0D00:
        return ThumbInstruction(
            address,
            4,
            "str_pre",
            {
                "rn": first & 0xF,
                "rt": (second >> 12) & 0xF,
                "imm": -(second & 0xFF),
            },
        )
    block = decode_block_transfer(address, first, second)
    if block is not None:
        return block
    if first & 0xFFB0 in (0xED20, 0xECB0) and second & 0x0F00 == 0x0B00:
        return ThumbInstruction(
            address,
            4,
            "vstmdb" if first & 0xFFB0 == 0xED20 else "vldmia",
            {
                "rn": first & 0xF,
                "first": (((first >> 6) & 0x1) << 4) | ((second >> 12) & 0xF),
                "count": (second & 0xFF) // 2,
            },
        )
    if first & 0xFFB0 in (0xED20, 0xECB0) and second & 0x0F00 == 0x0A00:
        return ThumbInstruction(
            address,
            4,
            "vstmdb" if first & 0xFFB0 == 0xED20 else "vldmia",
            {
                "rn": first & 0xF,
                "first": (((second >> 12) & 0xF) << 1) | ((first >> 6) & 0x1),
                "count": second & 0xFF,
                "single": True,
            },
        )
    if first == 0xF3BF and second in (0x8F4F, 0x8F6F):
        return ThumbInstruction(address, 4, "dsb" if second == 0x8F4F else "isb", {})
    mov_encoding = first & 0xFBF0
    if mov_encoding in (0xF240, 0xF2C0):
        immediate = (
            (((first >> 10) & 1) << 11)
            | ((first & 0xF) << 12)
            | (((second >> 12) & 7) << 8)
            | (second & 0xFF)
        )
        return ThumbInstruction(
            address,
            4,
            "movt" if mov_encoding == 0xF2C0 else "movw",
            {"rd": (second >> 8) & 0xF, "imm": immediate},
        )
    if first & 0xFFF0 == 0xFAB0 and second & 0xF0F0 == 0xF080:
        return ThumbInstruction(
            address, 4, "clz", {"rd": (second >> 8) & 0xF, "rm": second & 0xF}
        )
    if first & 0xFFF0 == 0xF850 and second & 0x0F00 == 0x0B00:
        return ThumbInstruction(
            address,
            4,
            "ldr_post",
            {
                "rn": first & 0xF,
                "rt": (second >> 12) & 0xF,
                "imm": second & 0xFF,
            },
        )
    if first & 0xFFF0 == 0xF850 and second & 0x0FC0 == 0:
        return ThumbInstruction(
            address,
            4,
            "ldr_reg",
            {
                "rt": (second >> 12) & 0xF,
                "rn": first & 0xF,
                "rm": second & 0xF,
                "shift_amount": (second >> 4) & 3,
            },
        )
    if first & 0xFE00 == 0xEA00:
        operation = (first >> 5) & 0xF
        if operation == 0xD:
            return ThumbInstruction(
                address,
                4,
                "ubfx",
                {
                    "rn": first & 0xF,
                    "rd": (second >> 8) & 0xF,
                    "lsb": (((second >> 12) & 7) << 2) | ((second >> 6) & 3),
                    "width": (second & 0x1F) + 1,
                },
            )
    if first & 0xF800 == 0xF000 and second & 0xD000 == 0xD000:
        s = (first >> 10) & 1
        i1 = (~(((second >> 13) & 1) ^ s)) & 1
        i2 = (~(((second >> 11) & 1) ^ s)) & 1
        offset = _sign_extend(
            (s << 24)
            | (i1 << 23)
            | (i2 << 22)
            | ((first & 0x3FF) << 12)
            | ((second & 0x7FF) << 1),
            25,
        )
        return ThumbInstruction(address, 4, "bl", {"target": address + 4 + offset})
    if first & 0xF800 == 0xF000:
        operation = (first >> 5) & 0xF
        names = {
            0: "and",
            1: "bic",
            2: "orr",
            3: "orn",
            4: "eor",
            8: "add",
            10: "adc",
            11: "sbc",
            13: "cmp",
            14: "rsb",
        }
        mnemonic = names.get(operation)
        if mnemonic is not None:
            immediate = (
                (((first >> 10) & 1) << 11)
                | (((second >> 12) & 7) << 8)
                | (second & 0xFF)
            )
            operands = {
                "rn": first & 0xF,
                "immediate": _thumb_expand_imm(immediate),
                "set_flags": mnemonic == "cmp" or bool(second & 0x1000),
            }
            if mnemonic not in {"cmp", "cmn", "tst", "teq"}:
                operands["rd"] = (second >> 8) & 0xF
            return ThumbInstruction(address, 4, mnemonic, operands)
    if first & 0xFE00 == 0xEA00:
        operation = (first >> 5) & 0xF
        names = {0: "and", 1: "bic", 2: "orr", 4: "eor", 8: "add", 13: "sub", 14: "rsb"}
        mnemonic = names.get(operation)
        if mnemonic is not None:
            return ThumbInstruction(
                address,
                4,
                mnemonic,
                {
                    "rn": first & 0xF,
                    "rd": (second >> 8) & 0xF,
                    "rm": second & 0xF,
                    "shift": (second >> 4) & 3,
                    "shift_amount": (((second >> 12) & 7) << 2) | ((second >> 6) & 3),
                    "set_flags": bool(first & 0x10),
                },
            )
    return ThumbInstruction(address, 4, "other", {"first": first, "second": second})


def _thumb_expand_imm(imm12: int) -> int:
    """Expand the Thumb-2 modified-immediate field to its 32-bit value."""
    imm8 = imm12 & 0xFF
    if imm12 & 0xC00 == 0:
        mode = (imm12 >> 8) & 3
        if mode == 0:
            return imm8
        if imm8 == 0:
            return 0
        if mode == 1:
            return (imm8 << 16) | imm8
        if mode == 2:
            return (imm8 << 24) | (imm8 << 8)
        return (imm8 << 24) | (imm8 << 16) | (imm8 << 8) | imm8
    unrotated = 0x80 | (imm12 & 0x7F)
    rotation = (imm12 >> 7) & 0x1F
    return ((unrotated >> rotation) | (unrotated << (32 - rotation))) & 0xFFFFFFFF


def resolved_literal(elf: ElfImage, instruction: ThumbInstruction) -> int | None:
    """Resolve the 32-bit word referenced by a PC-relative literal load."""
    literal = instruction.operands.get("literal")
    if not isinstance(literal, int):
        return None
    return read_thumb_u32(elf, literal)


def symbol_value_token(elf: ElfImage, value: int) -> str:
    """Normalize one literal/branch value by its symbol, not its link address."""
    if value == 0:
        return "0"
    symbol = elf.function_containing(value)
    if symbol is not None:
        offset = value - symbol.start
        return f"fn:{symbol.name}" + (f"+{offset:#x}" if offset else "")
    names = sorted(
        {
            name
            for name, address in elf.anchors
            if address == value and not name.startswith("$")
        }
    )
    if len(names) == 1:
        return f"sym:{names[0]}"
    return f"val:{value:#x}"


def semantic_token(elf: ElfImage, instruction: ThumbInstruction) -> str:
    """Describe one instruction without any relocatable absolute address."""
    operands = dict(instruction.operands)
    if instruction.mnemonic == "other":
        first = int(operands.get("first", 0))
        second = operands.get("second")
        if isinstance(second, int):
            value = first | (second << 16)
            token = symbol_value_token(elf, value)
            if token.startswith(("fn:", "sym:")):
                return f"raw_addr:{token}"
            return f"raw32:{first:#06x}:{second:#06x}"
        return f"raw16:{first:#06x}"
    if instruction.mnemonic == "ldr_literal":
        value = resolved_literal(elf, instruction)
        return (
            f"ldr_literal:r{operands.get('rt')}:"
            f"{symbol_value_token(elf, value if value is not None else 0)}"
        )
    if instruction.mnemonic == "bl":
        target = operands.get("target")
        return f"bl:{symbol_value_token(elf, int(target) if target is not None else 0)}"
    if instruction.mnemonic in {"b", "b_cond", "cbz", "cbnz"}:
        target = operands.pop("target", None)
        token = (
            symbol_value_token(elf, int(target)) if target is not None else "unknown"
        )
        return f"{instruction.mnemonic}:{token}:" + ",".join(
            f"{key}={value}" for key, value in sorted(operands.items())
        )
    operands.pop("literal", None)
    body = ",".join(f"{key}={value}" for key, value in sorted(operands.items()))
    return f"{instruction.mnemonic}:{body}"


def literal_pool_start(elf: ElfImage, start: int, end: int) -> int:
    """Find the aligned symbol-pointer suffix that follows executable code."""
    pool_start = end
    address = end - 4
    while address >= start and (address - start) % 2 == 0:
        value = read_thumb_u32(elf, address)
        if value is None:
            break
        token = symbol_value_token(elf, value)
        if not token.startswith(("fn:", "sym:")):
            break
        pool_start = address
        address -= 4
    return pool_start


def decode_thumb_stream(
    elf: ElfImage,
    symbol: str,
    start: int,
    end: int,
    limit: int,
    *,
    prefix: str = "profile",
    reject_unknown: bool = False,
) -> tuple[tuple[str, ...] | None, str]:
    """Decode every instruction and literal-pool word in one function span."""
    if end <= start or end - start < 2:
        return None, f"{prefix}-symbol-unbounded:{symbol}"
    pool_start = literal_pool_start(elf, start, end)
    tokens: list[str] = []
    address = start
    while address < pool_start:
        if len(tokens) >= limit:
            return None, f"{prefix}-scan-limit:{symbol}"
        decoded = decode_thumb(elf, address, pool_start)
        if isinstance(decoded, str):
            return None, f"{prefix}-mismatch:{symbol}:{decoded}"
        if decoded.address + decoded.size > pool_start:
            return None, f"{prefix}-mismatch:{symbol}:truncated"
        if reject_unknown and decoded.mnemonic == "other":
            return None, f"{prefix}-mismatch:{symbol}:unsupported-instruction"
        tokens.append(semantic_token(elf, decoded))
        address += decoded.size
    if address != pool_start:
        return None, f"{prefix}-mismatch:{symbol}:unaligned-end"
    for address in range(pool_start, end, 4):
        if address + 4 > end:
            return None, f"{prefix}-mismatch:{symbol}:pool-truncated"
        value = read_thumb_u32(elf, address)
        if value is None:
            return None, f"{prefix}-mismatch:{symbol}:pool-unreadable"
        tokens.append(f"pool:{symbol_value_token(elf, value)}")
    return tuple(tokens), "ok"


def expect_thumb(
    instruction: ThumbInstruction,
    mnemonic: str,
    expected: Mapping[str, int | bool | str],
) -> bool:
    """Return whether *instruction* matches *mnemonic* and all *expected* operands."""
    if instruction.mnemonic != mnemonic:
        return False
    return all(
        instruction.operands.get(key) == value for key, value in expected.items()
    )


def decode_expected_thumb(
    elf: ElfImage,
    symbol: str,
    start: int,
    end: int,
    expected: tuple[tuple[str, Mapping[str, int | bool | str]], ...],
    *,
    max_instructions: int = 96,
    prefix: str = "profile",
) -> tuple[tuple[ThumbInstruction, ...] | None, str]:
    """Decode and verify an expected sequential Thumb instruction list."""
    instructions: list[ThumbInstruction] = []
    address = start
    for index, (mnemonic, operands) in enumerate(expected):
        if address < start or address >= end or index >= max_instructions:
            return None, f"{prefix}-mismatch:{symbol}:{index}:truncated"
        decoded = decode_thumb(elf, address, end)
        if isinstance(decoded, str):
            return None, f"{prefix}-mismatch:{symbol}:{index}:{decoded}"
        if decoded.address + decoded.size > end:
            return None, f"{prefix}-mismatch:{symbol}:{index}:truncated"
        if not expect_thumb(decoded, mnemonic, operands):
            return None, f"{prefix}-mismatch:{symbol}:{index}"
        instructions.append(decoded)
        address += decoded.size
    return tuple(instructions), "ok"
