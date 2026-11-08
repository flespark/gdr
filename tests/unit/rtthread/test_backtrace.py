"""Pure RT-Thread saved-context decoding tests."""

from __future__ import annotations

import pytest

from core.unwind import unwind_chain
from core.unwind_meta import Endian
from rtthread.backtrace import (
    ARMV7A_INTEGER,
    ARMV7A_LWP_INTEGER,
    ARMV7EM_FPU,
    ARMV7EM_INTEGER,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_FPU,
    ARMV8M_MAINLINE_INTEGER,
    RISCV32_INTEGER,
    RISCV64_INTEGER,
    decode_armv7em_saved_context,
    decode_armv8m_mainline_saved_context,
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
    assert context.verified is True
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
    memory: _Memory, top: int, *, fill: bool = False, endian: Endian = "little"
) -> None:
    """Write the port's 20-word integer frame independently of the profile."""
    words = (
        0,
        0xFFFFFFFD,
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
    assert context.validation == "live" and context.verified
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


@pytest.mark.parametrize(
    "offset,value",
    [(0, 1), (4, 0xFFFFFFBC), (4, 0xFFFFFFED), (8, 0x2000), (12, 1)],
)
def test_armv8m_extended_or_cross_security_headers_are_refused_before_frame_reads(
    offset, value
):
    memory = _Memory()
    top = 0x3000
    _armv8m_context(memory, top)
    memory.word(top + offset, value)
    reads = []

    def read(address, size):
        reads.append((address, size))
        return memory(address, size)

    assert decode_saved_context(
        ARMV8M_MAINLINE_INTEGER,
        top,
        read,
        endian="little",
        stack_low=top,
        stack_high=top + 80,
    ) == (None, "unsupported-extension-context")
    assert all(address + size <= top + 16 for address, size in reads)


def test_armv8m_privileged_control_with_fp_active_still_decodes():
    memory = _Memory()
    top = 0x3000
    _armv8m_context(memory, top)
    memory.word(top + 12, 0xC)
    seed, reason = decode_saved_context(
        ARMV8M_MAINLINE_INTEGER,
        top,
        memory,
        endian="little",
        stack_low=top,
        stack_high=top + 80,
    )
    assert reason == "ok" and seed is not None
    assert seed.sp == top + 80


def test_armv8m_header_requires_readable_bounded_slots():
    memory = _Memory()
    top = 0x3000
    _armv8m_context(memory, top)

    def read(address, size):
        assert top + 4 <= address < top + 80
        return memory(address, size)

    assert decode_saved_context(
        ARMV8M_MAINLINE_INTEGER,
        top,
        read,
        endian="little",
        stack_low=top + 4,
        stack_high=top + 80,
    ) == (None, "saved-context-unreadable")


@pytest.mark.parametrize("fill", [False, True])
def test_rv32_four_byte_slots_preserve_integer_registers_and_exit(fill):
    memory = _Memory()
    top = 0x5000
    for index in range(32):
        memory.word(top + index * 4, 0xDEADBEEF if fill else 0x100 + index)
    memory.word(top, 0x80001000)
    memory.word(top + 4, 0x80002000)
    seed, reason = decode_saved_context(
        RISCV32_INTEGER,
        top,
        memory,
        endian="little",
        entry=0x80001000,
        stack_low=top,
        stack_high=top + 0x200,
    )
    assert reason == "ok" and seed is not None
    assert seed.sp == top + 128 and seed.regs[2] == seed.sp
    assert seed.pc == 0x80001000 and seed.regs[1] == 0x80002000
    assert seed.source == ("unstarted" if fill else "saved")
    assert seed.regs[8] == (0xDEADBEEF if fill else 0x108)


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


@pytest.mark.parametrize("context,bias", [(ARMV7A_INTEGER, 0), (ARMV7A_LWP_INTEGER, 8)])
@pytest.mark.parametrize("fill", [False, True])
def test_armv7a_cpsr_context_preserves_kernel_registers_and_frame_span(
    context, bias, fill
):
    memory = _Memory()
    top = 0x2000
    if bias:
        memory.word(top, 0x333)
        memory.word(top + 4, 0x444)
    memory.word(top + bias, 0x13)
    for register in range(13):
        memory.word(top + bias + 4 + register * 4, 0xDEADBEEF if fill else register)
    memory.word(top + bias + 56, 0x1100)
    memory.word(top + bias + 60, 0x1000)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        entry=0x1000,
        stack_low=top,
        stack_high=top + 0x200,
    )
    assert reason == "ok" and seed is not None
    assert context.status_kind == "cpsr" and context.pc_offset == bias + 60
    assert seed.pc == 0x1000 and seed.sp == top + bias + 64
    assert seed.regs[13] == seed.sp and seed.regs[14] == 0x1100
    assert seed.source == ("unstarted" if fill else "saved")


def test_armv7a_user_mode_is_not_decoded_as_a_kernel_stack():
    memory = _Memory()
    top = 0x2000
    for offset in range(0, 72, 4):
        memory.word(top + offset, 0)
    memory.word(top + 8, 0x10)
    memory.word(top + 68, 0x1000)
    assert decode_saved_context(ARMV7A_LWP_INTEGER, top, memory, endian="little") == (
        None,
        "unsupported-privilege-context",
    )


