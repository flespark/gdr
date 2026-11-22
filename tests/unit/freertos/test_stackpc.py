"""Unit tests for the FreeRTOS saved-PC decoder (pure: memory and symbols injected).

The attribute tests use the real ``.ARM.attributes`` bytes of the two Cortex-M
fixtures, so the ``Tag_File`` regression is pinned to real input, not to a
hand-made string. The proof tests inject synthetic memory, so they run without a
GDB session.
"""

from __future__ import annotations

import struct

from core.stack_frame_parser import SavedStackContext
from freertos.backtrace.unwind import (
    ARMV6M_INTEGER,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_MPU_INTEGER,
    ARMV8M_MAINLINE_SMP_INTEGER,
    RISCV64_INTEGER,
)
from freertos.stackpc import SavedPcLayout, decode_saved_pc, pc_layout

# Real build-attribute sections of the fixtures (Tag_CPU_name is 7-M and 8-M.MAIN).
M3_ATTRIBUTES = bytes.fromhex(
    "41 2a 00 00 00 61 65 61 62 69 00 01 20 00 00 00 05 37 2d 4d 00 06 0a 07 4d 09 02 12 04 14 01 15 01 17 03 18 01 1a 01 1e 01 22 01"
)
M33_ATTRIBUTES = bytes.fromhex(
    "41 31 00 00 00 61 65 61 62 69 00 01 27 00 00 00 05 38 2d 4d 2e 4d 41 49 4e 00 06 11 07 4d 09 03 12 04 14 01 15 01 17 03 18 01 1a 01 1e 01 22 01 2e 01"
)
M0_ATTRIBUTES = bytes.fromhex(
    "41 2a 00 00 00 61 65 61 62 69 00 01 20 00 00 00 05 36 2d 4d 00 06 0c 07 4d 09 02 12 04 14 01 15 01 17 03 18 01 1a 01 1e 01 22 01"
)


def test_last_pc_layout_is_projected_from_the_saved_context():
    context = SavedStackContext(
        profile_id="test/arm",
        pointer_size=4,
        registers=((4, 0),),
        pc_offset=56,
        lr_offset=52,
        status_offset=60,
        sp_offset=64,
        context_span=(0, 64),
        validation="live",
    )
    assert pc_layout(context) == SavedPcLayout(56, 4, 60)


def test_verified_pc_profiles_are_projected_with_mpu_and_smp_slots():
    assert pc_layout(ARMV6M_INTEGER) == SavedPcLayout(60, 4, 64)
    assert pc_layout(ARMV8M_MAINLINE_SMP_INTEGER) == SavedPcLayout(64, 4, 68)


class _Memory:
    """Sparse target memory: a read outside what was written fails, like a bad pointer."""

    def __init__(self, data: dict[int, bytes]) -> None:
        self.data = data

    def __call__(self, address: int, size: int) -> bytes | None:
        for base, blob in self.data.items():
            if base <= address and address + size <= base + len(blob):
                start = address - base
                return blob[start : start + size]
        return None


def _code(addresses: set[int]):
    return lambda address: address in addresses


def test_a_switched_cortex_m_frame_is_proven_by_code_and_thumb_xpsr():
    """Symbolised PC plus a Thumb xPSR proves a switched Cortex-M task."""
    top = 0x2000_1000
    layout = pc_layout(ARMV7M_INTEGER)
    pc = 0x0000_0DB6
    memory = _Memory(
        {
            top + 56: struct.pack("<I", pc),
            top + 60: struct.pack("<I", 0x6100_0000),  # bit 24 (Thumb) set
        }
    )
    decoded = decode_saved_pc(top, None, layout, read=memory, symbolised=_code({pc}))
    assert decoded is not None
    assert (decoded.pc, decoded.how) == (pc, "switched")


def test_a_frame_without_the_thumb_bit_is_not_proven():
    """A word that is code but whose xPSR lacks the Thumb bit is not a hardware frame."""
    top = 0x2000_1000
    layout = pc_layout(ARMV7M_INTEGER)
    pc = 0x0000_0DB6
    memory = _Memory(
        {
            top + 56: struct.pack("<I", pc),
            top + 60: struct.pack("<I", 0x0000_0000),
        }
    )
    assert (
        decode_saved_pc(top, None, layout, read=memory, symbolised=_code({pc})) is None
    )


def test_the_cortex_m33_frame_lies_below_the_top():
    """M33's PC is 24 bytes below pxTopOfStack; a slot above it is not read."""
    top = 0x2800_5000
    layout = pc_layout(ARMV8M_MAINLINE_MPU_INTEGER)
    pc = 0x1000_0440
    memory = _Memory(
        {
            top - 24: struct.pack("<I", pc),
            top - 20: struct.pack("<I", 0x6100_0000),
        }
    )
    decoded = decode_saved_pc(top, None, layout, read=memory, symbolised=_code({pc}))
    assert decoded is not None
    assert decoded.pc == pc


def test_riscv_proves_by_symbol_alone_in_its_first_slot():
    """RISC-V has no Thumb word to check; the symbol at pxTopOfStack is the proof."""
    top = 0x8002_D000
    layout = pc_layout(RISCV64_INTEGER)
    pc = 0x8000_0020
    memory = _Memory({top: struct.pack("<Q", pc)})
    decoded = decode_saved_pc(
        top, None, layout, read=memory, symbolised=_code({pc}), endian="little"
    )
    assert decoded is not None
    assert (decoded.pc, decoded.how) == (pc, "switched")


def test_the_unstarted_proof_admits_a_slot_equal_to_the_entry():
    """A slot that equals a known entry proves an unstarted task, without a code lookup."""
    top = 0x2000_2000
    layout = pc_layout(ARMV7M_INTEGER)
    entry = 0x0000_0200
    memory = _Memory({top + 56: struct.pack("<I", entry | 1)})
    decoded = decode_saved_pc(top, entry, layout, read=memory, symbolised=_code(set()))
    assert decoded is not None
    assert (decoded.pc, decoded.how) == (entry, "unstarted")


def test_no_proof_means_no_pc_not_an_unverified_address():
    """A value that is neither code nor an entry is refused, never shown."""
    top = 0x2000_3000
    layout = pc_layout(ARMV7M_INTEGER)
    memory = _Memory({top + 56: struct.pack("<I", 0xDEAD_BEEF)})
    assert (
        decode_saved_pc(top, 0x200, layout, read=memory, symbolised=_code(set()))
        is None
    )


def test_missing_memory_and_zero_stacks_are_unproven():
    """An unwritten stack or a null top decodes to nothing, not to an error."""
    layout = pc_layout(ARMV7M_INTEGER)
    assert (
        decode_saved_pc(0, None, layout, read=_Memory({}), symbolised=_code(set()))
        is None
    )
    assert (
        decode_saved_pc(0x2000, None, layout, read=_Memory({}), symbolised=_code(set()))
        is None
    )
