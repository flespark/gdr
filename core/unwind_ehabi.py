"""Bounded ARM EHABI tables and virtual-register unwinding (pure)."""

from __future__ import annotations

import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .unwind_meta import MAX_EHABI_OPCODES, Endian, Reader, _prel31, _read_uleb


@dataclass(frozen=True)
class _ExidxEntry:
    start: int
    unwind: int | None
    cant_unwind: bool
    inline: int | None = None


@dataclass(frozen=True)
class EhabiTable:
    """Parsed ``.ARM.exidx`` entries plus a reader for ``.ARM.extab``."""

    entries: tuple[_ExidxEntry, ...]
    read_at: Callable[[int, int], bytes | None]
    text_end: int

    def __bool__(self) -> bool:
        return bool(self.entries)

    def entry_for(self, pc: int) -> _ExidxEntry | None:
        """Return the exidx entry covering *pc*, never a nearest-guess entry."""
        for index, entry in enumerate(self.entries):
            end = (
                self.entries[index + 1].start
                if index + 1 < len(self.entries)
                else self.text_end
            )
            if entry.start <= pc < end:
                return entry
        return None


def parse_ehabi(
    exidx: bytes,
    *,
    section_address: int,
    read_at: Callable[[int, int], bytes | None],
    text_end: int,
    endian: Endian,
) -> EhabiTable:
    """Parse ``.ARM.exidx`` entries (8 bytes each, PREL31 encoded)."""
    entries: list[_ExidxEntry] = []
    order = "<" if endian == "little" else ">"
    for offset in range(0, len(exidx) - 7, 8):
        first = struct.unpack_from(f"{order}I", exidx, offset)[0]
        second = struct.unpack_from(f"{order}I", exidx, offset + 4)[0]
        start = _prel31(first, section_address + offset)
        cant = second == 1
        inline = second if second & 0x80000000 else None
        unwind = (
            None
            if cant or inline is not None
            else _prel31(second, section_address + offset + 4)
        )
        entries.append(
            _ExidxEntry(
                start=start,
                unwind=unwind,
                cant_unwind=cant,
                inline=inline,
            )
        )
    entries.sort(key=lambda entry: entry.start)
    return EhabiTable(entries=tuple(entries), read_at=read_at, text_end=text_end)


def _eh_insn_words(first: int, blob: bytes | None, endian: Endian) -> bytes | None:
    if first & 0x80000000 == 0:
        return None
    personality = (first >> 24) & 0x0F
    if personality == 0:
        return bytes(
            [
                (first >> 16) & 0xFF,
                (first >> 8) & 0xFF,
                first & 0xFF,
            ]
        )
    if personality not in (1, 2):
        return None
    count = (first >> 16) & 0xFF
    if blob is None:
        return None
    if len(blob) < count * 4:
        return None
    words: list[bytes] = [
        bytes([(first >> 8) & 0xFF, first & 0xFF]),
    ]
    for item in range(count):
        raw = blob[item * 4 : item * 4 + 4]
        word = int.from_bytes(raw, endian)
        words.append(
            bytes(
                [
                    (word >> 24) & 0xFF,
                    (word >> 16) & 0xFF,
                    (word >> 8) & 0xFF,
                    word & 0xFF,
                ]
            )
        )
    return b"".join(words)


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


def _eh_pop(
    regs: dict[int, int],
    vsp: int,
    registers: list[int],
    *,
    read: Reader,
    endian: Endian,
    stack_low: int | None,
    stack_high: int | None,
) -> tuple[int, str | None]:
    for register in registers:
        value = _read_value(
            read,
            vsp,
            4,
            endian,
            stack_low=stack_low,
            stack_high=stack_high,
        )
        if value is None:
            return vsp, "eh-memory-error"
        regs[register] = value
        vsp += 4
    return (regs[13] if 13 in registers else vsp), None


