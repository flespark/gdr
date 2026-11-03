"""Bounded ELF unwind-metadata parsing for :mod:`core.unwind` (pure module).

This module owns ELF sections, code symbols and common binary encodings.
The CFI and EHABI parsers consume these helpers without importing GDB.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Endian = Literal["little", "big"]
Arch = Literal["arm", "riscv"]
Method = Literal["seed", "ehabi", "cfi", "frame-pointer", "exception"]

Reader = Callable[[int, int], "bytes | None"]

EM_ARM = 40
EM_RISCV = 243

SHT_PROGBITS = 1
SHT_NOBITS = 8
SHT_ARM_EXIDX = 0x70000001
SHT_ATTRIBUTES = 0x70000003
SHF_EXECINSTR = 0x4

DW_EH_PE_ABSPTR = 0x00
DW_EH_PE_ULEB128 = 0x01
DW_EH_PE_UDATA2 = 0x02
DW_EH_PE_UDATA4 = 0x03
DW_EH_PE_UDATA8 = 0x04
DW_EH_PE_SLEB128 = 0x09
DW_EH_PE_SDATA2 = 0x0A
DW_EH_PE_SDATA4 = 0x0B
DW_EH_PE_SDATA8 = 0x0C
DW_EH_PE_PCREL = 0x10
DW_EH_PE_TEXTREL = 0x20
DW_EH_PE_DATAREL = 0x30
DW_EH_PE_FUNCREL = 0x40
DW_EH_PE_ALIGNED = 0x50
DW_EH_PE_INDIRECT = 0x80
DW_EH_PE_OMIT = 0xFF

MAX_ELF_BYTES = 64 * 1024 * 1024
MAX_SECTION_COUNT = 8192
MAX_CIE_COUNT = 4096
MAX_FDE_COUNT = 65536
MAX_CFI_OPCODES = 200_000
MAX_CFI_EXPRESSION_BYTES = 1024
MAX_CFI_EXPRESSION_STEPS = 4096
MAX_CFI_EXPRESSION_STACK = 64
MAX_CFI_EXPRESSION_READS = 32
MAX_CFI_EXPRESSION_READ_BYTES = 256
MAX_EHABI_OPCODES = 4096
MAX_STATE_DEPTH = 64
MAX_FRAMES = 64
MAX_FRAME_READ = 4096


class UnwindError(ValueError):
    """A bounded parse or unwind failure with a machine-readable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ElfSection:
    """One ELF section with a virtual address and file-backed bytes."""

    name: str
    address: int
    data: bytes
    flags: int
    section_type: int


@dataclass(frozen=True)
class CodeSymbol:
    """One executable ELF symbol with a non-zero size."""

    name: str
    start: int
    size: int

    @property
    def end(self) -> int:
        """One past the symbol's last byte."""
        return self.start + self.size


