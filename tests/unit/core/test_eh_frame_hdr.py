"""Unit coverage for PT_GNU_EH_FRAME, .eh_frame_hdr, and binary-search CFI."""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from core.backtrace import unwind_cfi as cfi
from core.backtrace.unwind import UnwindMetadata
from core.elf import (
    DW_EH_PE_DATAREL,
    DW_EH_PE_OMIT,
    DW_EH_PE_PCREL,
    DW_EH_PE_SDATA4,
    DW_EH_PE_UDATA4,
    DW_EH_PE_ULEB128,
    EM_ARM,
    EM_RISCV,
    PT_GNU_EH_FRAME,
    ElfImage,
)
from tests.unit.core.oracle_support import (
    SHF_ALLOC,
    SHF_EXECINSTR,
    SHT_PROGBITS,
    SectionSpec,
    build_elf,
)


def _eh_frame_with_functions(
    functions: list[tuple[int, int]],
    *,
    eh_address: int = 0x8000,
    endian: str = "little",
    dwarf64_cie: bool = False,
) -> tuple[bytes, list[tuple[int, int]]]:
    """Build a valid `.eh_frame` plus `(start, fde_vaddr)` pairs."""
    order = "<" if endian == "little" else ">"
    cie_body = (
        (b"\x00" * 8 if dwarf64_cie else b"\x00" * 4)
        + b"\x01zR\x00\x01\x7c\x0e\x01"
        + bytes([DW_EH_PE_PCREL | DW_EH_PE_SDATA4])
        + b"\x0c\x0d\x00"
    )
    if dwarf64_cie:
        cie = struct.pack(f"{order}IQ", 0xFFFFFFFF, len(cie_body)) + cie_body
    else:
        cie = struct.pack(f"{order}I", len(cie_body)) + cie_body
    blob = bytearray(cie)
    pairs: list[tuple[int, int]] = []
    for index, (start, size) in enumerate(functions):
        fde_offset = len(blob)
        id_pos = fde_offset + 4
        pc_pos = id_pos + 4
        cfa_offset = 8 + (index % 7) * 4
        body = (
            struct.pack(f"{order}I", id_pos)
            + struct.pack(f"{order}i", start - (eh_address + pc_pos))
            + struct.pack(f"{order}I", size)
            + bytes([0x00, 0x0E, cfa_offset])
        )
        blob.extend(struct.pack(f"{order}I", len(body)) + body)
        pairs.append((start, eh_address + fde_offset))
    return bytes(blob), pairs


def _build_eh_frame_hdr(
    *,
    hdr_address: int,
    eh_address: int,
    pairs: list[tuple[int, int]],
    endian: str = "little",
    version: int = 1,
    ptr_enc: int = DW_EH_PE_PCREL | DW_EH_PE_SDATA4,
    count_enc: int = DW_EH_PE_UDATA4,
    table_enc: int = DW_EH_PE_DATAREL | DW_EH_PE_SDATA4,
    fde_count_override: int | None = None,
) -> bytes:
    order = "<" if endian == "little" else ">"
    out = bytearray([version, ptr_enc, count_enc, table_enc])
    if ptr_enc != DW_EH_PE_OMIT:
        if ptr_enc & 0x70 == DW_EH_PE_PCREL:
            out.extend(struct.pack(f"{order}i", eh_address - (hdr_address + 4)))
        elif ptr_enc & 0x70 == DW_EH_PE_DATAREL:
            out.extend(struct.pack(f"{order}i", eh_address - hdr_address))
        else:
            out.extend(struct.pack(f"{order}I", eh_address))
    if count_enc != DW_EH_PE_OMIT:
        count = len(pairs) if fde_count_override is None else fde_count_override
        out.extend(struct.pack(f"{order}I", count))
    if table_enc == (DW_EH_PE_DATAREL | DW_EH_PE_SDATA4):
        for start, fde_addr in pairs:
            out.extend(
                struct.pack(
                    f"{order}ii",
                    start - hdr_address,
                    fde_addr - hdr_address,
                )
            )
    elif table_enc == (DW_EH_PE_PCREL | DW_EH_PE_SDATA4):
        for start, fde_addr in pairs:
            pos0 = hdr_address + len(out)
            out.extend(struct.pack(f"{order}i", start - pos0))
            pos1 = hdr_address + len(out)
            out.extend(struct.pack(f"{order}i", fde_addr - pos1))
    elif table_enc == DW_EH_PE_ULEB128:

        def uleb(v: int) -> bytes:
            b = bytearray()
            while True:
                byte = v & 0x7F
                v >>= 7
                b.append(byte | (0x80 if v else 0))
                if not v:
                    return bytes(b)

        for start, fde_addr in pairs:
            out.extend(uleb(start) + uleb(fde_addr))
    return bytes(out)


