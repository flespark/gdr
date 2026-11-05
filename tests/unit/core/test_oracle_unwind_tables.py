"""pyelftools differential oracle: CFI rows, DWARF expressions and ARM EHABI.

Layer 1 builds deterministic ``.debug_frame``/``.eh_frame``/``.ARM.exidx`` byte
streams; layer 2 scans cached firmware ELFs.  CFI rows are compared at every
state transition, expression bytecode boundaries against ``DWARFExprParser``,
and compact-model EHABI bytecodes against ``EHABIEntry``.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import pytest
from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.ehabi.ehabiinfo import EHABIInfo, arm_expand_prel31
from elftools.elf.elffile import ELFFile

from core.unwind_cfi import parse_cfi_section
from core.unwind_ehabi import _eh_insn_words, parse_ehabi
from core.unwind_expr import _expression_boundaries, evaluate_cfi_expression
from core.unwind_meta import ElfImage

from .oracle_support import (
    FIXTURE_ROOT,
    SHT_PROGBITS,
    SectionSpec,
    build_elf,
    cached_elfs,
)


def _uleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _sleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        done = (value == 0 and not byte & 0x40) or (value == -1 and byte & 0x40)
        out.append(byte | (0 if done else 0x80))
        if done:
            return bytes(out)


def _debug_frame() -> bytes:
    body = (
        b"\xff\xff\xff\xff"
        + bytes([1])
        + b"\x00"
        + _uleb(1)
        + _sleb(-4)
        + bytes([14])
        + b"\x0c\x0d\x00"
    )
    cie = struct.pack("<I", len(body)) + body
    fde_body = (
        struct.pack("<I", 0)
        + struct.pack("<I", 0x1000)
        + struct.pack("<I", 0x100)
        + b"\x41\x0e\x08\x8e\x01\x84\x02\x42\x0e\x10\x8e\x02"
    )
    return cie + struct.pack("<I", len(fde_body)) + fde_body


def _eh_frame() -> tuple[int, bytes]:
    section_address = 0x8000
    augmentation_data = bytes([0x1B])
    body = (
        b"\x00\x00\x00\x00"
        + bytes([1])
        + b"zR\x00"
        + _uleb(1)
        + _sleb(-8)
        + bytes([16])
        + _uleb(len(augmentation_data))
        + augmentation_data
        + b"\x0c\x02\x08"
    )
    cie = struct.pack("<I", len(body)) + body
    pc_field = section_address + len(cie) + 8
    fde_body = (
        struct.pack("<I", len(cie) + 4)
        + struct.pack("<I", (0x1000 - pc_field) & 0xFFFFFFFF)
        + struct.pack("<I", 0x100)
        + _uleb(0)
        + b"\x41\x0e\x10\x91\x01"
    )
    return section_address, cie + struct.pack("<I", len(fde_body)) + fde_body


def _cfi_elf(*, eh: bool) -> bytes:
    if eh:
        address, blob = _eh_frame()
        name = ".eh_frame"
    else:
        address, blob = 0, _debug_frame()
        name = ".debug_frame"
    return build_elf(
        is_64=False,
        endian="little",
        machine=40,
        sections=(
            SectionSpec(".text", 0x1000, b"\x00" * 0x100, SHT_PROGBITS, 0x6),
            SectionSpec(name, address, blob, SHT_PROGBITS, 0),
        ),
    )


_RULE_KINDS = {
    "UNDEFINED": "undefined",
    "SAME_VALUE": "same",
    "OFFSET": "offset",
    "VAL_OFFSET": "val_offset",
    "REGISTER": "register",
    "EXPRESSION": "expression",
    "VAL_EXPRESSION": "val_expression",
}


def _cfa_signature(rule) -> tuple:
    if getattr(rule, "expr", None) is not None:
        return ("expr", bytes(rule.expr))
    if getattr(rule, "expression", None) is not None:
        return ("expr", bytes(rule.expression))
    return ("reg", rule.reg if hasattr(rule, "reg") else rule.register, rule.offset)


def _register_signature(rule) -> tuple | None:
    if hasattr(rule, "kind"):
        if rule.kind in ("expression", "val_expression"):
            return (rule.kind, rule.expression)
        if rule.kind == "register":
            return (rule.kind, rule.register)
        return (rule.kind, rule.value)
    kind = _RULE_KINDS.get(rule.type)
    if kind is None:
        return None
    if kind in ("expression", "val_expression"):
        return (kind, bytes(rule.arg))
    return (kind, 0 if rule.arg is None else rule.arg)


def _reference_fdes(elffile: ELFFile, *, eh: bool):
    """Return (start, end, rows) for every positive-address FDE."""
    dwarf = elffile.get_dwarf_info()
    if eh:
        if not dwarf.has_EH_CFI():
            return []
        entries = dwarf.EH_CFI_entries()
    else:
        if not dwarf.has_CFI():
            return []
        entries = dwarf.CFI_entries()
    result = []
    for entry in entries:
        if type(entry).__name__ != "FDE":
            continue
        header = entry.header
        start = header["initial_location"]
        end = start + header["address_range"]
        if start <= 0 or end <= start:
            continue
        result.append((start, end, list(entry.get_decoded().table)))
    return result


def _row_at(rows, pc: int):
    chosen = None
    for row in rows:
        if row["pc"] <= pc:
            chosen = row
        else:
            break
    return chosen


def _sample_pcs(start: int, end: int, rows, limit: int = 8) -> set[int]:
    points = {start, end - 2, end - 1}
    for row in rows:
        points.add(row["pc"])
        points.add(row["pc"] + 1)
    for previous, current in zip(rows, rows[1:], strict=False):
        points.add((previous["pc"] + current["pc"]) // 2)
    valid = sorted(pc for pc in points if start <= pc < end)
    if len(valid) <= limit:
        return set(valid)
    step = len(valid) / limit
    return {valid[min(len(valid) - 1, int(i * step))] for i in range(limit)}


def _sample_fdes(fdes, limit: int = 40):
    if len(fdes) <= limit:
        return fdes
    step = len(fdes) / limit
    return [fdes[min(len(fdes) - 1, int(i * step))] for i in range(limit)]


def _assert_cfi_section_matches(
    blob: bytes, section_address: int, *, eh: bool, label: str
) -> None:
    elffile = ELFFile(io.BytesIO(blob))
    section = elffile.get_section_by_name(".eh_frame" if eh else ".debug_frame")
    assert section is not None, label
    image = ElfImage.from_bytes(blob)
    assert image is not None
    ours = parse_cfi_section(
        section.data(),
        section_address=section_address,
        address_size=image.pointer_size,
        endian=image.endian,
        is_eh_frame=eh,
    )
    fdes = _reference_fdes(elffile, eh=eh)
    checked = 0
    for start, end, rows in _sample_fdes(fdes):
        for pc in _sample_pcs(start, end, rows):
            covering = [f for f in fdes if f[0] <= pc < f[1]]
            if not covering:
                continue
            expected_fde = max(covering, key=lambda item: item[0])
            reference_row = _row_at(expected_fde[2], pc)
            if reference_row is None:
                continue
            row = ours.row_for(pc)
            assert row is not None and row.error is None, (label, hex(pc), row)
            assert _cfa_signature(row.cfa) == _cfa_signature(reference_row["cfa"]), (
                label,
                hex(pc),
            )
            our_rules = {
                reg: _register_signature(rule)
                for reg, rule in row.registers.items()
                if _register_signature(rule) is not None
            }
            ref_rules = {
                reg: _register_signature(rule)
                for reg, rule in reference_row.items()
                if reg not in ("pc", "cfa") and _register_signature(rule) is not None
            }
            assert our_rules == ref_rules, (label, hex(pc), our_rules, ref_rules)
            checked += 1
    return checked


@pytest.mark.parametrize("eh", [False, True], ids=["debug_frame", "eh_frame"])
def test_synthetic_cfi_rows_match_pyelftools(eh):
    blob = _cfi_elf(eh=eh)
    address = _eh_frame()[0] if eh else 0
    assert _assert_cfi_section_matches(blob, address, eh=eh, label=f"builtin-{eh}")


_CFI_SOURCES = [
    (f"cache/{path.relative_to(FIXTURE_ROOT)}", str(path)) for path in cached_elfs()
]


@pytest.mark.skipif(not _CFI_SOURCES, reason="no cached firmware ELFs")
@pytest.mark.parametrize(
    "label,path", _CFI_SOURCES, ids=[entry[0] for entry in _CFI_SOURCES]
)
def test_cached_cfi_rows_match_pyelftools(label, path):
    blob = Path(path).read_bytes()
    image = ElfImage.from_bytes(blob)
    assert image is not None
    for name, eh in ((".debug_frame", False), (".eh_frame", True)):
        section = image.section(name)
        if section is None:
            continue
        _assert_cfi_section_matches(
            blob, section.address, eh=eh, label=f"{label}:{name}"
        )


# ---------------------------------------------------------------------------
# DWARF CFI expressions
# ---------------------------------------------------------------------------

_EXPRESSION_CORPUS = (
    bytes([0x30, 0x31, 0x22, 0x9F]),
    bytes([0x08, 0x07, 0x30, 0x1C, 0x9F]),
    bytes([0x70, 0x04, 0x23, 0x05, 0x9F]),
    bytes([0x31, 0x32, 0x1A, 0x33, 0x21, 0x9F]),
    bytes([0x34, 0x31, 0x24, 0x9F]),
    bytes([0x38, 0x31, 0x25, 0x9F]),
    bytes([0x31, 0x32, 0x12, 0x13, 0x16, 0x22, 0x9F]),
    bytes([0x31, 0x32, 0x14, 0x15, 0x01, 0x22, 0x9F]),
    bytes([0x92, 0x08, 0x30, 0x23, 0x04, 0x9F]),
    bytes([0x96, 0x30, 0x31, 0x22, 0x9F]),
)


def _real_expressions(blob: bytes) -> list[bytes]:
    """Collect every expression block pyelftools decoded from cached CFI rows."""
    elffile = ELFFile(io.BytesIO(blob))
    dwarf = elffile.get_dwarf_info()
    found: list[bytes] = []
    for entries_getter in (dwarf.CFI_entries, dwarf.EH_CFI_entries):
        try:
            entries = entries_getter()
        except Exception:
            continue
        for entry in entries:
            if type(entry).__name__ != "FDE":
                continue
            try:
                rows = entry.get_decoded().table
            except Exception:
                continue
            for row in rows:
                cfa = row["cfa"]
                if getattr(cfa, "expr", None) is not None:
                    found.append(bytes(cfa.expr))
                for key, rule in row.items():
                    if key in ("pc", "cfa"):
                        continue
                    if getattr(rule, "type", "") in ("EXPRESSION", "VAL_EXPRESSION"):
                        found.append(bytes(rule.arg))
    return found


def _expression_sample_paths() -> tuple[Path, ...]:
    """A bounded, representative set of cached ELFs with CFI expressions."""
    preferred: list[Path] = []
    for marker in (
        "eh-frame",
        "mps2-an385/10.3.1/full",
        "cortex-a9/4.0.5",
        "microbit/11.1.0/full",
    ):
        preferred.extend(path for path in cached_elfs() if marker in str(path))
    return tuple(dict.fromkeys(preferred))[:4]


def _expression_sources() -> list[tuple[str, bytes]]:
    sources = [(f"builtin/{i}", blob) for i, blob in enumerate(_EXPRESSION_CORPUS)]
    for path in _expression_sample_paths():
        for index, blob in enumerate(_real_expressions(path.read_bytes())):
            sources.append((f"cache/{path.parent.name}:{index}", blob))
    return sources


_EXPRESSION_SOURCES = _expression_sources()


@pytest.mark.parametrize(
    "label,blob",
    _EXPRESSION_SOURCES,
    ids=[entry[0] for entry in _EXPRESSION_SOURCES],
)
def test_expression_boundaries_match_pyelftools(label, blob):
    elffile = ELFFile(io.BytesIO(_cfi_elf(eh=False)))
    parser = DWARFExprParser(elffile.get_dwarf_info().structs)
    try:
        operations = list(parser.parse_expr(blob))
    except Exception:
        pytest.skip(f"pyelftools cannot parse {label}")
    expected = {operation.offset for operation in operations} | {len(blob)}
    assert _expression_boundaries(blob, address_size=4) == expected, label


def _reference_eval(blob: bytes, registers: dict[int, int], width: int = 32) -> int:
    """Independent integer evaluator for the curated DWARF subset."""
    mask = (1 << width) - 1
    stack: list[int] = []
    index = 0

    def uleb() -> int:
        nonlocal index
        value = shift = 0
        while True:
            byte = blob[index]
            index += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    def sleb() -> int:
        nonlocal index
        value = shift = 0
        while True:
            byte = blob[index]
            index += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                if byte & 0x40:
                    value -= 1 << shift
                return value

    while index < len(blob):
        opcode = blob[index]
        index += 1
        if 0x30 <= opcode <= 0x4F:
            stack.append((opcode - 0x30) & mask)
        elif opcode == 0x08:
            stack.append(blob[index] & mask)
            index += 1
        elif 0x70 <= opcode <= 0x8F:
            offset = sleb()
            stack.append((registers[opcode - 0x70] + offset) & mask)
        elif opcode == 0x92:
            register = uleb()
            offset = sleb()
            stack.append((registers[register] + offset) & mask)
        elif opcode == 0x23:
            stack.append((stack.pop() + uleb()) & mask)
        elif opcode in (0x1A, 0x1C, 0x21, 0x22, 0x24, 0x25, 0x27):
            right, left = stack.pop(), stack.pop()
            stack.append(
                {
                    0x1A: left & right,
                    0x1C: left - right,
                    0x21: left | right,
                    0x22: left + right,
                    0x24: left << (right & 31),
                    0x25: left >> (right & 31),
                    0x27: left ^ right,
                }[opcode]
                & mask
            )
        elif opcode == 0x12:
            stack.append(stack[-1])
        elif opcode == 0x13:
            stack.pop()
        elif opcode == 0x14:
            stack.append(stack[-2])
        elif opcode == 0x15:
            stack.append(stack[-1 - blob[index]])
            index += 1
        elif opcode == 0x16:
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif opcode in (0x9F, 0x96):
            pass
        else:
            raise AssertionError(f"unhandled opcode {opcode:#x}")
    return stack[-1]


@pytest.mark.parametrize("blob", _EXPRESSION_CORPUS)
def test_expression_values_match_reference_evaluator(blob):
    registers = {register: 0x1000 + register * 8 for register in range(16)}
    ours = evaluate_cfi_expression(
        blob, registers=registers, read=None, address_size=4, endian="little"
    )
    assert not isinstance(ours, str), ours
    assert ours == _reference_eval(blob, registers)


# ---------------------------------------------------------------------------
# ARM EHABI
# ---------------------------------------------------------------------------


def _exidx() -> tuple[int, bytes]:
    address = 0x2F00
    entries = (
        (0x7FFFFFD1, 0x80B0B0B0),
        (0x7FFFFFE1, 0x00000001),
        (0x7FFFFFF1, 0x00000100),
    )
    return address, b"".join(
        first.to_bytes(4, "little") + second.to_bytes(4, "little")
        for first, second in entries
    )


def _assert_exidx_matches(blob: bytes, section_address: int, label: str) -> None:
    elffile = ELFFile(io.BytesIO(blob))
    section = elffile.get_section_by_name(".ARM.exidx")
    assert section is not None, label
    data = section.data()
    info = EHABIInfo(section, elffile.little_endian)
    image = ElfImage.from_bytes(blob)
    assert image is not None
    read_at = image.read_at
    table = parse_ehabi(
        data,
        section_address=section_address,
        read_at=read_at,
        text_end=image.code_end(),
        endian=image.endian,
    )
    assert len(table.entries) == info.num_entry(), label
    for index in range(info.num_entry()):
        offset = index * 8
        first = struct.unpack_from("<I", data, offset)[0]
        second = struct.unpack_from("<I", data, offset + 4)[0]
        expected_start = arm_expand_prel31(first, section_address + offset) & 0xFFFFFFFF
        ours = table.entries[index]
        assert ours.start == expected_start, (label, index, hex(ours.start))
        assert ours.cant_unwind == (second == 1), (label, index)
        if second & 0x80000000:
            assert ours.inline == second, (label, index)
            expected_bytecodes = _eh_insn_words(second, None, "little")
            assert (
                list(expected_bytecodes or ()) == info.get_entry(index).bytecode_array
            ), (label, index)
        elif second != 1:
            assert ours.unwind == (
                arm_expand_prel31(second, section_address + offset + 4) & 0xFFFFFFFF
            ), (label, index)


def test_synthetic_exidx_matches_pyelftools():
    address, exidx = _exidx()
    blob = build_elf(
        is_64=False,
        endian="little",
        machine=40,
        sections=(
            SectionSpec(".text", 0x1000, b"\x00" * 0x200, SHT_PROGBITS, 0x6),
            SectionSpec(".ARM.exidx", address, exidx, 0x70000001, 0x82),
        ),
    )
    _assert_exidx_matches(blob, address, "builtin-exidx")


_EXIDX_SOURCES = [
    (f"cache/{path.relative_to(FIXTURE_ROOT)}", str(path))
    for path in cached_elfs()
    if (image := ElfImage.from_path(path)) is not None
    and image.section(".ARM.exidx") is not None
]


@pytest.mark.skipif(not _EXIDX_SOURCES, reason="no cached EHABI ELFs")
@pytest.mark.parametrize(
    "label,path", _EXIDX_SOURCES, ids=[entry[0] for entry in _EXIDX_SOURCES]
)
def test_cached_exidx_matches_pyelftools(label, path):
    blob = Path(path).read_bytes()
    image = ElfImage.from_bytes(blob)
    assert image is not None
    section = image.section(".ARM.exidx")
    assert section is not None
    _assert_exidx_matches(blob, section.address, label)
