"""Pure RT-Thread saved-context decoding tests."""

from __future__ import annotations

from rtthread.backtrace import context_for, decode_saved_context


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
    context = context_for("cortex-m3")
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


def test_rtthread_m33_context_has_special_registers_above_the_frame():
    context = context_for("cortex-m33")
    assert context is not None and context.verified is False
    memory = _Memory()
    top = 0x3000
    for register, offset in context.registers:
        memory.word(top + offset, register)
    memory.word(top + 4, 0x222)  # EXC_RETURN
    memory.word(top + 72, 0x1235)
    memory.word(top + 76, 0x61000000)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        entry=0x1234,
        stack_low=0x2000,
        stack_high=0x4000,
    )
    assert reason == "ok" and seed is not None
    assert seed.pc == 0x1234
    assert seed.sp == top + 80


def test_rv64_context_is_closed_loop_verified_and_sp_is_above_it():
    context = context_for("riscv64")
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
    context = context_for("cortex-m3")
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