def _with_pt_gnu_eh_frame(
    elf_bytes: bytes,
    *,
    is_64: bool,
    endian: str,
    phdrs: list[tuple[int, int, int, int, int]],
) -> bytes:
    """Append ELF program headers ``(p_type, p_offset, p_vaddr, p_filesz, p_memsz)``."""
    order = "<" if endian == "little" else ">"
    data = bytearray(elf_bytes)
    phoff = len(data)
    phentsize = 56 if is_64 else 32
    for p_type, p_offset, p_vaddr, p_filesz, p_memsz in phdrs:
        if is_64:
            data.extend(
                struct.pack(
                    f"{order}IIQQQQQQ",
                    p_type,
                    4,
                    p_offset,
                    p_vaddr,
                    p_vaddr,
                    p_filesz,
                    p_memsz,
                    4,
                )
            )
        else:
            data.extend(
                struct.pack(
                    f"{order}IIIIIIII",
                    p_type,
                    p_offset,
                    p_vaddr,
                    p_vaddr,
                    p_filesz,
                    p_memsz,
                    4,
                    4,
                )
            )
    if is_64:
        struct.pack_into(f"{order}Q", data, 0x20, phoff)
        struct.pack_into(f"{order}HH", data, 0x36, phentsize, len(phdrs))
    else:
        struct.pack_into(f"{order}I", data, 0x1C, phoff)
        struct.pack_into(f"{order}HH", data, 0x2A, phentsize, len(phdrs))
    return bytes(data)


@pytest.mark.parametrize(
    "is_64,endian,table_enc",
    [
        (False, "little", DW_EH_PE_DATAREL | DW_EH_PE_SDATA4),
        (False, "big", DW_EH_PE_DATAREL | DW_EH_PE_SDATA4),
        (True, "little", DW_EH_PE_DATAREL | DW_EH_PE_SDATA4),
        (False, "little", DW_EH_PE_PCREL | DW_EH_PE_SDATA4),
        (False, "little", DW_EH_PE_ULEB128),
    ],
)
def test_pt_gnu_eh_frame_and_eh_frame_hdr_dispatch(is_64, endian, table_enc):
    eh_data, pairs = _eh_frame_with_functions(
        [(0x1000, 0x40), (0x1040, 0x30), (0x1100, 0x50)],
        eh_address=0x8000,
        endian=endian,
    )
    hdr_addr = 0x7000
    hdr_data = _build_eh_frame_hdr(
        hdr_address=hdr_addr,
        eh_address=0x8000,
        pairs=pairs,
        endian=endian,
        table_enc=table_enc,
    )
    blob = build_elf(
        is_64=is_64,
        endian=endian,
        machine=EM_RISCV if is_64 else EM_ARM,
        sections=(
            SectionSpec(
                ".text",
                0x1000,
                b"\x00" * 0x200,
                SHT_PROGBITS,
                SHF_ALLOC | SHF_EXECINSTR,
            ),
            SectionSpec(".eh_frame_hdr", hdr_addr, hdr_data, SHT_PROGBITS, SHF_ALLOC),
            SectionSpec(".eh_frame", 0x8000, eh_data, SHT_PROGBITS, SHF_ALLOC),
        ),
    )
    hdr_offset = blob.index(hdr_data)
    with_phdr = _with_pt_gnu_eh_frame(
        blob,
        is_64=is_64,
        endian=endian,
        phdrs=[(PT_GNU_EH_FRAME, hdr_offset, hdr_addr, len(hdr_data), len(hdr_data))],
    )
    image = ElfImage.from_bytes(with_phdr)
    assert image is not None
    assert image.gnu_eh_frame == (hdr_addr, hdr_data)
    assert image.gnu_eh_frame_error is None

    metadata = UnwindMetadata.from_elf(image)
    table = metadata.cfi_tables[0]
    assert bool(table) is True
    assert table.indexed and table.error is None and table.index_error is None
    assert table._fdes == ()
    assert table.row_for(0x0900) is None
    assert table.row_for(0x1070) is None
    assert table.row_for(0x1044).cfa.offset == 12
    assert table.row_for(0x1108).cfa.offset == 16
    assert table.row_for(0x1004).cfa.offset == 8
    assert table.indexed and table._fdes == ()