def unwind_ehabi(
    entry: _ExidxEntry,
    *,
    table: EhabiTable,
    sp: int,
    regs: Mapping[int, int],
    read: Reader,
    stack_low: int | None,
    stack_high: int | None,
    endian: Endian,
) -> tuple[dict[int, int], int, int] | str:
    """Execute one EHABI entry; return ``(regs, caller_sp, caller_pc)`` or a reason."""
    if entry.cant_unwind:
        return "ehabi-cantunwind"
    if entry.inline is None and entry.unwind is None:
        return "ehabi-no-entry"
    if entry.inline is not None:
        first = entry.inline
        extab_address = None
    else:
        assert entry.unwind is not None
        first_raw = table.read_at(entry.unwind, 4)
        if first_raw is None or len(first_raw) != 4:
            return "ehabi-missing-extab"
        first = int.from_bytes(first_raw, endian)
        extab_address = entry.unwind
    blob = None
    if first & 0x80000000 == 0:
        return "ehabi-generic-model-unsupported"
    personality = (first >> 24) & 0x0F
    if personality in (1, 2) and extab_address is not None:
        count = (first >> 16) & 0xFF
        blob = table.read_at(extab_address + 4, count * 4)
    instructions = _eh_insn_words(first, blob, endian)
    if instructions is None:
        return "ehabi-unsupported-personality"
    new_regs = dict(regs)
    vsp = sp
    pc_set = False
    index = 0
    steps = 0
    while index < len(instructions):
        steps += 1
        if steps > MAX_EHABI_OPCODES:
            return "ehabi-opcode-limit"
        opcode = instructions[index]
        index += 1
        if opcode <= 0x3F:
            vsp += (opcode << 2) + 4
        elif opcode <= 0x7F:
            vsp -= ((opcode & 0x3F) << 2) + 4
        elif 0x80 <= opcode <= 0x8F:
            if index >= len(instructions):
                return "ehabi-truncated"
            if opcode == 0x80 and instructions[index] == 0:
                return "ehabi-refuse"
            mask = ((opcode & 0x0F) << 8) | instructions[index]
            index += 1
            regs_to_pop = [4 + bit for bit in range(12) if mask & (1 << bit)]
            regs_to_pop.sort()
            vsp, failure = _eh_pop(
                new_regs,
                vsp,
                regs_to_pop,
                read=read,
                endian=endian,
                stack_low=stack_low,
                stack_high=stack_high,
            )
            if failure is not None:
                return failure
            if 15 in regs_to_pop:
                pc_set = True
        elif 0x90 <= opcode <= 0x9F:
            register = opcode & 0x0F
            if register in (13, 15) or register not in new_regs:
                return "ehabi-set-vsp-unsupported"
            vsp = new_regs[register]
        elif 0xA0 <= opcode <= 0xA7:
            count = (opcode & 0x07) + 1
            vsp, failure = _eh_pop(
                new_regs,
                vsp,
                list(range(4, 4 + count)),
                read=read,
                endian=endian,
                stack_low=stack_low,
                stack_high=stack_high,
            )
            if failure is not None:
                return failure
        elif 0xA8 <= opcode <= 0xAF:
            count = (opcode & 0x07) + 1
            vsp, failure = _eh_pop(
                new_regs,
                vsp,
                [*range(4, 4 + count), 14],
                read=read,
                endian=endian,
                stack_low=stack_low,
                stack_high=stack_high,
            )
            if failure is not None:
                return failure
        elif opcode == 0xB0:
            if not pc_set:
                new_regs[15] = new_regs.get(14, 0)
                pc_set = True
            break
        elif opcode == 0xB1:
            if index >= len(instructions):
                return "ehabi-truncated"
            mask = instructions[index]
            index += 1
            if mask == 0 or mask & 0xF0:
                return "ehabi-spare"
            registers = [bit for bit in range(4) if mask & (1 << bit)]
            vsp, failure = _eh_pop(
                new_regs,
                vsp,
                registers,
                read=read,
                endian=endian,
                stack_low=stack_low,
                stack_high=stack_high,
            )
            if failure is not None:
                return failure
        elif opcode == 0xB2:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "ehabi-truncated-uleb"
            value, index = decoded
            vsp += 0x204 + (value << 2)
        else:
            return "ehabi-opcode-unsupported"
    if not pc_set:
        return "ehabi-no-return-address"
    caller_pc = new_regs.get(15, 0)
    if caller_pc == 0:
        return "ehabi-zero-return-address"
    if vsp < sp:
        return "eh-no-progress"
    if stack_low is not None and vsp < stack_low:
        return "eh-stack-underflow"
    if stack_high is not None and vsp > stack_high:
        return "eh-stack-overflow"
    new_regs[13] = vsp
    return new_regs, vsp, caller_pc
