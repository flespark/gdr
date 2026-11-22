"""Pure FreeRTOS saved-context decoding tests."""

from __future__ import annotations

from freertos.backtrace.unwind import (
    ARMV7M_CM4F_BASIC,
    ARMV7M_CM4F_FPU,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_MPU_INTEGER,
    ARMV8M_MAINLINE_SMP_INTEGER,
    RISCV32_INTEGER,
    RISCV64_INTEGER,
    decode_cm4f_saved_context,
    decode_saved_context,
    initial_context,
)


class _Memory:
    def __init__(self) -> None:
        self.blocks: dict[int, bytearray] = {}

    def ensure(self, base: int, size: int) -> bytearray:
        if base not in self.blocks:
            self.blocks[base] = bytearray(size)
        return self.blocks[base]

    def put(self, address: int, data: bytes) -> None:
        base = address & ~0xFF
        blob = self.blocks.get(base)
        if blob is None:
            blob = bytearray(0x200)
            self.blocks[base] = blob
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


def _arm_saved_context(memory: _Memory, top: int, *, fill: bool = False) -> None:
    for index in range(8):
        memory.word(top + index * 4, 0xA5A5A5A5 if fill else 0x11111111 + index)
    memory.word(top + 32, 0)  # r0
    memory.word(top + 36, 0x2000)
    memory.word(top + 40, 0x100)
    memory.word(top + 44, 0x200)
    memory.word(top + 48, 0x300)
    memory.word(top + 52, 0x400)
    memory.word(top + 56, 0x1001)
    memory.word(top + 60, 0x01000000 if fill else 0x61000000)


