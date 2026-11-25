"""Bounded ELF parsing, code symbols, encodings, and CPU ISA/ABI attributes (pure module).

This module owns ELF section/program headers, sized function symbols, common
binary encodings, and target CPU/ABI attribute classification without importing GDB.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

Endian = Literal["little", "big"]
Arch = Literal["arm", "riscv"]
Method = Literal["seed", "ehabi", "cfi", "leaf", "frame-pointer", "exception"]

Reader = Callable[[int, int], "bytes | None"]

EM_ARM = 40
EM_RISCV = 243

SHT_PROGBITS = 1
SHT_NOBITS = 8
SHT_ARM_EXIDX = 0x70000001
SHT_ATTRIBUTES = 0x70000003
SHF_EXECINSTR = 0x4
PT_GNU_EH_FRAME = 0x6474E550

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
    """One executable function symbol with size and optional Arm instruction state."""

    name: str
    start: int
    size: int
    thumb: bool | None = None

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
    nobits_sections: tuple[tuple[str, int, int], ...] = ()
    zero_size_functions: tuple[CodeSymbol, ...] = ()
    gnu_eh_frame: tuple[int, bytes] | None = None
    gnu_eh_frame_error: str | None = None

    def eh_frame_hdr_source(self) -> tuple[tuple[int, bytes] | None, str | None]:
        """Return validated ``.eh_frame_hdr`` ``(address, bytes)`` or an index error.

        ``PT_GNU_EH_FRAME`` takes precedence over section names when present;
        malformed or conflicting program headers inhibit trusting
        ``.eh_frame_hdr`` silently so CFI parsing degrades safely.
        """
        if self.gnu_eh_frame_error is not None:
            return None, self.gnu_eh_frame_error
        section = self.section(".eh_frame_hdr")
        if self.gnu_eh_frame is not None:
            if (
                section is not None
                and (section.address, section.data) != self.gnu_eh_frame
            ):
                return None, "eh-hdr-program-section-mismatch"
            return self.gnu_eh_frame, None
        if section is not None and section.data:
            return (section.address, section.data), None
        return None, None

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

    def section_bounds(self, name: str) -> tuple[int, int] | None:
        """Return ``(low, high)`` for a named file-backed or allocated NOBITS section."""
        for section in self.sections:
            if section.name == name and section.data:
                return section.address, section.address + len(section.data)
        for sec_name, address, size in self.nobits_sections:
            if sec_name == name and size > 0:
                return address, address + size
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
        table = iter_elf_section_headers(data)
        if table is None or data[6] != 1:
            return None
        names = table.names
        if not names or names[0] != 0:
            return None

        sections: list[ElfSection] = []
        nobits_sections: list[tuple[str, int, int]] = []
        for header in table.headers:
            if header.section_type == 0 or header.name_offset >= len(names):
                continue
            end = names.find(b"\0", header.name_offset)
            if end < 0:
                return None
            name = names[header.name_offset : end].decode("ascii", errors="replace")
            if not name or header.size == 0:
                continue
            if header.section_type == SHT_NOBITS:
                if header.flags & 2 and header.address > 0:
                    nobits_sections.append((name, header.address, header.size))
                continue
            if header.offset > len(data) or header.size > len(data) - header.offset:
                return None
            if header.section_type in (
                SHT_PROGBITS,
                SHT_ARM_EXIDX,
                SHT_ATTRIBUTES,
            ):
                sections.append(
                    ElfSection(
                        name=name,
                        address=header.address,
                        data=data[header.offset : header.offset + header.size],
                        flags=header.flags,
                        section_type=header.section_type,
                    )
                )
        symbols, anchors, zero_size_functions = _parse_code_symbols(
            data,
            list(table.headers),
            is_64=table.is_64,
            endian=table.endian,
            machine=table.machine,
            section_count=len(table.headers),
        )
        gnu_eh_frame, gnu_eh_frame_error = _parse_gnu_eh_frame(
            data,
            is_64=table.is_64,
            endian=table.endian,
        )
        return cls(
            pointer_size=8 if table.is_64 else 4,
            endian=table.endian,
            machine=table.machine,
            sections=tuple(sections),
            symbols=symbols,
            flags=table.flags,
            anchors=anchors,
            nobits_sections=tuple(nobits_sections),
            zero_size_functions=zero_size_functions,
            gnu_eh_frame=gnu_eh_frame,
            gnu_eh_frame_error=gnu_eh_frame_error,
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
) -> tuple[tuple[CodeSymbol, ...], tuple[tuple[str, int], ...], tuple[CodeSymbol, ...]]:
    """Retain function types/state separately from generic allocated-section anchors."""
    order = "<" if endian == "little" else ">"
    entry_size = 24 if is_64 else 16
    symbols: list[CodeSymbol] = []
    anchors: list[tuple[str, int]] = []
    zero_size_functions: list[CodeSymbol] = []
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
            return (), (), ()
        string_offset, string_type, _sf, _sa, strings_at, strings_size, _sl = headers[
            link
        ]
        del string_offset, string_type
        if strings_at > len(data) or strings_size > len(data) - strings_at:
            return (), (), ()
        strings = data[strings_at : strings_at + strings_size]
        if entry_size == 0 or size % entry_size:
            return (), (), ()
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
            thumb = bool(value & 1) if machine == EM_ARM else None
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
                    and headers[section_index][2] & SHF_EXECINSTR
                ):
                    symbol = CodeSymbol(
                        name=name, start=value, size=value_size, thumb=thumb
                    )
                    (symbols if value_size > 0 else zero_size_functions).append(symbol)
        break
    symbols.sort(key=lambda symbol: symbol.start)
    zero_size_functions.sort(key=lambda symbol: symbol.start)
    return tuple(symbols), tuple(anchors), tuple(zero_size_functions)


def _read_section_header(
    data: bytes,
    shoff: int,
    is_64: bool,
    order: str,
    index: int,
    stride: int | None = None,
) -> tuple[int, int, int, int, int, int, int]:
    size = 64 if is_64 else 40
    base = shoff + index * (size if stride is None else stride)
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


class ElfSectionHeader(NamedTuple):
    """One parsed ELF section header before any type filtering."""

    name_offset: int
    section_type: int
    flags: int
    address: int
    offset: int
    size: int
    link: int


@dataclass(frozen=True)
class ElfSectionTable:
    """The ELF header fields and the complete, unfiltered section table."""

    is_64: bool
    endian: Endian
    machine: int
    flags: int
    shentsize: int
    names: bytes
    headers: tuple[ElfSectionHeader, ...]

    def __iter__(self) -> Iterator[ElfSectionHeader]:
        return iter(self.headers)


def iter_elf_section_headers(data: bytes) -> ElfSectionTable | None:
    """Parse the ELF header and section table, or ``None`` when malformed.

    This is the stateless, section-type-agnostic walk shared by
    :meth:`ElfImage.from_bytes` and ``core.gdb_bridge.section_payloads``.
    Name resolution, the ELF-ident version check and per-section payload
    bounds stay with each caller so their contracts do not change.
    """
    if len(data) < 16 or data[:4] != b"\x7fELF":
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
        try:
            shnum = _read_section_header(data, shoff, is_64, order, 0)[5]
        except ValueError:
            return None
        if shnum <= 0 or shnum > MAX_SECTION_COUNT:
            return None
    if shnum > (len(data) - shoff) // shentsize:
        return None
    if shstrndx == 0xFFFF:
        if shoff + section_size > len(data):
            return None
        try:
            shstrndx = _read_section_header(data, shoff, is_64, order, 0)[6]
        except ValueError:
            return None
    if shstrndx == 0 or shstrndx >= shnum:
        return None
    headers: list[ElfSectionHeader] = []
    try:
        headers.extend(
            ElfSectionHeader(
                *_read_section_header(
                    data, shoff, is_64, order, index, stride=shentsize
                )
            )
            for index in range(shnum)
        )
    except (ValueError, struct.error):
        return None
    names_header = headers[shstrndx]
    if (
        names_header.offset > len(data)
        or names_header.size > len(data) - names_header.offset
    ):
        return None
    try:
        flags = struct.unpack_from(f"{order}I", data, 48 if is_64 else 36)[0]
    except struct.error:
        return None
    return ElfSectionTable(
        is_64=is_64,
        endian=endian,
        machine=machine,
        flags=flags,
        shentsize=shentsize,
        names=data[names_header.offset : names_header.offset + names_header.size],
        headers=tuple(headers),
    )


def _parse_gnu_eh_frame(
    data: bytes, *, is_64: bool, endian: Endian
) -> tuple[tuple[int, bytes] | None, str | None]:
    """Extract an optional ``PT_GNU_EH_FRAME`` segment without aborting ELF load."""
    order = "<" if endian == "little" else ">"
    min_phentsize = 56 if is_64 else 32
    try:
        if is_64:
            phoff = struct.unpack_from(f"{order}Q", data, 0x20)[0]
            phentsize = struct.unpack_from(f"{order}H", data, 0x36)[0]
            phnum = struct.unpack_from(f"{order}H", data, 0x38)[0]
        else:
            phoff = struct.unpack_from(f"{order}I", data, 0x1C)[0]
            phentsize = struct.unpack_from(f"{order}H", data, 0x2A)[0]
            phnum = struct.unpack_from(f"{order}H", data, 0x2C)[0]
    except struct.error:
        return None, "eh-hdr-malformed-phdr"
    if phoff == 0 or phnum == 0:
        return None, None
    if (
        phentsize < min_phentsize
        or phoff > len(data)
        or phnum > MAX_SECTION_COUNT
        or phnum > (len(data) - phoff) // phentsize
    ):
        return None, "eh-hdr-malformed-phdr"
    found: tuple[int, bytes] | None = None
    for index in range(phnum):
        base = phoff + index * phentsize
        p_type = struct.unpack_from(f"{order}I", data, base)[0]
        if p_type != PT_GNU_EH_FRAME:
            continue
        if found is not None:
            return None, "eh-hdr-duplicate-phdr"
        if is_64:
            p_offset = struct.unpack_from(f"{order}Q", data, base + 8)[0]
            p_vaddr = struct.unpack_from(f"{order}Q", data, base + 16)[0]
            p_filesz = struct.unpack_from(f"{order}Q", data, base + 32)[0]
            p_memsz = struct.unpack_from(f"{order}Q", data, base + 40)[0]
        else:
            p_offset = struct.unpack_from(f"{order}I", data, base + 4)[0]
            p_vaddr = struct.unpack_from(f"{order}I", data, base + 8)[0]
            p_filesz = struct.unpack_from(f"{order}I", data, base + 16)[0]
            p_memsz = struct.unpack_from(f"{order}I", data, base + 20)[0]
        if (
            p_filesz == 0
            or p_offset > len(data)
            or p_filesz > len(data) - p_offset
            or (p_memsz and p_memsz < p_filesz)
        ):
            return None, "eh-hdr-truncated-phdr"
        found = (p_vaddr, data[p_offset : p_offset + p_filesz])
    return found, None


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


# ---------------------------------------------------------------------------
# Bounded ELF CPU ISA/ABI attributes and register conventions
# ---------------------------------------------------------------------------

MAX_ATTRIBUTE_BYTES = 64 * 1024

CpuFamily = Literal["arm", "riscv", "unknown"]

Isa = Literal[
    "armv6-m",
    "armv7-m",
    "armv7-a",
    "armv7e-m",
    "armv8-m.baseline",
    "armv8-m.mainline",
    "armv8.1-m.mainline",
    "riscv32",
    "riscv64",
]


@dataclass(frozen=True)
class RegisterConvention:
    """Architecture register numbers, GDB names and PC normalization rules."""

    family: Arch
    sp_register: int
    return_register: int
    frame_pointer_register: int
    pc_register: int | None
    pc_mask: int
    live_registers: tuple[tuple[int, str], ...]

    def normalize_pc(self, pc: int) -> int:
        """Clear instruction-state marker bits (such as Arm Thumb bit 0)."""
        return pc & self.pc_mask


ARM_CONVENTION = RegisterConvention(
    family="arm",
    sp_register=13,
    return_register=14,
    frame_pointer_register=7,
    pc_register=15,
    pc_mask=~1,
    live_registers=(
        *((register, f"r{register}") for register in range(13)),
        (13, "sp"),
        (14, "lr"),
        (15, "pc"),
    ),
)

RISCV_CONVENTION = RegisterConvention(
    family="riscv",
    sp_register=2,
    return_register=1,
    frame_pointer_register=8,
    pc_register=None,
    pc_mask=-1,
    live_registers=(
        (1, "ra"),
        (2, "sp"),
        (8, "s0"),
        (9, "s1"),
        (18, "s2"),
        (19, "s3"),
        (20, "s4"),
        (21, "s5"),
        (22, "s6"),
        (23, "s7"),
        (24, "s8"),
        (25, "s9"),
        (26, "s10"),
        (27, "s11"),
    ),
)


def convention_for_arch(arch: str) -> RegisterConvention:
    """Return the register convention for an architecture family or ISA name."""
    if arch.startswith("riscv"):
        return RISCV_CONVENTION
    return ARM_CONVENTION


def file_attributes(
    blob: bytes, vendor: bytes, endian: Endian = "little"
) -> dict[int, int | str] | None:
    """Read file-scoped attributes; reject truncation and conflicting values."""
    if not blob or len(blob) > MAX_ATTRIBUTE_BYTES or blob[:1] != b"A":
        return None
    result: dict[int, int | str] = {}
    found = False
    index = 1
    while index < len(blob):
        if index + 4 > len(blob):
            return None
        length = int.from_bytes(blob[index : index + 4], endian)
        end = index + length
        if length < 5 or end > len(blob):
            return None
        name_end = blob.find(b"\0", index + 4, end)
        if name_end < 0:
            return None
        if blob[index + 4 : name_end] == vendor:
            cursor = name_end + 1
            while cursor < end:
                subsection_start = cursor
                decoded = _read_uleb(blob, cursor, end)
                if decoded is None:
                    return None
                subsection_tag, cursor = decoded
                if cursor + 4 > end:
                    return None
                subsection_end = subsection_start + int.from_bytes(
                    blob[cursor : cursor + 4], endian
                )
                cursor += 4
                if subsection_end < cursor or subsection_end > end:
                    return None
                if subsection_tag == 1:
                    found = True
                    if not _parse_file_attribute_values(
                        blob, cursor, subsection_end, vendor, endian, result
                    ):
                        return None
                cursor = subsection_end
        index = end
    return result if found else None


def _parse_file_attribute_values(
    blob: bytes,
    cursor: int,
    end: int,
    vendor: bytes,
    endian: Endian,
    result: dict[int, int | str],
) -> bool:
    """Parse one file-scope attribute list, including nested Tag_File lists."""
    while cursor < end:
        item_start = cursor
        decoded = _read_uleb(blob, cursor, end)
        if decoded is None:
            return False
        tag, cursor = decoded
        if vendor == b"aeabi" and tag == 64:
            continue
        if vendor == b"aeabi" and tag in (1, 2, 3):
            if cursor + 4 > end:
                return False
            item_end = item_start + int.from_bytes(blob[cursor : cursor + 4], endian)
            cursor += 4
            if item_end < cursor or item_end > end:
                return False
            if item_end == cursor:
                return False
            if not _parse_file_attribute_values(
                blob, cursor, item_end, vendor, endian, result
            ):
                return False
            cursor = item_end
            continue

        if vendor == b"aeabi" and tag in (4, 5, 67):
            is_string = True
        elif vendor == b"aeabi" and tag in (32, 65):
            decoded = _read_uleb(blob, cursor, end)
            if decoded is None:
                return False
            _compatibility, cursor = decoded
            is_string = True
        elif vendor == b"aeabi":
            is_string = tag >= 32 and tag % 2 == 1
        else:
            is_string = tag % 2 == 1

        if is_string:
            value_end = blob.find(b"\0", cursor, end)
            if value_end < 0:
                return False
            try:
                value: int | str = blob[cursor:value_end].decode("ascii")
            except UnicodeDecodeError:
                return False
            cursor = value_end + 1
        else:
            decoded = _read_uleb(blob, cursor, end)
            if decoded is None:
                return False
            value, cursor = decoded

        known_tags = (
            {4, 5, 6, 7, *range(8, 29), 32, 48, 50, 52, 65, 66, 67, 68, 72, 74, 76}
            if vendor == b"aeabi"
            else {4, 5, 6, 8, 10, 12}
        )
        if tag not in known_tags:
            continue
        if tag in result:
            previous = result[tag]
            if vendor == b"aeabi" and tag in (4, 5):
                continue
            if vendor == b"aeabi" and tag in (6, 8, 9, 10, 48):
                result[tag] = max(int(previous), int(value))
                continue
            if previous != value:
                return False
        result[tag] = value
    return True


@dataclass(frozen=True)
class Cpu:
    """Image CPU ISA/ABI evidence; a recognized ISA does not prove a save layout."""

    isa: Isa | None
    family: CpuFamily
    reason: str
    abi: str | None = None
    extensions: frozenset[str] = frozenset()

    @property
    def convention(self) -> RegisterConvention | None:
        """Return the register convention when the architecture family is known."""
        if self.family == "unknown":
            return None
        return convention_for_arch(self.family)


def cpu_of(elf: ElfImage) -> Cpu:
    """Classify canonical ISA attributes, rejecting unsupported or conflicting ABI."""
    if elf.machine == EM_ARM:
        attrs = file_attributes(
            elf.section_data(".ARM.attributes") or b"", b"aeabi", elf.endian
        )
        if attrs is None or 6 not in attrs or 7 not in attrs:
            return Cpu(None, "arm", "cpu-evidence-unavailable")
        arch_number = attrs[6]
        if elf.pointer_size != 4 or not isinstance(attrs[7], int):
            return Cpu(None, "arm", "unsupported-cpu")
        if not isinstance(arch_number, int):
            return Cpu(None, "arm", "unsupported-cpu")
        if attrs[7] == ord("M"):
            isa_table: dict[int, Isa] = {
                10: "armv7-m",
                11: "armv6-m",
                12: "armv6-m",
                13: "armv7e-m",
                16: "armv8-m.baseline",
                17: "armv8-m.mainline",
                21: "armv8.1-m.mainline",
            }
            isa = isa_table.get(arch_number)
        elif attrs[7] == ord("A") and arch_number == 10:
            isa = "armv7-a"
        else:
            isa = None
        if isa is None:
            return Cpu(None, "arm", "unsupported-cpu")
        if isa == "armv7-a":
            return Cpu(isa, "arm", "ok", "aapcs32")
        if any(attrs.get(tag, 0) != 0 for tag in (12, 48, 50, 52, 74, 76)):
            return Cpu(isa, "arm", "unsupported-extension-context")
        if attrs.get(10, 0) not in (0, 1, 2, 3, 4, 5, 6, 7, 8):
            return Cpu(isa, "arm", "unsupported-extension-context")
        vfp_args = attrs.get(28, 0)
        if vfp_args not in (0, 1):
            return Cpu(isa, "arm", "unsupported-abi")
        abi = "aapcs32-vfp" if vfp_args == 1 else "aapcs32"
        return Cpu(isa, "arm", "ok", abi)
    if elf.machine != EM_RISCV:
        return Cpu(None, "unknown", "unsupported-cpu")
    attrs = file_attributes(
        elf.section_data(".riscv.attributes") or b"", b"riscv", elf.endian
    )
    if attrs is None:
        return Cpu(None, "riscv", "cpu-evidence-unavailable")
    arch_string = attrs.get(5)
    if not isinstance(arch_string, str):
        return Cpu(None, "riscv", "cpu-evidence-unavailable")
    match = re.fullmatch(
        r"rv(32|64)(i|e)([a-z]*)(?:[0-9]+p[0-9]+)?((?:_[a-z][a-z0-9]*)*)",
        arch_string,
    )
    if match is None:
        return Cpu(None, "riscv", "unsupported-cpu")
    xlen = int(match[1])
    isa = "riscv32" if xlen == 32 else "riscv64"
    extensions = frozenset(
        re.sub(r"[0-9]+p[0-9]+$", "", token)
        for token in (match[2], *match[3], *match[4].lstrip("_").split("_"))
        if token
    )
    if xlen != elf.pointer_size * 8 or bool(elf.flags & 8) != (match[2] == "e"):
        return Cpu(isa, "riscv", "cpu-evidence-conflict")
    if elf.endian != "little" or elf.flags & ~7 or match[2] != "i":
        return Cpu(isa, "riscv", "unsupported-abi", extensions=extensions)
    float_abi = {0: "", 2: "f", 4: "d", 6: "q"}[elf.flags & 6]
    if float_abi and float_abi not in extensions:
        return Cpu(isa, "riscv", "cpu-evidence-conflict")
    if elf.flags & 1 and "c" not in extensions:
        return Cpu(isa, "riscv", "cpu-evidence-conflict")
    allowed = {
        "i",
        "m",
        "a",
        "c",
        "f",
        "d",
        "zicsr",
        "zifencei",
        "zmmul",
        "zaamo",
        "zalrsc",
        "zca",
        "zcd",
        "zcf",
    }
    if extensions - allowed or attrs.get(4, 16) != 16:
        return Cpu(isa, "riscv", "unsupported-extension-context", extensions=extensions)
    return Cpu(
        isa, "riscv", "ok", ("ilp32" if xlen == 32 else "lp64") + float_abi, extensions
    )


# ---------------------------------------------------------------------------
# ELF debug-macro table walk (pure Python, stdlib only, no ``gdb`` import).
# ---------------------------------------------------------------------------


_SECTION_NAMES = (".debug_macro", ".debug_str", ".debug_line_str")

# `NAME <integer literal>` with an optional unsigned/long suffix, as stored in
# the debug strings (for example `NAME 4L` or `NAME 0x40005`).
_DEFINITION_RE = re.compile(
    rb"(?P<name>[A-Za-z_][A-Za-z0-9_]*) "
    rb"(?P<value>(?:0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*)"
)
_INTEGER_RE = re.compile(rb"^(?P<body>0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*$")


def scan_definitions(blob: bytes) -> dict[str, str]:
    """Collect ``{name: integer literal}`` from one debug string blob.

    Strings are NUL-separated; anything whose text is not exactly a
    ``NAME VALUE`` pair is ignored, which keeps ordinary debug strings (types,
    file names, unit producers) out of the result.
    """
    found: dict[str, str] = {}
    for chunk in blob.split(b"\x00"):
        # NOTE: an inline definition is stored right after its opcode and line
        # number, so the definition is the *trailing* `NAME VALUE` of the chunk,
        # not necessarily the whole of it.
        for match in _DEFINITION_RE.finditer(chunk.strip()):
            if match.end() != len(chunk.strip()):
                continue
            name = match.group("name").decode("ascii")
            value = match.group("value").decode("ascii")
            found.setdefault(name, value)
    return found


def parse_integer_literal(text: str) -> int | None:
    """Parse a C integer literal, accepting an unsigned/long suffix."""
    try:
        encoded = text.strip().encode("ascii")
    except UnicodeEncodeError:
        return None
    match = _INTEGER_RE.match(encoded)
    if match is None:
        return None
    try:
        return int(match.group("body"), 0)
    except ValueError:
        return None


def _read_cstring(data: bytes, offset: int) -> str | None:
    """Return the NUL-terminated string at *offset*, or ``None`` when out of range."""
    if offset < 0 or offset >= len(data):
        return None
    end = data.find(b"\x00", offset)
    if end < 0:
        return None
    return data[offset:end].decode("utf-8", errors="replace")


def section_payloads(
    data: bytes, names: tuple[str, ...] = _SECTION_NAMES
) -> dict[str, bytes]:
    """Return ``{section name: payload}`` for the requested sections of an ELF.

    An empty map means the input is not an ELF or carries none of them; callers
    treat a missing section as "no macros here" rather than an error.
    """
    if len(data) < 0x40 or data[:4] != b"\x7fELF":
        return {}
    table = iter_elf_section_headers(data)
    if table is None:
        return {}
    sections: dict[str, bytes] = {}
    for header in table.headers:
        name = _read_cstring(table.names, header.name_offset)
        if name in names and header.offset and header.size:
            sections[name] = data[header.offset : header.offset + header.size]
    return sections


def macro_definitions_from_elf(path: str | Path) -> dict[str, str]:
    """Read ``{macro name: integer literal}`` from the ELF at *path*.

    Returns an empty map rather than raising when the file is missing, is not an
    ELF, or carries no macro strings: an absent macro and an unreadable image are
    the same answer to the caller.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {}
    sections = section_payloads(data)
    if not sections:
        return {}
    found: dict[str, str] = {}
    # NOTE: indirect definitions live in .debug_str while inline ones live in
    # .debug_macro, so both are scanned; earlier sections win so a stable result
    # does not depend on section order.
    for name in (".debug_macro", ".debug_str", ".debug_line_str"):
        payload = sections.get(name)
        if not payload:
            continue
        for macro_name, value in scan_definitions(payload).items():
            found.setdefault(macro_name, value)
    return found
