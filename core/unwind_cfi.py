"""Bounded DWARF call-frame tables and encoded pointers (pure)."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from .unwind_meta import (
    DW_EH_PE_ABSPTR,
    DW_EH_PE_ALIGNED,
    DW_EH_PE_DATAREL,
    DW_EH_PE_FUNCREL,
    DW_EH_PE_INDIRECT,
    DW_EH_PE_OMIT,
    DW_EH_PE_PCREL,
    DW_EH_PE_SDATA2,
    DW_EH_PE_SDATA4,
    DW_EH_PE_SDATA8,
    DW_EH_PE_SLEB128,
    DW_EH_PE_TEXTREL,
    DW_EH_PE_UDATA2,
    DW_EH_PE_UDATA4,
    DW_EH_PE_UDATA8,
    DW_EH_PE_ULEB128,
    MAX_CFI_OPCODES,
    MAX_CIE_COUNT,
    MAX_FDE_COUNT,
    MAX_STATE_DEPTH,
    Endian,
    _read_sleb,
    _read_uleb,
)


@dataclass(frozen=True)
class CfaRule:
    """Canonical frame address: ``register + offset`` or an unsupported rule."""

    register: int | None = None
    offset: int = 0
    error: str | None = None


@dataclass(frozen=True)
class RegisterRule:
    """How one register is recovered in the caller's frame."""

    kind: Literal["offset", "val_offset", "same", "undefined", "register"]
    value: int = 0
    register: int | None = None


@dataclass(frozen=True)
class CfiRow:
    """One CFI row at a PC; ``error`` means the row is unsafe to use."""

    cfa: CfaRule = field(default_factory=CfaRule)
    registers: Mapping[int, RegisterRule] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class _Cie:
    code_alignment: int
    data_alignment: int
    return_register: int
    initial: CfiRow
    augmentation: str
    fde_encoding: int | None


@dataclass(frozen=True)
class _Fde:
    start: int
    end: int
    cie: _Cie
    instructions: bytes
    section_address: int


class CfiTable:
    """A bounded CIE/FDE table parsed from one CFI section."""

    def __init__(
        self,
        fdes: tuple[_Fde, ...],
        *,
        address_size: int,
        endian: Endian,
        error: str | None = None,
    ) -> None:
        self._fdes = fdes
        self._address_size = address_size
        self._endian = endian
        self.error = error

    @property
    def address_size(self) -> int:
        """DWARF address size of the section."""
        return self._address_size

    @property
    def endian(self) -> Endian:
        """Byte order of the section."""
        return self._endian

    def __bool__(self) -> bool:
        return bool(self._fdes)

    def row_for(self, pc: int) -> CfiRow | None:
        """Execute the most specific FDE covering *pc*, or ``None``.

        Linkers can leave zero-address FDEs for discarded sections in
        ``.debug_frame``.  They overlap real functions, so a lookup ignores
        them and, among valid overlaps, takes the entry with the greatest
        start address (the innermost function).
        """
        candidates = [
            fde for fde in self._fdes if fde.start > 0 and fde.start <= pc < fde.end
        ]
        if not candidates:
            return None
        fde = max(candidates, key=lambda item: item.start)
        return _execute_cfi(
            fde,
            pc,
            address_size=self._address_size,
            endian=self._endian,
        )


