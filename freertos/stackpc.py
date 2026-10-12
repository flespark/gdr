"""Recover a FreeRTOS task's last PC from its saved context frame.

A FreeRTOS TCB keeps ``pxTopOfStack``, the stack pointer of the saved context.
Its PC is not a TCB field: it sits in the frame the port pushed when the task
left the CPU. The slot is fixed per port, and the ports differ, so the table
below is keyed by port, not by kernel version. Cortex-M3 and Cortex-M33 both use
``PendSV_Handler`` but lay the frame out in opposite directions, so a version
number cannot pick the slot.

The port is decided from evidence in the image, never guessed:

* ``xPortStartFirstTask`` present: RISC-V (``mepc`` is the first 8-byte slot);
* ``PendSV_Handler`` present and the ARM build attribute ``Tag_CPU_name`` reads
  ``8-M.MAIN``: Cortex-M33 (the PC is 24 bytes below ``pxTopOfStack``);
* ``PendSV_Handler`` present and the attribute reads ``7-M``: Cortex-M3 (the PC
  is 56 bytes above ``pxTopOfStack``).

Anything else is ``None`` and the PC column reads ``N/A``.

A decoded value must then pass one proof before it is shown:

* ``switched``: the slot names code (a symbolised address). On Cortex-M the xPSR
  word beside it must also carry the Thumb bit, which a hardware frame always
  has; RISC-V has no such bit and relies on the symbol alone.
* ``unstarted``: the slot equals the task's declared entry, the value the port's
  stack initialiser wrote for a task that never ran.

The module is pure: memory and symbol lookups are injected, so the layout table,
the port decision and the proofs are unit tested without a GDB session. The same
method was verified on the fixtures (see the verification notes in the plan).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Reader = Callable[[int, int], "bytes | None"]

Port = Literal["cortex-m3", "cortex-m33", "riscv64"]


@dataclass(frozen=True)
class SavedPcLayout:
    """Where one port keeps the saved PC, relative to ``pxTopOfStack``.

    ``offset`` may be negative (Cortex-M33 places its frame below the top).
    ``width`` is the slot size in bytes; ``xpsr_offset`` is the xPSR word beside
    the PC on Cortex-M, or ``None`` where the port has no Thumb-bit word to check.
    """

    offset: int
    width: int
    xpsr_offset: int | None


PORT_LAYOUTS: dict[Port, SavedPcLayout] = {
    # PendSV frame: {r0..r3,r12,lr,pc,xpsr} above the saved {r4..r11}.
    "cortex-m3": SavedPcLayout(offset=56, width=4, xpsr_offset=60),
    # PendSV saves {r4..r11,PSPLIM,CONTROL,EXC_RETURN} above the hardware frame;
    # the measured frame lies below pxTopOfStack, PC at -24, xPSR at -20.
    "cortex-m33": SavedPcLayout(offset=-24, width=4, xpsr_offset=-20),
    # portSAVE_CONTEXT stores mepc as the first 8-byte slot.
    "riscv64": SavedPcLayout(offset=0, width=8, xpsr_offset=None),
}


def _uleb128(data: bytes, index: int) -> tuple[int, int] | None:
    """Decode one unsigned LEB128 at *index*; ``(value, next index)`` or None."""
    value = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index
        shift += 7
        if shift > 35:
            return None
    return None


def cpu_name_from_attributes(blob: bytes) -> str | None:
    """The ``Tag_CPU_name`` string of an ARM build-attribute section, or None.

    The section is a version byte ``A`` followed by sub-sections, each
    ``uint32 length`` (covering itself), a NUL-terminated vendor name, and
    ULEB128-tagged attributes. Tag 5 is ``Tag_CPU_name``, a NUL string. Every
    step is bounds-checked, so a malformed section yields None, never a read past
    its end.
    """
    if len(blob) < 5 or blob[0:1] != b"A":
        return None
    index = 1
    while index + 4 <= len(blob):
        length = int.from_bytes(blob[index : index + 4], "little")
        end = index + length
        if length < 5 or end > len(blob):
            return None
        vendor_end = blob.find(b"\x00", index + 4, end)
        if vendor_end == -1:
            return None
        body = vendor_end + 1
        if blob[index + 4 : vendor_end] == b"aeabi":
            cursor = body
            while cursor < end:
                decoded = _uleb128(blob, cursor)
                if decoded is None:
                    return None
                tag, cursor = decoded
                if tag == 1:
                    # Tag_File: a 4-byte length (covering this tag) then more
                    # attributes; step over the length and keep scanning.
                    cursor += 4
                    continue
                if tag == 5:
                    name_end = blob.find(b"\x00", cursor, end)
                    if name_end == -1:
                        return None
                    return blob[cursor:name_end].decode("ascii", errors="replace")
                # Reason: only the tags seen in these builds are decoded; an
                # unknown tag stops the scan rather than being mis-stepped.
                return None
                # type; only Tag_CPU_name is needed to choose the port, so an
                # unknown tag stops the scan rather than being mis-decoded.
                return None
        index = end
    return None


def port_of(
    *,
    has_start_first_task: bool,
    has_pendsv: bool,
    cpu_name: str | None,
) -> Port | None:
    """The FreeRTOS port the image was built for, decided from its evidence."""
    if has_start_first_task:
        return "riscv64"
    if has_pendsv and cpu_name == "8-M.MAIN":
        return "cortex-m33"
    if has_pendsv and cpu_name == "7-M":
        return "cortex-m3"
    return None


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
    if layout.xpsr_offset is None:
        return SavedPc(code, "switched")
    xpsr_raw = read(top_of_stack + layout.xpsr_offset, 4)
    if xpsr_raw is None or len(xpsr_raw) != 4:
        return None
    xpsr = int.from_bytes(xpsr_raw, byteorder=endian)
    if xpsr & (1 << 24):
        return SavedPc(code, "switched")
    return None
