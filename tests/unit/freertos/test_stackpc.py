"""Unit tests for the FreeRTOS saved-PC decoder (pure: memory and symbols injected).

The attribute tests use the real ``.ARM.attributes`` bytes of the two Cortex-M
fixtures, so the ``Tag_File`` regression is pinned to real input, not to a
hand-made string. The proof tests inject synthetic memory, so they run without a
GDB session.
"""

from __future__ import annotations

import struct

from freertos.stackpc import (
    PORT_LAYOUTS,
    SavedPcLayout,
    cpu_name_from_attributes,
    decode_saved_pc,
    port_of,
)

# Real build-attribute sections of the fixtures (Tag_CPU_name is 7-M and 8-M.MAIN).
M3_ATTRIBUTES = bytes.fromhex(
    "41 2a 00 00 00 61 65 61 62 69 00 01 20 00 00 00 05 37 2d 4d 00 06 0a 07 4d 09 02 12 04 14 01 15 01 17 03 18 01 1a 01 1e 01 22 01"
)
M33_ATTRIBUTES = bytes.fromhex(
    "41 31 00 00 00 61 65 61 62 69 00 01 27 00 00 00 05 38 2d 4d 2e 4d 41 49 4e 00 06 11 07 4d 09 03 12 04 14 01 15 01 17 03 18 01 1a 01 1e 01 22 01 2e 01"
)


def test_cpu_name_is_read_from_the_real_m3_attributes():
    """Tag_File precedes Tag_CPU_name; the scan must step over it, not stop."""
    assert cpu_name_from_attributes(M3_ATTRIBUTES) == "7-M"


def test_cpu_name_is_read_from_the_real_m33_attributes():
    assert cpu_name_from_attributes(M33_ATTRIBUTES) == "8-M.MAIN"


def test_malformed_attributes_yield_none_never_an_overread():
    """Every truncation or bad header stops the scan; none reads past the end."""
    assert cpu_name_from_attributes(b"") is None
    assert cpu_name_from_attributes(b"B" + M3_ATTRIBUTES[1:]) is None, (
        "wrong version byte"
    )
    for cut in range(1, len(M3_ATTRIBUTES)):
        cpu_name_from_attributes(M3_ATTRIBUTES[:cut])  # must not raise
    assert cpu_name_from_attributes(M3_ATTRIBUTES[:20]) is None, (
        "a cut name is not a name"
    )


def test_port_is_decided_by_evidence_not_by_version():
    """RISC-V by its start symbol; Cortex-M split by the CPU name, never guessed."""
    assert (
        port_of(has_start_first_task=True, has_pendsv=False, cpu_name=None) == "riscv64"
    )
    assert (
        port_of(has_start_first_task=False, has_pendsv=True, cpu_name="7-M")
        == "cortex-m3"
    )
    assert (
        port_of(has_start_first_task=False, has_pendsv=True, cpu_name="8-M.MAIN")
        == "cortex-m33"
    )
    # PendSV with an unknown or missing CPU name is undecided, not a default port.
    assert port_of(has_start_first_task=False, has_pendsv=True, cpu_name=None) is None
    assert port_of(has_start_first_task=False, has_pendsv=False, cpu_name="7-M") is None


def test_the_port_layouts_hold_the_measured_slots():
    """The offsets are the measured ones: M3 +56, M33 -24, RISC-V +0 (8 bytes)."""
    assert PORT_LAYOUTS["cortex-m3"] == SavedPcLayout(
        offset=56, width=4, xpsr_offset=60
    )
    assert PORT_LAYOUTS["cortex-m33"] == SavedPcLayout(
        offset=-24, width=4, xpsr_offset=-20
    )
    assert PORT_LAYOUTS["riscv64"] == SavedPcLayout(offset=0, width=8, xpsr_offset=None)


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
    layout = PORT_LAYOUTS["cortex-m3"]
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
    layout = PORT_LAYOUTS["cortex-m3"]
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
    layout = PORT_LAYOUTS["cortex-m33"]
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
    layout = PORT_LAYOUTS["riscv64"]
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
    layout = PORT_LAYOUTS["cortex-m3"]
    entry = 0x0000_0200
    memory = _Memory({top + 56: struct.pack("<I", entry | 1)})
    decoded = decode_saved_pc(top, entry, layout, read=memory, symbolised=_code(set()))
    assert decoded is not None
    assert (decoded.pc, decoded.how) == (entry, "unstarted")


def test_no_proof_means_no_pc_not_an_unverified_address():
    """A value that is neither code nor an entry is refused, never shown."""
    top = 0x2000_3000
    layout = PORT_LAYOUTS["cortex-m3"]
    memory = _Memory({top + 56: struct.pack("<I", 0xDEAD_BEEF)})
    assert (
        decode_saved_pc(top, 0x200, layout, read=memory, symbolised=_code(set()))
        is None
    )


def test_missing_memory_and_zero_stacks_are_unproven():
    """An unwritten stack or a null top decodes to nothing, not to an error."""
    layout = PORT_LAYOUTS["cortex-m3"]
    assert (
        decode_saved_pc(0, None, layout, read=_Memory({}), symbolised=_code(set()))
        is None
    )
    assert (
        decode_saved_pc(0x2000, None, layout, read=_Memory({}), symbolised=_code(set()))
        is None
    )
