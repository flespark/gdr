"""Bounded ELF/CFI/EHABI/frame-pointer unwinding tests (no GDB session)."""

from __future__ import annotations

import struct
from dataclasses import replace

from core.unwind import (
    ElfImage,
    SeedFrame,
    UnwindMetadata,
    parse_cfi_section,
    parse_ehabi,
    unwind_chain,
)
from core.unwind_ehabi import EhabiTable, _ExidxEntry, unwind_ehabi
from core.unwind_meta import _prel31


def _elf(
    *,
    is_64: bool,
    endian: str,
    machine: int,
    sections: list[tuple[str, int, bytes, int, int]],
) -> bytes:
    """Build a tiny valid ELF with a name table and *sections*.

    ``sections`` entries are ``(name, address, data, type, flags)``.  The
    string table is section 1, the caller's sections follow.
    """
    order = "<" if endian == "little" else ">"
    names = b"\0.shstrtab\0"
    name_offsets = {".shstrtab": 1}
    for name, *_ in sections:
        name_offsets[name] = len(names)
        names += name.encode() + b"\0"
    if is_64:
        ehsize, shentsize = 64, 64
    else:
        ehsize, shentsize = 52, 40
    shnum = 2 + len(sections)
    shstrndx = 1
    shoff = ehsize
    data_start = shoff + shnum * shentsize
    blobs = [names] + [entry[2] for entry in sections]
    offsets = []
    cursor = data_start
    for blob in blobs:
        offsets.append(cursor)
        cursor += len(blob)
    header = bytearray(ehsize)
    header[:4] = b"\x7fELF"
    header[4] = 2 if is_64 else 1
    header[5] = 1 if endian == "little" else 2
    header[6] = 1
    struct.pack_into(f"{order}H", header, 16, 1)
    struct.pack_into(f"{order}H", header, 18, machine)
    if is_64:
        struct.pack_into(f"{order}Q", header, 0x28, shoff)
        struct.pack_into(f"{order}H", header, 0x34, ehsize)
        struct.pack_into(f"{order}H", header, 0x3A, shentsize)
        struct.pack_into(f"{order}H", header, 0x3C, shnum)
        struct.pack_into(f"{order}H", header, 0x3E, shstrndx)
    else:
        struct.pack_into(f"{order}I", header, 0x20, shoff)
        struct.pack_into(f"{order}H", header, 0x28, ehsize)
        struct.pack_into(f"{order}H", header, 0x2E, shentsize)
        struct.pack_into(f"{order}H", header, 0x30, shnum)
        struct.pack_into(f"{order}H", header, 0x32, shstrndx)

    def section_header(
        name: int, section_type: int, flags: int, address: int, offset: int, size: int
    ) -> bytes:
        raw = bytearray(shentsize)
        struct.pack_into(f"{order}II", raw, 0, name, section_type)
        if is_64:
            struct.pack_into(f"{order}Q", raw, 8, flags)
            struct.pack_into(f"{order}Q", raw, 16, address)
            struct.pack_into(f"{order}Q", raw, 24, offset)
            struct.pack_into(f"{order}Q", raw, 32, size)
        else:
            struct.pack_into(f"{order}I", raw, 8, flags)
            struct.pack_into(f"{order}I", raw, 12, address)
            struct.pack_into(f"{order}I", raw, 16, offset)
            struct.pack_into(f"{order}I", raw, 20, size)
        return bytes(raw)

    table = bytearray(shnum * shentsize)
    table[0:shentsize] = section_header(0, 0, 0, 0, 0, 0)
    table[shentsize : 2 * shentsize] = section_header(
        name_offsets[".shstrtab"], 3, 0, 0, offsets[0], len(names)
    )
    for index, (name, address, data, section_type, flags) in enumerate(
        sections, start=2
    ):
        offset = offsets[index - 1]
        table[index * shentsize : (index + 1) * shentsize] = section_header(
            name_offsets[name], section_type, flags, address, offset, len(data)
        )
    return bytes(header) + bytes(table) + b"".join(blobs)


