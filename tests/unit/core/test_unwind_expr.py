"""Bounded DWARF CFI expressions and explicit encoded-pointer contexts."""

from __future__ import annotations

import struct

import pytest

import core.unwind_cfi as cfi
import core.unwind_expr as expr
from core.unwind import _unwind_cfi
from core.unwind_cfi import CfaRule, CfiRow, EncodedPointerContext, RegisterRule
from tests.unit.core.test_unwind import _Memory
from tests.unit.core.test_unwind_cfi import _run


def _expr(
    blob,
    *,
    registers=None,
    initial_stack=(),
    read=None,
    address_size=4,
    endian="little",
    **kwargs,
):
    return expr.evaluate_cfi_expression(
        blob,
        registers=registers or {},
        initial_stack=initial_stack,
        read=read,
        address_size=address_size,
        endian=endian,
        **kwargs,
    )


def test_expression_rules_are_parsed_for_cfa_save_location_and_value():
    row = _run(b"\x0f\x02\x72\x20")
    assert row.cfa == CfaRule(expression=b"\x72\x20")
    row = _run(b"\x10\x0e\x02\x48\x1c")
    assert row.registers[14] == RegisterRule("expression", expression=b"\x48\x1c")
    row = _run(b"\x16\x80\x01\x04\x13\x72\x08\x06")
    assert row.registers[128] == RegisterRule(
        "val_expression", expression=b"\x13\x72\x08\x06"
    )


@pytest.mark.parametrize("followup", [b"\x0d\x0d", b"\x0e\x00", b"\x13\x7c"])
def test_expression_cfa_rejects_register_offset_followups(followup):
    assert _run(b"\x0f\x02\x72\x20" + followup) == "cfi-cfa-rule-invalid"


@pytest.mark.parametrize("blob", [b"\x0d\x0d", b"\x0e\x00", b"\x13\x7c"])
def test_cfa_register_followups_require_a_register_rule(blob):
    assert _run(blob, initial=CfiRow()) == "cfi-cfa-rule-invalid"


@pytest.mark.parametrize(
    "blob,reason",
    [
        (b"\x0f\x05\x00", "cfi-truncated-expression"),
        (b"\x0f\x81\x08", "cfi-expression-too-long"),
        (b"\x10\x0e\x81\x08", "cfi-expression-too-long"),
        (b"\x10\x0e\x05\x00", "cfi-truncated-expression"),
        (b"\x16\x0e\x81\x08", "cfi-expression-too-long"),
        (b"\x16\x0e\x05\x00", "cfi-truncated-expression"),
    ],
)
def test_expression_blocks_are_bounded(blob, reason):
    assert _run(blob) == reason


@pytest.mark.parametrize(
    "blob,expected",
    [
        (b"\x31", 1),
        (b"\x4f", 31),
        (b"\x09\xff", 0xFFFFFFFF),
        (b"\x0a\x34\x12", 0x1234),
        (b"\x0b\xff\xff", 0xFFFFFFFF),
        (b"\x0c\x78\x56\x34\x12", 0x12345678),
        (b"\x0d\xff\xff\xff\xff", 0xFFFFFFFF),
        (b"\x10\x80\x01", 0x80),
        (b"\x11\x7f", 0xFFFFFFFF),
        (b"\x32\x33\x22", 5),
        (b"\x38\x33\x1c", 5),
        (b"\x35\x23\x03", 8),
        (b"\x31\x32\x16\x1c", 1),
        (b"\x31\x32\x14\x22\x22", 4),
        (b"\x31\x32\x33\x17\x22\x22", 6),
        (b"\x31\x32\x33\x15\x02\x22\x22\x22", 7),
        (b"\x37\x12\x22", 14),
        (b"\x37\x38\x13", 7),
        (b"\x31\x32", 2),
        (b"\x37\x38\x24", 0x700),
        (b"\x38\x31\x25", 4),
    ],
)
def test_integer_literals_stack_and_bit_operations(blob, expected):
    assert _expr(blob) == expected


def test_rot_uses_the_dwarf_top_three_permutation():
    """``[1,2,3]`` becomes ``[3,1,2]``; encoding third/second/top yields 312."""
    blob = bytes.fromhex("31 32 33 17 15 02 08 64 1e 15 02 3a 1e 22 15 01 22")
    assert _expr(blob) == 312


