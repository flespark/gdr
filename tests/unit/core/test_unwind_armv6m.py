"""Armv6-M exception-return and frame-recovery acceptance contracts."""

import struct
from dataclasses import replace
from typing import Any

import pytest

from core.unwind import SeedFrame, unwind_chain
from core.unwind_arm import (
    armv6m_exception_number_kind,
    armv6m_exception_return_candidate,
    armv6m_exception_transition,
)
from core.unwind_arm import (
    decode_armv6m_exception_return as decode_exception_return,
)
from core.unwind_segments import ExceptionTransitionContext, StackSegment
from tests.unit.core.test_unwind import _cfi_bytes, _cfi_metadata, _Memory

TASK = StackSegment(
    "task:worker", "worker", 0x2000, 0x2200, "recorded", "process", "task"
)
IRQ = StackSegment("exception:25", "irq", 0x3000, 0x4000, "linker", "main", "exception")


def _frame(*, xpsr=0x01000000, pc=0x1010, lr=0x1101):
    return struct.pack("<8I", 1, 2, 3, 4, 12, lr, pc, xpsr)


def _context(**overrides):
    values: dict[str, Any] = dict(
        value=0xFFFFFFFD,
        registers={4: 0x44, 7: 0x77},
        frame_sp=0x3010,
        current_segment=IRQ,
        segments={TASK.ident: TASK, IRQ.ident: IRQ},
        process_sp=0x2100,
        read=_Memory({0x2100: _frame()}),
        stkalign=True,
        remaining_exception_bytes=256,
    )
    values.update(overrides)
    return ExceptionTransitionContext(**values)


@pytest.mark.parametrize(
    "value,mode,stack",
    [
        (0xFFFFFFF1, "handler", "main"),
        (0xFFFFFFF9, "thread", "main"),
        (0xFFFFFFFD, "thread", "process"),
    ],
)
def test_only_basic_returns_select_the_defined_mode_and_stack(value, mode, stack):
    result = decode_exception_return(value)
    assert not isinstance(result, str)
    assert (result.value, result.mode, result.stack) == (value, mode, stack)


@pytest.mark.parametrize(
    "value",
    [
        0,
        0xFFFFFFF0,
        0xFFFFFFF5,
        0xFFFFFFE1,
        0xFFFFFFE9,
        0xFFFFFFED,
        0xFFFFFFC1,
        0xFFFFFFCD,
        0xFFFFFFDD,
        0x1FFFFFFFD,
    ],
)
def test_v7_extended_and_v8_security_encodings_are_illegal(value):
    assert decode_exception_return(value) == "exception-return-illegal"


@pytest.mark.parametrize(
    "value,reason",
    [
        (0x1101, None),
        (0xFFFFFFE1, "exception-return-illegal"),
        (0xFFFFFFC1, "exception-return-illegal"),
        (0xFFFFFFF5, "exception-return-illegal"),
    ],
)
def test_nonframes_and_illegal_returns_do_not_read_memory(value, reason):
    def read(*_args):
        pytest.fail("frame read before exception-return validation")

    assert armv6m_exception_transition(_context(value=value, read=read)) == reason


@pytest.mark.parametrize(
    "value,candidate",
    [
        (0xFFFFFFF1, True),
        (0xFFFFFFE1, True),
        (0xFFFFFFF0, True),
        (0xEFFFFFF1, False),
        (0x1234, False),
    ],
)
def test_armv6m_candidate_uses_the_architectural_reserved_high_field(value, candidate):
    assert armv6m_exception_return_candidate(value) is candidate


@pytest.mark.parametrize(
    "number",
    [
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        24,
        25,
        47,
        48,
        63,
        64,
        511,
        512,
        -1,
    ],
)
def test_exception_numbers_follow_v6_not_v7_rules(number):
    expected = {
        0: "thread",
        2: "system",
        3: "fault",
        11: "scheduler",
        14: "scheduler",
        15: "system",
    }.get(number, "invalid")
    if 16 <= number <= 47:
        expected = "external"
    assert armv6m_exception_number_kind(number) == expected


def test_v6m_reserved_xpsr_exception_bits_are_not_discarded():
    read = _Memory({0x3010: _frame(xpsr=0x01000000 | 0x40)})
    result = armv6m_exception_transition(_context(value=0xFFFFFFF1, read=read))
    assert result == "exception-number-invalid"


@pytest.mark.parametrize("padding", [False, True])
def test_process_frame_recovers_every_hardware_register_and_exact_pc(padding):
    read = _Memory({0x2100: _frame(xpsr=0x01000000 | (padding << 9))})
    result = armv6m_exception_transition(_context(read=read))
    assert result is not None and not isinstance(result, str)
    expected_sp = 0x2120 + 4 * padding
    assert result.registers == {
        0: 1,
        1: 2,
        2: 3,
        3: 4,
        4: 0x44,
        7: 0x77,
        12: 12,
        13: expected_sp,
        14: 0x1101,
        15: 0x1010,
    }
    assert result.pc == 0x1010 and result.sp == expected_sp and result.exact_pc
    assert result.bytes_read == 32 and result.source == "armv6m-basic-exception-frame"
    assert result.segment == TASK