def parse_cfi_section(
    data: bytes,
    *,
    section_address: int,
    address_size: int,
    endian: Endian,
    is_eh_frame: bool,
) -> CfiTable:
    """Parse one ``.debug_frame``/``.eh_frame`` section.

    A section that cannot be parsed as a whole returns an empty table carrying
    ``error``; individual malformed CIE/FDE records are skipped.  The caller
    treats either as an unavailable metadata source.
    """
    order = "<" if endian == "little" else ">"
    cies: dict[int, _Cie] = {}
    fdes: list[_Fde] = []
    index = 0
    error: str | None = None
    opcode_budget = MAX_CFI_OPCODES
    while index < len(data):
        if len(fdes) >= MAX_FDE_COUNT or len(cies) >= MAX_CIE_COUNT:
            error = "cfi-record-limit"
            break
        unit_start = index
        if index + 4 > len(data):
            error = "cfi-truncated-length"
            break
        length = struct.unpack_from(f"{order}I", data, index)[0]
        index += 4
        offset_size = 4
        if length == 0xFFFFFFFF:
            if index + 8 > len(data):
                error = "cfi-truncated-dwarf64"
                break
            length = struct.unpack_from(f"{order}Q", data, index)[0]
            index += 8
            offset_size = 8
        if length == 0:
            break
        unit_end = index + length
        if unit_end < index or unit_end > len(data):
            error = "cfi-truncated-unit"
            break
        if index + offset_size > unit_end:
            error = "cfi-truncated-id"
            break
        identifier = int.from_bytes(
            data[index : index + offset_size], endian, signed=False
        )
        id_offset = index
        index += offset_size
        if identifier == (0 if is_eh_frame else (1 << (8 * offset_size)) - 1):
            parsed = _parse_cie(
                data,
                index,
                unit_end,
                order=order,
                address_size=address_size,
            )
            if parsed is None:
                index = unit_end
                continue
            cies[unit_start] = parsed
            index = unit_end
            continue
        cie_offset = id_offset - identifier if is_eh_frame else identifier
        cie = cies.get(cie_offset)
        if cie is None:
            index = unit_end
            continue
        parsed_fde = _parse_fde(
            data,
            index,
            unit_end,
            cie=cie,
            order=order,
            address_size=address_size,
            section_address=section_address,
            is_eh_frame=is_eh_frame,
            opcode_budget=opcode_budget,
        )
        if parsed_fde is None:
            index = unit_end
            continue
        fde, used = parsed_fde
        opcode_budget -= used
        if opcode_budget <= 0:
            error = "cfi-opcode-limit"
            break
        fdes.append(fde)
        index = unit_end
    fdes.sort(key=lambda item: item.start)
    return CfiTable(
        tuple(fdes),
        address_size=address_size,
        endian=endian,
        error=error,
    )


def _parse_cie(
    data: bytes,
    index: int,
    end: int,
    *,
    order: str,
    address_size: int,
) -> _Cie | None:
    if index >= end:
        return None
    byteorder = "little" if order == "<" else "big"
    version = data[index]
    index += 1
    aug_end = data.find(b"\x00", index, end)
    if aug_end < 0:
        return None
    augmentation = data[index:aug_end].decode("ascii", errors="replace")
    index = aug_end + 1
    if version not in (1, 3, 4):
        return None
    if version == 4:
        if index + 2 > end or data[index] != address_size or data[index + 1] != 0:
            return None
        index += 2
    code = _read_uleb(data, index, end)
    if code is None:
        return None
    code_alignment, index = code
    data_align = _read_sleb(data, index, end)
    if data_align is None:
        return None
    data_alignment, index = data_align
    if version == 1:
        if index >= end:
            return None
        return_register = data[index]
        index += 1
    else:
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            return None
        return_register, index = decoded
    fde_encoding: int | None = None
    if augmentation.startswith("z"):
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            return None
        aug_length, index = decoded
        aug_end_data = index + aug_length
        if aug_end_data > end:
            return None
        cursor = index
        for char in augmentation[1:]:
            if char == "R":
                if cursor >= aug_end_data:
                    return None
                fde_encoding = data[cursor]
                cursor += 1
            elif char == "L":
                cursor += 1
            elif char == "P":
                if cursor >= aug_end_data:
                    return None
                encoding = data[cursor]
                cursor += 1
                skipped = _encoded_pointer_skip(
                    data, cursor, encoding, aug_end_data, address_size=address_size
                )
                if skipped is None:
                    return None
                cursor = skipped
            elif char in "SB":
                continue
            else:
                cursor = aug_end_data
        index = aug_end_data
    initial = _run_cfi(
        data[index:end],
        CfiRow(cfa=CfaRule(register=0, offset=0)),
        location=0,
        target=None,
        code_alignment=code_alignment,
        data_alignment=data_alignment,
        address_size=address_size,
        endian=byteorder,
        cie_row=None,
        max_steps=MAX_CFI_OPCODES,
    )
    if isinstance(initial, str):
        return None
    return _Cie(
        code_alignment=code_alignment,
        data_alignment=data_alignment,
        return_register=return_register,
        initial=initial,
        augmentation=augmentation,
        fde_encoding=fde_encoding,
    )