@pytest.mark.parametrize("address_size", [4, 8])
def test_signed_comparisons_use_the_address_width(address_size):
    mask = (1 << (address_size * 8)) - 1
    assert _expr(b"\x09\xff\x08\x01\x2d", address_size=address_size) == 1
    assert _expr(b"\x09\xff\x08\x01\x2b", address_size=address_size) == 0
    assert _expr(b"\x09\xf8\x31\x25", address_size=address_size) == (
        0x7FFFFFFC if address_size == 4 else 0x7FFFFFFFFFFFFFFC
    )
    assert _expr(b"\x09\xf8\x31\x26", address_size=address_size) == mask - 3


@pytest.mark.parametrize(
    "blob,expected",
    [
        (b"\x36\x33\x1b", 2),
        (b"\x37\x33\x1d", 1),
        (b"\x09\xf9\x08\x03\x1b", 0xFFFFFFFE),
        (b"\x09\xf9\x08\x03\x1d", 0),
        (b"\x09\xfd\x19", 3),
        (b"\x31\x1f", 0xFFFFFFFF),
        (b"\x31\x20", 0xFFFFFFFE),
        (b"\x36\x33\x27", 5),
        (b"\x36\x33\x21", 7),
        (b"\x36\x33\x1a", 2),
        (b"\x36\x33\x1e", 18),
    ],
)
def test_signed_integer_arithmetic_and_logic(blob, expected):
    assert _expr(blob) == expected


@pytest.mark.parametrize("address_size", [4, 8])
def test_mod_is_unsigned_while_div_remains_signed(address_size):
    mask = (1 << (address_size * 8)) - 1
    assert _expr(bytes.fromhex("09fa341d"), address_size=address_size) == 2
    assert _expr(bytes.fromhex("3609fc1d"), address_size=address_size) == 6
    assert _expr(bytes.fromhex("09f9331d"), address_size=address_size) == 0
    assert _expr(bytes.fromhex("09fa09fc1d"), address_size=address_size) == mask - 5
    assert _expr(bytes.fromhex("0cffffffff321d"), address_size=address_size) == 1
    assert _expr(bytes.fromhex("09f908031b"), address_size=address_size) == mask - 1
    assert _expr(bytes.fromhex("3609fc1b"), address_size=address_size) == mask
    assert _expr(bytes.fromhex("3108001d"), address_size=address_size) == (
        "cfi-expression-divide-by-zero"
    )
    assert _expr(bytes.fromhex("3108001b"), address_size=address_size) == (
        "cfi-expression-divide-by-zero"
    )
    if address_size == 8:
        assert _expr(bytes.fromhex("0f" + "ff" * 8 + "331d"), address_size=8) == 0


@pytest.mark.parametrize(
    "blob,reason",
    [
        (b"\x31\x08\x00\x1b", "cfi-expression-divide-by-zero"),
        (b"\x31\x08\x00\x1d", "cfi-expression-divide-by-zero"),
        (b"\x31\x08\x20\x24", "cfi-expression-invalid-shift"),
        (b"\x0c\x00\x00\x00\x80\x19", "cfi-expression-overflow"),
    ],
)
def test_invalid_integer_operations_fail_explicitly(blob, reason):
    assert _expr(blob, address_size=4) == reason


def test_deref_and_deref_size_use_the_target_width_and_endian():
    memory = _Memory({0x2000: b"\x78\x56\x34\x12\xef\xcd\xab\x89"})
    assert _expr(b"\x0a\x00\x20\x06", read=memory) == 0x12345678
    assert _expr(b"\x0a\x00\x20\x06", read=memory, address_size=8) == (
        0x89ABCDEF12345678
    )
    assert _expr(b"\x0a\x00\x20\x94\x02", read=memory) == 0x5678
    assert _expr(b"\x0a\x20\x00\x06", read=memory, endian="big") == 0x78563412
    assert _expr(b"\x0a\x20\x00\x94\x02", read=memory, endian="big") == 0x7856


def test_deref_size_rejects_widths_above_the_address_size():
    assert _expr(b"\x0a\x00\x20\x94\x08", read=_Memory({})) == (
        "cfi-expression-unsupported"
    )
    assert _expr(b"\x0a\x00\x20\x94\x03", read=_Memory({})) == (
        "cfi-expression-unsupported"
    )