def test_pt_gnu_eh_frame_validation_rejections_degrade_safely():
    eh_data, pairs = _eh_frame_with_functions([(0x1000, 0x40)], eh_address=0x8000)
    hdr_data = _build_eh_frame_hdr(hdr_address=0x7000, eh_address=0x8000, pairs=pairs)
    base = build_elf(
        is_64=False,
        endian="little",
        machine=EM_ARM,
        sections=(
            SectionSpec(
                ".text", 0x1000, b"\x00" * 0x80, SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR
            ),
            SectionSpec(".eh_frame_hdr", 0x7000, hdr_data, SHT_PROGBITS, SHF_ALLOC),
            SectionSpec(".eh_frame", 0x8000, eh_data, SHT_PROGBITS, SHF_ALLOC),
        ),
    )
    hdr_offset = base.index(hdr_data)

    dup = _with_pt_gnu_eh_frame(
        base,
        is_64=False,
        endian="little",
        phdrs=[
            (PT_GNU_EH_FRAME, hdr_offset, 0x7000, len(hdr_data), len(hdr_data)),
            (PT_GNU_EH_FRAME, hdr_offset, 0x7000, len(hdr_data), len(hdr_data)),
        ],
    )
    dup_img = ElfImage.from_bytes(dup)
    assert dup_img is not None and dup_img.gnu_eh_frame_error == "eh-hdr-duplicate-phdr"
    dup_table = UnwindMetadata.from_elf(dup_img).cfi_tables[0]
    assert not dup_table.indexed and dup_table.index_error == "eh-hdr-duplicate-phdr"
    assert dup_table.error is None and dup_table.row_for(0x1010).cfa.offset == 8

    trunc_phdr = _with_pt_gnu_eh_frame(
        base,
        is_64=False,
        endian="little",
        phdrs=[
            (PT_GNU_EH_FRAME, hdr_offset, 0x7000, len(hdr_data) + 99999, len(hdr_data))
        ],
    )
    trunc_img = ElfImage.from_bytes(trunc_phdr)
    assert (
        trunc_img is not None
        and trunc_img.gnu_eh_frame_error == "eh-hdr-truncated-phdr"
    )

    bad_table_phdr = bytearray(trunc_phdr)
    struct.pack_into("<H", bad_table_phdr, 0x2A, 8)
    malformed_img = ElfImage.from_bytes(bytes(bad_table_phdr))
    assert (
        malformed_img is not None
        and malformed_img.gnu_eh_frame_error == "eh-hdr-malformed-phdr"
    )

    valid = ElfImage.from_bytes(
        _with_pt_gnu_eh_frame(
            base,
            is_64=False,
            endian="little",
            phdrs=[(PT_GNU_EH_FRAME, hdr_offset, 0x7000, len(hdr_data), len(hdr_data))],
        )
    )
    assert valid is not None
    mismatched = replace(valid, gnu_eh_frame=(0x7004, hdr_data))
    mm_table = UnwindMetadata.from_elf(mismatched).cfi_tables[0]
    assert mm_table.index_error == "eh-hdr-program-section-mismatch"
    assert mm_table.error is None and mm_table.row_for(0x1010).cfa.offset == 8


