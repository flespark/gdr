"""Bounded RISC-V saved-frame code evidence tests."""

from __future__ import annotations

import pytest

from core.elf import CodeSymbol, ElfImage
from core.stack_frame_parser import (
    EvidenceError,
    RiscvCodeParser,
    riscv_all_store_offsets,
    riscv_code_decoder,
    riscv_frame_accesses,
    riscv_next_pc,
    riscv_slot_map,
    riscv_stack_frame_size,
    riscv_target_loads,
)
from tests.unit.core.test_unwind import _elf


def _i(op: int, rd: int, fn: int, rs: int, imm: int) -> int:
    return (imm & 0xFFF) << 20 | rs << 15 | fn << 12 | rd << 7 | op


def _s(op: int, rs: int, rt: int, imm: int, fn: int) -> int:
    return (
        ((imm >> 5) & 0x7F) << 25
        | rt << 20
        | rs << 15
        | fn << 12
        | (imm & 31) << 7
        | op
    )


def _image(text: bytes) -> ElfImage:
    blob = _elf(
        is_64=True,
        endian="little",
        machine=243,
        sections=[(".text", 0x1000, text, 1, 4)],
    )
    parsed = ElfImage.from_bytes(blob)
    assert parsed is not None
    return ElfImage(
        parsed.pointer_size,
        parsed.endian,
        parsed.machine,
        parsed.sections,
        parsed.symbols,
        parsed.flags,
        (("entry", 0x1000),),
    )


@pytest.mark.parametrize(
    "word,expected",
    [
        (_i(0x13, 5, 0, 6, -8), ("addi", 5, 6, -8)),
        (_i(0x03, 5, 3, 6, 24), ("ld", 5, 6, 24)),
        (_s(0x23, 2, 5, 40, 3), ("sd", 5, 2, 40)),
        (_i(0x73, 5, 2, 0, 0x300), ("csrr", 5, 0x300)),
        (0x30200073, ("mret",)),
    ],
)
def test_normalizes_supported_integer_instructions(word, expected):
    assert riscv_code_decoder(word, 4, 0x1000, 64) == expected


def test_compressed_stack_load_store_and_branch_targets():
    c_ldsp = 0x6002 | (5 << 7) | (3 << 5)
    assert riscv_code_decoder(c_ldsp, 2, 0x1000, 64) == ("ld", 5, 2, 24)
    c_sdsp = 0xE002 | (5 << 2) | (3 << 10)
    assert riscv_code_decoder(c_sdsp, 2, 0x1000, 64) == ("sd", 5, 2, 24)
    assert riscv_code_decoder(0x8082, 2, 0x1000, 64) == ("ret",)


def test_unknown_or_non_integer_extensions_are_rejected():
    with pytest.raises(EvidenceError, match="unsupported"):
        riscv_code_decoder(_i(0x07, 5, 3, 2, 0), 4, 0x1000, 64)
    with pytest.raises(EvidenceError, match="compressed"):
        riscv_code_decoder(0x2002, 2, 0x1000, 32)


def test_decode_rejects_unsupported_float_loads_and_malformed_encodings():
    with pytest.raises(EvidenceError, match="unsupported"):
        riscv_code_decoder(_i(0x07, 5, 3, 2, 0), 4, 0x1000, 64)
    with pytest.raises(EvidenceError, match="unsupported"):
        riscv_code_decoder(0xFFFFFFFF, 4, 0x1000, 64)


def test_elf_code_matching_requires_unique_anchors_and_bounded_executable_reads():
    elf = _image(_i(0x13, 5, 0, 0, 1).to_bytes(4, "little"))
    code = RiscvCodeParser(elf, "entry")
    code.expect("addi", 5, 0, 1)
    with pytest.raises(EvidenceError, match="instruction unavailable"):
        code.take()
    ambiguous = ElfImage(
        elf.pointer_size,
        elf.endian,
        elf.machine,
        elf.sections,
        elf.symbols,
        elf.flags,
        (("entry", 0x1000), ("entry", 0x1002)),
    )
    with pytest.raises(EvidenceError, match="anchor"):
        RiscvCodeParser(ambiguous, "entry")


