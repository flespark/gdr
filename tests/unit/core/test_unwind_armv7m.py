"""Bounded Armv7-M exception-frame decoding and segment transitions."""

from __future__ import annotations

import struct
from dataclasses import replace
from typing import Any

import pytest

from core.unwind import SeedFrame, unwind_chain
from core.unwind_arm import (
    armv7m_exception_number_kind,
    armv7m_exception_return_candidate,
    armv7m_exception_transition,
    armv7m_max_external_exception,
    prepare_arm_handler_unwind,
)
from core.unwind_arm import (
    decode_armv7m_exception_return as decode_exception_return,
)
from core.unwind_segments import (
    ExceptionTransitionContext,
    ExceptionTransitionResult,
    StackSegment,
)
from tests.unit.core.test_unwind import _cfi_bytes, _cfi_metadata, _Memory

FD = 0xFFFFFFFD
F1 = 0xFFFFFFF1
F9 = 0xFFFFFFF9

TASK = StackSegment(
    ident="task:worker",
    owner="worker",
    low=0x2000,
    high=0x2200,
    source="task-recorded",
    stack="process",
    kind="task",
)
IRQ = StackSegment(
    ident="exception:25",
    owner="exception:25",
    low=0x3000,
    high=0x4000,
    source="linker:irq-stack",
    stack="main",
    kind="exception",
)


def _frame(
    *,
    pc: int,
    lr: int,
    xpsr: int = 0x01000000,
    r0: int = 0,
    r1: int = 0,
    r2: int = 0,
    r3: int = 0,
    r12: int = 0,
) -> bytes:
    return struct.pack("<8I", r0, r1, r2, r3, r12, lr, pc, xpsr)


def _context(
    *,
    value: int = FD,
    frame_sp: int = 0x3010,
    process_sp: int | None = 0x2100,
    **kwargs: Any,
):
    return ExceptionTransitionContext(
        value=value,
        registers=kwargs.pop("registers", {13: frame_sp, 14: value}),
        frame_sp=frame_sp,
        current_segment=kwargs.pop("current_segment", IRQ),
        segments=kwargs.pop("segments", {TASK.ident: TASK, IRQ.ident: IRQ}),
        process_sp=process_sp,
        read=kwargs.pop("read", _Memory({})),
        stkalign=kwargs.pop("stkalign", True),
        remaining_exception_bytes=kwargs.pop("remaining_exception_bytes", 256),
        guard=kwargs.pop("guard", None),
    )


@pytest.mark.parametrize("value", [F1, F9, FD, 0xFFFFFFE9, 0xFFFFFFC1])
def test_exception_return_range_is_recognised(value):
    assert armv7m_exception_return_candidate(value)
    assert not armv7m_exception_return_candidate(0xFFFFFFFE & ~0x100)
    assert not armv7m_exception_return_candidate(0x00FFFFFD)


@pytest.mark.parametrize(
    "number,kind",
    [
        (0, "thread"),
        (1, "invalid"),
        (2, "system"),
        (3, "fault"),
        (4, "fault"),
        (5, "fault"),
        (6, "fault"),
        (7, "invalid"),
        (8, "invalid"),
        (9, "invalid"),
        (10, "invalid"),
        (11, "scheduler"),
        (12, "system"),
        (13, "invalid"),
        (14, "scheduler"),
        (15, "system"),
        (16, "external"),
        (17, "external"),
        (24, "external"),
        (25, "external"),
        (64, "external"),
        (511, "external"),
        (512, "invalid"),
        (-1, "invalid"),
    ],
)
def test_exception_number_classification_follows_the_armv7m_table(number, kind):
    assert armv7m_exception_number_kind(number) == kind


@pytest.mark.parametrize(
    "value,candidate",
    [
        (0xFFFFFFF1, True),
        (0xFFFFFFE1, True),
        (0xFFFFFFC1, True),
        (0xEFFFFFF1, False),
        (0x1234, False),
    ],
)
def test_armv7m_candidate_uses_its_reserved_high_field(value, candidate):
    assert armv7m_exception_return_candidate(value) is candidate


@pytest.mark.parametrize(
    "ictr,limit",
    [
        (0, 47),
        (1, 79),
        (14, 495),
        (15, 511),
        (16, None),
        (0x100, None),
        (-1, None),
        (1 << 32, None),
    ],
)
def test_external_capacity_is_derived_from_ictr_with_safe_limits(ictr, limit):
    assert armv7m_max_external_exception(ictr) == limit