def test_deref_checks_the_32_bit_address_space_before_reading():
    calls = []

    def read(address, size):
        calls.append((address, size))
        return b"\x00" * size

    assert _expr(b"\x0c\xff\xff\xff\xff\x06", read=read) == (
        "cfi-expression-read-error"
    )
    assert calls == []
    assert _expr(b"\x0c\xfc\xff\xff\xff\x06", read=read) == 0
    assert calls == [(0xFFFFFFFC, 4)]
    calls.clear()
    assert _expr(b"\x0c\xff\xff\xff\xff\x94\x01", read=read) == 0
    assert calls == [(0xFFFFFFFF, 1)]
    calls.clear()
    assert _expr(b"\x0c\xff\xff\xff\xff\x94\x02", read=read) == (
        "cfi-expression-read-error"
    )
    assert calls == []
    assert _expr(b"\x0c\xfc\xff\xff\xff\x94\x04", read=read) == 0
    assert calls == [(0xFFFFFFFC, 4)]
    calls.clear()
    assert _expr(b"\x0c\xfd\xff\xff\xff\x94\x04", read=read) == (
        "cfi-expression-read-error"
    )
    assert calls == []


def test_deref_checks_the_64_bit_address_space_before_reading():
    calls = []

    def read(address, size):
        calls.append((address, size))
        return b"\x00" * size

    end_minus_eight = (2**64 - 8).to_bytes(8, "little")
    end_minus_four = (2**64 - 4).to_bytes(8, "little")
    end_minus_three = (2**64 - 3).to_bytes(8, "little")
    assert _expr(b"\x0e" + end_minus_eight + b"\x06", read=read, address_size=8) == 0
    assert calls == [(0xFFFFFFFFFFFFFFF8, 8)]
    calls.clear()
    assert _expr(b"\x0e" + end_minus_four + b"\x06", read=read, address_size=8) == (
        "cfi-expression-read-error"
    )
    assert calls == []
    assert _expr(b"\x0e" + end_minus_four + b"\x94\x04", read=read, address_size=8) == 0
    assert calls == [(0xFFFFFFFFFFFFFFFC, 4)]
    calls.clear()
    assert _expr(
        b"\x0e" + end_minus_three + b"\x94\x04", read=read, address_size=8
    ) == ("cfi-expression-read-error")
    assert calls == []


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"read": None}, "cfi-expression-read-error"),
        ({"read": lambda _a, _n: None}, "cfi-expression-read-error"),
        ({"read": lambda _a, _n: b"x"}, "cfi-expression-read-error"),
        ({"read": _Memory({}), "stack_low": 0x2001}, "cfi-expression-read-error"),
        ({"read": _Memory({}), "stack_high": 0x2002}, "cfi-expression-read-error"),
        (
            {"read": _Memory({0x2000: b"\x00" * 8}), "max_reads": 0},
            "cfi-expression-read-limit",
        ),
        (
            {"read": _Memory({0x2000: b"\x00" * 8}), "max_read_bytes": 3},
            "cfi-expression-read-limit",
        ),
    ],
)
def test_expression_reads_are_bounded(kwargs, reason):
    assert _expr(b"\x0a\x00\x20\x06", **kwargs) == reason


@pytest.mark.parametrize(
    "blob,expected",
    [
        (b"\x31\x2f\x01\x00\x32", 1),
        (b"\x31\x35\x28\x01\x00\x37", 1),
        (b"\x30\x28\x01\x00\x37", 7),
    ],
)
def test_limited_control_flow(blob, expected):
    assert _expr(blob) == expected


@pytest.mark.parametrize(
    "instruction",
    [
        b"\x03" + b"\x00" * 4,
        b"\x08\x00",
        b"\x0a\x00\x00",
        b"\x0c" + b"\x00" * 4,
        b"\x0e" + b"\x00" * 8,
        b"\x10\x81\x01",
        b"\x11\x81\x7f",
        b"\x15\x00",
        b"\x23\x01",
        b"\x28\x00\x00",
        b"\x2f\x00\x00",
        b"\x70\x00",
        b"\x92\x01\x00",
        b"\x94\x04",
    ],
)
def test_branch_targets_cannot_enter_a_fixed_uleb_or_sleb_operand(instruction):
    assert _expr(b"\x2f\x01\x00" + instruction) == "cfi-expression-branch-invalid"