def test_instruction_matcher_stops_at_the_next_elf_symbol_anchor():
    first = _i(0x13, 5, 0, 0, 1).to_bytes(4, "little")
    second = _i(0x13, 6, 0, 0, 2).to_bytes(4, "little")
    image = _image(first + second)
    bounded = ElfImage(
        image.pointer_size,
        image.endian,
        image.machine,
        image.sections,
        image.symbols,
        image.flags,
        (("entry", 0x1000), ("next", 0x1004)),
    )
    code = RiscvCodeParser(bounded, "entry")
    assert code.end == 0x1004
    code.expect("addi", 5, 0, 1)
    with pytest.raises(EvidenceError, match="instruction unavailable"):
        code.take()


@pytest.mark.parametrize(
    "word,expected",
    [
        (_i(0x03, 5, 2, 6, 24), ("lw", 5, 6, 24)),
        (_i(0x03, 5, 3, 6, 24), ("ld", 5, 6, 24)),
        (_s(0x23, 2, 5, 40, 2), ("sw", 5, 2, 40)),
        (_s(0x23, 2, 5, 40, 3), ("sd", 5, 2, 40)),
        (_i(0x13, 5, 7, 6, -1), ("andi", 5, 6, -1)),
        (_i(0x13, 5, 4, 6, 7), ("xori", 5, 6, 7)),
        (_i(0x13, 5, 1, 6, 3), ("slli", 5, 6, 3)),
        (_i(0x13, 5, 5, 6, 3), ("srli", 5, 6, 3)),
        (_i(0x1B, 5, 0, 6, 7), ("addiw", 5, 6, 7)),
        (_i(0x33, 5, 0, 6, 7), ("add", 5, 6, 7)),
        (_i(0x33, 5, 4, 6, 7), ("xor", 5, 6, 7)),
        (_i(0x33, 5, 6, 6, 7), ("or", 5, 6, 7)),
        (_i(0x33, 5, 7, 6, 7), ("and", 5, 6, 7)),
        (_i(0x17, 5, 0, 0, 0x10), ("auipc", 5, 0x1000000)),
        (_i(0x37, 5, 0, 0, 0x10), ("lui", 5, 0x1000000)),
        (_i(0x73, 5, 2, 0, 0x300), ("csrr", 5, 0x300)),
        (_i(0x73, 0, 1, 5, 0x300), ("csrw", 0x300, 5)),
        (_i(0x73, 0, 2, 5, 0x300), ("csrs", 0x300, 5)),
        (_i(0x67, 5, 0, 6, 8), ("jalr", 5, 6, 8)),
    ],
)
def test_normalizes_integer_arithmetic_loads_and_csrs(word, expected):
    assert riscv_code_decoder(word, 4, 0x1000, 64) == expected


@pytest.mark.parametrize(
    "word,expected",
    [
        (0x00000063, ("beq", 0, 0, 0x1000)),
        (0x00001063, ("bne", 0, 0, 0x1000)),
        (0x00004063, ("blt", 0, 0, 0x1000)),
        (0x00005063, ("bge", 0, 0, 0x1000)),
        (0x00006063, ("bltu", 0, 0, 0x1000)),
        (0x00007063, ("bgeu", 0, 0, 0x1000)),
        (_i(0x6F, 1, 0, 0, 0), ("jal", 1, 0x1000)),
        (_i(0x67, 1, 0, 0, 0), ("jalr", 1, 0, 0)),
    ],
)
def test_normalizes_branches_jumps_and_returns(word, expected):
    assert riscv_code_decoder(word, 4, 0x1000, 64) == expected


@pytest.mark.parametrize(
    "word,expected",
    [
        (0x0001, ("nop",)),
        (0x050D, ("addi", 10, 10, 3)),
        (0x4515, ("addi", 10, 0, 5)),
        (0x2505, ("addiw", 10, 10, 1)),
        (0x6141, ("addi", 2, 2, 16)),
        (0x8105, ("srli", 10, 10, 1)),
        (0x8505, ("srai", 10, 10, 1)),
        (0x8905, ("andi", 10, 10, 1)),
        (0x8D0D, ("sub", 10, 10, 11)),
        (0x8D2D, ("xor", 10, 10, 11)),
        (0x8D4D, ("or", 10, 10, 11)),
        (0x8D6D, ("and", 10, 10, 11)),
        (0x8082, ("ret",)),
        (0x8282, ("jalr", 0, 5, 0)),
        (0x9282, ("jalr", 1, 5, 0)),
        (0x8532, ("add", 10, 0, 12)),
        (0x9532, ("add", 10, 10, 12)),
        (0x0506, ("slli", 10, 10, 1)),
        (0x62A1, ("lui", 5, 0x8000)),
        (0x4108, ("lw", 10, 10, 0)),
        (0x6108, ("ld", 10, 10, 0)),
        (0xC02A, ("sw", 10, 2, 0)),
        (0xE02A, ("sd", 10, 2, 0)),
        (0x4502, ("lw", 10, 2, 0)),
        (0x6502, ("ld", 10, 2, 0)),
        (0xC02E, ("sw", 11, 2, 0)),
        (0xE02E, ("sd", 11, 2, 0)),
    ],
)
def test_normalizes_compressed_integer_instructions(word, expected):
    assert riscv_code_decoder(word, 2, 0x1000, 64) == expected


