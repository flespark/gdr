"""Bounded A32 decoding exposes semantic operands and refuses unknown spans."""

from __future__ import annotations

from dataclasses import replace

from core.insn_arm import (
    decode_arm,
    decode_arm_prefix,
    decode_arm_stream,
    decode_arm_word,
    expect_arm,
)
from core.insn_thumb import decode_thumb16, decode_thumb32, decode_thumb_stream
from core.unwind_leaf import prove_leaf_return
from tests.unit.core.test_unwind_leaf import _image


def test_arm_decoder_normalizes_leaf_instructions_and_operands():
    assert decode_arm_word(0x1000, 0xE12FFF1E).mnemonic == "bx"
    mrs = decode_arm_word(0x1000, 0xE10F4000)
    assert expect_arm(mrs, "mrs", {"rd": 4, "psr": "cpsr", "cond": "al"})
    assert decode_arm_word(0x1000, 0xE320F000).mnemonic == "nop"
    cps = decode_arm_word(0x1000, 0xF1020012)
    assert expect_arm(cps, "cps", {"mode": 18, "flags": 0})


def test_arm_decoder_covers_context_save_restore_operand_effects():
    cases = (
        (0xE92D5FFF, "stmdb", {"rn": 13, "wback": True, "reglist": 0x5FFF}),
        (
            0xE8FDDFFF,
            "ldmia",
            {"rn": 13, "wback": True, "user_bank": True, "reglist": 0xDFFF},
        ),
        (0xE24DD008, "sub", {"rd": 13, "rn": 13, "immediate": 8}),
        (0xE580D000, "str", {"rd": 13, "rn": 0, "offset": 0}),
        (0xE591D000, "ldr", {"rd": 13, "rn": 1, "offset": 0}),
        (0xE31E0001, "tst", {"rn": 14, "immediate": 1, "set_flags": True}),
        (0x13844020, "orr", {"rd": 4, "rn": 4, "immediate": 32, "cond": "ne"}),
    )
    for word, mnemonic, operands in cases:
        assert expect_arm(decode_arm_word(0x1000, word), mnemonic, operands)


def test_arm_stream_bounds_alignment_and_unknown_encodings_are_closed():
    elf = _image(bytes.fromhex("1eff2fe100f020e3"), thumb=False, isa="armv7-a")
    decoded = decode_arm_stream(elf, 0x1000, 0x1008, 2)
    assert decoded is not None and [item.mnemonic for item in decoded] == ["bx", "nop"]
    assert decode_arm_prefix(elf, 0x1000, 0x1008, 1) == decoded[:1]
    assert decode_arm(elf, 0x1001, 0x1008) == "instruction-outside-function"
    assert decode_arm_stream(elf, 0x1000, 0x1008, 1) is None
    assert decode_arm_stream(elf, 0x1000, 0x1006, 2) is None
    unknown = decode_arm_word(0x1000, 0xE7F000F0)
    assert unknown.mnemonic == "other"


def test_arm_leaf_proof_uses_decoded_mnemonics_and_refuses_conditions():
    image = _image(
        bytes.fromhex("0d00a0e10000a0e11eff2fe1"), thumb=False, isa="armv7-a"
    )
    proof = prove_leaf_return(image, 0x1008)
    assert proof is not None and proof.return_register == 14
    conditional = _image(
        bytes.fromhex("0d00a0e10000001a1eff2fe1"), thumb=False, isa="armv7-a"
    )
    assert prove_leaf_return(conditional, 0x1008) is None


def test_arm_predicated_scalar_instruction_is_not_a_straight_line_leaf():
    image = _image(
        bytes.fromhex("0d00a0e10000001a1eff2fe1"), thumb=False, isa="armv7-a"
    )
    assert prove_leaf_return(image, 0x1008) is None


def test_thumb_decoder_exposes_branch_alu_shift_and_wide_immediate_semantics():
    cases = (
        (decode_thumb16(0x1000, 0xB11B), "cbz", {"rn": 3, "target": 0x100A}),
        (decode_thumb16(0x1000, 0x4298), "cmp", {"rn": 0, "rm": 3}),
        (decode_thumb16(0x1000, 0xD905), "b_cond", {"cond": "ls", "target": 0x100E}),
        (decode_thumb16(0x1000, 0x1AC3), "subs", {"rd": 3, "rn": 3, "rm": 0}),
        (decode_thumb16(0x1000, 0x441A), "add", {"rd": 2, "rm": 3}),
        (
            decode_thumb32(0x1000, 0xF1B2, 0x3FA5),
            "cmp",
            {"rn": 2, "immediate": 0xA5A5A5A5},
        ),
        (decode_thumb32(0x1000, 0xFAB4, 0xF484), "clz", {"rd": 4, "rm": 4}),
        (
            decode_thumb32(0x1000, 0xF1C4, 0x041F),
            "rsb",
            {"rn": 4, "rd": 4, "immediate": 31},
        ),
        (
            decode_thumb32(0x1000, 0xEB04, 0x0384),
            "add",
            {"rn": 4, "rd": 3, "rm": 4, "shift_amount": 2},
        ),
        (decode_thumb32(0x1000, 0xF241, 0x30FA), "movw", {"rd": 0, "imm": 0x13FA}),
        (
            decode_thumb32(0x1000, 0xF04E, 0x0E0D),
            "orr_imm",
            {"rd": 14, "rn": 14, "imm": 13},
        ),
        (
            decode_thumb16(0x1000, 0xC1F0),
            "stmia",
            {"rn": 1, "wback": True, "reglist": 0xF0},
        ),
        (
            decode_thumb16(0x1000, 0xC9F0),
            "ldmia",
            {"rn": 1, "wback": True, "reglist": 0xF0},
        ),
    )
    for instruction, mnemonic, operands in cases:
        assert instruction.mnemonic == mnemonic
        assert all(
            instruction.operands.get(key) == value for key, value in operands.items()
        )


def test_decoders_read_big_endian_arm_words_and_refuse_partial_instructions():
    image = _image(bytes.fromhex("e12fff1e"), thumb=False, isa="armv7-a")
    sections = tuple(
        replace(section, data=bytes.fromhex("e12fff1e") + section.data[4:])
        if section.name == ".text"
        else section
        for section in image.sections
    )
    big = replace(image, endian="big", sections=sections)
    assert decode_arm(big, 0x1000, 0x1004).mnemonic == "bx"
    assert decode_arm(big, 0x1000, 0x1003) == "instruction-outside-function"


def test_thumb_profile_decoder_can_reject_unknown_instruction_evidence():
    elf = _image(bytes.fromhex("00bf00de7047"))
    permissive, reason = decode_thumb_stream(elf, "leaf", 0x1000, 0x1006, 8)
    assert reason == "ok" and permissive is not None
    strict, reason = decode_thumb_stream(
        elf, "leaf", 0x1000, 0x1006, 8, reject_unknown=True
    )
    assert strict is None and reason == "profile-mismatch:leaf:unsupported-instruction"