def _encoded_pointer_skip(
    data: bytes, index: int, encoding: int, end: int, *, address_size: int = 4
) -> int | None:
    """Consume an encoded pointer without interpreting it; return a new index."""
    if encoding == DW_EH_PE_OMIT:
        return index
    fmt = encoding & 0x0F
    if fmt == DW_EH_PE_ULEB128:
        decoded = _read_uleb(data, index, end)
        return None if decoded is None else decoded[1]
    if fmt == DW_EH_PE_SLEB128:
        decoded = _read_sleb(data, index, end)
        return None if decoded is None else decoded[1]
    sizes = {
        DW_EH_PE_ABSPTR: address_size,
        DW_EH_PE_UDATA2: 2,
        DW_EH_PE_UDATA4: 4,
        DW_EH_PE_UDATA8: 8,
        DW_EH_PE_SDATA2: 2,
        DW_EH_PE_SDATA4: 4,
        DW_EH_PE_SDATA8: 8,
    }
    size = sizes.get(fmt)
    if size is None or index + size > end:
        return None
    return index + size


def _decode_encoded_pointer(
    data: bytes,
    index: int,
    end: int,
    *,
    encoding: int,
    address_size: int,
    endian: str,
    section_address: int,
    absolute_position: int | None = None,
) -> tuple[int, int] | None:
    if encoding == DW_EH_PE_OMIT:
        return 0, index
    fmt = encoding & 0x0F
    application = encoding & 0x70
    if encoding & DW_EH_PE_INDIRECT or application in (
        DW_EH_PE_TEXTREL,
        DW_EH_PE_FUNCREL,
    ):
        return None
    if application == DW_EH_PE_ALIGNED:
        alignment = address_size
        index += (-(section_address + index)) % alignment
    pointer_position = index if absolute_position is None else absolute_position
    if fmt == DW_EH_PE_ABSPTR:
        size = address_size
        if index + size > end:
            return None
        unsigned = int.from_bytes(data[index : index + size], endian)
        index += size
        value = unsigned
    elif fmt in (DW_EH_PE_UDATA2, DW_EH_PE_UDATA4, DW_EH_PE_UDATA8):
        size = {DW_EH_PE_UDATA2: 2, DW_EH_PE_UDATA4: 4, DW_EH_PE_UDATA8: 8}[fmt]
        if index + size > end:
            return None
        value = int.from_bytes(data[index : index + size], endian)
        index += size
    elif fmt in (DW_EH_PE_SDATA2, DW_EH_PE_SDATA4, DW_EH_PE_SDATA8):
        size = {DW_EH_PE_SDATA2: 2, DW_EH_PE_SDATA4: 4, DW_EH_PE_SDATA8: 8}[fmt]
        if index + size > end:
            return None
        value = int.from_bytes(data[index : index + size], endian, signed=True)
        index += size
    elif fmt == DW_EH_PE_ULEB128:
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            return None
        value, index = decoded
    elif fmt == DW_EH_PE_SLEB128:
        decoded = _read_sleb(data, index, end)
        if decoded is None:
            return None
        value, index = decoded
    else:
        return None
    base = 0
    if application == DW_EH_PE_PCREL:
        base = section_address + pointer_position
    elif application == DW_EH_PE_DATAREL:
        base = section_address
    elif application not in (0, DW_EH_PE_ALIGNED):
        return None
    return value + base, index