def test_compressed_wide_loads_are_rejected_on_rv32():
    with pytest.raises(EvidenceError, match="compressed"):
        riscv_code_decoder(0x6108, 2, 0x1000, 32)
    with pytest.raises(EvidenceError, match="compressed"):
        riscv_code_decoder(0xE02A, 2, 0x1000, 32)


def test_unsupported_instruction_size_is_rejected():
    with pytest.raises(EvidenceError, match="size"):
        riscv_code_decoder(0, 3, 0x1000, 64)


def test_code_decodes_bounded_instructions_and_stops_at_a_terminal_opcode():
    text = (
        _i(0x13, 5, 0, 0, 1).to_bytes(4, "little")
        + b"\x67\x80\x00\x00"
        + _i(0x13, 6, 0, 0, 2).to_bytes(4, "little")
    )
    code = RiscvCodeParser(_image(text), "entry")
    decoded = code.instructions(terminal=("ret",))
    assert list(decoded.values()) == [("addi", 5, 0, 1), ("ret",)]
    assert code.load == "ld" and code.store == "sd"


def test_code_rejects_odd_anchors_and_invalid_ranges():
    text = b"\x13\x05\x00\x00" * 2
    image = _image(text)
    odd = ElfImage(
        image.pointer_size,
        image.endian,
        image.machine,
        image.sections,
        image.symbols,
        image.flags,
        (("entry", 0x1001),),
    )
    with pytest.raises(EvidenceError, match="anchor"):
        RiscvCodeParser(odd, "entry")
    outside = ElfImage(
        image.pointer_size,
        image.endian,
        image.machine,
        image.sections,
        (CodeSymbol("entry", 0x1000, 0x100),),
        image.flags,
        (("entry", 0x1000),),
    )
    with pytest.raises(EvidenceError, match="range"):
        RiscvCodeParser(outside, "entry")


def test_code_load_symbol_resolves_an_adjacent_pc_relative_pair():
    upper = _i(0x17, 10, 0, 0, 0)
    lower = _i(0x03, 10, 3, 10, 8)
    text = upper.to_bytes(4, "little") + lower.to_bytes(4, "little") + b"\x00" * 8
    image = _image(text)
    anchored = ElfImage(
        image.pointer_size,
        image.endian,
        image.machine,
        image.sections,
        image.symbols,
        image.flags,
        (("entry", 0x1000), ("value", 0x1008)),
    )
    code = RiscvCodeParser(anchored, "entry")
    code.load_symbol(10, "value")
    assert code.next_address(0x1000) == 0x1004


def test_helper_maps_project_frame_accesses_and_loads():
    code = {
        0x1000: ("addi", 2, 2, -32),
        0x1004: ("sd", 1, 2, 8),
        0x1008: ("ld", 5, 2, 16),
        0x100C: ("auipc", 10, 0xFF4),
        0x1010: ("ld", 10, 10, 8),
    }
    assert riscv_next_pc(code, 0x1000) == 0x1004
    assert riscv_next_pc(code, 0x1010) is None
    stores = riscv_frame_accesses(code, "store", 2)
    assert stores == {8: {1}}
    assert riscv_all_store_offsets(code, 2) == {8}
    assert riscv_slot_map(stores) == {1: {8}}
    assert riscv_slot_map({}) == {}
    assert riscv_frame_accesses(code, "load", 2) == {16: {5}}
    assert riscv_stack_frame_size(code) == 32
    assert (
        riscv_stack_frame_size(
            {0x1000: ("addi", 2, 2, -8), 0x1004: ("addi", 2, 2, -16)}
        )
        is None
    )
    image = _image(b"\x00" * 32)
    anchored = ElfImage(
        image.pointer_size,
        image.endian,
        image.machine,
        image.sections,
        image.symbols,
        image.flags,
        (("target", 0x2008),),
    )
    assert riscv_target_loads(anchored, code) == {10: "target"}