@pytest.mark.parametrize(
    "modifier,expected",
    [
        (lambda _eh, _pairs: b"\x01\x1b\x03", "eh-hdr-truncated"),
        (
            lambda _eh, pairs: _build_eh_frame_hdr(
                hdr_address=0x7000, eh_address=0x8000, pairs=pairs, version=2
            ),
            "eh-hdr-version-unsupported",
        ),
        (
            lambda _eh, pairs: _build_eh_frame_hdr(
                hdr_address=0x7000, eh_address=0x9000, pairs=pairs
            ),
            "eh-hdr-pointer-mismatch",
        ),
        (
            lambda _eh, _pairs: _build_eh_frame_hdr(
                hdr_address=0x7000, eh_address=0x8000, pairs=[], count_enc=DW_EH_PE_OMIT
            ),
            "eh-hdr-no-table",
        ),
        (
            lambda _eh, _pairs: _build_eh_frame_hdr(
                hdr_address=0x7000, eh_address=0x8000, pairs=[], table_enc=DW_EH_PE_OMIT
            ),
            "eh-hdr-no-table",
        ),
        (
            lambda _eh, pairs: _build_eh_frame_hdr(
                hdr_address=0x7000,
                eh_address=0x8000,
                pairs=pairs,
                fde_count_override=100000,
            ),
            "eh-hdr-record-limit",
        ),
        (
            lambda _eh, pairs: _build_eh_frame_hdr(
                hdr_address=0x7000, eh_address=0x8000, pairs=list(reversed(pairs))
            ),
            "eh-hdr-unsorted",
        ),
        (
            lambda eh, pairs: _build_eh_frame_hdr(
                hdr_address=0x7000,
                eh_address=0x8000,
                pairs=[(pairs[0][0], 0x8000 + len(eh)), pairs[1]],
            ),
            "eh-hdr-fde-out-of-range",
        ),
    ],
)
def test_parse_eh_frame_hdr_rejects_corrupt_indexes_and_falls_back(modifier, expected):
    eh_data, pairs = _eh_frame_with_functions(
        [(0x1000, 0x20), (0x1020, 0x20)], eh_address=0x8000
    )
    bad_hdr = modifier(eh_data, pairs)
    table = cfi.parse_cfi_section(
        eh_data,
        section_address=0x8000,
        address_size=4,
        endian="little",
        is_eh_frame=True,
        eh_frame_hdr=(0x7000, bad_hdr),
    )
    assert not table.indexed
    assert table.index_error == expected
    assert table.error is None
    assert table.row_for(0x1004).cfa.offset == 8
    assert table.row_for(0x1024).cfa.offset == 12


