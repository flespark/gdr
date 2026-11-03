"""Pure RT-Thread saved-context decoding tests."""

from __future__ import annotations

import pytest

from core.unwind import unwind_chain
from rtthread.backtrace import (
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_INTEGER,
    RISCV64_INTEGER,
    decode_saved_context,
)
from tests.unit.core.test_unwind import _cfi_bytes, _cfi_metadata


class _Memory:
    def __init__(self) -> None:
        self.blocks: dict[int, bytearray] = {}

    def put(self, address: int, data: bytes) -> None:
        base = address & ~0xFF
        blob = self.blocks.setdefault(base, bytearray(0x200))
        offset = address - base
        blob[offset : offset + len(data)] = data

    def word(self, address: int, value: int, size: int = 4) -> None:
        self.put(address, value.to_bytes(size, "little"))

    def __call__(self, address: int, size: int) -> bytes | None:
        for base, blob in self.blocks.items():
            if base <= address and address + size <= base + len(blob):
                start = address - base
                return bytes(blob[start : start + size])
        return None


def _m3_context(memory: _Memory, top: int, *, fill: bool = False) -> None:
    for index in range(8):
        memory.word(top + index * 4, 0xDEADBEEF if fill else 0x11111111 + index)
    memory.word(top + 32, 0)
    memory.word(top + 36, 0)
    memory.word(top + 40, 0)
    memory.word(top + 44, 0)
    memory.word(top + 48, 0)
    memory.word(top + 52, 0x600)
    memory.word(top + 56, 0x1001)
    memory.word(top + 60, 0x01000000 if fill else 0x61000000)


def test_m3_context_has_the_verified_rtthread_layout():
    context = ARMV7M_INTEGER
    assert context is not None
    assert context.verified is False
    assert context.pc_offset == 56 and context.sp_offset == 64
    memory = _Memory()
    top = 0x2000
    _m3_context(memory, top)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        entry=0x1000,
        stack_low=0x1000,
        stack_high=0x3000,
    )
    assert reason == "ok" and seed is not None
    assert seed.pc == 0x1000
    assert seed.sp == top + 64
    assert seed.root_pc == 0x1000


def _armv8m_context(
    memory: _Memory, top: int, *, fill: bool = False, endian: str = "little"
) -> None:
    """Write the port's 20-word integer frame independently of the profile."""
    words = (
        0,
        0xFFFFFFBC,
        0,
        0,
        *(8 * [0xDEADBEEF] if fill else range(4, 12)),
        0x123,
        0,
        0,
        0,
        0,
        0x1331,
        0x1235,
        0x01000000,
    )
    for index, value in enumerate(words):
        memory.put(top + index * 4, value.to_bytes(4, endian))


@pytest.mark.parametrize("fill", [False, True])
@pytest.mark.parametrize("endian", ["little", "big"])
def test_armv8m_context_recovers_task_lr_separately_from_exception_return(fill, endian):
    context = ARMV8M_MAINLINE_INTEGER
    assert context.validation == "synthetic" and not context.verified
    assert context.lr_offset == 68
    memory = _Memory()
    top = 0x3000
    _armv8m_context(memory, top, fill=fill, endian=endian)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian=endian,
        entry=0x1234,
        stack_low=0x2000,
        stack_high=0x4000,
    )
    assert reason == "ok" and seed is not None
    assert seed.pc == 0x1234
    assert seed.sp == top + 80
    assert seed.regs[14] == 0x1331
    assert seed.regs[0] == 0x123
    assert seed.regs[13] == seed.sp and seed.regs[15] == 0x1235
    assert [seed.regs[register] for register in range(4, 12)] == (
        8 * [0xDEADBEEF] if fill else list(range(4, 12))
    )
    assert seed.source == ("unstarted" if fill else "saved")
    assert seed.root_pc == 0x1234


@pytest.mark.parametrize("lr_readable", [False, True])
def test_armv8m_cfi_uses_only_the_stacked_task_lr(lr_readable):
    memory = _Memory()
    top = 0x3000
    _armv8m_context(memory, top)

    def read(address: int, size: int) -> bytes | None:
        if not lr_readable and address == top + 68:
            return None
        return memory(address, size)

    seed, reason = decode_saved_context(
        ARMV8M_MAINLINE_INTEGER,
        top,
        read,
        endian="little",
        stack_low=top,
        stack_high=top + 0x200,
    )
    assert reason == "ok" and seed is not None
    assert seed.regs.get(14) == (0x1331 if lr_readable else None)
    metadata = _cfi_metadata(
        _cfi_bytes(start=0x1234, length=0x10, instructions=b"\x0e\x04\x08\x0e")
    )
    result = unwind_chain(
        seed,
        metadata,
        read=read,
        max_depth=2,
        stack_low=top,
        stack_high=top + 0x200,
        allow_frame_pointer=False,
    )
    if lr_readable:
        assert [frame.pc for frame in result.frames] == [0x1234, 0x1330]
        assert result.frames[1].method == "cfi"
        assert result.frames[1].address_in_block == 0x132F
        assert result.stop_reason == "max-depth" and result.truncated
    else:
        assert [frame.pc for frame in result.frames] == [0x1234]
        assert result.stop_reason == "cfi-return-address-missing"
        assert not result.truncated
    assert not result.complete


def test_rv64_context_is_closed_loop_verified_and_sp_is_above_it():
    context = RISCV64_INTEGER
    assert context is not None and context.verified is True
    memory = _Memory()
    top = 0x5000
    for register, offset in context.registers:
        memory.word(top + offset, 0x100 + register, size=8)
    memory.word(top, 0x80000123, size=8)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        entry=0x80000122,
        stack_low=0x4000,
        stack_high=0x6000,
    )
    assert reason == "ok" and seed is not None
    assert seed.pc == 0x80000123
    assert seed.sp == top + 32 * 8
    assert seed.regs[8] == 0x108


def test_rtthread_unstarted_requires_entry_and_fill():
    context = ARMV7M_INTEGER
    assert context is not None
    memory = _Memory()
    top = 0x2000
    _m3_context(memory, top, fill=True)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        entry=0x1000,
        stack_low=0x1000,
        stack_high=0x3000,
    )
    assert reason == "ok" and seed is not None
    assert seed.source == "unstarted" and seed.root_pc == 0x1000