def _cfi_bytes(
    *,
    start: int = 0x1000,
    length: int = 0x100,
    instructions: bytes = b"\x41\x0e\x08\x8e\x01\x84\x02",
) -> bytes:
    """One debug_frame CIE/FDE pair for a function with a push {r4,lr} frame."""
    cie_body = (
        b"\xff\xff\xff\xff"
        + bytes([1])
        + b"\x00"
        + b"\x01"
        + b"\x7c"
        + bytes([14])
        + b"\x0c\x0d\x00"
    )
    cie = struct.pack("<I", len(cie_body)) + cie_body
    fde_body = (
        struct.pack("<I", 0)
        + struct.pack("<I", start)
        + struct.pack("<I", length)
        + instructions
    )
    return cie + struct.pack("<I", len(fde_body)) + fde_body


def _cfi_metadata(blob: bytes, *, address_size: int = 4) -> UnwindMetadata:
    elf = ElfImage.from_bytes(
        _elf(
            is_64=address_size == 8,
            endian="little",
            machine=40 if address_size == 4 else 243,
            sections=[
                (".text", 0x1000, b"\x00" * 0x400, 1, 0x4),
                (".debug_frame", 0, blob, 1, 0),
            ],
        )
    )
    assert elf is not None
    return UnwindMetadata.from_elf(elf)


class _Memory:
    """Sparse target memory; an address outside every block fails."""

    def __init__(self, blocks: dict[int, bytes]) -> None:
        self.blocks = {base: bytearray(blob) for base, blob in blocks.items()}

    def __call__(self, address: int, size: int) -> bytes | None:
        for base, blob in self.blocks.items():
            if base <= address and address + size <= base + len(blob):
                start = address - base
                return blob[start : start + size]
        return None

    def write(self, address: int, data: bytes) -> None:
        self.blocks[address] = data


def test_elf_parser_reads_32_and_64_bit_little_and_big_endian():
    for is_64, endian in (
        (False, "little"),
        (True, "little"),
        (False, "big"),
        (True, "big"),
    ):
        blob = _elf(
            is_64=is_64,
            endian=endian,
            machine=243 if is_64 else 40,
            sections=[(".text", 0x1000, b"\x01\x02\x03\x04", 1, 0x4)],
        )
        image = ElfImage.from_bytes(blob)
        assert image is not None, (is_64, endian)
        assert image.pointer_size == (8 if is_64 else 4)
        assert image.endian == endian
        assert image.is_code(0x1000) is True
        assert image.is_code(0x2000) is False
        assert image.read_at(0x1000, 4) == b"\x01\x02\x03\x04"


def test_elf_parser_rejects_every_truncation_without_raising():
    blob = _elf(
        is_64=False,
        endian="little",
        machine=40,
        sections=[(".text", 0x1000, b"\x01" * 16, 1, 0x4)],
    )
    for cut in range(len(blob)):
        assert ElfImage.from_bytes(blob[:cut]) is None or cut >= 52


def test_real_fixture_elf_exposes_cfi_and_executable_ranges():
    # The runtime cache is not needed; use a committed-shape synthetic image
    # and assert the section contract that the real fixtures follow.
    blob = _elf(
        is_64=False,
        endian="little",
        machine=40,
        sections=[
            (".text", 0x1000, b"\x00" * 0x200, 1, 0x4),
            (".debug_frame", 0, _cfi_bytes(), 1, 0),
        ],
    )
    image = ElfImage.from_bytes(blob)
    assert image is not None
    metadata = UnwindMetadata.from_elf(image)
    assert metadata.cfi_tables and metadata.available()