def test_indexed_fde_mismatch_and_bad_offset_degrade_to_full_parse():
    eh_data, pairs = _eh_frame_with_functions(
        [(0x1000, 0x20), (0x1020, 0x20)], eh_address=0x8000, dwarf64_cie=True
    )
    # Valid DWARF64 CIE works via index first.
    good_hdr = _build_eh_frame_hdr(hdr_address=0x7000, eh_address=0x8000, pairs=pairs)
    good = cfi.parse_cfi_section(
        eh_data,
        section_address=0x8000,
        address_size=4,
        endian="little",
        is_eh_frame=True,
        eh_frame_hdr=(0x7000, good_hdr),
    )
    assert good.indexed and good.row_for(0x1024).cfa.offset == 12

    # Point first entry at CIE offset instead of FDE -> degrades safely and still works.
    cie_pointing_hdr = _build_eh_frame_hdr(
        hdr_address=0x7000,
        eh_address=0x8000,
        pairs=[(pairs[0][0], 0x8000), pairs[1]],
    )
    degraded = cfi.parse_cfi_section(
        eh_data,
        section_address=0x8000,
        address_size=4,
        endian="little",
        is_eh_frame=True,
        eh_frame_hdr=(0x7000, cie_pointing_hdr),
    )
    assert degraded.indexed
    row = degraded.row_for(0x1004)
    assert row is not None and row.cfa.offset == 8
    assert not degraded.indexed
    assert degraded.index_error == "eh-hdr-fde-is-cie"
    assert degraded.error is None

    # Swap FDE addresses while keeping starts sorted -> eh-hdr-fde-mismatch fallback.
    swapped_hdr = _build_eh_frame_hdr(
        hdr_address=0x7000,
        eh_address=0x8000,
        pairs=[(pairs[0][0], pairs[1][1]), (pairs[1][0], pairs[0][1])],
    )
    degraded_swap = cfi.parse_cfi_section(
        eh_data,
        section_address=0x8000,
        address_size=4,
        endian="little",
        is_eh_frame=True,
        eh_frame_hdr=(0x7000, swapped_hdr),
    )
    assert degraded_swap.row_for(0x1004).cfa.offset == 8
    assert not degraded_swap.indexed
    assert degraded_swap.index_error == "eh-hdr-fde-mismatch"


def test_cfi_table_binary_search_preserves_overlap_and_discarded_fde_semantics():
    rows = [
        (0, 0x2000, cfi.CfiRow(cfa=cfi.CfaRule(register=2, offset=99))),
        (0x1000, 0x1100, cfi.CfiRow(cfa=cfi.CfaRule(register=2, offset=16))),
        (0x1020, 0x1050, cfi.CfiRow(cfa=cfi.CfaRule(register=2, offset=32))),
        (0x1020, 0x1040, cfi.CfiRow(cfa=cfi.CfaRule(register=2, offset=48))),
        (0x1200, 0x1300, cfi.CfiRow(cfa=cfi.CfaRule(register=2, offset=64))),
    ]
    table = cfi.CfiTable.from_rows(rows)
    assert table.row_for(0x0500) is None
    assert table.row_for(0x1010).cfa.offset == 16
    # Highest start address wins when nested; first wins on identical start.
    assert table.row_for(0x1028).cfa.offset == 32
    # Past the nested child, outer FDE [0x1000, 0x1100) still covers 0x1080.
    assert table.row_for(0x1080).cfa.offset == 16
    assert table.row_for(0x1150) is None
    assert table.row_for(0x1210).cfa.offset == 64


def test_large_eh_frame_hdr_table_matches_linear_parse_without_full_decode():
    functions = [(0x10000 + i * 0x40, 0x30) for i in range(5000)]
    eh_data, pairs = _eh_frame_with_functions(functions, eh_address=0x100000)
    hdr_data = _build_eh_frame_hdr(
        hdr_address=0x90000, eh_address=0x100000, pairs=pairs
    )
    indexed = cfi.parse_cfi_section(
        eh_data,
        section_address=0x100000,
        address_size=4,
        endian="little",
        is_eh_frame=True,
        eh_frame_hdr=(0x90000, hdr_data),
    )
    linear = cfi.parse_cfi_section(
        eh_data,
        section_address=0x100000,
        address_size=4,
        endian="little",
        is_eh_frame=True,
    )
    assert indexed.indexed and indexed._fdes == ()
    for sample in (0, 1, 2500, 4998, 4999):
        pc = 0x10000 + sample * 0x40 + 4
        assert indexed.row_for(pc).cfa.offset == linear.row_for(pc).cfa.offset
        assert indexed.row_for(pc + 0x34) is None
    assert len(indexed._indexed_fdes) == 5
