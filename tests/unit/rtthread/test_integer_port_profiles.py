"""Integer Armv8-M Mainline and RV32/RV64 compiled-port rejection contracts."""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from core.elf import ElfImage
from rtthread.stack_frame_profile import select_context
from tests.unit.core.oracle_support import (
    SectionSpec,
    SymbolSpec,
    attributes_blob,
    build_elf,
)
from tests.unit.rtthread.test_stack_frame_profile import _mutate


def _arm_image():
    text = bytearray(struct.pack("<H", 0xBF00) * 0x180)
    addresses = {}

    def put(offset, name, *halfwords):
        raw = struct.pack("<" + "H" * len(halfwords), *halfwords)
        text[offset : offset + len(raw)] = raw
        addresses[name] = 0x1000 + offset
        return offset + len(raw)

    put(0, "request", 0x6010, 0x4770)
    cursor = 0x20
    for name, words in (
        ("psp-read", (0xF3EF, 0x8109)),
        ("core-save", (0xE921, 0x0FF0)),
        ("tz-read", (0x4A00 | ((0x12F0 - ((0x1000 + cursor + 12) & ~3)) // 4),)),
        ("tz-value", (0x6812,)),
        ("exc-return", (0x4673,)),
        ("psplim-read", (0xF3EF, 0x840B)),
        ("control-read", (0xF3EF, 0x8514)),
        ("header-save", (0xE921, 0x003C)),
        ("from-address", (0x6800,)),
        ("from-store", (0x6001,)),
        ("header-load", (0xC93C,)),
        ("psplim-write", (0xF384, 0x880B)),
        ("control-write", (0xF385, 0x8814)),
        ("exc-restore", (0x469E,)),
    ):
        cursor = put(cursor, name, *words)
    cursor = put(
        cursor, "tz-write", 0x4E00 | ((0x12F0 - ((0x1000 + cursor + 4) & ~3)) // 4)
    )
    cursor = put(cursor, "tz-store", 0x6032)
    cursor = put(cursor, "core-load", 0xE8B1, 0x0FF0)
    cursor = put(cursor, "psp-write", 0xF381, 0x8809)
    cursor = put(cursor, "return", 0x4770)
    init = 0x100
    init = put(init, "allocation", 0x3B50)
    for offset in (0, 4, 8, 12, 48, 52, 56, 60, 64, 68, 72, 76):
        if offset == 4:
            init = put(init, "initial-exc-return", 0xF06F, 0x0202)
        init = put(init, f"init-{offset}", 0x6000 | (offset // 4 << 6) | (3 << 3) | 2)
    init = put(init, "init-return", 0x4770)
    text[0x2F0:0x2F4] = struct.pack("<I", 0x2000)
    attributes = attributes_blob(b"aeabi", ((6, 17), (7, "M"), (9, 2)))
    elf = ElfImage.from_bytes(
        build_elf(
            is_64=False,
            endian="little",
            machine=40,
            sections=(
                SectionSpec(".text", 0x1000, bytes(text), 1, 6),
                SectionSpec(".data", 0x2000, bytes(4), 1, 3),
                SectionSpec(".ARM.attributes", 0, attributes, 0x70000003, 0),
            ),
            symbols=(
                SymbolSpec("rt_hw_context_switch", 0x1000, 4, 2),
                SymbolSpec("PendSV_Handler", 0x1020, cursor - 0x20, 2),
                SymbolSpec("rt_hw_context_switch_to", 0x1000 + cursor, 0, 2),
                SymbolSpec("rt_hw_stack_init", 0x1100, init - 0x100, 2),
                SymbolSpec("rt_trustzone_current_context", 0x2000, 4, 3, info=1),
            ),
        )
    )
    assert elf is not None
    return elf, addresses


def test_armv8m_integer_layout_effects_match_the_profile():
    elf, _addresses = _arm_image()
    decision = select_context(elf)
    assert decision.reason == "ok" and decision.context is not None
    assert decision.isa == "armv8-m.mainline"


@pytest.mark.parametrize(
    "name",
    [
        "request",
        "psp-read",
        "core-save",
        "tz-read",
        "tz-value",
        "exc-return",
        "psplim-read",
        "control-read",
        "header-save",
        "from-store",
        "header-load",
        "psplim-write",
        "control-write",
        "exc-restore",
        "tz-write",
        "tz-store",
        "core-load",
        "psp-write",
        "allocation",
        "initial-exc-return",
        "init-68",
        "init-72",
        "init-76",
    ],
)
def test_armv8m_missing_layout_fact_is_refused(name):
    elf, addresses = _arm_image()
    assert select_context(elf).reason == "ok"
    broken = _mutate(elf, addresses[name], struct.pack("<H", 0xBF00))
    decision = select_context(broken)
    assert decision.context is None
    assert decision.reason == "unrecognizable-stack-frame-layout"


def test_armv8m_non_secure_initializer_is_not_the_verified_secure_profile():
    elf, addresses = _arm_image()
    assert select_context(elf).reason == "ok"
    broken = _mutate(
        elf, addresses["initial-exc-return"], struct.pack("<HH", 0xF06F, 0x0243)
    )
    assert select_context(broken).reason == "unrecognizable-stack-frame-layout"


def _riscv_image(word):
    def i(op, rd, base, offset, fn=0):
        return (offset & 0xFFF) << 20 | base << 15 | fn << 12 | rd << 7 | op

    def store(reg, base, offset):
        return (
            (offset >> 5) << 25
            | reg << 20
            | base << 15
            | (2 if word == 4 else 3) << 12
            | (offset & 31) << 7
            | 0x23
        )

    saves = [
        i(0x13, 2, 2, -32 * word),
        store(2, 10, 0),
        store(1, 2, 0),
        store(1, 2, word),
    ]
    saves += [store(reg, 2, reg * word) for reg in range(4, 32)]
    saves += [i(3, 2, 11, 0, 2 if word == 4 else 3), 0x0040006F]
    restore_address = 0x1000 + len(saves) * 4
    restores = [i(3, 10, 2, 0, 2 if word == 4 else 3), 0x34151073]
    restores += [
        i(3, reg, 2, reg * word, 2 if word == 4 else 3) for reg in (1, *range(4, 32))
    ]
    restores += [i(0x13, 2, 2, 32 * word), 0x30200073]
    blob = struct.pack("<" + "I" * (len(saves) + len(restores)), *saves, *restores)
    attributes = attributes_blob(b"riscv", ((4, 16), (5, f"rv{word * 8}imac_zicsr")))
    elf = ElfImage.from_bytes(
        build_elf(
            is_64=word == 8,
            endian="little",
            machine=243,
            sections=(
                SectionSpec(".text", 0x1000, blob, 1, 6),
                SectionSpec(".riscv.attributes", 0, attributes, 0x70000003, 0),
            ),
            symbols=(
                SymbolSpec("rt_hw_context_switch", 0x1000, len(saves) * 4, 2),
                SymbolSpec(
                    "rt_hw_context_switch_exit", restore_address, len(restores) * 4, 2
                ),
            ),
        )
    )
    assert elf is not None
    return elf


@pytest.mark.parametrize("word", [4, 8])
def test_riscv_integer_layout_uses_target_word_width(word):
    decision = select_context(_riscv_image(word))
    assert decision.reason == "ok" and decision.context is not None
    assert decision.context.pointer_size == word
    assert decision.context.sp_offset == 32 * word


@pytest.mark.parametrize("word", [4, 8])
@pytest.mark.parametrize(
    "offset", [0, 4, 8, 12, 16, 32 * 4, 33 * 4, 34 * 4, 35 * 4, 36 * 4, 65 * 4, 66 * 4]
)
def test_riscv_missing_frame_or_restore_effect_is_refused(word, offset):
    elf = _riscv_image(word)
    assert select_context(elf).reason == "ok"
    broken = _mutate(elf, 0x1000 + offset, struct.pack("<I", 0x00000013))
    assert select_context(broken).reason == "unrecognizable-stack-frame-layout"


def test_riscv_unsupported_pointer_width_is_refused():
    elf = replace(_riscv_image(4), pointer_size=2)
    assert select_context(elf).reason == "cpu-evidence-conflict"