def test_cortex_m3_saved_context_decodes_pc_sp_and_registers():
    memory = _Memory()
    top = 0x2000
    _arm_saved_context(memory, top)
    seed, reason = decode_saved_context(
        ARMV7M_INTEGER,
        top,
        memory,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
        exit_pc=0x400,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.pc == 0x1000
    assert seed.sp == top + 64
    assert seed.source == "saved"
    assert seed.regs[14] == 0x400
    assert seed.regs[13] == top + 64


def test_cortex_m3_initial_frame_requires_independent_entry_evidence():
    memory = _Memory()
    top = 0x2000
    _arm_saved_context(memory, top, fill=True)
    seed, reason = decode_saved_context(
        ARMV7M_INTEGER,
        top,
        memory,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
        exit_pc=0x400,
        entry_pc=0x1000,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.source == "unstarted"
    assert seed.root_pc == 0x1000


def test_cortex_m33_mpu_context_uses_the_psp_slot_and_negative_offsets():
    memory = _Memory()
    top = 0x3000
    context = ARMV8M_MAINLINE_MPU_INTEGER
    for register, offset in context.registers:
        memory.word(top + offset, 0x100 + register)
    memory.word(top - 24, 0x1235)
    memory.word(top - 20, 0x61000000)
    memory.word(top - 28, 0x222)
    memory.word(top - 16, 0x2080)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        stack_low=0x2000,
        stack_high=0x2100,
        context_low=top - 80,
        context_high=top + 4,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.pc == 0x1234
    assert seed.sp == 0x20A0
    assert seed.regs[14] == 0x222


def test_cortex_m33_smp_context_places_the_hardware_frame_above_the_top():
    memory = _Memory()
    top = 0x4000
    context = ARMV8M_MAINLINE_SMP_INTEGER
    for register, offset in context.registers:
        memory.word(top + offset, 0x100 + register)
    memory.word(top + 64, 0x3457)
    memory.word(top + 68, 0x61000000)
    memory.word(top + 60, 0x222)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        stack_low=0x3000,
        stack_high=0x5000,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.pc == 0x3456
    assert seed.sp == top + 72


def test_riscv64_context_maps_pc_ra_and_saved_sp():
    memory = _Memory()
    top = 0x5000
    context = RISCV64_INTEGER
    for register, offset in context.registers:
        memory.word(top + offset, 0x100 + register, size=8)
    memory.word(top, 0x80000123, size=8)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        stack_low=0x4000,
        stack_high=0x6000,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.pc == 0x80000123
    assert seed.sp == top + 31 * 8
    assert seed.regs[8] == 0x108


def test_riscv32_context_uses_four_byte_words():
    memory = _Memory()
    top = 0x6000
    context = RISCV32_INTEGER
    for register, offset in context.registers:
        memory.word(top + offset, 0x100 + register)
    memory.word(top, 0x80000123)
    seed, reason = decode_saved_context(
        context,
        top,
        memory,
        endian="little",
        stack_low=0x5000,
        stack_high=0x7000,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.sp == top + 31 * 4


def test_missing_or_non_thumb_saved_context_is_not_a_frame():
    memory = _Memory()
    assert (
        decode_saved_context(ARMV7M_INTEGER, 0, memory, endian="little")[1]
        == "no-saved-context"
    )
    top = 0x2000
    _arm_saved_context(memory, top)
    memory.word(top + 60, 0)
    assert (
        decode_saved_context(
            ARMV7M_INTEGER,
            top,
            memory,
            endian="little",
            stack_low=0x1000,
            stack_high=0x3000,
        )[1]
        == "saved-frame-not-thumb"
    )


def test_saved_context_rejects_a_handler_ipsr():
    memory = _Memory()
    top = 0x2000
    _arm_saved_context(memory, top)
    memory.word(top + 60, 0x01000018)
    seed, reason = decode_saved_context(
        ARMV7M_INTEGER,
        top,
        memory,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
    )
    assert seed is None and reason == "saved-frame-ipsr-invalid"


def test_initial_context_requires_the_unstarted_proof():
    memory = _Memory()
    top = 0x2000
    _arm_saved_context(memory, top)
    assert (
        initial_context(
            ARMV7M_INTEGER,
            top,
            memory,
            endian="little",
            stack_low=0x1000,
            stack_high=0x3000,
        )[1]
        == "not-unstarted"
    )
    _arm_saved_context(memory, top, fill=True)
    seed, reason = initial_context(
        ARMV7M_INTEGER,
        top,
        memory,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
        entry_pc=0x1000,
        exit_pc=0x400,
    )
    assert reason == "ok" and seed is not None


def test_initial_registers_without_entry_evidence_do_not_prove_unstarted():
    memory = _Memory()
    top = 0x2000
    _arm_saved_context(memory, top, fill=True)
    for pc, entry in ((0x100D, None), (0x100D, 0x1000), (0x1001, None)):
        memory.word(top + 56, pc)
        seed, reason = decode_saved_context(
            ARMV7M_INTEGER,
            top,
            memory,
            endian="little",
            exit_pc=0x400,
            entry_pc=entry,
        )
        assert reason == "ok"
        assert seed.source == "saved"
        assert seed.root_pc is None


def test_mpu_context_needs_its_own_bounds_before_reading_memory():
    reads = []
    seed, reason = decode_saved_context(
        ARMV8M_MAINLINE_MPU_INTEGER,
        0x3000,
        lambda address, size: reads.append((address, size)),
        endian="little",
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert seed is None and reason == "context-bounds-unavailable"
    assert reads == []


def test_m33_mpu_and_smp_profiles_have_distinct_ids():
    assert ARMV8M_MAINLINE_MPU_INTEGER.profile_id != (
        ARMV8M_MAINLINE_SMP_INTEGER.profile_id
    )


def test_saved_context_rejects_restored_sp_outside_task_stack():
    memory = _Memory()
    top = 0x2000
    _arm_saved_context(memory, top)
    seed, reason = decode_saved_context(
        ARMV7M_INTEGER,
        top,
        memory,
        endian="little",
        stack_low=top,
        stack_high=top + 60,
    )
    assert seed is None and reason == "saved-stack-overflow"


def test_hardware_alignment_padding_is_part_of_restored_sp():
    memory = _Memory()
    top = 0x2000
    _arm_saved_context(memory, top)
    memory.word(top + 60, 0x01000200)
    seed, reason = decode_saved_context(
        ARMV7M_INTEGER,
        top,
        memory,
        endian="little",
        stack_low=top,
        stack_high=top + 68,
    )
    assert reason == "ok" and seed.sp == top + 68
    assert seed.regs[13] == top + 68


def _cm4f_basic_context(memory, top, *, fill=False, pc=0x1001, lr=0x400):
    for index in range(8):
        memory.word(top + index * 4, 0xA5A5A5A5 if fill else 0x11111111 + index)
    memory.word(top + 32, 0xFFFFFFFD)
    memory.word(top + 36, 0)
    memory.word(top + 40, 0x2000)
    memory.word(top + 44, 0x100)
    memory.word(top + 48, 0x200)
    memory.word(top + 52, 0x300)
    memory.word(top + 56, lr)
    memory.word(top + 60, pc)
    memory.word(top + 64, 0x01000000 if fill else 0x61000000)


def _cm4f_fpu_context(memory, top, *, pc=0x1001, lr=0x400):
    for index in range(8):
        memory.word(top + index * 4, 0x40 + index)
    memory.word(top + 32, 0xFFFFFFED)
    for index in range(16):
        memory.word(top + 36 + index * 4, 0xFFFFFFFF)
    memory.word(top + 100, 1)
    memory.word(top + 104, 2)
    memory.word(top + 108, 3)
    memory.word(top + 112, 4)
    memory.word(top + 116, 12)
    memory.word(top + 120, lr)
    memory.word(top + 124, pc)
    memory.word(top + 128, 0x61000000)


def test_cm4f_basic_saved_context_decodes_pc_sp_and_registers():
    memory = _Memory()
    top = 0x2000
    _cm4f_basic_context(memory, top)
    seed, reason = decode_cm4f_saved_context(
        top,
        memory,
        basic=ARMV7M_CM4F_BASIC,
        fpu=ARMV7M_CM4F_FPU,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
        exit_pc=0x400,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.pc == 0x1000
    assert seed.sp == top + 68
    assert seed.source == "saved"
    assert seed.regs[14] == 0x400
    assert seed.regs[0] == 0
    assert seed.regs[4] == 0x11111111


def test_cm4f_fpu_saved_context_decodes_extended_layout():
    memory = _Memory()
    top = 0x2000
    _cm4f_fpu_context(memory, top)
    seed, reason = decode_cm4f_saved_context(
        top,
        memory,
        basic=ARMV7M_CM4F_BASIC,
        fpu=ARMV7M_CM4F_FPU,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
        exit_pc=0x400,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.pc == 0x1000
    assert seed.sp == top + 204
    assert seed.source == "saved"
    assert seed.regs[14] == 0x400
    assert seed.regs[0] == 1
    assert seed.regs[4] == 0x40


def test_cm4f_unstarted_initial_image_selects_the_basic_layout():
    memory = _Memory()
    top = 0x2000
    _cm4f_basic_context(memory, top, fill=True, pc=0x3000, lr=0x400)
    seed, reason = decode_cm4f_saved_context(
        top,
        memory,
        basic=ARMV7M_CM4F_BASIC,
        fpu=ARMV7M_CM4F_FPU,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
        exit_pc=0x400,
        entry_pc=0x3000,
    )
    assert reason == "ok"
    assert seed is not None
    assert seed.source == "unstarted"
    assert seed.root_pc == 0x3000


def test_cm4f_unknown_exc_return_is_refused_without_further_reads():
    memory = _Memory()
    top = 0x2000
    _cm4f_basic_context(memory, top)
    memory.word(top + 32, 0x12345678)
    assert decode_cm4f_saved_context(
        top,
        memory,
        basic=ARMV7M_CM4F_BASIC,
        fpu=ARMV7M_CM4F_FPU,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
    ) == (None, "saved-context-exc-return-unproven")


def test_cm4f_unreadable_exc_return_slot_is_refused():
    memory = _Memory()
    assert decode_cm4f_saved_context(
        0x2000,
        memory,
        basic=ARMV7M_CM4F_BASIC,
        fpu=ARMV7M_CM4F_FPU,
        endian="little",
        stack_low=0x1000,
        stack_high=0x3000,
    ) == (None, "saved-context-unreadable")


def test_cm4f_basic_and_fpu_profiles_have_distinct_ids():
    assert ARMV7M_CM4F_BASIC.profile_id != ARMV7M_CM4F_FPU.profile_id
    assert ARMV7M_CM4F_FPU.fpu and not ARMV7M_CM4F_BASIC.fpu