@pytest.mark.parametrize("condition", [b"\x30", b"\x31"])
def test_conditional_branch_validates_its_target_for_both_conditions(condition):
    assert _expr(condition + b"\x28\x01\x00\x0c\x00\x00\x00\x00") == (
        "cfi-expression-branch-invalid"
    )


def test_conditional_branch_may_skip_a_forward_instruction():
    assert _expr(b"\x30\x28\x01\x00\x36") == 6
    assert _expr(b"\x31\x28\x00\x00\x36") == 6


@pytest.mark.parametrize(
    "blob,kwargs,reason",
    [
        (b"\x2f\x9c\xff", {}, "cfi-expression-branch-invalid"),
        (b"\x31\x12\x28\xfc\xff", {"max_steps": 8}, "cfi-expression-step-limit"),
        (b"\x28\x00\x00", {}, "cfi-expression-stack-underflow"),
    ],
)
def test_control_flow_failures_are_bounded(blob, kwargs, reason):
    assert _expr(blob, **kwargs) == reason


@pytest.mark.parametrize(
    "blob",
    [
        b"\x2f\x01\x00\x0a\x31\x00",
        b"\x2f\x01\x00\x0a\x31\x96",
        b"\x2f\x01\x00\x10\x81\x01\x31",
        b"\x2f\x01\x00\x11\x81\x7f\x31",
        b"\x2f\x01\x00\x0c\x31\x32\x33\x34",
        b"\x0a\x31\x96\x31\x2f\xfa\xff",
        b"\x31\x35\x28\x01\x00\x0c\x31\x32\x33\x34",
    ],
)
def test_branch_targets_must_be_instruction_boundaries(blob):
    assert _expr(blob) == "cfi-expression-branch-invalid"


def test_branch_can_target_an_instruction_start_or_the_end():
    assert _expr(b"\x31\x2f\x00\x00\x32") == 2
    assert _expr(b"\x31\x35\x28\x01\x00\x37") == 1


def test_predecode_does_not_relax_an_unknown_opcode_after_a_branch():
    assert _expr(b"\x31\x28\x01\x00\xe0") == "cfi-expression-unsupported"


@pytest.mark.parametrize(
    "opcode",
    [0x97, 0x98, 0x99, 0x9A, 0x9C, 0xA1, 0xA2, 0xA4, 0xA5, 0xA6, 0xA8, 0xA9],
)
def test_dwarf_forbidden_cfi_operators_are_rejected(opcode):
    assert _expr(bytes([opcode])) == "cfi-expression-forbidden"


@pytest.mark.parametrize(
    "opcode", [0x18, 0x91, 0x9B, 0x9D, 0x9E, 0xA0, 0xA3, 0xA7, 0xE0]
)
def test_unsupported_expression_operators_are_not_guessed(opcode):
    assert _expr(bytes([opcode])) == "cfi-expression-unsupported"


def test_nop_stack_value_addr_and_breg():
    assert _expr(b"\x35\x96\x9f") == 5
    assert _expr(b"\x35\x9f") == 5
    assert _expr(b"\x03\x78\x56\x34\x12") == 0x12345678
    assert _expr(b"\x92\x01\x7c", registers={1: 0x1234}) == 0x1230


@pytest.mark.parametrize(
    "blob", [b"\x51", b"\x90\x01", b"\x51\x31\x22", b"\x90\x01\x31\x22"]
)
def test_register_location_operators_are_not_integer_values(blob):
    assert _expr(blob, registers={1: 0x2010}) == "cfi-expression-register-location"


@pytest.mark.parametrize("blob", [b"\x31\x9f\x32", b"\x31\x28\x01\x00\x9f\x32"])
def test_stack_value_must_terminate_the_expression(blob):
    assert _expr(blob, purpose="value") == "cfi-expression-stack-value-not-terminal"


def test_stack_value_is_rejected_in_address_and_cfa_contexts():
    assert _expr(b"\x31\x9f", purpose="address") == (
        "cfi-expression-stack-value-location"
    )
    assert _expr(b"\x31\x9f", purpose="cfa") == ("cfi-expression-stack-value-location")
    assert _expr(b"\x31\x9f", purpose="value") == 1


@pytest.mark.parametrize("blob", [b"\x70\x00", b"\x92\x01\x7c"])
def test_missing_expression_registers_are_not_zero(blob):
    assert _expr(blob) == "cfi-expression-register-missing"


