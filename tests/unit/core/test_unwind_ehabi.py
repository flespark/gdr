"""EHABI virtual-register, stack-pointer and rejection regressions."""

from __future__ import annotations

import pytest

import core.unwind_ehabi as eh
from core.unwind_ehabi import EhabiTable, _ExidxEntry
from tests.unit.core.test_unwind import _Memory


def _execute(instructions, *, memory=None, regs=None, low=0x2000, high=0x3000):
    words = instructions.ljust(3, b"\xb0")
    assert len(words) == 3
    entry = _ExidxEntry(0x1000, None, False, 0x80000000 | int.from_bytes(words, "big"))
    table = EhabiTable((entry,), lambda _address, _size: None, 0x1100)
    return eh.unwind_ehabi(
        entry,
        table=table,
        sp=0x2000,
        regs={13: 0x2000, 14: 0x1235, **(regs or {})},
        read=memory or _Memory({0x2000: b"\x00" * 0x100}),
        stack_low=low,
        stack_high=high,
        endian="little",
    )


@pytest.mark.parametrize(
    "instructions,delta",
    [(b"\x00\xb0", 4), (b"\x02\x40\xb0", 8), (b"\xb2\x01\xb0", 0x208)],
)
def test_adjustments_update_the_returned_virtual_sp(instructions, delta):
    registers, sp, pc = _execute(instructions)
    assert sp == 0x2000 + delta
    assert registers[13] == sp
    assert pc == 0x1235


@pytest.mark.parametrize("opcode,count", [(0xA0, 1), (0xA7, 8), (0xA8, 2), (0xAF, 9)])
def test_compact_register_pops_preserve_order_and_advance_sp(opcode, count):
    values = [0x1100 + index * 4 for index in range(count)]
    memory = _Memory(
        {0x2000: b"".join(value.to_bytes(4, "little") for value in values)}
    )
    registers, sp, pc = _execute(bytes([opcode, 0xB0]), memory=memory)
    assert registers[4] == values[0]
    assert sp == registers[13] == 0x2000 + 4 * count
    assert pc == (values[-1] if opcode >= 0xA8 else 0x1235)


def test_pop_r13_takes_effect_after_other_selected_registers_are_loaded():
    memory = _Memory(
        {0x2000: (0x2080).to_bytes(4, "little") + (0x1123).to_bytes(4, "little")}
    )
    registers, sp, pc = _execute(b"\x8a\x00\xb0", memory=memory)
    assert registers[13] == sp == 0x2080
    assert registers[15] == pc == 0x1123


def test_low_register_mask_and_set_vsp():
    memory = _Memory(
        {0x2000: (0x55).to_bytes(4, "little") + (0x77).to_bytes(4, "little")}
    )
    registers, sp, _ = _execute(b"\xb1\x05\xb0", memory=memory)
    assert (registers[0], registers[2], sp) == (0x55, 0x77, 0x2008)
    assert _execute(b"\x94\xb0", regs={4: 0x2080})[1] == 0x2080


@pytest.mark.parametrize(
    "instructions,reason",
    [
        (b"\x80\x00", "ehabi-refuse"),
        (b"\x00\x00\x80", "ehabi-truncated"),
        (b"\x00\x00\xb1", "ehabi-truncated"),
        (b"\x00\x00\xb2", "ehabi-truncated-uleb"),
        (b"\xb1\x00", "ehabi-spare"),
        (b"\xb1\x10", "ehabi-spare"),
        (b"\x9d", "ehabi-set-vsp-unsupported"),
        (b"\x9f", "ehabi-set-vsp-unsupported"),
        (b"\x94", "ehabi-set-vsp-unsupported"),
        (b"\xb3", "ehabi-opcode-unsupported"),
        (b"\x40\xb0", "eh-no-progress"),
        (b"\x00\x00\x00", "ehabi-no-return-address"),
    ],
)
def test_malformed_or_unsupported_entries_are_explicit_failures(instructions, reason):
    assert _execute(instructions) == reason