def test_thread_using_main_stack_requires_a_unique_nonexception_segment():
    main = replace(TASK, ident="main-thread", low=0x3000, high=0x4000, stack="main")
    context = _context(
        value=0xFFFFFFF9,
        read=_Memory({0x3010: _frame()}),
        segments={IRQ.ident: IRQ, main.ident: main},
    )
    result = armv6m_exception_transition(context)
    assert result is not None and not isinstance(result, str)
    assert result.segment == main and result.sp == 0x3030
    duplicate = replace(main, ident="other")
    assert (
        armv6m_exception_transition(
            replace(
                context,
                segments={IRQ.ident: IRQ, main.ident: main, duplicate.ident: duplicate},
            )
        )
        == "exception-context-unproven"
    )


@pytest.mark.parametrize("ipsr", [0, 1, 4, 5, 6, 7, 12, 13, 64, 511])
def test_nested_frame_rejects_invalid_v6_exception_numbers(ipsr):
    result = armv6m_exception_transition(
        _context(
            value=0xFFFFFFF1, read=_Memory({0x3010: _frame(xpsr=0x01000000 | ipsr)})
        )
    )
    assert result == (
        "exception-frame-ipsr-invalid" if ipsr == 0 else "exception-number-invalid"
    )


@pytest.mark.parametrize("ipsr", [2, 3, 11, 14, 15, 16, 24, 47])
def test_architecture_accepts_valid_numbers_without_claiming_port_support(ipsr):
    result = armv6m_exception_transition(
        _context(
            value=0xFFFFFFF1, read=_Memory({0x3010: _frame(xpsr=0x01000000 | ipsr)})
        )
    )
    assert result is not None and not isinstance(result, str)
    assert result.segment.ident == f"exception:{ipsr}"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"stkalign": False}, "exception-frame-alignment-invalid"),
        ({"process_sp": 0x2104}, "exception-frame-misaligned"),
        ({"process_sp": None}, "exception-process-sp-unavailable"),
        ({"process_sp": 0x2200}, "exception-context-unproven"),
        ({"frame_sp": 0x2800}, "exception-frame-outside-main-stack"),
        ({"current_segment": TASK}, "exception-context-unproven"),
        (
            {"current_segment": replace(IRQ, low=None)},
            "exception-frame-bounds-unavailable",
        ),
        ({"remaining_exception_bytes": 31}, "exception-read-budget-exceeded"),
        ({"read": lambda *_: None}, "exception-frame-unreadable"),
        ({"read": lambda *_: b"short"}, "exception-frame-unreadable"),
        ({"read": _Memory({0x2100: _frame(xpsr=0)})}, "exception-frame-not-thumb"),
        (
            {"read": _Memory({0x2100: _frame(xpsr=0x01000018)})},
            "exception-frame-ipsr-invalid",
        ),
        ({"read": _Memory({0x2100: _frame(pc=0x1011)})}, "exception-frame-pc-invalid"),
        ({"guard": lambda *_: "port-refused"}, "port-refused"),
    ],
)
def test_invalid_evidence_is_refused(changes, reason):
    assert armv6m_exception_transition(_context(**changes)) == reason


def test_padding_must_fit_in_the_task_stack():
    context = _context(
        process_sp=0x21E0, read=_Memory({0x21E0: _frame(xpsr=0x01000200)})
    )
    assert armv6m_exception_transition(context) == "exception-context-outside-bounds"


@pytest.mark.parametrize(
    "depth,byte_budget,reason",
    [
        (8, 256, "max-depth"),
        (1, 256, "exception-depth-exceeded"),
        (8, 32, "exception-read-budget-exceeded"),
    ],
)
def test_driver_crosses_nested_m0_frames_with_independent_budgets(
    depth, byte_budget, reason
):
    metadata = _cfi_metadata(_cfi_bytes(instructions=b""))
    read = _Memory(
        {0x3010: _frame(pc=0x1008, lr=0xFFFFFFFD, xpsr=0x01000018), 0x2100: _frame()}
    )
    seed = SeedFrame(0x1004, 0x3010, {13: 0x3010, 14: 0xFFFFFFF1}, process_sp=0x2100)
    result = unwind_chain(
        seed,
        metadata,
        read=read,
        max_depth=3,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv6m_exception_return_candidate,
        exception_hook=armv6m_exception_transition,
        max_exception_depth=depth,
        max_exception_bytes=byte_budget,
    )
    assert result.stop_reason == reason
    assert result.frames[1].pc == 0x1008 and result.frames[1].address_in_block == 0x1008
    if reason == "max-depth":
        assert [f.pc for f in result.frames] == [0x1004, 0x1008, 0x1010]
        assert len(result.transitions) == 2
    else:
        assert len(result.transitions) == 1
