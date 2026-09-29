"""Read macro definitions straight out of an ELF's debug macro strings.

GDB's macro lookup is scoped to the selected compilation unit and, for a
``list <function>``-based CU switch, needs the build tree to still exist at the
path recorded in the DWARF.  Neither is acceptable for a version constant the
debugger must resolve while inspecting an already-flashed image: the tree is
usually long gone, and the halted PC decides which CU happens to be in scope.

This module therefore reads the definition text out of the image itself.  A
macro's definition is stored as a ``NAME VALUE`` string, either inline in
``.debug_macro`` (``DW_MACRO_define``/``define_strx``) or, for the indirect
forms GCC emits for header macros, in ``.debug_str`` -- so scanning those
sections for NUL-terminated ``NAME <integer>`` strings recovers the version
constants without depending on opcode tables, unit versions or string-index
bases.

Semantics note: this reports a definition *present in the image*, not the macro
that is in scope at one source line.  For version constants that is the question
being asked ("which version was this firmware built from"), and an image with a
conflicting redefinition of the same version macro does not occur in practice;
if it did, the first definition encountered wins and the value is still one the
build contained.

The module is pure Python (stdlib only, no ``gdb`` import) so the container walk
and the pattern handling are unit-testable with synthetic bytes.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

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
        # Reason: an inline definition is stored right after its opcode and line
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
    is_64 = data[4] == 2
    big_endian = data[5] == 2
    order = ">" if big_endian else "<"
    try:
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
        return {}
    if shoff == 0 or shnum == 0 or shstrndx >= shnum:
        return {}

    offset_fmt = f"{order}{'Q' if is_64 else 'I'}"

    def header(index: int) -> tuple[int, int, int]:
        base = shoff + index * shentsize
        try:
            name = struct.unpack_from(f"{order}I", data, base)[0]
            offset = struct.unpack_from(offset_fmt, data, base + (24 if is_64 else 16))[
                0
            ]
            size = struct.unpack_from(offset_fmt, data, base + (32 if is_64 else 20))[0]
        except struct.error:
            return 0, 0, 0
        return name, offset, size

    _, names_offset, names_size = header(shstrndx)
    names_blob = data[names_offset : names_offset + names_size]
    sections: dict[str, bytes] = {}
    for index in range(shnum):
        name_offset, offset, size = header(index)
        name = _read_cstring(names_blob, name_offset)
        if name in names and offset and size:
            sections[name] = data[offset : offset + size]
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
    # Reason: indirect definitions live in .debug_str while inline ones live in
    # .debug_macro, so both are scanned; earlier sections win so a stable result
    # does not depend on section order.
    for name in (".debug_macro", ".debug_str", ".debug_line_str"):
        payload = sections.get(name)
        if not payload:
            continue
        for macro_name, value in scan_definitions(payload).items():
            found.setdefault(macro_name, value)
    return found