def _parse_fde(
    data: bytes,
    index: int,
    end: int,
    *,
    cie: _Cie,
    order: str,
    address_size: int,
    section_address: int,
    is_eh_frame: bool,
    opcode_budget: int,
) -> tuple[_Fde, int] | None:
    byteorder = "little" if order == "<" else "big"
    if is_eh_frame and cie.fde_encoding is not None:
        start_pos = index
        decoded = _decode_encoded_pointer(
            data,
            index,
            end,
            encoding=cie.fde_encoding,
            address_size=address_size,
            endian=byteorder,
            section_address=section_address,
            absolute_position=start_pos,
        )
        if decoded is None:
            return None
        start, index = decoded
        fmt = cie.fde_encoding & 0x0F
        size_map = {
            DW_EH_PE_ABSPTR: address_size,
            DW_EH_PE_UDATA2: 2,
            DW_EH_PE_UDATA4: 4,
            DW_EH_PE_UDATA8: 8,
            DW_EH_PE_SDATA2: 2,
            DW_EH_PE_SDATA4: 4,
            DW_EH_PE_SDATA8: 8,
        }
        size = size_map.get(fmt)
        if size is None or index + size > end:
            return None
        length = int.from_bytes(data[index : index + size], byteorder)
        index += size
    else:
        if index + address_size * 2 > end:
            return None
        start = int.from_bytes(data[index : index + address_size], byteorder)
        index += address_size
        length = int.from_bytes(data[index : index + address_size], byteorder)
        index += address_size
    if cie.augmentation.startswith("z"):
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            return None
        aug_length, index = decoded
        if index + aug_length > end:
            return None
        index += aug_length
    if length == 0 or start + length <= start:
        return None
    instructions = data[index:end]
    used = min(len(instructions) + 1, opcode_budget)
    return (
        _Fde(
            start=start,
            end=start + length,
            cie=cie,
            instructions=instructions,
            section_address=section_address,
        ),
        used,
    )


def _execute_cfi(
    fde: _Fde,
    pc: int,
    *,
    address_size: int,
    endian: Endian,
) -> CfiRow:
    result = _run_cfi(
        fde.instructions,
        fde.cie.initial,
        location=fde.start,
        target=pc,
        code_alignment=fde.cie.code_alignment,
        data_alignment=fde.cie.data_alignment,
        address_size=address_size,
        endian=endian,
        cie_row=fde.cie.initial,
        max_steps=MAX_CFI_OPCODES,
    )
    if isinstance(result, str):
        return CfiRow(error=result)
    return result


