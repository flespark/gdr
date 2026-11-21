"""Last-PC decoding projected from the selected immutable save profile.

Only PC and status slots are read; recovering the full register set is not
a prerequisite. Runtime selection belongs to :mod:`freertos.stack_frame_profile`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from core.elf import Reader
from core.stack_frame_parser import SavedStackContext


@dataclass(frozen=True)
class SavedPcLayout:
    """PC and optional Arm status slots relative to the saved context pointer."""

    offset: int
    width: int
    status_offset: int | None


Port = Literal["armv6-m", "armv7-m", "armv8-m.mainline", "riscv32", "riscv64"]


def pc_layout(context: SavedStackContext) -> SavedPcLayout:
    """Project the PC-only view without another save-layout registry."""
    return SavedPcLayout(context.pc_offset, context.pointer_size, context.status_offset)


@dataclass(frozen=True)
class SavedPc:
    """A decoded last PC and the proof that admitted it (``switched``/``unstarted``)."""

    pc: int
    how: Literal["switched", "unstarted"]


def decode_saved_pc(
    top_of_stack: int,
    entry: int | None,
    layout: SavedPcLayout,
    *,
    read: Reader,
    symbolised: Callable[[int], bool],
    endian: Literal["little", "big"] = "little",
) -> SavedPc | None:
    """Decode one task's last PC, or ``None`` when no proof admits it.

    ``read(address, size)`` returns target bytes or ``None``; ``symbolised``
    tells whether an address names code. A read failure is an unproven value,
    never an error: a task whose stack is not yet written reads as zeros.
    """
    if top_of_stack == 0:
        return None
    raw = read(top_of_stack + layout.offset, layout.width)
    if raw is None or len(raw) != layout.width:
        return None
    pc = int.from_bytes(raw, byteorder=endian)
    code = pc & ~1
    if code == 0:
        return None
    if entry is not None and code == (entry & ~1):
        return SavedPc(code, "unstarted")
    if not symbolised(code):
        return None
    if layout.status_offset is None:
        return SavedPc(code, "switched")
    xpsr_raw = read(top_of_stack + layout.status_offset, 4)
    if xpsr_raw is None or len(xpsr_raw) != 4:
        return None
    xpsr = int.from_bytes(xpsr_raw, byteorder=endian)
    if xpsr & (1 << 24):
        return SavedPc(code, "switched")
    return None
