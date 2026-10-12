"""Recover a thread's last PC from its saved switch frame.

``struct rt_thread`` holds no program counter. A thread that left the CPU
saved its context on its own stack, and ``rt_hw_context_switch_exit`` restores
the return address from one fixed slot: ``thread->sp + offset``. The same exit
path serves cooperative switches and interrupt-driven ones, so one slot covers
both. The offset is per port and per release, so it is a table keyed by
``(version, pointer width)`` rather than a guess, and every decoded value must
pass one of three checks before it is shown:

* ``started``: the slot holds the thread's entry (a thread that never ran);
* ``switch``: the instruction before the slot is a call to
  ``rt_hw_context_switch`` (a cooperative switch);
* ``interrupt``: the slot falls inside a symbolised code address (an
  interrupt-saved frame has no call site to check).

Anything else is ``None`` and renders as ``N/A``. This module is pure: memory
and symbol lookups are injected, so the layout table and the checks are unit
tested without a GDB session.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Reader = Callable[[int, int], "bytes | None"]


@dataclass(frozen=True)
class SavedPcLayout:
    """Where one port/release keeps the saved return address.

    ``offset`` is the byte offset from ``thread->sp``; ``width`` the slot size
    (4 on ARM, 8 on RISC-V); ``call`` names the instruction that makes a
    cooperative switch: ``arm_bl`` (ARM ``bl``) or ``rv64_jal`` (``jal ra``).
    """

    offset: int
    width: int
    call: Literal["arm_bl", "rv64_jal"]


@dataclass(frozen=True)
class SavedPc:
    """A decoded last PC and how it was validated (``started``/``switch``/``interrupt``)."""

    pc: int
    how: Literal["started", "switch", "interrupt"]


def saved_pc_layout(
    version: tuple[int, int, int] | None, pointer_size: int
) -> SavedPcLayout | None:
    """The saved-PC slot for one RT-Thread release, or ``None`` when unknown.

    cortex-a9 3.1.x stores the frame without the banked user ``sp``/``lr`` pair
    (``rt_hw_context_switch`` has no ``stmdb sp, {sp, lr}^``), so the return
    address sits 8 bytes earlier than on 4.0.x and 4.1.x. RISC-V (64-bit
    pointers) stores ``ra``/``mepc`` at the frame base on 4.0.x and 4.1.x.
    Releases not listed here get ``None``: the PC column reads ``N/A``.
    """
    if version is None:
        return None
    major, minor, _patch = version
    if pointer_size == 4 and major == 3 and minor == 1:
        return SavedPcLayout(offset=60, width=4, call="arm_bl")
    if pointer_size == 4 and major == 4:
        return SavedPcLayout(offset=68, width=4, call="arm_bl")
    if pointer_size == 8 and major == 4:
        return SavedPcLayout(offset=0, width=8, call="rv64_jal")
    return None


def _word(data: bytes, endian: Literal["little", "big"]) -> int:
    return int.from_bytes(data, byteorder=endian)


def calls_switch(
    call: Literal["arm_bl", "rv64_jal"],
    insn: bytes,
    insn_addr: int,
    switch_addr: int,
    endian: Literal["little", "big"],
) -> bool:
    """Whether the 4-byte instruction at ``insn_addr`` calls ``switch_addr``.

    ARM ``bl`` (cond != 0xF): the target is ``insn_addr + 8 + sext(imm24) << 2``.
    RISC-V ``jal`` with ``rd == ra``: the target is ``insn_addr + sext(imm21)``.
    """
    if len(insn) != 4:
        return False
    word = _word(insn, endian)
    if call == "arm_bl":
        if (word >> 28) == 0xF or ((word >> 25) & 0x7) != 0x5 or not (word >> 24) & 1:
            return False
        imm = word & 0xFFFFFF
        if imm & 0x800000:
            imm -= 0x1000000
        return insn_addr + 8 + (imm << 2) == switch_addr
    # rv64_jal
    if word & 0x7F != 0x6F or (word >> 7) & 0x1F != 1:
        return False
    imm = (
        (((word >> 31) & 1) << 20)
        | (((word >> 12) & 0xFF) << 12)
        | (((word >> 20) & 1) << 11)
        | (((word >> 21) & 0x3FF) << 1)
    )
    if imm & (1 << 20):
        imm -= 1 << 21
    return insn_addr + imm == switch_addr


def decode_saved_pc(
    sp: int,
    entry: int,
    layout: SavedPcLayout,
    *,
    read: Reader,
    switch_addr: int | None,
    symbolised: Callable[[int], bool],
    endian: Literal["little", "big"] = "little",
) -> SavedPc | None:
    """Decode and validate one thread's last PC, or ``None`` when unproven.

    ``read(address, size)`` returns target bytes or ``None``; ``symbolised``
    tells whether an address names code. A read failure is an unproven value,
    never an error: a garbage stack pointer is the common case for a thread
    that has not started.
    """
    if sp == 0:
        return None
    raw = read(sp + layout.offset, layout.width)
    if raw is None or len(raw) != layout.width:
        return None
    pc = _word(raw, endian)
    if pc == 0:
        return None
    if entry != 0 and pc == entry:
        return SavedPc(pc, "started")
    if switch_addr is not None:
        insn = read(pc - 4, 4)
        if insn is not None and calls_switch(
            layout.call, insn, pc - 4, switch_addr, endian
        ):
            return SavedPc(pc, "switch")
    if symbolised(pc & ~1):
        return SavedPc(pc, "interrupt")
    return None


def pack_word(value: int, width: int, endian: Literal["little", "big"]) -> bytes:
    """Encode one slot value; used by the unit tests to build target memory."""
    if width == 4:
        return struct.pack("<I" if endian == "little" else ">I", value)
    return struct.pack("<Q" if endian == "little" else ">Q", value)
