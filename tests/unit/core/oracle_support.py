"""Shared helpers for the pyelftools/capstone differential oracle suites.

Layer 1 builds deterministic ELF and build-attribute corpora in memory so the
suites run in CI without a fixture cache.  Layer 2 discovers real firmware ELFs
under ``ci/.cache/fixtures`` when they are present locally.  Nothing here is
imported by production code: ``pyelftools`` and ``capstone`` stay dev-only.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = ROOT / "ci/.cache/fixtures"

SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_NOBITS = 8
SHT_ARM_EXIDX = 0x70000001
SHT_ATTRIBUTES = 0x70000003
SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4
SHF_ARM_EXIDX = 0x80
STT_FUNC = 2
STT_OBJECT = 1


def cached_elfs() -> tuple[Path, ...]:
    """Every firmware ELF in the local cache (empty in CI unit containers)."""
    return tuple(sorted(FIXTURE_ROOT.glob("**/*.elf")))


class SectionSpec(NamedTuple):
    name: str
    address: int
    data: bytes
    section_type: int = SHT_PROGBITS
    flags: int = 0
    size: int | None = None
    link: int = 0
    info: int = 0
    entsize: int = 0


class SymbolSpec(NamedTuple):
    name: str
    value: int
    size: int
    shndx: int
    info: int = STT_FUNC
    thumb: bool = False


def build_elf(
    *,
    is_64: bool,
    endian: str,
    machine: int,
    sections: tuple[SectionSpec, ...],
    symbols: tuple[SymbolSpec, ...] = (),
    flags: int = 0,
) -> bytes:
    """Build one valid ELF with a name table, optional NOBITS and a symtab.

    Section indices are ``0`` NULL, ``1`` ``.shstrtab``, then the caller's
    sections; a symtab pair follows when ``symbols`` is supplied.  ``shndx``
    in a :class:`SymbolSpec` therefore names a caller section starting at 2.
    """
    order = "<" if endian == "little" else ">"
    ehsize, shentsize = (64, 64) if is_64 else (52, 40)
    entries: list[SectionSpec] = list(sections)
    strtab = b"\0"
    offsets: dict[str, int] = {}
    symtab = b""
    if symbols:
        for symbol in symbols:
            offsets[symbol.name] = len(strtab)
            strtab += symbol.name.encode() + b"\0"
        for symbol in symbols:
            value = symbol.value | (1 if symbol.thumb else 0)
            if is_64:
                symtab += struct.pack(
                    f"{order}IBBHQQ",
                    offsets[symbol.name],
                    symbol.info,
                    0,
                    symbol.shndx,
                    value,
                    symbol.size,
                )
            else:
                symtab += struct.pack(
                    f"{order}IIIBBH",
                    offsets[symbol.name],
                    value,
                    symbol.size,
                    symbol.info,
                    0,
                    symbol.shndx,
                )
        strtab_index = 2 + len(entries)
        entries.append(SectionSpec(".strtab", 0, strtab, SHT_STRTAB))
        entries.append(
            SectionSpec(
                ".symtab",
                0,
                symtab,
                SHT_SYMTAB,
                link=strtab_index,
                info=1,
                entsize=24 if is_64 else 16,
            )
        )

    names = b"\0.shstrtab\0"
    name_offsets: dict[str, int] = {".shstrtab": 1}
    for spec in entries:
        name_offsets[spec.name] = len(names)
        names += spec.name.encode() + b"\0"
    shnum = 2 + len(entries)
    shoff = ehsize
    data_start = shoff + shnum * shentsize

    blobs = [names]
    blobs.extend(spec.data for spec in entries if spec.section_type != SHT_NOBITS)
    blob_offsets: list[int] = []
    cursor = data_start
    for blob in blobs:
        blob_offsets.append(cursor)
        cursor += len(blob)
    file_offsets: list[int] = []
    index = 1
    for spec in entries:
        if spec.section_type == SHT_NOBITS:
            file_offsets.append(0)
        else:
            file_offsets.append(blob_offsets[index])
            index += 1

    header = bytearray(ehsize)
    header[:4] = b"\x7fELF"
    header[4] = 2 if is_64 else 1
    header[5] = 1 if endian == "little" else 2
    header[6] = 1
    struct.pack_into(f"{order}H", header, 16, 1)
    struct.pack_into(f"{order}H", header, 18, machine)
    struct.pack_into(f"{order}I", header, 48 if is_64 else 36, flags)
    if is_64:
        struct.pack_into(f"{order}Q", header, 0x28, shoff)
        struct.pack_into(f"{order}H", header, 0x34, ehsize)
        struct.pack_into(f"{order}H", header, 0x3A, shentsize)
        struct.pack_into(f"{order}H", header, 0x3C, shnum)
        struct.pack_into(f"{order}H", header, 0x3E, 1)
    else:
        struct.pack_into(f"{order}I", header, 0x20, shoff)
        struct.pack_into(f"{order}H", header, 0x28, ehsize)
        struct.pack_into(f"{order}H", header, 0x2E, shentsize)
        struct.pack_into(f"{order}H", header, 0x30, shnum)
        struct.pack_into(f"{order}H", header, 0x32, 1)

    def section_header(
        name: int,
        section_type: int,
        sec_flags: int,
        address: int,
        offset: int,
        size: int,
        link: int,
        info: int,
        entsize: int,
    ) -> bytes:
        raw = bytearray(shentsize)
        struct.pack_into(f"{order}II", raw, 0, name, section_type)
        if is_64:
            struct.pack_into(f"{order}Q", raw, 8, sec_flags)
            struct.pack_into(f"{order}Q", raw, 16, address)
            struct.pack_into(f"{order}Q", raw, 24, offset)
            struct.pack_into(f"{order}Q", raw, 32, size)
            struct.pack_into(f"{order}II", raw, 40, link, info)
            struct.pack_into(f"{order}QQ", raw, 48, 0, entsize)
        else:
            struct.pack_into(f"{order}I", raw, 8, sec_flags)
            struct.pack_into(f"{order}I", raw, 12, address)
            struct.pack_into(f"{order}I", raw, 16, offset)
            struct.pack_into(f"{order}I", raw, 20, size)
            struct.pack_into(f"{order}II", raw, 24, link, info)
            struct.pack_into(f"{order}II", raw, 32, 0, entsize)
        return bytes(raw)

    table = bytearray(shnum * shentsize)
    table[0:shentsize] = section_header(0, 0, 0, 0, 0, 0, 0, 0, 0)
    table[shentsize : 2 * shentsize] = section_header(
        name_offsets[".shstrtab"],
        SHT_STRTAB,
        0,
        0,
        blob_offsets[0],
        len(names),
        0,
        0,
        0,
    )
    for i, spec in enumerate(entries, start=2):
        size = spec.size if spec.size is not None else len(spec.data)
        table[i * shentsize : (i + 1) * shentsize] = section_header(
            name_offsets[spec.name],
            spec.section_type,
            spec.flags,
            spec.address,
            file_offsets[i - 2],
            size,
            spec.link,
            spec.info,
            spec.entsize,
        )
    return bytes(header) + bytes(table) + b"".join(blobs)


def attributes_blob(
    vendor: bytes, attrs: tuple[tuple[int, int | str], ...], endian: str = "little"
) -> bytes:
    """One aeabi/riscv file-scope attribute subsection (DWARF-agnostic bytes)."""
    order = "little" if endian == "little" else "big"
    values = bytearray()
    for tag, value in attrs:
        values.append(tag)
        if isinstance(value, str) and tag in (4, 5):
            values.extend(value.encode() + b"\0")
        elif isinstance(value, str):
            values.append(ord(value))
        else:
            assert value < 128
            values.append(value)
    scope = b"\x01" + (len(values) + 5).to_bytes(4, order) + bytes(values)
    subsection = vendor + b"\0" + scope
    return b"A" + (len(subsection) + 4).to_bytes(4, order) + subsection


# A deterministic Thumb prologue/epilogue used by every Layer-1 ELF.
THUMB_CODE = bytes.fromhex("034b5b68da68034b1a6010bd704700bf")
A32_CODE = bytes.fromhex("00482de900108de8010000e12fff1e")
RISCV_CODE = bytes.fromhex("130105fd97000000e780000001000082")


def synthetic_corpus() -> tuple[tuple[str, bytes], ...]:
    """ELF32/ELF64, little/big-endian Arm and RISC-V images built in memory."""
    corpus: list[tuple[str, bytes]] = []
    arm_attrs = ((5, "7-M"), (6, 10), (7, "M"), (9, 2), (18, 4), (20, 1))
    riscv_attrs = ((5, "rv64i2p1_m2p0_a2p1_c2p0"), (4, 16))
    words = (0x7FFFFFD1, 0x80B0B0B0, 0x7FFFFFE1, 0x00000001)
    for endian in ("little", "big"):
        arm = attributes_blob(b"aeabi", arm_attrs, endian)
        exidx = b"".join(word.to_bytes(4, endian) for word in words)
        corpus.append(
            (
                f"elf32-{endian}-arm",
                build_elf(
                    is_64=False,
                    endian=endian,
                    machine=40,
                    sections=(
                        SectionSpec(
                            ".text",
                            0x1000,
                            THUMB_CODE,
                            SHT_PROGBITS,
                            SHF_ALLOC | SHF_EXECINSTR,
                        ),
                        SectionSpec(".ARM.attributes", 0, arm, SHT_ATTRIBUTES),
                        SectionSpec(
                            ".ARM.exidx",
                            0x2F00,
                            exidx,
                            SHT_ARM_EXIDX,
                            SHF_ALLOC | 0x80,
                        ),
                        SectionSpec(
                            ".bss",
                            0x20000000,
                            b"",
                            SHT_NOBITS,
                            SHF_ALLOC | SHF_WRITE,
                            size=0x400,
                        ),
                    ),
                    symbols=(
                        SymbolSpec("leaf", 0x1001, 8, 2, thumb=True),
                        SymbolSpec("worker", 0x1009, 6, 2, thumb=True),
                        SymbolSpec("boundary", 0x1004, 0, 2),
                        SymbolSpec("$t", 0x1000, 0, 2, info=STT_OBJECT),
                        SymbolSpec("$d", 0x1010, 0, 2, info=STT_OBJECT),
                    ),
                ),
            )
        )
    riscv = attributes_blob(b"riscv", riscv_attrs, "little")
    corpus.append(
        (
            "elf64-little-riscv",
            build_elf(
                is_64=True,
                endian="little",
                machine=243,
                sections=(
                    SectionSpec(
                        ".text",
                        0x1000,
                        RISCV_CODE,
                        SHT_PROGBITS,
                        SHF_ALLOC | SHF_EXECINSTR,
                    ),
                    SectionSpec(".riscv.attributes", 0, riscv, SHT_ATTRIBUTES),
                    SectionSpec(
                        ".bss",
                        0x20000000,
                        b"",
                        SHT_NOBITS,
                        SHF_ALLOC | SHF_WRITE,
                        size=0x800,
                    ),
                ),
                symbols=(
                    SymbolSpec("leaf", 0x1000, 12, 2),
                    SymbolSpec("$x", 0x1000, 0, 2, info=STT_OBJECT),
                ),
                flags=0x1,
            ),
        )
    )
    return tuple(corpus)