@pytest.mark.parametrize(
    "blob,registers,limit",
    [
        (b"\x92\x00\x00", {0: 0x1234}, 0),
        (b"\x92\x63\x00", {99: 0x1234}, 32),
    ],
)
def test_expression_registers_outside_the_architecture_are_rejected(
    blob, registers, limit
):
    assert _expr(blob, registers=registers, register_limit=limit) == (
        "cfi-expression-register-unsupported"
    )


def test_register_failure_preserves_the_prior_invalidation_reason():
    def failure(register):
        assert register == 7
        return "cfi-register-undefined"

    assert _expr(b"\x77\x00", register_failure=failure) == "cfi-register-undefined"


@pytest.mark.parametrize(
    "blob,reason",
    [
        (b"\x13", "cfi-expression-stack-underflow"),
        (b"\x14", "cfi-expression-stack-underflow"),
        (b"\x16", "cfi-expression-stack-underflow"),
        (b"\x17", "cfi-expression-stack-underflow"),
        (b"\x15\x00", "cfi-expression-stack-underflow"),
        (b"\x31\x13", "cfi-expression-invalid-result"),
    ],
)
def test_expression_stack_failures_are_explicit(blob, reason):
    assert _expr(blob) == reason


def test_expression_stack_and_bytecode_limits():
    assert _expr(b"\x31\x32", max_stack=1) == "cfi-expression-stack-limit"
    assert _expr(b"\x31" * 1025) == "cfi-expression-too-long"
    assert _expr(b"\x31", address_size=2) == "cfi-expression-address-size"
    assert _expr(b"\x31", max_steps=0) == "cfi-expression-step-limit"


def _driver_row(
    row,
    *,
    regs,
    sp,
    memory,
    ptr_size=4,
    endian="little",
    return_register=14,
    sp_register=13,
):
    return _unwind_cfi(
        row,
        regs=regs,
        sp=sp,
        read=memory,
        ptr_size=ptr_size,
        endian=endian,
        return_register=return_register,
        sp_register=sp_register,
        stack_low=0x2000,
        stack_high=0x2200,
    )


def test_driver_cfa_expression_computes_the_cfa_from_registers():
    row = CfiRow(
        CfaRule(expression=b"\x7d\x10"),
        {14: RegisterRule("offset", -4)},
    )
    memory = _Memory({0x200C: (0x1104).to_bytes(4, "little")})
    result = _driver_row(row, regs={13: 0x2000}, sp=0x2000, memory=memory)
    assert isinstance(result, tuple)
    regs, sp, pc = result
    assert sp == regs[13] == 0x2010 and pc == 0x1104


def test_driver_expression_rule_reads_the_computed_save_address():
    row = CfiRow(
        CfaRule(13, 8), {14: RegisterRule("expression", expression=b"\x34\x1c")}
    )
    memory = _Memory({0x2044: (0x1104).to_bytes(4, "little")})
    result = _driver_row(row, regs={13: 0x2040}, sp=0x2040, memory=memory)
    assert isinstance(result, tuple)
    assert result[2] == 0x1104 and result[1] == 0x2048


def test_driver_val_expression_is_a_direct_value_not_an_address():
    row = CfiRow(
        CfaRule(13, 8),
        {128: RegisterRule("val_expression", expression=b"\x13\x0c\x04\x11\x00\x00")},
        return_register=128,
    )

    def read(_address, _size):
        pytest.fail("val_expression must not dereference its result")

    result = _driver_row(
        row,
        regs={13: 0x2040},
        sp=0x2040,
        memory=read,
        return_register=128,
    )
    assert isinstance(result, tuple)
    regs, sp, pc = result
    assert pc == 0x1104 and sp == 0x2048 and 128 not in regs


def test_driver_expression_uses_the_current_cfa_as_its_initial_stack():
    row = CfiRow(
        CfaRule(13, 8), {14: RegisterRule("expression", expression=b"\x34\x1c")}
    )
    memory = _Memory({0x2044: (0x1104).to_bytes(4, "little")})
    result = _driver_row(row, regs={13: 0x2040}, sp=0x2040, memory=memory)
    assert isinstance(result, tuple) and result[2] == 0x1104


