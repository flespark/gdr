"""pyelftools differential oracle: ELF sections, symbols, anchors and attributes.

Layer 1 (``synthetic_corpus``) always runs, including in CI containers without
a fixture cache.  Layer 2 adds every cached firmware ELF when present locally.
Every assertion compares our bounded parser against pyelftools, never against
the component under test's own report.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from elftools.elf.elffile import ELFFile

from core.elf import ElfImage, cpu_of, file_attributes
from core.gdb_bridge import section_payloads
from tests.support.loader import cached_elfs, fixture_cache_root

from .oracle_support import (
    PT_GNU_EH_FRAME,
    SHF_ALLOC,
    SHF_EXECINSTR,
    SHT_ARM_EXIDX,
    SHT_ATTRIBUTES,
    SHT_PROGBITS,
    synthetic_corpus,
)

_KEEP_TYPES = {SHT_PROGBITS, SHT_ARM_EXIDX, SHT_ATTRIBUTES}
_TYPE_NUMBERS = {
    "SHT_PROGBITS": SHT_PROGBITS,
    "SHT_ARM_EXIDX": SHT_ARM_EXIDX,
    "SHT_ARM_ATTRIBUTES": SHT_ATTRIBUTES,
    "SHT_RISCV_ATTRIBUTES": SHT_ATTRIBUTES,
}


def _sources() -> list[tuple[str, str | None, bytes | None]]:
    builtin = [(f"builtin/{label}", None, data) for label, data in synthetic_corpus()]
    cached = [
        (f"cache/{path.relative_to(fixture_cache_root())}", str(path), None)
        for path in cached_elfs()
    ]
    return [*builtin, *cached]


_SOURCES = _sources()


def _load(source: tuple[str, str | None, bytes | None]) -> bytes:
    _label, path, data = source
    return Path(path).read_bytes() if path is not None else data or b""


def _decoded_elf(blob: bytes) -> tuple[ElfImage, ELFFile]:
    image = ElfImage.from_bytes(blob)
    assert image is not None
    return image, ELFFile(io.BytesIO(blob))


def _arm(elffile: ELFFile) -> bool:
    return elffile.header["e_machine"] == "EM_ARM"


def _normalise(value: int, arm: bool) -> int:
    return value & ~1 if arm else value


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_file_backed_sections_match_pyelftools(source):
    image, elffile = _decoded_elf(_load(source))
    seen = 0
    for section in elffile.iter_sections():
        numeric = _TYPE_NUMBERS.get(section["sh_type"])
        if numeric not in _KEEP_TYPES or section["sh_size"] == 0:
            continue
        ours = image.section(section.name)
        assert ours is not None, section.name
        assert ours.address == section["sh_addr"], section.name
        assert ours.data == section.data(), section.name
        seen += 1
    assert seen > 0


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_allocated_nobits_bounds_match_pyelftools(source):
    image, elffile = _decoded_elf(_load(source))
    for section in elffile.iter_sections():
        if section["sh_type"] != "SHT_NOBITS":
            continue
        if not section["sh_flags"] & SHF_ALLOC or section["sh_addr"] <= 0:
            continue
        assert image.section_bounds(section.name) == (
            section["sh_addr"],
            section["sh_addr"] + section["sh_size"],
        ), section.name


def _pyelftools_symbols(
    elffile: ELFFile,
) -> tuple[dict[str, tuple[int, int]], set[tuple[str, int]], set[str]]:
    symtab: Any = elffile.get_section_by_name(".symtab")
    if symtab is None:
        return {}, set(), set()
    sections = list(elffile.iter_sections())
    arm = _arm(elffile)
    functions: dict[str, tuple[int, int]] = {}
    anchors: set[tuple[str, int]] = set()
    zero_functions: set[str] = set()
    for symbol in symtab.iter_symbols():
        name = symbol.name
        shndx = symbol["st_shndx"]
        if not name or not isinstance(shndx, int) or not 0 < shndx < len(sections):
            continue
        flags = sections[shndx]["sh_flags"]
        if not flags & SHF_ALLOC:
            continue
        value = _normalise(symbol["st_value"], arm)
        anchors.add((name, value))
        if not flags & SHF_EXECINSTR:
            continue
        if symbol["st_info"]["type"] != "STT_FUNC":
            continue
        if symbol["st_size"] > 0:
            functions[name] = (value, symbol["st_size"])
        else:
            zero_functions.add(name)
    return functions, anchors, zero_functions


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_function_symbols_and_anchors_match_pyelftools(source):
    image, elffile = _decoded_elf(_load(source))
    functions, anchors, zero_functions = _pyelftools_symbols(elffile)
    assert {s.name: (s.start, s.size) for s in image.symbols} == functions
    assert set(image.anchors) == anchors
    assert {s.name for s in image.zero_size_functions} == zero_functions


def _pyelftools_attributes(elffile: ELFFile) -> dict[int, int | str] | None:
    section: Any = elffile.get_section_by_name(".ARM.attributes")
    struct = getattr(elffile.structs, "Elf_Arm_Attribute_Tag", None)
    vendor = b"aeabi"
    if section is None:
        section = elffile.get_section_by_name(".riscv.attributes")
        struct = getattr(elffile.structs, "Elf_RiscV_Attribute_Tag", None)
        vendor = b"riscv"
    if section is None or struct is None:
        return None
    encoding = struct.subcons[0].encoding
    result: dict[int, int | str] = {}
    for subsection in section.iter_subsections():
        if subsection["vendor_name"] != vendor.decode():
            continue
        for subsubsection in subsection.iter_subsubsections():
            if subsubsection.header.tag != "TAG_FILE":
                continue
            for attribute in subsubsection.iter_attributes():
                tag = encoding.get(attribute.tag)
                if tag is not None:
                    result[tag] = attribute.value
    return result


def _expected_isa(elffile: ELFFile, reference: dict[int, int | str]) -> str | None:
    machine = elffile.header["e_machine"]
    if machine == "EM_ARM":
        arch, profile = reference.get(6), reference.get(7)
        if profile == ord("M") and isinstance(arch, int):
            return {
                10: "armv7-m",
                11: "armv6-m",
                12: "armv6-m",
                13: "armv7e-m",
                16: "armv8-m.baseline",
                17: "armv8-m.mainline",
                21: "armv8.1-m.mainline",
            }.get(arch)
        if profile == ord("A") and arch == 10:
            return "armv7-a"
        return None
    if machine == "EM_RISCV":
        arch = reference.get(5)
        if isinstance(arch, str) and arch.startswith("rv"):
            return "riscv32" if arch.startswith("rv32") else "riscv64"
    return None


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_build_attributes_and_isa_match_pyelftools(source):
    blob = _load(source)
    image, elffile = _decoded_elf(blob)
    reference = _pyelftools_attributes(elffile)
    section = elffile.get_section_by_name(".ARM.attributes")
    vendor = b"aeabi"
    if section is None:
        section = elffile.get_section_by_name(".riscv.attributes")
        vendor = b"riscv"
    if section is None or reference is None:
        assert not image.section(".ARM.attributes")
        assert not image.section(".riscv.attributes")
        return
    ours = file_attributes(section.data(), vendor, image.endian)
    assert ours is not None
    disagreements = {
        tag: (reference[tag], ours[tag])
        for tag in ours
        if tag in reference and reference[tag] != ours[tag]
    }
    assert not disagreements, disagreements
    for tag in (6, 7) if vendor == b"aeabi" else (5,):
        assert tag in ours and reference.get(tag) == ours[tag], tag
    assert cpu_of(image).isa == _expected_isa(elffile, reference)


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_vaddr_reads_and_code_bounds_match_pyelftools(source):
    image, elffile = _decoded_elf(_load(source))
    all_retained_spans: list[tuple[int, int]] = []
    retained_spans: list[tuple[str, int, int, bytes, bool]] = []
    expected_code_end = 0
    for section in elffile.iter_sections():
        numeric = _TYPE_NUMBERS.get(section["sh_type"])
        start = section["sh_addr"]
        size = section["sh_size"]
        if numeric in _KEEP_TYPES and size > 0:
            data = section.data()
            end = start + len(data)
            is_exec = bool(section["sh_flags"] & SHF_EXECINSTR)
            if is_exec and end > expected_code_end:
                expected_code_end = end
            all_retained_spans.append((start, end))
            if start > 0:
                retained_spans.append((section.name, start, end, data, is_exec))
        elif (
            section["sh_type"] == "SHT_NOBITS"
            and section["sh_flags"] & SHF_ALLOC
            and start > 0
            and size > 0
        ):
            assert image.read_at(start, 1) is None, section.name
            assert not image.is_code(start), section.name

    assert image.code_end() == expected_code_end
    assert not image.is_code(0)
    assert image.read_at(0x1000, 0) is None

    for name, start, end, data, is_exec in retained_spans:
        size = len(data)
        assert image.section_bounds(name) == (start, end), name
        head = min(size, 16)
        assert image.read_at(start, head) == data[:head], name
        assert image.read_at(end - head, head) == data[size - head :], name
        if size > 4:
            mid = size // 2
            span = min(4, size - mid)
            assert image.read_at(start + mid, span) == data[mid : mid + span], name
        assert image.is_code(start) is is_exec, name
        assert image.is_code(end - 1) is is_exec, name
        if not any(
            other_start <= end - 1 and end + 1 <= other_end
            for other_start, other_end in all_retained_spans
        ):
            assert image.read_at(end - 1, 2) is None, name
        if not any(
            other_start <= end < other_end
            for _, other_start, other_end, _, other_exec in retained_spans
            if other_exec
        ):
            assert not image.is_code(end), name


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_symbol_lookup_and_containing_functions_match_pyelftools(source):
    image, elffile = _decoded_elf(_load(source))
    symtab: Any = elffile.get_section_by_name(".symtab")
    if symtab is None:
        return
    sections = list(elffile.iter_sections())
    arm = _arm(elffile)
    by_name: dict[str, set[int]] = {}
    sized_funcs: list[tuple[str, int, int, bool | None]] = []
    for symbol in symtab.iter_symbols():
        name = symbol.name
        shndx = symbol["st_shndx"]
        if not name or not isinstance(shndx, int) or not 0 < shndx < len(sections):
            continue
        flags = sections[shndx]["sh_flags"]
        if not flags & SHF_ALLOC:
            continue
        raw_value = symbol["st_value"]
        value = _normalise(raw_value, arm)
        by_name.setdefault(name, set()).add(value)
        if (
            flags & SHF_EXECINSTR
            and symbol["st_info"]["type"] == "STT_FUNC"
            and symbol["st_size"] > 0
        ):
            thumb = bool(raw_value & 1) if arm else None
            sized_funcs.append((name, value, value + symbol["st_size"], thumb))

    sized_funcs.sort(key=lambda item: item[1])
    sampled_names = [name for name, addrs in by_name.items() if len(addrs) > 1] + list(
        by_name
    )[:128]
    for name in dict.fromkeys(sampled_names):
        addrs = by_name[name]
        expected = next(iter(addrs)) if len(addrs) == 1 else None
        assert image.symbol_address(name) == expected, name
    assert image.symbol_address("__gdr_absent_symbol__") is None

    for _name, start, end, _thumb in sized_funcs[:64]:
        for pc in (start, (start + end) // 2, end - 1):
            ref = next(item for item in sized_funcs if item[1] <= pc < item[2])
            ours = image.function_containing(pc)
            assert ours is not None, hex(pc)
            assert (ours.name, ours.start, ours.end, ours.thumb) == ref


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_gnu_eh_frame_segment_matches_pyelftools(source):
    image, elffile = _decoded_elf(_load(source))
    segments = [
        seg
        for seg in elffile.iter_segments()
        if seg["p_type"] in ("PT_GNU_EH_FRAME", PT_GNU_EH_FRAME)
    ]
    section = elffile.get_section_by_name(".eh_frame_hdr")
    expected_seg = (
        (segments[0]["p_vaddr"], segments[0].data())
        if len(segments) == 1 and segments[0]["p_filesz"] > 0
        else None
    )
    assert image.gnu_eh_frame == expected_seg
    assert image.gnu_eh_frame_error is None
    expected_sec = (
        (section["sh_addr"], section.data())
        if section is not None and section["sh_size"] > 0
        else None
    )
    hdr, err = image.eh_frame_hdr_source()
    if expected_seg is not None and expected_sec is not None:
        if expected_seg == expected_sec:
            assert (hdr, err) == (expected_seg, None)
        else:
            assert (hdr, err) == (None, "eh-hdr-program-section-mismatch")
    else:
        assert (hdr, err) == (expected_seg or expected_sec, None)


@pytest.mark.parametrize("source", _SOURCES, ids=[entry[0] for entry in _SOURCES])
def test_debug_macro_section_payloads_match_pyelftools(source):
    blob = _load(source)
    _image, elffile = _decoded_elf(blob)
    expected: dict[str, bytes] = {}
    for name in (".debug_macro", ".debug_str", ".debug_line_str"):
        section = elffile.get_section_by_name(name)
        if section is not None and section["sh_offset"] and section["sh_size"]:
            expected[name] = section.data()
    assert section_payloads(blob) == expected