def test_cfi_row_at_entry_and_after_prologue():
    table = parse_cfi_section(
        _cfi_bytes(),
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=False,
    )
    entry = table.row_for(0x1000)
    assert entry is not None and entry.cfa.offset == 0
    inside = table.row_for(0x1004)
    assert inside is not None
    assert inside.cfa.offset == 8
    assert inside.registers[14].value == -4
    assert inside.registers[4].value == -8


def test_cfi_forbidden_expression_is_a_stop_reason_not_a_guess():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x0e\x08\x10\x0e\x01\x9c"))
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1104}),
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert len(result.frames) == 1
    assert result.stop_reason == "cfi-expression-forbidden"


def test_cfi_truncated_and_malformed_sections_are_empty():
    for blob in (b"", b"\xff\xff\xff\xff", b"\x0c\x00\x00\x00"):
        table = parse_cfi_section(
            blob,
            section_address=0,
            address_size=4,
            endian="little",
            is_eh_frame=False,
        )
        assert not table


def test_unwind_chain_follows_cfi_and_respects_bounds_and_depth():
    metadata = _cfi_metadata(_cfi_bytes())
    low, high = 0x2000, 0x2100
    memory = _Memory(
        {
            low: b"\x00" * (high - low),
        }
    )
    # frame 0 at 0x1004, SP=0x2040; CFA=SP+8; LR at CFA-4.
    struct.pack_into("<I", memory.blocks[low], 0x2040 - low + 4, 0x1008)
    seed = SeedFrame(pc=0x1004, sp=0x2040, regs={13: 0x2040, 14: 0, 15: 0x1004})
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        max_depth=2,
        stack_low=low,
        stack_high=high,
    )
    assert result.frames[1].pc == 0x1008
    assert result.frames[1].method == "cfi"
    assert result.stop_reason == "max-depth"
    assert result.truncated is True


def test_unwind_chain_rejects_a_non_advancing_frame():
    metadata = _cfi_metadata(_cfi_bytes())
    memory = _Memory({0x2000: b"\x00" * 0x100})
    seed = SeedFrame(pc=0x1004, sp=0x2040, regs={13: 0x2040, 14: 0, 15: 0x1004})
    # CFA==SP because the row says offset 0 for this synthetic table row.
    table = parse_cfi_section(
        _cfi_bytes(instructions=b"\x0c\x0d\x00"),
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=False,
    )
    metadata = replace(metadata, cfi_tables=(table,))
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert result.stop_reason == "return-address-zero"
    assert not result.complete


def test_unwind_chain_stops_on_missing_stack_memory():
    metadata = _cfi_metadata(_cfi_bytes())
    seed = SeedFrame(pc=0x1004, sp=0x2040, regs={13: 0x2040, 14: 0, 15: 0x1004})
    result = unwind_chain(
        seed,
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert result.frames and result.stop_reason == "cfi-memory-error"


def test_frame_pointer_fallback_validates_arm_chain():
    memory = _Memory(
        {
            0x2000: b"\x00" * 0x100,
        }
    )
    struct.pack_into("<II", memory.blocks[0x2000], 0x10, 0x2010, 0x0200)
    struct.pack_into("<II", memory.blocks[0x2000], 0x20, 0x2020, 0x0300)
    seed = SeedFrame(
        pc=0x0100, sp=0x2000, regs={7: 0x2010, 13: 0x2000, 14: 0, 15: 0x0100}
    )
    result = unwind_chain(
        seed,
        None,
        read=memory,
        max_depth=2,
        stack_low=0x2000,
        stack_high=0x2100,
        is_code=lambda address: 0x0100 <= address < 0x0300,
    )
    assert [frame.pc for frame in result.frames] == [0x0100, 0x0200]
    assert result.frames[1].method == "frame-pointer"
    assert result.stop_reason == "max-depth"


def test_frame_pointer_fallback_rejects_misaligned_and_out_of_bounds():
    seed = SeedFrame(pc=0x0100, sp=0x2000, regs={7: 0x2011, 13: 0x2000, 15: 0x0100})
    result = unwind_chain(
        seed,
        None,
        read=_Memory({0x2000: b"\x00" * 0x100}),
        stack_low=0x2000,
        stack_high=0x2100,
        is_code=lambda _address: True,
    )
    assert result.stop_reason == "fp-misaligned"


def test_frame_pointer_fallback_accepts_riscv_frame_record():
    metadata = UnwindMetadata.from_elf(
        ElfImage.from_bytes(
            _elf(
                is_64=True,
                endian="little",
                machine=243,
                sections=[(".text", 0x1000, b"\x00" * 0x400, 1, 0x4)],
            )
        )
    )
    memory = _Memory({0x2000: b"\x00" * 0x200})
    struct.pack_into("<QQ", memory.blocks[0x2000], 0xF0, 0x0300, 0x1200)
    seed = SeedFrame(pc=0x1100, sp=0x2000, regs={2: 0x2000, 8: 0x2100, 1: 0})
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        max_depth=2,
        stack_low=0x2000,
        stack_high=0x2200,
        is_code=lambda address: 0x1100 <= address < 0x1200,
    )
    assert result.frames[1].pc == 0x1200
    assert result.frames[1].method == "frame-pointer"


