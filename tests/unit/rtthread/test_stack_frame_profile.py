"""RT-Thread compiled-port evidence: layout proofs, not fixed stream fingerprints.

Layer 1 builds deterministic Arm A32/Thumb ELFs in memory so the suite runs in
CI without a fixture cache.  Layer 2 scans cached RT-Thread firmware ELFs when
they are present locally.  Mutations remove individual semantic facts and must
still be refused, which guards against a fallback to symbol-name trust.
"""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from core.unwind_meta import ElfImage
from rtthread.stack_frame_profile import select_context
from tests.unit.core.oracle_support import (
    FIXTURE_ROOT,
    SectionSpec,
    SymbolSpec,
    attributes_blob,
    build_elf,
    cached_elfs,
)

_SHF_CODE = 0x6
_SHT_PROGBITS = 1
_SHT_ATTRIBUTES = 0x70000003
_A32_NOP = 0xE1A00000
_THUMB_NOP = 0xBF00
_THUMB_UDF = 0xDE00

_A32_SWITCH = (
    0xE92D4000,  # stmdb sp!, {lr}
    0xE92D5FFF,  # stmdb sp!, {r0-r12, lr}
    0xE10F4000,  # mrs r4, cpsr
    0xE92D0010,  # stmdb sp!, {r4}
    0xE24DD008,  # sub sp, sp, #8  (LWP header)
    0xE580D000,  # str sp, [r0, #0]
    0xE591D000,  # ldr sp, [r1, #0]
)
_THUMB_PENDSV = (
    0xF3EF,
    0x8109,  # mrs r1, psp
    0xE921,
    0x0FF0,  # stmdb r1!, {r4-r11}
    0x6001,  # str r1, [r0, #0]
    0xF381,
    0x8809,  # msr psp, r1
    0xE8B1,
    0x0FF0,  # ldmia r1!, {r4-r11}
    0x4770,  # bx lr
)


def _image(isa: str) -> tuple[ElfImage, bytes]:
    """Build one deterministic Arm port ELF for *isa*."""
    a32 = isa == "armv7-a"
    attributes = attributes_blob(
        b"aeabi",
        (
            (5, "7-A" if a32 else "7-M"),
            (6, 10),
            (7, "A" if a32 else "M"),
            (9, 2),
        ),
    )
    text = bytearray(b"\x00" * 0x100)

    def place(offset: int, words: tuple[int, ...], *, halfword: bool = False) -> None:
        raw = b"".join(
            struct.pack("<H", word) if halfword else struct.pack("<I", word)
            for word in words
        )
        text[offset : offset + len(raw)] = raw

    if a32:
        place(0x00, _A32_SWITCH)
        place(0x80, (0xE5810000,))  # rt_hw_stack_init: str r0, [r1, #0]
        symbols = (
            SymbolSpec("rt_hw_context_switch", 0x1000, 0x1C, 2),
            SymbolSpec("rt_hw_context_switch_interrupt", 0x1020, 0, 2),
            SymbolSpec("rt_hw_stack_init", 0x1080, 4, 2),
        )
    else:
        place(0x00, (0x6010,), halfword=True)  # request: str r0, [r2, #0]
        place(0x20, _THUMB_PENDSV, halfword=True)
        place(0x80, (0x6008, 0x4770), halfword=True)  # initializer
        symbols = (
            SymbolSpec("rt_hw_context_switch", 0x1000, 2, 2),
            SymbolSpec("PendSV_Handler", 0x1020, 0x40, 2),
            SymbolSpec("rt_hw_context_switch_to", 0x1060, 0, 2),
            SymbolSpec("rt_hw_stack_init", 0x1080, 4, 2),
        )
    data = build_elf(
        is_64=False,
        endian="little",
        machine=40,
        sections=(
            SectionSpec(".text", 0x1000, bytes(text), _SHT_PROGBITS, _SHF_CODE),
            SectionSpec(".ARM.attributes", 0, attributes, _SHT_ATTRIBUTES, 0),
        ),
        symbols=symbols,
    )
    image = ElfImage.from_bytes(data)
    assert image is not None
    return image, data


