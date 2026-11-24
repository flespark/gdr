"""Unit tests for the saved-PC decoder (pure: memory and symbols are injected)."""

from __future__ import annotations

import struct

from rtthread.stackpc import (
    SavedPcLayout,
    calls_switch,
    decode_saved_pc,
    saved_pc_layout,
)

SWITCH = 0x6008_90D0  # rt_hw_context_switch in the cortex-a9 4.0.5 fixture
RV64_SWITCH = 0x8000_0060  # rt_hw_context_switch in the rv64 4.0.5 fixture


def _arm_bl(at: int, target: int) -> bytes:
    """Encode an ARM ``bl`` at *at* that lands on *target*."""
    offset = (target - (at + 8)) >> 2
    word = 0xEB000000 | (offset & 0xFFFFFF)
    return struct.pack("<I", word)


def _rv64_jal_ra(at: int, target: int) -> bytes:
    """Encode a RISC-V ``jal ra, target`` at *at*."""
    offset = target - at
    imm = offset & 0x1FFFFF
    word = (
        (((imm >> 20) & 1) << 31)
        | (((imm >> 1) & 0x3FF) << 21)
        | (((imm >> 11) & 1) << 20)
        | (((imm >> 12) & 0xFF) << 12)
        | (1 << 7)
        | 0x6F
    )
    return struct.pack("<I", word)


class _Memory:
    """A sparse target memory: reads outside what was written fail, like a bad pointer."""

    def __init__(self, data: dict[int, bytes]) -> None:
        self.data = data

    def __call__(self, address: int, size: int) -> bytes | None:
        for base, blob in self.data.items():
            if base <= address and address + size <= base + len(blob):
                start = address - base
                return blob[start : start + size]
        return None


def _symbolised(addresses: set[int]):
    return lambda address: address in addresses


def test_layout_table_covers_the_verified_releases_only():
    """3.1.x and 4.x on ARM, 4.x on RISC-V; anything else has no slot."""
    assert saved_pc_layout((3, 1, 5), 4) == SavedPcLayout(60, 4, "arm_bl")
    assert saved_pc_layout((4, 0, 5), 4) == SavedPcLayout(68, 4, "arm_bl")
    assert saved_pc_layout((4, 1, 1), 4) == SavedPcLayout(68, 4, "arm_bl")
    assert saved_pc_layout((4, 0, 5), 8) == SavedPcLayout(0, 8, "rv64_jal")
    assert saved_pc_layout((5, 0, 0), 4) is None
    assert saved_pc_layout(None, 4) is None


def test_arm_bl_check_accepts_only_a_call_to_the_switch():
    """The instruction before a return address must be ``bl rt_hw_context_switch``."""
    at = 0x6002_0854
    assert calls_switch("arm_bl", _arm_bl(at, SWITCH), at, SWITCH, "little")
    assert not calls_switch("arm_bl", _arm_bl(at, SWITCH + 4), at, SWITCH, "little")
    # A conditional-never word is not a bl.
    assert not calls_switch(
        "arm_bl", struct.pack("<I", 0xFB000000), at, SWITCH, "little"
    )
    assert not calls_switch("arm_bl", b"\x00\x00", at, SWITCH, "little")


def test_rv64_jal_check_requires_ra_as_the_link_register():
    """``jal`` only counts as a switch call when it links through ``ra``."""
    at = 0x8000_163A - 4
    assert calls_switch(
        "rv64_jal", _rv64_jal_ra(at, RV64_SWITCH), at, RV64_SWITCH, "little"
    )
    word = int.from_bytes(_rv64_jal_ra(at, RV64_SWITCH), "little")
    non_link = struct.pack("<I", word & ~(1 << 7))
    assert not calls_switch("rv64_jal", non_link, at, RV64_SWITCH, "little")


def test_a_cooperative_switch_frame_decodes_as_switch():
    """The slot holds a return address preceded by a call to the switch."""
    sp = 0x600A_0000
    return_address = 0x6002_0858
    layout = SavedPcLayout(68, 4, "arm_bl")
    memory = _Memory(
        {
            sp + 68: struct.pack("<I", return_address),
            return_address - 4: _arm_bl(return_address - 4, SWITCH),
        }
    )

    decoded = decode_saved_pc(
        sp,
        0x6001_5554,
        layout,
        read=memory,
        switch_addr=SWITCH,
        symbolised=_symbolised(set()),
    )

    assert decoded is not None
    assert (decoded.pc, decoded.how) == (return_address, "switch")