def test_segment_contains_requires_both_bounds():
    assert TASK.contains(0x2000, 0x200)
    assert not TASK.contains(0x1FFF, 1)
    assert not TASK.contains(0x2100, 0x200)
    unknown = StackSegment("unknown", "unknown", None, None, "unknown")
    assert not unknown.contains(0x2000)


@pytest.mark.parametrize(
    "value,reason",
    [
        (0xFFFFFFC1, "exception-return-illegal"),
        (0xFFFFFFD9, "exception-return-illegal"),
        (0xFFFFFFF5, "exception-return-illegal"),
        (0xFFFFFFF0, "exception-return-illegal"),
    ],
)
def test_unsupported_and_illegal_returns_are_named(value, reason):
    assert decode_exception_return(value) == reason


def test_armv7m_decoder_does_not_claim_armv8m_security_encodings():
    assert decode_exception_return(0xFFFFFFC1) == "exception-return-illegal"
    assert decode_exception_return(0xFFFFFFD9) == "exception-return-illegal"


def test_basic_frame_restores_exact_pc_and_registers():
    memory = _Memory(
        {0x2100: _frame(pc=0x1010, lr=0x1101, r0=1, r1=2, r2=3, r3=4, r12=12)}
    )
    result = armv7m_exception_transition(_context(read=memory))
    assert not isinstance(result, str) and result is not None
    assert result.pc == 0x1010
    assert result.sp == 0x2120
    assert result.exact_pc
    assert result.segment.ident == TASK.ident
    assert result.registers[0] == 1 and result.registers[3] == 4
    assert result.registers[12] == 12 and result.registers[14] == 0x1101
    assert result.registers[13] == 0x2120 and result.registers[15] == 0x1010


def test_stkalign_frame_restores_the_four_byte_padding():
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0x1101, xpsr=0x01000200)})
    result = armv7m_exception_transition(_context(read=memory))
    assert not isinstance(result, str) and result is not None
    assert result.sp == 0x2124
    assert "xpsr=0x01000200" in result.evidence


def test_handler_setup_refuses_unverified_baseline_model():
    seed = SeedFrame(pc=0x1008, sp=0x2000, regs={}, source="current")
    seed = replace(seed, exception_number=24)
    result = prepare_arm_handler_unwind(
        seed,
        "armv8-m.baseline",
        scheduler_running=lambda: False,
        irq_stack_bounds=lambda: (0x2000, 0x3000),
        capacity_reader=lambda: 32,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
    )
    assert isinstance(result, tuple) and result[0] == "unsupported-exception-model"


def test_handler_frame_creates_a_nested_exception_segment():
    memory = _Memory({0x3010: _frame(pc=0x1008, lr=FD, xpsr=0x01000018, r0=0xAA)})
    result = armv7m_exception_transition(
        _context(value=F1, frame_sp=0x3010, registers={13: 0x3010, 14: F1}, read=memory)
    )
    assert not isinstance(result, str) and result is not None
    assert result.segment.ident == "exception:24"
    assert result.segment.kind == "exception"
    assert result.pc == 0x1008 and result.sp == 0x3030


@pytest.mark.parametrize("ipsr", [1, 7, 8, 9, 10, 13])
def test_handler_frame_rejects_an_architecturally_invalid_ipsr(ipsr):
    memory = _Memory({0x3010: _frame(pc=0x1008, lr=FD, xpsr=0x01000000 | ipsr)})
    result = armv7m_exception_transition(
        _context(
            value=F1,
            frame_sp=0x3010,
            registers={13: 0x3010, 14: F1},
            read=memory,
        )
    )
    assert result == "exception-number-invalid"


@pytest.mark.parametrize("ipsr", [2, 12, 15, 16, 24, 25, 511])
def test_handler_frame_accepts_valid_system_and_external_numbers(ipsr):
    memory = _Memory({0x3010: _frame(pc=0x1008, lr=FD, xpsr=0x01000000 | ipsr)})
    result = armv7m_exception_transition(
        _context(
            value=F1,
            frame_sp=0x3010,
            registers={13: 0x3010, 14: F1},
            read=memory,
        )
    )
    assert not isinstance(result, str) and result is not None
    assert result.segment.ident == f"exception:{ipsr}"
    assert result.evidence.endswith(f"ipsr={ipsr}")