def _mutate(image: ElfImage, address: int, raw: bytes) -> ElfImage:
    sections = []
    for section in image.sections:
        if section.address <= address < section.address + len(section.data):
            data = bytearray(section.data)
            offset = address - section.address
            data[offset : offset + len(raw)] = raw
            sections.append(replace(section, data=bytes(data)))
        else:
            sections.append(section)
    return ElfImage(
        image.pointer_size,
        image.endian,
        image.machine,
        tuple(sections),
        image.symbols,
        image.flags,
        image.anchors,
        image.nobits_sections,
        image.zero_size_functions,
    )


@pytest.mark.parametrize("isa", ["armv7-a", "armv7-m"])
def test_synthetic_ports_prove_their_layout(isa):
    image, _data = _image(isa)
    selected = select_context(image)
    assert selected.reason == "ok" and selected.context is not None
    assert selected.isa == isa


@pytest.mark.parametrize(
    "offset,raw,label",
    [
        (0x10, struct.pack("<I", _A32_NOP), "no LWP header bias"),
        (0x04, struct.pack("<I", 0xE92D5FFE), "truncated save bitmap"),
        (0x08, struct.pack("<I", _A32_NOP), "no CPSR save"),
        (0x14, struct.pack("<I", _A32_NOP), "no from-SP store"),
        (0x18, struct.pack("<I", _A32_NOP), "no to-SP load"),
        (0x80, struct.pack("<I", _A32_NOP), "no initializer store"),
    ],
)
def test_a32_mutations_are_refused(offset, raw, label):
    image, _data = _image("armv7-a")
    broken = _mutate(image, 0x1000 + offset, raw)
    result = select_context(broken)
    assert result.context is None, label
    assert result.reason == "unrecognizable-stack-frame-layout", label


@pytest.mark.parametrize(
    "offset,raw,label",
    [
        (0x20, struct.pack("<HH", _THUMB_NOP, _THUMB_NOP), "no PSP read"),
        (0x24, struct.pack("<HH", _THUMB_NOP, _THUMB_NOP), "no core save"),
        (0x28, struct.pack("<H", _THUMB_NOP), "no from-SP store"),
        (0x2A, struct.pack("<HH", _THUMB_NOP, _THUMB_NOP), "no PSP write"),
        (0x2E, struct.pack("<HH", _THUMB_NOP, _THUMB_NOP), "no core restore"),
        (0x80, struct.pack("<H", _THUMB_NOP), "no initializer store"),
        (0x00, struct.pack("<H", _THUMB_NOP), "no switch request store"),
    ],
)
def test_thumb_mutations_are_refused(offset, raw, label):
    image, _data = _image("armv7-m")
    broken = _mutate(image, 0x1000 + offset, raw)
    result = select_context(broken)
    assert result.context is None, label
    assert result.reason == "unrecognizable-stack-frame-layout", label


def test_undecodable_port_instruction_is_refused():
    image, _data = _image("armv7-m")
    broken = _mutate(image, 0x1002, struct.pack("<H", _THUMB_UDF))
    assert select_context(broken).reason == "unrecognizable-stack-frame-layout"


def test_initializer_without_a_return_is_refused():
    image, _data = _image("armv7-m")
    broken = _mutate(image, 0x1082, struct.pack("<H", _THUMB_NOP))
    assert select_context(broken).reason == "unrecognizable-stack-frame-layout"


def test_missing_port_symbols_are_refused():
    image, _data = _image("armv7-m")
    stripped = replace(image, anchors=(), symbols=())
    assert select_context(stripped).reason == "unrecognizable-stack-frame-layout"


_CACHE_ELFS = tuple(
    path
    for path in cached_elfs()
    if path.relative_to(FIXTURE_ROOT).parts[0] == "rtthread"
)


@pytest.mark.skipif(not _CACHE_ELFS, reason="no cached RT-Thread firmware ELFs")
@pytest.mark.parametrize(
    "path", _CACHE_ELFS, ids=[str(p.relative_to(FIXTURE_ROOT)) for p in _CACHE_ELFS]
)
def test_cached_firmware_selects_a_layout_only_with_lwp_evidence(path):
    image = ElfImage.from_path(path)
    assert image is not None
    result = select_context(image)
    if "cortex-a9/3.1" in str(path):
        # Pre-4.0 ports lack the 8-byte LWP header; the LWP slot table does
        # not apply, so refusing them is a layout decision, not a stale hash.
        assert result.context is None
        assert result.reason == "unrecognizable-stack-frame-layout"
    else:
        assert result.reason == "ok" and result.context is not None
