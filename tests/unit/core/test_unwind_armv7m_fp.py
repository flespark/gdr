"""Armv7-M FPU extended exception frames, non-secure only (synthetic)."""

from __future__ import annotations

import struct

from core.backtrace.unwind_arm import (
    EXTENDED_EXC_RETURNS,
    armv7m_exception_transition,
    decode_armv7m_exception_return,
)
from core.backtrace.unwind_segments import ExceptionTransitionContext, StackSegment
from tests.unit.core.test_unwind import _Memory

ED = 0xFFFFFFED
E9 = 0xFFFFFFE9
E1 = 0xFFFFFFE1
FPCCR = 0xE000EF34

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


def _basic_words(*, pc: int, lr: int, xpsr: int = 0x01000000) -> bytes:
    return struct.pack("<8I", 1, 2, 3, 4, 12, lr, pc, xpsr)


def _extended_frame(*, pc: int, lr: int, xpsr: int = 0x01000000) -> bytes:
    basic = _basic_words(pc=pc, lr=lr, xpsr=xpsr)
    fp = struct.pack("<16I", *range(16)) + struct.pack("<2I", 0x12345678, 0x0)
    return basic + fp


def _context(*, value: int, frame_sp: int, process_sp: int, memory: _Memory):
    return ExceptionTransitionContext(
        value=value,
        registers={13: frame_sp, 14: value},
        frame_sp=frame_sp,
        current_segment=IRQ,
        segments={TASK.ident: TASK, IRQ.ident: IRQ},
        process_sp=process_sp,
        read=memory,
        stkalign=True,
        remaining_exception_bytes=256,
        guard=None,
    )


def test_extended_returns_decode_to_extended_thread_frames():
    for value, stack in ((E1, "main"), (E9, "main"), (ED, "process")):
        decoded = decode_armv7m_exception_return(value)
        assert not isinstance(decoded, str)
        assert decoded.extended is True
        assert decoded.stack == stack


def test_extended_frame_restores_basic_registers_with_104_byte_span():
    memory = _Memory(
        {
            0x2100: _extended_frame(pc=0x1010, lr=0x1101),
            FPCCR: struct.pack("<I", 0xC0000000),
        }
    )
    result = armv7m_exception_transition(
        _context(value=ED, frame_sp=0x3010, process_sp=0x2100, memory=memory)
    )
    assert not isinstance(result, str) and result is not None
    assert result.pc == 0x1010
    assert result.sp == 0x2100 + 0x68
    assert result.bytes_read == 0x68
    assert result.registers[15] == 0x1010
    assert "extended" in result.evidence


def test_extended_frame_with_alignment_padding_restores_sp_plus_4():
    memory = _Memory(
        {
            0x2100: _extended_frame(pc=0x1010, lr=0x1101, xpsr=0x01000200),
            FPCCR: struct.pack("<I", 0xC0000000),
        }
    )
    result = armv7m_exception_transition(
        _context(value=ED, frame_sp=0x3010, process_sp=0x2100, memory=memory)
    )
    assert not isinstance(result, str) and result is not None
    assert result.sp == 0x2100 + 0x68 + 4


def test_extended_lazy_stacking_still_unwinds_basic_registers():
    memory = _Memory(
        {
            0x2100: _extended_frame(pc=0x1010, lr=0x1101),
            FPCCR: struct.pack("<I", 0xC0000001),
        }
    )
    result = armv7m_exception_transition(
        _context(value=ED, frame_sp=0x3010, process_sp=0x2100, memory=memory)
    )
    assert not isinstance(result, str) and result is not None
    assert result.pc == 0x1010
    assert "lspact=1" in result.evidence


def test_extended_frame_without_fpccr_fails_closed():
    memory = _Memory({0x2100: _extended_frame(pc=0x1010, lr=0x1101)})
    result = armv7m_exception_transition(
        _context(value=ED, frame_sp=0x3010, process_sp=0x2100, memory=memory)
    )
    assert result == "exception-fp-state-unavailable"


def test_extended_frame_requires_eight_byte_alignment():
    memory = _Memory(
        {
            0x2104: _extended_frame(pc=0x1010, lr=0x1101),
            FPCCR: struct.pack("<I", 0xC0000000),
        }
    )
    result = armv7m_exception_transition(
        _context(value=ED, frame_sp=0x3010, process_sp=0x2104, memory=memory)
    )
    assert result == "exception-frame-misaligned"


def test_extended_budget_counts_104_bytes():
    memory = _Memory(
        {
            0x2100: _extended_frame(pc=0x1010, lr=0x1101),
            FPCCR: struct.pack("<I", 0xC0000000),
        }
    )
    context = ExceptionTransitionContext(
        value=ED,
        registers={13: 0x2100, 14: ED},
        frame_sp=0x3010,
        current_segment=IRQ,
        segments={TASK.ident: TASK, IRQ.ident: IRQ},
        process_sp=0x2100,
        read=memory,
        stkalign=True,
        remaining_exception_bytes=0x67,
        guard=None,
    )
    assert armv7m_exception_transition(context) == "exception-read-budget-exceeded"


def test_extended_encodings_stay_in_candidate_set():
    for value in (E1, E9, ED):
        assert value in EXTENDED_EXC_RETURNS