@pytest.mark.parametrize(
    "read,reason",
    [
        (_Memory({}), "exception-frame-unreadable"),
        (
            _Memory({0x2100: _frame(pc=0x1010, lr=0, xpsr=0x00000000)}),
            "exception-frame-not-thumb",
        ),
        (
            _Memory({0x2100: _frame(pc=0x1010, lr=0, xpsr=0x01000001)}),
            "exception-frame-ipsr-invalid",
        ),
        (
            _Memory({0x2100: _frame(pc=0x1011, lr=0)}),
            "exception-frame-pc-invalid",
        ),
    ],
)
def test_corrupted_frames_stop_for_their_exact_reason(read, reason):
    context = _context(read=read)
    result = armv7m_exception_transition(context)
    assert result == reason


def test_frame_outside_the_main_stack_is_rejected():
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    context = _context(read=memory, frame_sp=0x2800, registers={13: 0x2800, 14: FD})
    assert armv7m_exception_transition(context) == "exception-frame-outside-main-stack"


def test_alignment_flag_without_stkalign_is_rejected():
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0, xpsr=0x01000200)})
    assert (
        armv7m_exception_transition(_context(read=memory, stkalign=False))
        == "exception-frame-alignment-invalid"
    )


def test_stkalign_frame_base_must_be_eight_byte_aligned():
    memory = _Memory({0x2104: _frame(pc=0x1010, lr=0)})
    assert (
        armv7m_exception_transition(_context(read=memory, process_sp=0x2104))
        == "exception-frame-misaligned"
    )


def test_non_stkalign_frame_base_is_only_four_byte_aligned():
    memory = _Memory({0x2104: _frame(pc=0x1010, lr=0)})
    result = armv7m_exception_transition(
        _context(read=memory, process_sp=0x2104, stkalign=False)
    )
    assert not isinstance(result, str) and result is not None
    assert result.sp == 0x2124

    memory = _Memory({0x2102: _frame(pc=0x1010, lr=0)})
    assert (
        armv7m_exception_transition(
            _context(read=memory, process_sp=0x2102, stkalign=False)
        )
        == "exception-frame-misaligned"
    )


def test_missing_process_sp_and_budget_are_distinct():
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    assert (
        armv7m_exception_transition(_context(read=memory, process_sp=None))
        == "exception-process-sp-unavailable"
    )
    assert (
        armv7m_exception_transition(_context(read=memory, remaining_exception_bytes=31))
        == "exception-read-budget-exceeded"
    )


def test_current_segment_must_be_an_exception_main_stack():
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    assert (
        armv7m_exception_transition(_context(read=memory, current_segment=TASK))
        == "exception-context-unproven"
    )
    wrong_stack = StackSegment(
        "exception:25",
        "exception:25",
        0x3000,
        0x4000,
        "linker",
        "process",
        "exception",
    )
    assert (
        armv7m_exception_transition(_context(read=memory, current_segment=wrong_stack))
        == "exception-context-unproven"
    )


def test_process_frame_must_fit_the_selected_task_segment():
    memory = _Memory({0x5000: _frame(pc=0x1010, lr=0)})
    assert (
        armv7m_exception_transition(_context(read=memory, process_sp=0x5000))
        == "exception-context-unproven"
    )
    assert (
        armv7m_exception_transition(_context(read=memory, process_sp=0x21F0))
        == "exception-context-unproven"
    )


def test_process_return_does_not_need_a_main_frame_above_the_msp_entry():
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    result = armv7m_exception_transition(
        _context(
            read=memory,
            frame_sp=0x3FF0,
            registers={13: 0x3FF0, 14: FD},
        )
    )
    assert not isinstance(result, str) and result is not None
    assert result.segment.ident == TASK.ident


def test_thread_main_return_requires_an_ordinary_main_segment():
    memory = _Memory({0x3010: _frame(pc=0x1010, lr=0)})
    assert (
        armv7m_exception_transition(
            _context(
                value=F9,
                frame_sp=0x3010,
                registers={13: 0x3010, 14: F9},
                read=memory,
            )
        )
        == "exception-context-unproven"
    )


def test_non_candidate_values_are_not_decoded():
    assert armv7m_exception_transition(_context(value=0x1234)) is None


def test_frame_base_must_be_positive_and_aligned():
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    assert (
        armv7m_exception_transition(_context(read=memory, process_sp=0))
        == "exception-frame-invalid"
    )
    assert (
        armv7m_exception_transition(_context(read=memory, process_sp=0x2102))
        == "exception-frame-misaligned"
    )
    assert (
        armv7m_exception_transition(
            _context(read=memory, process_sp=0x2102, stkalign=False)
        )
        == "exception-frame-misaligned"
    )


