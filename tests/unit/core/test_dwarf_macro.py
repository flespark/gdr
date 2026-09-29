"""Unit tests for the ELF debug-macro reader.

The reader recovers ``NAME VALUE`` definition strings from the image instead of
walking macro bytecode, so these tests pin the two things that could silently
produce a wrong version constant: the string pattern and the section walk.
"""

from __future__ import annotations

import struct
from pathlib import Path

from gdr.dwarf_macro import (
    macro_definitions_from_elf,
    parse_integer_literal,
    scan_definitions,
    section_payloads,
)


def _elf64(sections: dict[str, bytes]) -> bytes:
    """A minimal little-endian ELF64 carrying *sections*."""
    shstr = b""
    name_offsets: dict[str, int] = {}
    for name in [*sections, ".shstrtab"]:
        name_offsets[name] = len(shstr)
        shstr += name.encode() + b"\x00"

    payload = b""
    data_offsets: dict[str, int] = {}
    for name, blob in sections.items():
        data_offsets[name] = 64 + len(payload)
        payload += blob
    shstr_offset = 64 + len(payload)
    payload += shstr
    shoff = 64 + len(payload)

    entries = [b"\x00" * 64]
    for name, blob in sections.items():
        entries.append(
            struct.pack(
                "<IIQQQQIIQQ",
                name_offsets[name],
                1,
                0,
                0,
                data_offsets[name],
                len(blob),
                0,
                0,
                1,
                0,
            )
        )
    entries.append(
        struct.pack(
            "<IIQQQQIIQQ",
            name_offsets[".shstrtab"],
            3,
            0,
            0,
            shstr_offset,
            len(shstr),
            0,
            0,
            1,
            0,
        )
    )
    header = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    header += struct.pack("<HHI", 1, 0x28, 1)
    header += struct.pack("<QQQ", 0, 0, shoff)
    header += struct.pack("<IHHHHHH", 0, 64, 0, 0, 64, len(entries), len(entries) - 1)
    assert len(header) == 64
    return header + payload + b"".join(entries)


def test_definition_strings_are_recognised_with_their_suffix():
    """A macro definition is a ``NAME VALUE`` string, suffix included."""
    blob = b"RT_VERSION 4L\x00RT_VER_NUM 0x40005\x00RT_SUBVERSION 0\x00"

    assert scan_definitions(blob) == {
        "RT_VERSION": "4L",
        "RT_VER_NUM": "0x40005",
        "RT_SUBVERSION": "0",
    }


def test_ordinary_debug_strings_are_not_mistaken_for_definitions():
    """Types, paths and producers must not leak into the definition map."""
    blob = (
        b"struct tskTaskControlBlock\x00"
        b"/tmp/build/src/thread.c\x00"
        b"GNU C17 15.2.1 -g3 -gdwarf-4\x00"
        b"RT_VERSION 4L\x00"
        b"RT_VERSION 4L extra words\x00"
    )

    assert scan_definitions(blob) == {"RT_VERSION": "4L"}


def test_integer_literals_accept_c_suffixes_and_reject_junk():
    """The image stores C literals; a suffix is not a parse failure."""
    assert parse_integer_literal("4L") == 4
    assert parse_integer_literal("0x40005") == 0x40005
    assert parse_integer_literal("8UL") == 8
    assert parse_integer_literal("n/a") is None
    assert parse_integer_literal("") is None


def test_section_walk_finds_the_debug_sections():
    """The section-header walk is what makes the file scan usable."""
    payloads = {".debug_macro": b"inline text\x00", ".debug_str": b"RT_VERSION 4L\x00"}
    sections = section_payloads(_elf64(payloads))

    assert sections[".debug_str"] == b"RT_VERSION 4L\x00"
    assert sections[".debug_macro"] == b"inline text\x00"


def test_definitions_come_from_the_macro_and_string_sections():
    """Inline and indirect definitions are both recovered from the image."""
    image = _elf64(
        {
            # Inline form: the definition text sits in .debug_macro itself.
            ".debug_macro": b"\x01RT_REVISION 5L\x00",
            # Indirect form: the text is in .debug_str.
            ".debug_str": b"RT_VERSION 4L\x00RT_SUBVERSION 0L\x00",
        }
    )
    tmp = Path(__file__).with_name("synthetic-macros.elf")
    tmp.write_bytes(image)
    try:
        assert macro_definitions_from_elf(tmp) == {
            "RT_REVISION": "5L",
            "RT_VERSION": "4L",
            "RT_SUBVERSION": "0L",
        }
    finally:
        tmp.unlink()


def test_non_elf_and_missing_files_degrade_to_no_definitions():
    """An unreadable image is an empty map, never an exception."""
    tmp = Path(__file__).with_name("not-an-elf.bin")
    tmp.write_bytes(b"not an elf at all")
    try:
        assert macro_definitions_from_elf(tmp) == {}
        assert macro_definitions_from_elf(tmp.with_name("missing-file.bin")) == {}
        assert section_payloads(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8) == {}
    finally:
        tmp.unlink()