def test_a_never_started_thread_reports_its_entry():
    """Before the first run the slot holds the entry point itself."""
    sp = 0x600A_0000
    entry = 0x6001_5554
    layout = SavedPcLayout(68, 4, "arm_bl")
    memory = _Memory({sp + 68: struct.pack("<I", entry)})

    decoded = decode_saved_pc(
        sp,
        entry,
        layout,
        read=memory,
        switch_addr=SWITCH,
        symbolised=_symbolised(set()),
    )

    assert decoded is not None
    assert (decoded.pc, decoded.how) == (entry, "started")


def test_an_interrupt_frame_is_accepted_only_inside_code():
    """No call site to check: the value must name a symbolised code address."""
    sp = 0x600A_0000
    interrupted = 0x6001_2254
    layout = SavedPcLayout(68, 4, "arm_bl")
    memory = _Memory({sp + 68: struct.pack("<I", interrupted)})

    decoded = decode_saved_pc(
        sp,
        0,
        layout,
        read=memory,
        switch_addr=SWITCH,
        symbolised=_symbolised({interrupted}),
    )
    assert decoded is not None
    assert (decoded.pc, decoded.how) == (interrupted, "interrupt")

    rejected = decode_saved_pc(
        sp,
        0,
        layout,
        read=memory,
        switch_addr=SWITCH,
        symbolised=_symbolised(set()),
    )
    assert rejected is None


def test_garbage_frames_are_unproven_not_errors():
    """Canaries, null slots, unreadable stacks and a missing switch all give None."""
    layout = SavedPcLayout(68, 4, "arm_bl")
    sp = 0x600A_0000
    garbage = _Memory({sp + 68: struct.pack("<I", 0xDEADBEEF)})
    assert (
        decode_saved_pc(
            sp,
            0,
            layout,
            read=garbage,
            switch_addr=SWITCH,
            symbolised=_symbolised(set()),
        )
        is None
    )
    null = _Memory({sp + 68: struct.pack("<I", 0)})
    assert (
        decode_saved_pc(
            sp, 0, layout, read=null, switch_addr=SWITCH, symbolised=_symbolised(set())
        )
        is None
    )
    assert (
        decode_saved_pc(
            0,
            0,
            layout,
            read=garbage,
            switch_addr=SWITCH,
            symbolised=_symbolised(set()),
        )
        is None
    )
    unreadable = _Memory({})
    assert (
        decode_saved_pc(
            sp,
            0,
            layout,
            read=unreadable,
            switch_addr=SWITCH,
            symbolised=_symbolised(set()),
        )
        is None
    )


def test_rv64_slot_is_eight_bytes_at_the_frame_base():
    """RISC-V stores the 64-bit return address at ``sp + 0``."""
    sp = 0x8002_DCE0
    return_address = 0x8000_163A
    layout = SavedPcLayout(0, 8, "rv64_jal")
    memory = _Memory(
        {
            sp: struct.pack("<Q", return_address),
            return_address - 4: _rv64_jal_ra(return_address - 4, RV64_SWITCH),
        }
    )

    decoded = decode_saved_pc(
        sp,
        0x8000_045E,
        layout,
        read=memory,
        switch_addr=RV64_SWITCH,
        symbolised=_symbolised(set()),
        endian="little",
    )
    assert decoded is not None
    assert (decoded.pc, decoded.how) == (return_address, "switch")


