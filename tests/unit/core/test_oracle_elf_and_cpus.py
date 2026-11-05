"""pyelftools differential oracle: ELF sections, symbols, anchors and attributes.

Layer 1 (``synthetic_corpus``) always runs, including in CI containers without
a fixture cache.  Layer 2 adds every cached firmware ELF when present locally.
Every assertion compares our bounded parser against pyelftools, never against
the component under test's own report.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from core.cpus import cpu_of, file_attributes
from core.unwind_meta import ElfImage

from .oracle_support import (
    FIXTURE_ROOT,
    SHF_ALLOC,
    SHF_EXECINSTR,
    SHT_ARM_EXIDX,
    SHT_ATTRIBUTES,
    SHT_PROGBITS,
    cached_elfs,
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
        (f"cache/{path.relative_to(FIXTURE_ROOT)}", str(path), None)
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
    symtab = elffile.get_section_by_name(".symtab")
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
    section = elffile.get_section_by_name(".ARM.attributes")
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
        if profile == ord("M"):
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