def _run_cfi(
    instructions: bytes,
    start_row: CfiRow,
    *,
    location: int,
    target: int | None,
    code_alignment: int,
    data_alignment: int,
    address_size: int,
    endian: str,
    cie_row: CfiRow | None,
    max_steps: int,
) -> CfiRow | str:
    row = CfiRow(cfa=start_row.cfa, registers=dict(start_row.registers))
    index = 0
    steps = 0
    state_stack: list[CfiRow] = []
    while index < len(instructions):
        steps += 1
        if steps > max_steps:
            return "cfi-opcode-limit"
        opcode = instructions[index]
        index += 1
        high = opcode & 0xC0
        low = opcode & 0x3F
        if high == 0x40:
            if target is not None and location + low * code_alignment > target:
                break
            location += low * code_alignment
            continue
        if high == 0x80:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            offset, index = decoded
            row.registers[low] = RegisterRule(
                kind="offset", value=offset * data_alignment
            )
            continue
        if high == 0xC0:
            restored = cie_row.registers.get(low) if cie_row is not None else None
            if restored is None:
                row.registers.pop(low, None)
            else:
                row.registers[low] = restored
            continue
        if opcode == 0x00:
            continue
        if opcode == 0x01:
            if index + address_size > len(instructions):
                return "cfi-truncated-set-loc"
            location = int.from_bytes(
                instructions[index : index + address_size], endian
            )
            index += address_size
            continue
        if opcode == 0x02:
            if index >= len(instructions):
                return "cfi-truncated-advance"
            delta = instructions[index]
            index += 1
            if target is not None and location + delta * code_alignment > target:
                break
            location += delta * code_alignment
            continue
        if opcode == 0x03:
            if index + 2 > len(instructions):
                return "cfi-truncated-advance"
            delta = int.from_bytes(instructions[index : index + 2], endian)
            index += 2
            if target is not None and location + delta * code_alignment > target:
                break
            location += delta * code_alignment
            continue
        if opcode == 0x04:
            if index + 4 > len(instructions):
                return "cfi-truncated-advance"
            delta = int.from_bytes(instructions[index : index + 4], endian)
            index += 4
            if target is not None and location + delta * code_alignment > target:
                break
            location += delta * code_alignment
            continue
        if opcode == 0x0A:
            if len(state_stack) >= MAX_STATE_DEPTH:
                return "cfi-state-stack-limit"
            state_stack.append(CfiRow(cfa=row.cfa, registers=dict(row.registers)))
            continue
        if opcode == 0x0B:
            if not state_stack:
                return "cfi-state-stack-underflow"
            row = state_stack.pop()
            continue
        if opcode == 0x07:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            row.registers[reg] = RegisterRule(kind="undefined")
            continue
        if opcode == 0x08:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            row.registers[reg] = RegisterRule(kind="same")
            continue
        if opcode == 0x09:
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            source = _read_uleb(instructions, index, len(instructions))
            if source is None:
                return "cfi-truncated-uleb"
            source_value, index = source
            row.registers[reg_value] = RegisterRule(
                kind="register", register=source_value
            )
            continue
        if opcode == 0x0C:
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = _read_uleb(instructions, index, len(instructions))
            if offset is None:
                return "cfi-truncated-uleb"
            offset_value, index = offset
            row = CfiRow(
                cfa=CfaRule(register=reg_value, offset=offset_value),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x0D:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=reg, offset=row.cfa.offset),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x0E:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            offset, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=row.cfa.register, offset=offset),
                registers=dict(row.registers),
            )
            continue
        if opcode in (0x0F, 0x10, 0x16):
            return "cfi-expression-unsupported"
        if opcode == 0x11:
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = _read_sleb(instructions, index, len(instructions))
            if offset is None:
                return "cfi-truncated-sleb"
            offset_value, index = offset
            row.registers[reg_value] = RegisterRule(
                kind="offset", value=offset_value * data_alignment
            )
            continue
        if opcode == 0x12:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            decoded = _read_sleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-sleb"
            offset, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=reg, offset=offset * data_alignment),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x13:
            decoded = _read_sleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-sleb"
            offset, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=row.cfa.register, offset=offset * data_alignment),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x06:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            restored = cie_row.registers.get(reg) if cie_row is not None else None
            if restored is None:
                row.registers.pop(reg, None)
            else:
                row.registers[reg] = restored
            continue
        if opcode in (0x14, 0x15):
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = (_read_sleb if opcode == 0x15 else _read_uleb)(
                instructions, index, len(instructions)
            )
            if offset is None:
                return "cfi-truncated-uleb"
            offset_value, index = offset
            row.registers[reg_value] = RegisterRule(
                kind="val_offset", value=offset_value * data_alignment
            )
            continue
        if opcode == 0x2E:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            _value, index = decoded
            continue
        if opcode == 0x2F:
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = _read_uleb(instructions, index, len(instructions))
            if offset is None:
                return "cfi-truncated-uleb"
            offset_value, index = offset
            row.registers[reg_value] = RegisterRule(
                kind="offset", value=-(offset_value * data_alignment)
            )
            continue
        return "cfi-opcode-unsupported"
    if row.cfa.error is not None:
        return row.cfa.error
    if row.cfa.register is None:
        return "cfi-no-cfa"
    return row