def test_handler_mode_frame_cannot_claim_an_ipsr_zero_context():
    memory = _Memory({0x3010: _frame(pc=0x1010, lr=0, xpsr=0x01000000)})
    assert (
        armv7m_exception_transition(
            _context(
                value=F1,
                frame_sp=0x3010,
                registers={13: 0x3010, 14: F1},
                read=memory,
            )
        )
        == "exception-frame-ipsr-invalid"
    )


def test_handler_restored_sp_must_stay_inside_the_main_segment():
    memory = _Memory({0x3FE0: _frame(pc=0x1010, lr=0, xpsr=0x01000218)})
    assert (
        armv7m_exception_transition(
            _context(
                value=F1,
                frame_sp=0x3FE0,
                registers={13: 0x3FE0, 14: F1},
                read=memory,
            )
        )
        == "exception-context-outside-bounds"
    )


def _single_frame_chain(*, root_pc=0x1010):
    metadata = _cfi_metadata(_cfi_bytes(instructions=b""))
    memory = _Memory(
        {0x2100: _frame(pc=0x1010, lr=0x1101, r0=1, r1=2, r2=3, r3=4, r12=12)}
    )
    seed = SeedFrame(
        0x1004,
        0x3010,
        {13: 0x3010, 14: FD, 4: 0x44, 7: 0x77},
        process_sp=0x2100,
        root_pc=root_pc,
        root_reason="task-entry",
    )
    return metadata, memory, seed


def test_driver_crosses_one_proven_frame_and_keeps_exact_pc():
    metadata, memory, seed = _single_frame_chain()
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        max_depth=4,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
    )
    assert [frame.pc for frame in result.frames] == [0x1004, 0x1010]
    assert result.frames[1].method == "exception"
    assert result.frames[1].address_in_block == 0x1010
    assert result.frames[1].segment == TASK.ident
    assert result.complete and result.stop_reason == "task-entry"
    assert len(result.transitions) == 1
    transition = result.transitions[0]
    assert transition.from_segment == IRQ.ident
    assert transition.to_segment == TASK.ident
    assert transition.value == FD and transition.exact_pc
    assert {segment.ident for segment in result.segments} == {TASK.ident, IRQ.ident}


def _nested_chain():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b""))
    memory = _Memory(
        {
            0x3010: _frame(pc=0x1008, lr=FD, xpsr=0x01000018),
            0x2100: _frame(pc=0x1010, lr=0, xpsr=0x01000000),
        }
    )
    seed = SeedFrame(
        0x1004,
        0x3010,
        {13: 0x3010, 14: F1, 4: 0x44},
        process_sp=0x2100,
    )
    return metadata, memory, seed


def test_nested_handlers_produce_two_owned_transitions():
    metadata, memory, seed = _nested_chain()
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        max_depth=3,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
    )
    assert [frame.pc for frame in result.frames] == [0x1004, 0x1008, 0x1010]
    assert [frame.method for frame in result.frames] == [
        "seed",
        "exception",
        "exception",
    ]
    assert [transition.to_segment for transition in result.transitions] == [
        "exception:24",
        TASK.ident,
    ]
    assert result.stop_reason == "max-depth" and result.truncated


def test_exception_depth_budget_is_independent():
    metadata, memory, seed = _nested_chain()
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        max_depth=8,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
        max_exception_depth=1,
    )
    assert [frame.pc for frame in result.frames] == [0x1004, 0x1008]
    assert result.stop_reason == "exception-depth-exceeded"
    assert len(result.transitions) == 1


def test_exception_memory_budget_is_independent():
    metadata, memory, seed = _nested_chain()
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        max_depth=8,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
        max_exception_bytes=32,
    )
    assert [frame.pc for frame in result.frames] == [0x1004, 0x1008]
    assert result.stop_reason == "exception-read-budget-exceeded"


def test_missing_interrupt_bounds_stop_before_any_transition():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b""))
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    unbounded = StackSegment(
        "exception:25", "exception:25", None, None, "unknown", "main", "exception"
    )
    seed = SeedFrame(0x1004, 0x3010, {13: 0x3010, 14: FD}, process_sp=0x2100)
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        segments=(TASK, unbounded),
        current_segment=unbounded.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
    )
    assert len(result.frames) == 1
    assert result.stop_reason == "exception-frame-bounds-unavailable"


def test_scheduler_guard_refuses_a_nested_pendsv_context():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b""))
    memory = _Memory({0x3010: _frame(pc=0x1008, lr=FD, xpsr=0x0100000E)})
    seed = SeedFrame(0x1004, 0x3010, {13: 0x3010, 14: F1}, process_sp=0x2100)
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
        exception_guard=lambda number, _pc: (
            "scheduler-transition-unproven" if number in (11, 14) else None
        ),
    )
    assert len(result.frames) == 1
    assert result.stop_reason == "scheduler-transition-unproven"


