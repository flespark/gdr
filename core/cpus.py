"""Bounded ELF CPU and ABI evidence, without RTOS policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .unwind_meta import EM_ARM, EM_RISCV, Arch, ElfImage, Endian, _read_uleb

MAX_ATTRIBUTE_BYTES = 64 * 1024

CpuFamily = Literal["arm", "riscv", "unknown"]

Isa = Literal[
    "armv6-m",
    "armv7-m",
    "armv7-a",
    "armv7e-m",
    "armv8-m.baseline",
    "armv8-m.mainline",
    "armv8.1-m.mainline",
    "riscv32",
    "riscv64",
]


@dataclass(frozen=True)
class RegisterConvention:
    """Architecture register numbers, GDB names and PC normalization rules."""

    family: Arch
    sp_register: int
    return_register: int
    frame_pointer_register: int
    pc_register: int | None
    pc_mask: int
    live_registers: tuple[tuple[int, str], ...]

    def normalize_pc(self, pc: int) -> int:
        """Clear instruction-state marker bits (such as Arm Thumb bit 0)."""
        return pc & self.pc_mask


ARM_CONVENTION = RegisterConvention(
    family="arm",
    sp_register=13,
    return_register=14,
    frame_pointer_register=7,
    pc_register=15,
    pc_mask=~1,
    live_registers=(
        *((register, f"r{register}") for register in range(13)),
        (13, "sp"),
        (14, "lr"),
        (15, "pc"),
    ),
)

RISCV_CONVENTION = RegisterConvention(
    family="riscv",
    sp_register=2,
    return_register=1,
    frame_pointer_register=8,
    pc_register=None,
    pc_mask=-1,
    live_registers=(
        (1, "ra"),
        (2, "sp"),
        (8, "s0"),
        (9, "s1"),
        (18, "s2"),
        (19, "s3"),
        (20, "s4"),
        (21, "s5"),
        (22, "s6"),
        (23, "s7"),
        (24, "s8"),
        (25, "s9"),
        (26, "s10"),
        (27, "s11"),
    ),
)


def convention_for_arch(arch: str) -> RegisterConvention:
    """Return the register convention for an architecture family or ISA name."""
    if arch.startswith("riscv"):
        return RISCV_CONVENTION
    return ARM_CONVENTION


def file_attributes(
    blob: bytes, vendor: bytes, endian: Endian = "little"
) -> dict[int, int | str] | None:
    """Read file-scoped attributes; reject truncation and conflicting values."""
    if not blob or len(blob) > MAX_ATTRIBUTE_BYTES or blob[:1] != b"A":
        return None
    result: dict[int, int | str] = {}
    found = False
    index = 1
    while index < len(blob):
        if index + 4 > len(blob):
            return None
        length = int.from_bytes(blob[index : index + 4], endian)
        end = index + length
        if length < 5 or end > len(blob):
            return None
        name_end = blob.find(b"\0", index + 4, end)
        if name_end < 0:
            return None
        if blob[index + 4 : name_end] == vendor:
            cursor = name_end + 1
            while cursor < end:
                subsection_start = cursor
                decoded = _read_uleb(blob, cursor, end)
                if decoded is None:
                    return None
                subsection_tag, cursor = decoded
                if cursor + 4 > end:
                    return None
                subsection_end = subsection_start + int.from_bytes(
                    blob[cursor : cursor + 4], endian
                )
                cursor += 4
                if subsection_end < cursor or subsection_end > end:
                    return None
                if subsection_tag == 1:
                    found = True
                    if not _parse_file_attribute_values(
                        blob, cursor, subsection_end, vendor, endian, result
                    ):
                        return None
                cursor = subsection_end
        index = end
    return result if found else None


def _parse_file_attribute_values(
    blob: bytes,
    cursor: int,
    end: int,
    vendor: bytes,
    endian: Endian,
    result: dict[int, int | str],
) -> bool:
    """Parse one file-scope attribute list, including nested Tag_File lists."""
    while cursor < end:
        item_start = cursor
        decoded = _read_uleb(blob, cursor, end)
        if decoded is None:
            return False
        tag, cursor = decoded
        if vendor == b"aeabi" and tag == 64:
            continue
        if vendor == b"aeabi" and tag in (1, 2, 3):
            if cursor + 4 > end:
                return False
            item_end = item_start + int.from_bytes(blob[cursor : cursor + 4], endian)
            cursor += 4
            if item_end < cursor or item_end > end:
                return False
            if item_end == cursor:
                return False
            if not _parse_file_attribute_values(
                blob, cursor, item_end, vendor, endian, result
            ):
                return False
            cursor = item_end
            continue

        if vendor == b"aeabi" and tag in (4, 5, 67):
            is_string = True
        elif vendor == b"aeabi" and tag in (32, 65):
            decoded = _read_uleb(blob, cursor, end)
            if decoded is None:
                return False
            _compatibility, cursor = decoded
            is_string = True
        elif vendor == b"aeabi":
            is_string = tag >= 32 and tag % 2 == 1
        else:
            is_string = tag % 2 == 1

        if is_string:
            value_end = blob.find(b"\0", cursor, end)
            if value_end < 0:
                return False
            try:
                value: int | str = blob[cursor:value_end].decode("ascii")
            except UnicodeDecodeError:
                return False
            cursor = value_end + 1
        else:
            decoded = _read_uleb(blob, cursor, end)
            if decoded is None:
                return False
            value, cursor = decoded

        known_tags = (
            {4, 5, 6, 7, *range(8, 29), 32, 48, 50, 52, 65, 66, 67, 68, 72, 74, 76}
            if vendor == b"aeabi"
            else {4, 5, 6, 8, 10, 12}
        )
        if tag not in known_tags:
            continue
        if tag in result:
            previous = result[tag]
            if vendor == b"aeabi" and tag in (4, 5):
                continue
            if vendor == b"aeabi" and tag in (6, 8, 9, 10, 48):
                result[tag] = max(int(previous), int(value))
                continue
            if previous != value:
                return False
        result[tag] = value
    return True


@dataclass(frozen=True)
class Cpu:
    """Image CPU ISA/ABI evidence; a recognized ISA does not prove a save layout."""

    isa: Isa | None
    family: CpuFamily
    reason: str
    abi: str | None = None
    extensions: frozenset[str] = frozenset()

    @property
    def convention(self) -> RegisterConvention | None:
        """Return the register convention when the architecture family is known."""
        if self.family == "unknown":
            return None
        return convention_for_arch(self.family)


def cpu_of(elf: ElfImage) -> Cpu:
    """Classify canonical ISA attributes, rejecting unsupported or conflicting ABI."""
    if elf.machine == EM_ARM:
        attrs = file_attributes(
            elf.section_data(".ARM.attributes") or b"", b"aeabi", elf.endian
        )
        if attrs is None or 6 not in attrs or 7 not in attrs:
            return Cpu(None, "arm", "cpu-evidence-unavailable")
        arch_number = attrs[6]
        if elf.pointer_size != 4 or not isinstance(attrs[7], int):
            return Cpu(None, "arm", "unsupported-cpu")
        if not isinstance(arch_number, int):
            return Cpu(None, "arm", "unsupported-cpu")
        if attrs[7] == ord("M"):
            isa_table: dict[int, Isa] = {
                10: "armv7-m",
                11: "armv6-m",
                12: "armv6-m",
                13: "armv7e-m",
                16: "armv8-m.baseline",
                17: "armv8-m.mainline",
                21: "armv8.1-m.mainline",
            }
            isa = isa_table.get(arch_number)
        elif attrs[7] == ord("A") and arch_number == 10:
            isa = "armv7-a"
        else:
            isa = None
        if isa is None:
            return Cpu(None, "arm", "unsupported-cpu")
        if isa == "armv7-a":
            return Cpu(isa, "arm", "ok", "aapcs32")
        if any(attrs.get(tag, 0) != 0 for tag in (12, 48, 50, 52, 74, 76)):
            return Cpu(isa, "arm", "unsupported-extension-context")
        if attrs.get(10, 0) not in (0, 1, 2, 3, 4, 5, 6, 7, 8):
            return Cpu(isa, "arm", "unsupported-extension-context")
        vfp_args = attrs.get(28, 0)
        if vfp_args not in (0, 1):
            return Cpu(isa, "arm", "unsupported-abi")
        abi = "aapcs32-vfp" if vfp_args == 1 else "aapcs32"
        return Cpu(isa, "arm", "ok", abi)
    if elf.machine != EM_RISCV:
        return Cpu(None, "unknown", "unsupported-cpu")
    attrs = file_attributes(
        elf.section_data(".riscv.attributes") or b"", b"riscv", elf.endian
    )
    if attrs is None:
        return Cpu(None, "riscv", "cpu-evidence-unavailable")
    arch_string = attrs.get(5)
    if not isinstance(arch_string, str):
        return Cpu(None, "riscv", "cpu-evidence-unavailable")
    match = re.fullmatch(
        r"rv(32|64)(i|e)([a-z]*)(?:[0-9]+p[0-9]+)?((?:_[a-z][a-z0-9]*)*)",
        arch_string,
    )
    if match is None:
        return Cpu(None, "riscv", "unsupported-cpu")
    xlen = int(match[1])
    isa = "riscv32" if xlen == 32 else "riscv64"
    extensions = frozenset(
        re.sub(r"[0-9]+p[0-9]+$", "", token)
        for token in (match[2], *match[3], *match[4].lstrip("_").split("_"))
        if token
    )
    if xlen != elf.pointer_size * 8 or bool(elf.flags & 8) != (match[2] == "e"):
        return Cpu(isa, "riscv", "cpu-evidence-conflict")
    if elf.endian != "little" or elf.flags & ~7 or match[2] != "i":
        return Cpu(isa, "riscv", "unsupported-abi", extensions=extensions)
    float_abi = {0: "", 2: "f", 4: "d", 6: "q"}[elf.flags & 6]
    if float_abi and float_abi not in extensions:
        return Cpu(isa, "riscv", "cpu-evidence-conflict")
    if elf.flags & 1 and "c" not in extensions:
        return Cpu(isa, "riscv", "cpu-evidence-conflict")
    allowed = {
        "i",
        "m",
        "a",
        "c",
        "f",
        "d",
        "zicsr",
        "zifencei",
        "zmmul",
        "zaamo",
        "zalrsc",
        "zca",
        "zcd",
        "zcf",
    }
    if extensions - allowed or attrs.get(4, 16) != 16:
        return Cpu(isa, "riscv", "unsupported-extension-context", extensions=extensions)
    return Cpu(
        isa, "riscv", "ok", ("ilp32" if xlen == 32 else "lp64") + float_abi, extensions
    )