def test_verified_mprofile_and_rv32_saved_pc_handles_fpu_and_basic_frames():
    """Verified M-profile (including FPU basic/extended) and RV32 decode saved PC."""
    from core.stack_frame_parser import StackContextSelection
    from rtthread.backtrace.unwind import (
        ARMV7EM_FPU,
        ARMV7EM_INTEGER,
        ARMV7M_INTEGER,
        ARMV8M_MAINLINE_FPU,
        ARMV8M_MAINLINE_INTEGER,
        RISCV32_INTEGER,
    )
    from rtthread.stackpc import decode_verified_saved_pc

    sp = 0x2800_2000
    pc = 0x1000_2400

    # CM33 FPU extended (EXC_RETURN=0xFFFFFFED -> offset 136/140) and basic (72/76).
    cm33_sel = StackContextSelection(
        "armv8-m.mainline",
        "arm",
        ARMV8M_MAINLINE_INTEGER,
        "ok",
        alt_context=ARMV8M_MAINLINE_FPU,
    )
    cm33_fpu = _Memory(
        {
            sp + 4: struct.pack("<I", 0xFFFFFFED),
            sp + 136: struct.pack("<I", pc),
            sp + 140: struct.pack("<I", 0x6100_0000),
        }
    )
    dec = decode_verified_saved_pc(
        sp, 0, cm33_sel, read=cm33_fpu, symbolised=_symbolised({pc})
    )
    assert dec is not None and (dec.pc, dec.how) == (pc, "interrupt")

    cm33_basic = _Memory(
        {
            sp + 4: struct.pack("<I", 0xFFFFFFFD),
            sp + 72: struct.pack("<I", pc),
            sp + 76: struct.pack("<I", 0x0100_0000),
        }
    )
    dec_b = decode_verified_saved_pc(
        sp, 0, cm33_sel, read=cm33_basic, symbolised=_symbolised({pc})
    )
    assert dec_b is not None and dec_b.pc == pc
    assert (
        decode_verified_saved_pc(
            sp,
            0,
            cm33_sel,
            read=_Memory({sp + 4: struct.pack("<I", 0xDEADBEEF)}),
            symbolised=_symbolised({pc}),
        )
        is None
    )

    # CM4F FPU flag=1 (124/128), flag=0 (60/64), bad flag -> None.
    cm4f_sel = StackContextSelection(
        "armv7e-m", "arm", ARMV7EM_INTEGER, "ok", alt_context=ARMV7EM_FPU
    )
    cm4f_fpu = _Memory(
        {
            sp + 0: struct.pack("<I", 1),
            sp + 124: struct.pack("<I", pc),
            sp + 128: struct.pack("<I", 0x0100_0000),
        }
    )
    assert (
        decode_verified_saved_pc(
            sp, 0, cm4f_sel, read=cm4f_fpu, symbolised=_symbolised({pc})
        )
        is not None
    )
    cm4f_basic = _Memory(
        {
            sp + 0: struct.pack("<I", 0),
            sp + 60: struct.pack("<I", pc),
            sp + 64: struct.pack("<I", 0x0100_0000),
        }
    )
    assert (
        decode_verified_saved_pc(
            sp, 0, cm4f_sel, read=cm4f_basic, symbolised=_symbolised({pc})
        )
        is not None
    )
    assert (
        decode_verified_saved_pc(
            sp,
            0,
            cm4f_sel,
            read=_Memory({sp: struct.pack("<I", 2)}),
            symbolised=_symbolised({pc}),
        )
        is None
    )

    # CM3 integer without Thumb bit fails closed; entry match proves started.
    cm3_sel = StackContextSelection("armv7-m", "arm", ARMV7M_INTEGER, "ok")
    assert (
        decode_verified_saved_pc(
            sp,
            0,
            cm3_sel,
            read=_Memory(
                {sp + 56: struct.pack("<I", pc), sp + 60: struct.pack("<I", 0)}
            ),
            symbolised=_symbolised({pc}),
        )
        is None
    )
    started = decode_verified_saved_pc(
        sp,
        pc,
        cm3_sel,
        read=_Memory({sp + 56: struct.pack("<I", pc | 1)}),
        symbolised=_symbolised(set()),
    )
    assert started is not None and (started.pc, started.how) == (pc, "started")

    # RV32 verified integer context reads 4-byte PC at offset 0.
    rv32_sel = StackContextSelection("riscv32", "riscv", RISCV32_INTEGER, "ok")
    rv_dec = decode_verified_saved_pc(
        sp,
        0,
        rv32_sel,
        read=_Memory({sp: struct.pack("<I", pc)}),
        symbolised=_symbolised({pc}),
    )
    assert rv_dec is not None and rv_dec.pc == pc