def test_driver_stack_value_is_rejected_for_address_and_cfa_results():
    address_row = CfiRow(
        CfaRule(13, 8), {14: RegisterRule("expression", expression=b"\x31\x9f")}
    )
    assert (
        _driver_row(address_row, regs={13: 0x2040}, sp=0x2040, memory=_Memory({}))
        == "cfi-expression-stack-value-location"
    )
    cfa_row = CfiRow(CfaRule(expression=b"\x31\x9f"), {14: RegisterRule("same")})
    assert (
        _driver_row(
            cfa_row,
            regs={13: 0x2040, 14: 0x1104},
            sp=0x2040,
            memory=_Memory({}),
        )
        == "cfi-expression-stack-value-location"
    )


def test_driver_stack_value_is_a_valid_val_expression_result():
    row = CfiRow(
        CfaRule(13, 8),
        {14: RegisterRule("val_expression", expression=b"\x31\x9f")},
    )
    result = _driver_row(row, regs={13: 0x2040}, sp=0x2040, memory=_Memory({}))
    assert isinstance(result, tuple) and result[2] == 1


def test_driver_cfa_expression_does_not_receive_a_previous_cfa():
    row = CfiRow(CfaRule(expression=b"\x34\x1c"), {14: RegisterRule("same")})
    result = _driver_row(
        row, regs={13: 0x2040, 14: 0x1104}, sp=0x2040, memory=_Memory({})
    )
    assert result == "cfi-expression-stack-underflow"


@pytest.mark.parametrize(
    "row,regs",
    [
        (
            CfiRow(CfaRule(expression=b"\x51\x31\x22"), {14: RegisterRule("same")}),
            {13: 0x2040, 14: 0x1104},
        ),
        (
            CfiRow(CfaRule(expression=b"\x90\x01\x31\x22"), {14: RegisterRule("same")}),
            {13: 0x2040, 14: 0x1104},
        ),
        (
            CfiRow(
                CfaRule(13, 8),
                {14: RegisterRule("expression", expression=b"\x51\x31\x22")},
            ),
            {13: 0x2040},
        ),
        (
            CfiRow(
                CfaRule(13, 8),
                {14: RegisterRule("val_expression", expression=b"\x90\x01\x31\x22")},
            ),
            {13: 0x2040},
        ),
    ],
)
def test_driver_rejects_register_locations_in_every_expression_rule(row, regs):
    assert (
        _driver_row(row, regs=regs, sp=0x2040, memory=_Memory({}))
        == "cfi-expression-register-location"
    )


def test_driver_cfa_expression_conflict_is_rejected():
    row = CfiRow(CfaRule(13, 8, expression=b"\x72\x10"))
    assert (
        _driver_row(row, regs={13: 0x2040}, sp=0x2040, memory=_Memory({}))
        == "cfi-cfa-rule-invalid"
    )


def test_driver_expression_rejects_an_out_of_table_register_reference():
    row = CfiRow(
        CfaRule(13, 8), {14: RegisterRule("expression", expression=b"\x92\x63\x00")}
    )
    result = _driver_row(
        row, regs={13: 0x2040, 99: 0x1104}, sp=0x2040, memory=_Memory({})
    )
    assert result == "cfi-expression-register-unsupported"


def test_driver_expression_cannot_reuse_an_invalidated_register():
    first = _driver_row(
        CfiRow(CfaRule(13, 8), {7: RegisterRule("undefined")}),
        regs={7: 0x2080, 13: 0x2040, 14: 0x1104},
        sp=0x2040,
        memory=_Memory({}),
    )
    assert isinstance(first, tuple)
    regs, sp, _pc = first
    second = _driver_row(
        CfiRow(CfaRule(expression=b"\x77\x00"), {14: RegisterRule("same")}),
        regs=regs,
        sp=sp,
        memory=_Memory({}),
    )
    assert second == "cfi-register-undefined"


def test_driver_expression_failure_does_not_fall_back_to_frame_pointer():
    row = CfiRow(CfaRule(13, 8), {14: RegisterRule("expression", expression=b"\x9c")})
    result = _driver_row(row, regs={13: 0x2040}, sp=0x2040, memory=_Memory({}))
    assert result == "cfi-expression-forbidden"