def test_driver_reports_an_invalid_stacked_ipsr_before_any_transition():
    metadata, _memory, seed = _nested_chain()
    memory = _Memory({0x3010: _frame(pc=0x1008, lr=FD, xpsr=0x01000007)})
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
    )
    assert [frame.pc for frame in result.frames] == [0x1004]
    assert result.stop_reason == "exception-number-invalid"


def test_illegal_exception_return_stops_at_the_boundary():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b""))
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    seed = SeedFrame(0x1004, 0x3010, {13: 0x3010, 14: 0xFFFFFFF5}, process_sp=0x2100)
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
    )
    assert len(result.frames) == 1
    assert result.stop_reason == "exception-return-illegal"


def test_restored_context_cannot_resolve_a_missing_cfa_register():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x0c\x07\x00"))
    memory = _Memory({0x2100: _frame(pc=0x1010, lr=0)})
    seed = SeedFrame(
        0x13FC,
        0x3010,
        {13: 0x3010, 14: FD},
        process_sp=0x2100,
        at_exception_entry=True,
    )
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        max_depth=4,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
    )
    assert [frame.pc for frame in result.frames] == [0x13FC, 0x1010]
    assert result.stop_reason == "cfi-cfa-register-missing"


@pytest.mark.parametrize(
    "registers",
    [
        {13: 0x3010, 14: 0x1100},
        {13: 0x3010},
    ],
)
def test_proven_handler_entry_requires_an_exception_return(registers):
    metadata, _memory, _seed = _nested_chain()
    seed = SeedFrame(
        0x1000,
        0x3010,
        registers,
        process_sp=0x2100,
        at_exception_entry=True,
    )
    result = unwind_chain(
        seed,
        metadata,
        read=_Memory({}),
        max_depth=4,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
    )
    assert [frame.pc for frame in result.frames] == [0x1000]
    assert result.stop_reason == "exception-return-unproven"
    assert not result.complete


def test_proven_handler_entry_requires_the_exception_hook():
    metadata, _memory, _seed = _nested_chain()
    seed = SeedFrame(
        0x1000,
        0x3010,
        {13: 0x3010, 14: FD},
        process_sp=0x2100,
        at_exception_entry=True,
    )
    result = unwind_chain(
        seed,
        metadata,
        read=_Memory({}),
        max_depth=4,
        segments=(TASK, IRQ),
        current_segment=IRQ.ident,
        exception_detector=armv7m_exception_return_candidate,
    )
    assert [frame.pc for frame in result.frames] == [0x1000]
    assert result.stop_reason == "exception-return-unproven"


def test_duplicate_segment_identity_is_rejected_without_unwinding():
    metadata, memory, seed = _single_frame_chain()
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        segments=(TASK, TASK),
        current_segment=TASK.ident,
    )
    assert result.frames == () and result.stop_reason == "segment-duplicate"


def test_loop_identity_includes_the_stack_context():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b""))

    def fake_hook(context):
        current = int(context.current_segment.ident.split(":")[1])
        target = StackSegment(
            ident=f"exception:{current - 1}",
            owner=f"exception:{current - 1}",
            low=0x3000,
            high=0x4000,
            source="fake-exception",
            stack="main",
            kind="exception",
        )
        return ExceptionTransitionResult(
            segment=target,
            pc=0x1010,
            sp=0x3030,
            registers={**context.registers, 13: 0x3030, 14: F1},
            method="exception",
            source="fake-exception",
            value=context.value,
            bytes_read=0,
        )

    seed = SeedFrame(
        0x13FC,
        0x3010,
        {13: 0x3010, 14: F1},
        at_exception_entry=True,
    )
    result = unwind_chain(
        seed,
        metadata,
        read=_Memory({}),
        max_depth=5,
        segments=(IRQ,),
        current_segment=IRQ.ident,
        exception_detector=lambda _value: True,
        exception_hook=fake_hook,
    )
    assert len(result.frames) == 5
    assert len(result.transitions) == 4
    assert result.stop_reason == "max-depth" and result.truncated


def test_unknown_current_segment_is_rejected_without_unwinding():
    metadata, memory, seed = _single_frame_chain()
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        segments=(TASK, IRQ),
        current_segment="missing",
    )
    assert result.frames == () and result.stop_reason == "segment-unknown"