@dataclass(frozen=True)
class ElfImage:
    """The parts of an ELF image needed by the unwind parsers.

    ``sections`` excludes ``SHT_NOBITS`` (there are no file bytes to parse) and
    keeps only names, addresses, flags and data.  ``read_at`` maps a virtual
    address to a section's file bytes; it never reads target memory.
    """

    pointer_size: int
    endian: Endian
    machine: int
    sections: tuple[ElfSection, ...]
    symbols: tuple[CodeSymbol, ...] = ()
    flags: int = 0
    anchors: tuple[tuple[str, int], ...] = ()

    def symbol_address(self, name: str) -> int | None:
        """Return a defined symbol's unique address, including zero-size labels."""
        addresses = {address for symbol, address in self.anchors if symbol == name}
        return next(iter(addresses)) if len(addresses) == 1 else None

    def section(self, name: str) -> ElfSection | None:
        """Return one named section, or ``None`` when it is absent."""
        for section in self.sections:
            if section.name == name:
                return section
        return None

    def section_data(self, name: str) -> bytes | None:
        """Return one section's bytes, or ``None`` when it is absent."""
        section = self.section(name)
        return section.data if section is not None else None

    def read_at(self, address: int, size: int) -> bytes | None:
        """Read *size* file-backed bytes at a virtual address, or ``None``."""
        if size <= 0 or size > MAX_FRAME_READ:
            return None
        for section in self.sections:
            start = section.address
            end = start + len(section.data)
            if start <= address and address + size <= end:
                offset = address - start
                return section.data[offset : offset + size]
        return None

    def is_code(self, address: int) -> bool:
        """Whether *address* lies in an executable, file-backed section."""
        if address <= 0:
            return False
        for section in self.sections:
            if not section.flags & SHF_EXECINSTR:
                continue
            if section.address <= address < section.address + len(section.data):
                return True
        return False

    def function_containing(self, address: int) -> CodeSymbol | None:
        """Return the sized executable symbol containing *address*, if any.

        This is the evidence used to reject an EHABI entry that only precedes
        a PC because an intervening function emitted no exidx row.
        """
        for symbol in self.symbols:
            if symbol.start <= address < symbol.end:
                return symbol
        return None

    def code_end(self) -> int:
        """Highest end address of any executable section (0 when there is none)."""
        return max(
            (
                section.address + len(section.data)
                for section in self.sections
                if section.flags & SHF_EXECINSTR and section.data
            ),
            default=0,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ElfImage | None:
        """Parse the ELF header and section table, or return ``None``.

        A truncated, malformed or unsupported image returns ``None``; callers
        then report that no metadata is available instead of reading guessed
        offsets.
        """
        if len(data) < 16 or data[:4] != b"\x7fELF" or data[6] != 1:
            return None
        if data[4] not in (1, 2) or data[5] not in (1, 2):
            return None
        is_64 = data[4] == 2
        endian: Endian = "little" if data[5] == 1 else "big"
        order = "<" if endian == "little" else ">"
        header_size = 64 if is_64 else 52
        section_size = 64 if is_64 else 40
        if len(data) < header_size:
            return None
        try:
            machine = struct.unpack_from(f"{order}H", data, 18)[0]
            if is_64:
                shoff = struct.unpack_from(f"{order}Q", data, 0x28)[0]
                shentsize = struct.unpack_from(f"{order}H", data, 0x3A)[0]
                shnum = struct.unpack_from(f"{order}H", data, 0x3C)[0]
                shstrndx = struct.unpack_from(f"{order}H", data, 0x3E)[0]
            else:
                shoff = struct.unpack_from(f"{order}I", data, 0x20)[0]
                shentsize = struct.unpack_from(f"{order}H", data, 0x2E)[0]
                shnum = struct.unpack_from(f"{order}H", data, 0x30)[0]
                shstrndx = struct.unpack_from(f"{order}H", data, 0x32)[0]
        except struct.error:
            return None
        if shoff == 0 or shentsize < section_size:
            return None
        if shoff > len(data) or shnum > MAX_SECTION_COUNT:
            return None
        if shnum == 0:
            if shoff + section_size > len(data):
                return None
            shnum = _read_section_header(data, shoff, is_64, order, 0)[5]
            if shnum <= 0 or shnum > MAX_SECTION_COUNT:
                return None
        if shnum > (len(data) - shoff) // shentsize:
            return None
        if shstrndx == 0xFFFF:
            if shoff + section_size > len(data):
                return None
            shstrndx = _read_section_header(data, shoff, is_64, order, 0)[6]
        if shstrndx == 0 or shstrndx >= shnum:
            return None

        def header(index: int) -> tuple[int, int, int, int, int, int, int]:
            return _read_section_header(data, shoff, is_64, order, index)

        (
            names_name,
            _names_type,
            _names_flags,
            names_addr,
            names_offset,
            names_size,
            _,
        ) = header(shstrndx)
        del names_name, names_addr
        if names_offset > len(data) or names_size > len(data) - names_offset:
            return None
        names = data[names_offset : names_offset + names_size]
        if not names or names[0] != 0:
            return None

        sections: list[ElfSection] = []
        all_headers: list[tuple[int, int, int, int, int, int, int]] = []
        for index in range(shnum):
            (
                name_offset,
                section_type,
                flags,
                address,
                offset,
                size,
                link,
            ) = header(index)
            all_headers.append(
                (name_offset, section_type, flags, address, offset, size, link)
            )
            if (
                section_type == 0
                or section_type == SHT_NOBITS
                or name_offset >= len(names)
            ):
                continue
            end = names.find(b"\0", name_offset)
            if end < 0:
                return None
            name = names[name_offset:end].decode("ascii", errors="replace")
            if not name or size == 0:
                continue
            if offset > len(data) or size > len(data) - offset:
                return None
            if section_type in (SHT_PROGBITS, SHT_ARM_EXIDX, SHT_ATTRIBUTES):
                sections.append(
                    ElfSection(
                        name=name,
                        address=address,
                        data=data[offset : offset + size],
                        flags=flags,
                        section_type=section_type,
                    )
                )
        symbols, anchors = _parse_code_symbols(
            data,
            all_headers,
            is_64=is_64,
            endian=endian,
            machine=machine,
            section_count=shnum,
        )
        return cls(
            pointer_size=8 if is_64 else 4,
            endian=endian,
            machine=machine,
            sections=tuple(sections),
            symbols=symbols,
            flags=struct.unpack_from(f"{order}I", data, 48 if is_64 else 36)[0],
            anchors=anchors,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> ElfImage | None:
        """Read and parse the ELF at *path*; ``None`` on any I/O or format error."""
        try:
            if Path(path).stat().st_size > MAX_ELF_BYTES:
                return None
            blob = Path(path).read_bytes()
        except (OSError, TypeError, ValueError):
            return None
        return cls.from_bytes(blob)


SHT_SYMTAB = 2
SHT_STRTAB = 3
STT_FUNC = 2


def _parse_code_symbols(
    data: bytes,
    headers: list[tuple[int, int, int, int, int, int, int]],
    *,
    is_64: bool,
    endian: Endian,
    machine: int,
    section_count: int,
) -> tuple[tuple[CodeSymbol, ...], tuple[tuple[str, int], ...]]:
    """Parse sized functions and defined, allocated-section symbol anchors."""
    order = "<" if endian == "little" else ">"
    entry_size = 24 if is_64 else 16
    symbols: list[CodeSymbol] = []
    anchors: list[tuple[str, int]] = []
    for index, (
        _name_offset,
        section_type,
        _flags,
        _addr,
        offset,
        size,
        link,
    ) in enumerate(headers):
        if section_type != SHT_SYMTAB or index >= section_count:
            continue
        if (
            link <= 0
            or link >= section_count
            or offset > len(data)
            or size > len(data) - offset
        ):
            return (), ()
        string_offset, string_type, _sf, _sa, strings_at, strings_size, _sl = headers[
            link
        ]
        del string_offset, string_type
        if strings_at > len(data) or strings_size > len(data) - strings_at:
            return (), ()
        strings = data[strings_at : strings_at + strings_size]
        if entry_size == 0 or size % entry_size:
            return (), ()
        for item in range(0, size - entry_size + 1, entry_size):
            base = offset + item
            name_at = struct.unpack_from(f"{order}I", data, base)[0]
            if is_64:
                value = struct.unpack_from(f"{order}Q", data, base + 8)[0]
                value_size = struct.unpack_from(f"{order}Q", data, base + 16)[0]
                info = data[base + 4]
            else:
                value = struct.unpack_from(f"{order}I", data, base + 4)[0]
                value_size = struct.unpack_from(f"{order}I", data, base + 8)[0]
                info = data[base + 12]
            section_index = struct.unpack_from(
                f"{order}H", data, base + (6 if is_64 else 14)
            )[0]
            if (
                name_at >= len(strings)
                or not 0 < section_index < section_count
                or not headers[section_index][2] & 2
            ):
                continue
            if machine == EM_ARM:
                value &= ~1
            end = strings.find(b"\0", name_at)
            if end < 0:
                continue
            name = strings[name_at:end].decode("ascii", errors="replace")
            if name:
                anchors.append((name, value))
                if (
                    info & 0x0F == STT_FUNC
                    and value_size > 0
                    and headers[section_index][2] & SHF_EXECINSTR
                ):
                    symbols.append(CodeSymbol(name=name, start=value, size=value_size))
        break
    symbols.sort(key=lambda symbol: symbol.start)
    return tuple(symbols), tuple(anchors)


def _read_section_header(
    data: bytes,
    shoff: int,
    is_64: bool,
    order: str,
    index: int,
) -> tuple[int, int, int, int, int, int, int]:
    size = 64 if is_64 else 40
    base = shoff + index * size
    if base < 0 or base + size > len(data):
        raise ValueError("section header out of range")
    name = struct.unpack_from(f"{order}I", data, base)[0]
    section_type = struct.unpack_from(f"{order}I", data, base + 4)[0]
    flags = struct.unpack_from(f"{order}Q" if is_64 else f"{order}I", data, base + 8)[0]
    if is_64:
        address = struct.unpack_from(f"{order}Q", data, base + 16)[0]
        offset = struct.unpack_from(f"{order}Q", data, base + 24)[0]
        size_value = struct.unpack_from(f"{order}Q", data, base + 32)[0]
        link = struct.unpack_from(f"{order}I", data, base + 40)[0]
    else:
        address = struct.unpack_from(f"{order}I", data, base + 12)[0]
        offset = struct.unpack_from(f"{order}I", data, base + 16)[0]
        size_value = struct.unpack_from(f"{order}I", data, base + 20)[0]
        link = struct.unpack_from(f"{order}I", data, base + 24)[0]
    return name, section_type, flags, address, offset, size_value, link


# ---------------------------------------------------------------------------
# Common encodings
# ---------------------------------------------------------------------------


def _read_uleb(data: bytes, index: int, end: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    while index < end:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index
        shift += 7
        if shift > 63:
            return None
    return None


def _read_sleb(data: bytes, index: int, end: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    while index < end:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte & 0x80 == 0:
            if shift < 64 and byte & 0x40:
                value -= 1 << shift
            return value, index
        if shift > 63:
            return None
    return None


def _sign_extend(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & sign else value


def _prel31(word: int, place: int) -> int:
    return place + _sign_extend(word, 31)