def test_driver_cfa_expression_failure_does_not_fall_back_to_frame_pointer():
    row = CfiRow(CfaRule(expression=b"\x9c"), {14: RegisterRule("same")})
    result = _driver_row(
        row,
        regs={7: 0x2080, 13: 0x2040, 14: 0x1104},
        sp=0x2040,
        memory=_Memory({}),
    )
    assert result == "cfi-expression-forbidden"


def test_driver_expression_failure_invalidates_a_non_return_register():
    row = CfiRow(
        CfaRule(13, 8),
        {7: RegisterRule("expression", expression=b"\x9c"), 14: RegisterRule("same")},
    )
    result = _driver_row(
        row, regs={7: 0x2080, 13: 0x2040, 14: 0x1104}, sp=0x2040, memory=_Memory({})
    )
    assert isinstance(result, tuple)
    regs, sp, _pc = result
    assert 7 not in regs and regs.unavailable[7] == "cfi-expression-forbidden"
    second = _driver_row(
        CfiRow(CfaRule(expression=b"\x77\x00"), {14: RegisterRule("same")}),
        regs=regs,
        sp=sp,
        memory=_Memory({}),
    )
    assert second == "cfi-expression-forbidden"


def test_driver_expression_without_a_cfa_rule_fails_explicitly():
    row = CfiRow(CfaRule(), {14: RegisterRule("same")})
    assert (
        _driver_row(row, regs={13: 0x2040}, sp=0x2040, memory=_Memory({}))
        == "cfi-no-cfa"
    )


def _eh_section(
    *,
    encoding,
    start,
    length=0x80,
    address_size=4,
    signed_start=True,
    instructions=b"\x0e\x08",
):
    """Build one CIE/FDE shape with an explicit FDE pointer encoding."""
    cie_body = (
        b"\x00" * 4
        + b"\x03"
        + b"zR\x00"
        + b"\x01"
        + b"\x7c"
        + b"\x0e"
        + b"\x01"
        + bytes([encoding])
        + b"\x0c\x0d\x00"
    )
    cie = struct.pack("<I", len(cie_body)) + cie_body
    fde_id_offset = len(cie) + 4
    body = struct.pack("<I", fde_id_offset)
    body += int(start).to_bytes(address_size, "little", signed=signed_start)
    body += int(length).to_bytes(address_size, "little")
    body += b"\x00" + instructions
    return cie + struct.pack("<I", len(body)) + body


def test_non_pc_relative_fde_pointer_requires_an_explicit_base():
    blob = _eh_section(encoding=0x2B, start=0x1000 - 0x8000)
    table = cfi.parse_cfi_section(
        blob,
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=True,
    )
    assert table.error == "cfi-fde-pointer-unavailable"
    assert table.row_for(0x1004) is None

    resolved = cfi.parse_cfi_section(
        blob,
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=True,
        pointer_context=EncodedPointerContext(text_base=0x8000),
    )
    assert resolved.error is None and resolved.row_for(0x1004) is not None


@pytest.mark.parametrize(
    "application,field",
    [(0x20, "text_base"), (0x30, "data_base"), (0x40, "function_base")],
)
def test_each_explicit_pointer_base_resolves_only_its_own_encoding(application, field):
    blob = _eh_section(encoding=application | 0x0B, start=0x1000 - 0x8000)
    context = EncodedPointerContext(**{field: 0x8000})
    table = cfi.parse_cfi_section(
        blob,
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=True,
        pointer_context=context,
    )
    assert table.error is None and table.row_for(0x1004) is not None


def test_indirect_pointer_needs_an_explicit_reader():
    data = b"\x10\x00\x00\x00"
    assert (
        cfi._decode_encoded_pointer(
            data,
            0,
            len(data),
            encoding=0x83,
            address_size=4,
            endian="little",
            section_address=0,
        )
        is None
    )
    context = EncodedPointerContext(
        read=lambda address, size: (
            (0x2000).to_bytes(size, "little") if address == 0x10 else None
        )
    )
    assert cfi._decode_encoded_pointer(
        data,
        0,
        len(data),
        encoding=0x83,
        address_size=4,
        endian="little",
        section_address=0,
        context=context,
    ) == (0x2000, 4)
    short = EncodedPointerContext(read=lambda _address, _size: b"x")
    assert (
        cfi._decode_encoded_pointer(
            data,
            0,
            len(data),
            encoding=0x83,
            address_size=4,
            endian="little",
            section_address=0,
            context=short,
        )
        is None
    )