@pytest.mark.parametrize("opcode", [b"\xa0", b"\xa8", b"\x80\x01", b"\xb1\x01"])
def test_pop_memory_errors_are_not_successful_frames(opcode):
    assert _execute(opcode, memory=_Memory({})) == "eh-memory-error"


def test_bounds_return_sentinel_and_opcode_budget(monkeypatch):
    assert _execute(b"\xb0", regs={14: 0}) == "ehabi-zero-return-address"
    assert _execute(b"\x00\xb0", high=0x2002) == "eh-stack-overflow"
    assert _execute(b"\xb0", low=0x2004) == "eh-stack-underflow"
    monkeypatch.setattr(eh, "MAX_EHABI_OPCODES", 1)
    assert _execute(b"\x00\xb0") == "ehabi-opcode-limit"


@pytest.mark.parametrize(
    "first,expected",
    [
        (0, "ehabi-generic-model-unsupported"),
        (0x83000000, "ehabi-unsupported-personality"),
    ],
)
def test_noncompact_and_unknown_personalities_fail_closed(first, expected):
    entry = _ExidxEntry(0x1000, None, False, first)
    table = EhabiTable((entry,), lambda _address, _size: None, 0x1100)
    assert (
        eh.unwind_ehabi(
            entry,
            table=table,
            sp=0x2000,
            regs={},
            read=lambda _a, _n: None,
            stack_low=None,
            stack_high=None,
            endian="little",
        )
        == expected
    )


def test_extab_extended_personality_reads_instruction_words():
    words = (0x810100B0).to_bytes(4, "little") + (0xB0B0B0B0).to_bytes(4, "little")
    memory = _Memory({0x1800: words})
    entry = _ExidxEntry(0x1000, 0x1800, False)
    table = EhabiTable((entry,), memory, 0x1100)
    result = eh.unwind_ehabi(
        entry,
        table=table,
        sp=0x2000,
        regs={14: 0x1235},
        read=memory,
        stack_low=0x2000,
        stack_high=0x2100,
        endian="little",
    )
    assert result[1:] == (0x2004, 0x1235)
    assert eh._eh_insn_words(0, None, "little") is None
    assert eh._eh_insn_words(0x81010000, None, "little") is None
    assert eh._eh_insn_words(0x81010000, b"", "little") is None
    assert eh._eh_insn_words(0x82010000, words[4:], "little").endswith(b"\xb0" * 4)


@pytest.mark.parametrize(
    "entry,reason",
    [
        (_ExidxEntry(0x1000, None, True), "ehabi-cantunwind"),
        (_ExidxEntry(0x1000, None, False), "ehabi-no-entry"),
        (_ExidxEntry(0x1000, 0x1800, False), "ehabi-missing-extab"),
    ],
)
def test_missing_and_cantunwind_entries(entry, reason):
    table = EhabiTable((entry,), lambda _a, _n: None, 0x1100)
    assert table and table.entry_for(0x1000) == entry
    assert table.entry_for(0x1100) is None
    assert (
        eh.unwind_ehabi(
            entry,
            table=table,
            sp=0x2000,
            regs={},
            read=lambda _a, _n: None,
            stack_low=None,
            stack_high=None,
            endian="little",
        )
        == reason
    )


@pytest.mark.parametrize(
    "address,size,low,high",
    [
        (0, 4, None, None),
        (0x2000, 0, None, None),
        (0x2000, 9, None, None),
        (0x2000, 4, 0x2004, None),
        (0x2000, 4, None, 0x2002),
    ],
)
def test_ehabi_reader_rejects_ranges_before_touching_memory(address, size, low, high):
    reads = []
    assert (
        eh._read_value(
            lambda a, n: reads.append((a, n)),
            address,
            size,
            "little",
            stack_low=low,
            stack_high=high,
        )
        is None
    )
    assert reads == []