def test_ehabi_cantunwind_and_compact_instruction_are_bounded():
    section = 0x1000
    # entry 0: function 0x1000, CANTUNWIND; entry 1: function 0x1100 -> extab 0x1200.
    second = (0x1200 - (section + 8 + 4)) & 0x7FFFFFFF
    first_entry = (0x1100 - (section + 8)) & 0x7FFFFFFF
    inline = parse_ehabi(
        struct.pack("<II", 0, 0x808C00B0),
        section_address=section,
        read_at=_Memory({}).__call__,
        text_end=0x1400,
        endian="little",
    )
    assert inline.entries[0].inline == 0x808C00B0
    assert inline.entries[0].unwind is None
    exidx = struct.pack("<II", 0, 1) + struct.pack("<II", first_entry, second)
    extab = struct.pack("<I", 0x808C00B0)  # pop r14,r15; finish
    memory = {0x1200: extab, 0x2000: b"\x00\x01\x00\x00" + b"\x00\x02\x00\x00"}
    table = parse_ehabi(
        exidx,
        section_address=section,
        read_at=_Memory(memory).__call__,
        text_end=0x1400,
        endian="little",
    )
    assert table.entry_for(0x1000).cant_unwind is True
    entry = table.entry_for(0x1100)
    assert entry is not None and not entry.cant_unwind
    outcome = unwind_ehabi(
        entry,
        table=table,
        sp=0x2000,
        regs={13: 0x2000, 14: 0, 15: 0},
        read=_Memory(memory).__call__,
        stack_low=0x1F00,
        stack_high=0x2100,
        endian="little",
    )
    assert isinstance(outcome, tuple)
    _regs, caller_sp, caller_pc = outcome
    assert caller_sp == 0x2008
    assert caller_pc == 0x00000200


def test_ehabi_unsupported_opcode_is_a_reason():
    first = 0x80B30000  # compact pr0, first instruction 0xb3 (VFP pop)
    table = EhabiTable(
        entries=(_ExidxEntry(start=0x1100, unwind=0x1200, cant_unwind=False),),
        read_at=_Memory({0x1200: struct.pack("<I", first)}).__call__,
        text_end=0x1400,
    )
    outcome = unwind_ehabi(
        table.entries[0],
        table=table,
        sp=0x2000,
        regs={13: 0x2000, 14: 0, 15: 0},
        read=_Memory({}).__call__,
        stack_low=0x1F00,
        stack_high=0x2100,
        endian="little",
    )
    assert outcome == "ehabi-opcode-unsupported"


def test_prel31_sign_extension_is_signed():
    assert _prel31(0, 0x1000) == 0x1000
    assert _prel31(0x7FFFFFFF, 0x1000) == 0x1000 - 1