def test_armv7m_saved_context_honors_alignment_and_refuses_handler_ipsr():
    memory = _Memory()
    top = 0x2000
    _m3_context(memory, top)
    memory.word(top + 60, 0x01000200)
    seed, reason = decode_saved_context(ARMV7M_INTEGER, top, memory, endian="little")
    assert reason == "ok" and seed.sp == top + 68 and seed.regs[13] == seed.sp
    memory.word(top + 60, 0x01000018)
    assert decode_saved_context(ARMV7M_INTEGER, top, memory, endian="little") == (
        None,
        "saved-frame-ipsr-invalid",
    )


def _armv8m_mainline_fpu_context(memory: _Memory, top: int) -> None:
    memory.word(top, 0)
    memory.word(top + 4, 0xFFFFFFED)
    memory.word(top + 8, 0)
    memory.word(top + 12, 0)
    for index in range(8):
        memory.word(top + 16 + index * 4, 0x40 + index)
    for index in range(16):
        memory.word(top + 48 + index * 4, 0xFFFFFFFF)
    memory.word(top + 112, 0x123)
    memory.word(top + 116, 0)
    memory.word(top + 120, 0)
    memory.word(top + 124, 0)
    memory.word(top + 128, 0)
    memory.word(top + 132, 0x1331)
    memory.word(top + 136, 0x1235)
    memory.word(top + 140, 0x01000000)


def _armv7em_fpu_context(memory: _Memory, top: int) -> None:
    memory.word(top, 1)
    for index in range(8):
        memory.word(top + 4 + index * 4, 0x40 + index)
    for index in range(16):
        memory.word(top + 36 + index * 4, 0x55000000 + index)
    memory.word(top + 100, 0x123)
    memory.word(top + 104, 0)
    memory.word(top + 108, 0)
    memory.word(top + 112, 0)
    memory.word(top + 116, 0)
    memory.word(top + 120, 0x1331)
    memory.word(top + 124, 0x1235)
    memory.word(top + 128, 0x01000000)


def test_armv7em_fpu_saved_context_decodes_extended_layout():
    memory = _Memory()
    top = 0x3000
    _armv7em_fpu_context(memory, top)
    seed, reason = decode_armv7em_saved_context(
        top,
        memory,
        basic=ARMV7EM_INTEGER,
        fpu=ARMV7EM_FPU,
        endian="little",
        entry=0x1234,
        stack_low=0x2000,
        stack_high=0x4000,
    )
    assert reason == "ok" and seed is not None
    assert seed.pc == 0x1234
    assert seed.sp == top + 204
    assert seed.source == "saved"
    assert seed.regs[14] == 0x1331
    assert seed.regs[0] == 0x123


def test_armv7em_basic_flag_selects_the_68_byte_layout():
    memory = _Memory()
    top = 0x3000
    memory.word(top, 0)
    for index in range(8):
        memory.word(top + 4 + index * 4, 0x40 + index)
    memory.word(top + 36, 0x123)
    memory.word(top + 40, 0)
    memory.word(top + 44, 0)
    memory.word(top + 48, 0)
    memory.word(top + 52, 0)
    memory.word(top + 56, 0x1331)
    memory.word(top + 60, 0x1234)
    memory.word(top + 64, 0x01000000)
    seed, reason = decode_armv7em_saved_context(
        top,
        memory,
        basic=ARMV7EM_INTEGER,
        fpu=ARMV7EM_FPU,
        endian="little",
        entry=0x1234,
        stack_low=0x2000,
        stack_high=0x4000,
    )
    assert reason == "ok" and seed is not None
    assert seed.sp == top + 68
    assert seed.source == "saved"


def test_armv7em_foreign_flag_is_refused():
    memory = _Memory()
    top = 0x3000
    _armv7em_fpu_context(memory, top)
    memory.word(top, 2)
    assert decode_armv7em_saved_context(
        top,
        memory,
        basic=ARMV7EM_INTEGER,
        fpu=ARMV7EM_FPU,
        endian="little",
        stack_low=0x2000,
        stack_high=0x4000,
    ) == (None, "unsupported-extension-context")


def test_armv8m_mainline_fpu_saved_context_decodes_extended_layout():
    memory = _Memory()
    top = 0x3000
    _armv8m_mainline_fpu_context(memory, top)
    seed, reason = decode_armv8m_mainline_saved_context(
        top,
        memory,
        basic=ARMV8M_MAINLINE_INTEGER,
        fpu=ARMV8M_MAINLINE_FPU,
        endian="little",
        entry=0x1234,
        stack_low=0x2000,
        stack_high=0x4000,
    )
    assert reason == "ok" and seed is not None
    assert seed.pc == 0x1234
    assert seed.sp == top + 216
    assert seed.source == "saved"
    assert seed.regs[14] == 0x1331
    assert seed.regs[0] == 0x123


def test_armv8m_mainline_secure_or_foreign_header_is_refused():
    memory = _Memory()
    top = 0x3000
    _armv8m_mainline_fpu_context(memory, top)
    memory.word(top + 4, 0xFFFFFFBC)
    assert decode_armv8m_mainline_saved_context(
        top,
        memory,
        basic=ARMV8M_MAINLINE_INTEGER,
        fpu=ARMV8M_MAINLINE_FPU,
        endian="little",
        stack_low=0x2000,
        stack_high=0x4000,
    ) == (None, "unsupported-extension-context")
