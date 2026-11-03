"""Binary-format regression vectors for the supported DWARF CFI subset."""

from __future__ import annotations

import struct

import pytest

import core.unwind_cfi as cfi
from core.unwind_cfi import CfaRule, CfiRow, RegisterRule


def _run(blob, *, target=None, initial=None, **kwargs):
    row = initial or CfiRow(CfaRule(13, 0))
    return cfi._run_cfi(
        blob,
        row,
        location=0x1000,
        target=target,
        code_alignment=1,
        data_alignment=-4,
        address_size=4,
        endian="little",
        cie_row=row,
        max_steps=kwargs.get("max_steps", 1000),
    )


@pytest.mark.parametrize(
    "blob,register,kind,value",
    [
        (b"\x8e\x02", 14, "offset", -8),
        (b"\x05\x0e\x02", 14, "offset", -8),
        (b"\x05\x80\x01\x03", 128, "offset", -12),
        (b"\x11\x0e\x7e", 14, "offset", 8),
        (b"\x14\x0e\x02", 14, "val_offset", -8),
        (b"\x15\x0e\x7e", 14, "val_offset", 8),
        (b"\x07\x0e", 14, "undefined", 0),
        (b"\x08\x0e", 14, "same", 0),
        (b"\x2f\x0e\x02", 14, "offset", 8),
    ],
)
def test_register_rules_use_the_specified_operand_signedness(
    blob, register, kind, value
):
    row = _run(blob)
    assert row.registers[register].kind == kind
    assert row.registers[register].value == value


@pytest.mark.parametrize(
    "blob,register,offset",
    [
        (b"\x0c\x07\x10", 7, 16),
        (b"\x0d\x07", 7, 0),
        (b"\x0e\x10", 13, 16),
        (b"\x12\x07\x7e", 7, 8),
        (b"\x13\x7d", 13, 12),
    ],
)
def test_cfa_rules_are_distinct_from_register_restore(blob, register, offset):
    row = _run(blob)
    assert (row.cfa.register, row.cfa.offset) == (register, offset)


def test_register_copy_and_restore_and_remember_state():
    initial = CfiRow(CfaRule(13, 4), {14: RegisterRule("offset", -4)})
    assert _run(b"\x09\x04\x05").registers[4].register == 5
    for restore in (b"\xce", b"\x06\x0e"):
        row = _run(b"\x8e\x03" + restore, initial=initial)
        assert row.registers[14] == initial.registers[14]
    for restore in (b"\xc4", b"\x06\x04"):
        assert 4 not in _run(b"\x84\x03" + restore).registers
    row = _run(b"\x0a\x0e\x20\x0b", initial=initial)
    assert row.cfa == initial.cfa


@pytest.mark.parametrize(
    "advance", [b"\x44", b"\x02\x04", b"\x03\x04\x00", b"\x04\x04\x00\x00\x00"]
)
def test_location_advance_changes_only_subsequent_rows(advance):
    blob = advance + b"\x0e\x08"
    assert _run(blob, target=0x1003).cfa.offset == 0
    assert _run(blob, target=0x1004).cfa.offset == 8


@pytest.mark.parametrize(
    "blob,reason",
    [
        (b"\x80", "cfi-truncated-uleb"),
        (b"\x05", "cfi-truncated-uleb"),
        (b"\x05\x80", "cfi-truncated-uleb"),
        (b"\x05\x0e", "cfi-truncated-uleb"),
        (b"\x05\x0e\x80", "cfi-truncated-uleb"),
        (b"\x01", "cfi-truncated-set-loc"),
        (b"\x02", "cfi-truncated-advance"),
        (b"\x03\x01", "cfi-truncated-advance"),
        (b"\x04\x01", "cfi-truncated-advance"),
        (b"\x0b", "cfi-state-stack-underflow"),
        (b"\x07", "cfi-truncated-uleb"),
        (b"\x08", "cfi-truncated-uleb"),
        (b"\x09", "cfi-truncated-uleb"),
        (b"\x09\x04", "cfi-truncated-uleb"),
        (b"\x0c", "cfi-truncated-uleb"),
        (b"\x0c\x0d", "cfi-truncated-uleb"),
        (b"\x0d", "cfi-truncated-uleb"),
        (b"\x0e", "cfi-truncated-uleb"),
        (b"\x11", "cfi-truncated-uleb"),
        (b"\x11\x0e", "cfi-truncated-sleb"),
        (b"\x12", "cfi-truncated-uleb"),
        (b"\x12\x0d", "cfi-truncated-sleb"),
        (b"\x13", "cfi-truncated-sleb"),
        (b"\x06", "cfi-truncated-uleb"),
        (b"\x14", "cfi-truncated-uleb"),
        (b"\x14\x0e", "cfi-truncated-uleb"),
        (b"\x2e", "cfi-truncated-uleb"),
        (b"\x2f", "cfi-truncated-uleb"),
        (b"\x2f\x0e", "cfi-truncated-uleb"),
        (b"\x0f", "cfi-truncated-uleb"),
        (b"\x0f\x01", "cfi-truncated-expression"),
        (b"\x10", "cfi-truncated-uleb"),
        (b"\x10\x0e", "cfi-truncated-uleb"),
        (b"\x10\x0e\x01", "cfi-truncated-expression"),
        (b"\x16", "cfi-truncated-uleb"),
        (b"\x16\x0e\x01", "cfi-truncated-expression"),
        (b"\x17", "cfi-opcode-unsupported"),
    ],
)
def test_malformed_instructions_fail_with_specific_reasons(blob, reason):
    assert _run(blob) == reason


def test_cfi_budgets_and_nops(monkeypatch):
    assert isinstance(_run(b"\x00\x2e\x01"), CfiRow)
    assert _run(b"\x00\x00", max_steps=1) == "cfi-opcode-limit"
    monkeypatch.setattr(cfi, "MAX_STATE_DEPTH", 1)
    assert _run(b"\x0a\x0a") == "cfi-state-stack-limit"
    assert _run(b"", initial=CfiRow()) == "cfi-no-cfa"
    assert _run(b"", initial=CfiRow(CfaRule(error="bad-cfa"))) == "bad-cfa"


def _section(
    *,
    version=4,
    dwarf64=False,
    eh=False,
    endian="little",
    augmentation=b"",
    aug_data=b"",
    instructions=b"\x0e\x08",
):
    width = 8 if dwarf64 else 4
    address_size = 8 if dwarf64 else 4

    def unit(body):
        length = len(body).to_bytes(width, endian)
        return (b"\xff" * 4 if dwarf64 else b"") + length + body

    identifier = 0 if eh else (1 << (8 * width)) - 1
    cie = identifier.to_bytes(width, endian) + bytes([version]) + augmentation + b"\x00"
    if version == 4:
        cie += bytes([address_size, 0])
    cie += b"\x01\x7c\x0e"
    if augmentation.startswith(b"z"):
        cie += bytes([len(aug_data)]) + aug_data
    cie += b"\x0c\x0d\x00"
    cie = unit(cie)
    id_position = len(cie) + (12 if dwarf64 else 4)
    ref = id_position if eh else 0
    body = ref.to_bytes(width, endian)
    body += (0x1000).to_bytes(address_size, endian) + (0x80).to_bytes(
        address_size, endian
    )
    if augmentation.startswith(b"z"):
        body += b"\x00"
    return cie + unit(body + instructions)


@pytest.mark.parametrize("version", [1, 3, 4])
@pytest.mark.parametrize("dwarf64", [False, True])
@pytest.mark.parametrize("endian", ["little", "big"])
def test_debug_frame_versions_and_dwarf64_identifiers(version, dwarf64, endian):
    table = cfi.parse_cfi_section(
        _section(version=version, dwarf64=dwarf64, endian=endian),
        section_address=0,
        address_size=8 if dwarf64 else 4,
        endian=endian,
        is_eh_frame=False,
    )
    assert table and table.error is None
    assert table.address_size == (8 if dwarf64 else 4)
    assert table.endian == endian
    assert table.row_for(0x1004).cfa.offset == 8
    assert table.row_for(0x2000) is None


def test_eh_frame_augmentation_and_pcrel_fde():
    cie_body = b"\x00" * 4 + b"\x01zR\x00\x01\x7c\x0e\x01\x1b\x0c\x0d\x00"
    cie = struct.pack("<I", len(cie_body)) + cie_body
    pointer_position = len(cie) + 8
    body = struct.pack("<IiI", len(cie) + 4, 0x1000 - (0x4000 + pointer_position), 0x80)
    body += b"\x00\x0e\x08"
    table = cfi.parse_cfi_section(
        cie + struct.pack("<I", len(body)) + body,
        section_address=0x4000,
        address_size=4,
        endian="little",
        is_eh_frame=True,
    )
    assert table.row_for(0x1004).cfa.offset == 8


def _eh_section(
    *,
    encoding,
    start,
    length,
    address_size=4,
    signed_start=False,
    instructions=b"\x0e\x08",
):
    cie_body = (
        b"\x00" * 4
        + b"\x03"
        + b"zR\x00"
        + b"\x01"
        + b"\x7c"
        + b"\x0e"
        + b"\x01"
        + bytes([encoding])
        + b"\x0c\x0d\x00"
    )
    cie = struct.pack("<I", len(cie_body)) + cie_body
    fde_id_offset = len(cie) + 4
    body = struct.pack("<I", fde_id_offset)
    body += int(start).to_bytes(address_size, "little", signed=signed_start)
    body += int(length).to_bytes(address_size, "little")
    body += b"\x00" + instructions
    return cie + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize(
    "address_size,encoding,start,length,signed_start,error",
    [
        (4, 0x03, 0xFFFFFF80, 0x80, False, None),
        (4, 0x03, 0xFFFFFF80, 0x81, False, "cfi-fde-range-invalid"),
        (4, 0x03, 0xFFFFFFFF, 1, False, None),
        (4, 0x03, 0xFFFFFFFF, 2, False, "cfi-fde-range-invalid"),
        (4, 0x0B, -8, 1, True, "cfi-fde-pointer-unavailable"),
        (8, 0x04, 2**64 - 0x80, 0x80, False, None),
        (8, 0x04, 2**64 - 0x80, 0x81, False, "cfi-fde-range-invalid"),
        (8, 0x0C, -8, 1, True, "cfi-fde-pointer-unavailable"),
    ],
)
def test_fde_start_and_range_stay_inside_the_address_space(
    address_size, encoding, start, length, signed_start, error
):
    blob = _eh_section(
        encoding=encoding,
        start=start,
        length=length,
        address_size=address_size,
        signed_start=signed_start,
    )
    table = cfi.parse_cfi_section(
        blob,
        section_address=0,
        address_size=address_size,
        endian="little",
        is_eh_frame=True,
    )
    if error is not None:
        assert table.error == error
        return
    assert table.error is None
    assert table.row_for(start) is not None


@pytest.mark.parametrize(
    "encoding,size,signed",
    [
        (0, 4, False),
        (2, 2, False),
        (3, 4, False),
        (4, 8, False),
        (10, 2, True),
        (11, 4, True),
        (12, 8, True),
    ],
)
def test_fixed_encoded_pointers_are_not_accidentally_aligned(encoding, size, signed):
    value = -12 if signed else 12
    data = b"!" + value.to_bytes(size, "little", signed=signed)
    context = cfi.EncodedPointerContext(
        data_base=0x3000, text_base=0x2000, function_base=0x1000
    )
    for application, base in (
        (0, 0),
        (0x10, 0x4001),
        (0x20, 0x2000),
        (0x30, 0x3000),
        (0x40, 0x1000),
    ):
        result = cfi._decode_encoded_pointer(
            data,
            1,
            len(data),
            encoding=encoding | application,
            address_size=4,
            endian="little",
            section_address=0x4000,
            context=context,
        )
        if signed and application == 0:
            assert result is None
        else:
            assert result == (base + value, len(data))
    assert (
        cfi._decode_encoded_pointer(
            data,
            1,
            len(data) - 1,
            encoding=encoding,
            address_size=4,
            endian="little",
            section_address=0,
        )
        is None
    )


@pytest.mark.parametrize(
    "address_size,encoding,value,base,section_address,expected",
    [
        (4, 0x33, b"\xff\xff\xff\xff", 1, 0, None),
        (4, 0x33, b"\xf0\xff\xff\xff", 0x20, 0, None),
        (4, 0x33, b"\xef\xff\xff\xff", 0x10, 0, (0xFFFFFFFF, 4)),
        (4, 0x3B, b"\xff\xff\xff\xff", 1, 0, (0, 4)),
        (4, 0x3B, b"\xfe\xff\xff\xff", 1, 0, None),
        (4, 0x1B, b"\xf4\xff\xff\xff", 0, 0x4001, (0x3FF5, 4)),
        (4, 0x13, b"\xff\xff\xff\xff", 0, 0, (0xFFFFFFFF, 4)),
        (4, 0x13, b"\xff\xff\xff\xff", 0, 1, None),
        (8, 0x34, b"\xff" * 8, 1, 0, None),
        (8, 0x34, (2**64 - 2).to_bytes(8, "little"), 1, 0, (2**64 - 1, 8)),
        (8, 0x3C, b"\xff" * 8, 1, 0, (0, 8)),
        (8, 0x3C, b"\xfe" + b"\xff" * 7, 1, 0, None),
    ],
)
def test_encoded_pointer_base_and_offset_stay_inside_the_address_space(
    address_size, encoding, value, base, section_address, expected
):
    context = cfi.EncodedPointerContext(data_base=base)
    result = cfi._decode_encoded_pointer(
        value,
        0,
        len(value),
        encoding=encoding,
        address_size=address_size,
        endian="little",
        section_address=section_address,
        context=context,
    )
    assert result == expected


@pytest.mark.parametrize(
    "address_size,pointer,result",
    [
        (4, 0xFFFFFFFC, 0x2000),
        (4, 0xFFFFFFFD, None),
        (8, 2**64 - 8, 0x2000),
        (8, 2**64 - 7, None),
    ],
)
def test_indirect_pointer_read_stays_inside_the_address_space(
    address_size, pointer, result
):
    calls = []

    def read(address, size):
        calls.append((address, size))
        return (0x2000).to_bytes(size, "little")

    context = cfi.EncodedPointerContext(read=read)
    decoded = cfi._decode_encoded_pointer(
        int(pointer).to_bytes(address_size, "little"),
        0,
        address_size,
        encoding=0x83 if address_size == 4 else 0x84,
        address_size=address_size,
        endian="little",
        section_address=0,
        context=context,
    )
    if result is None:
        assert decoded is None and calls == []
    else:
        assert decoded == (result, address_size)
        assert calls == [(pointer, address_size)]


@pytest.mark.parametrize(
    "encoding,data,value", [(1, b"\x81\x01", 129), (9, b"\x7e", None), (0xFF, b"", 0)]
)
def test_variable_pointer_encodings(encoding, data, value):
    expected = None if value is None else (value, len(data))
    assert (
        cfi._decode_encoded_pointer(
            data,
            0,
            len(data),
            encoding=encoding,
            address_size=4,
            endian="little",
            section_address=0,
        )
        == expected
    )


@pytest.mark.parametrize("encoding", [0x20, 0x40, 0x80, 0x60, 0x07])
def test_unknown_bases_and_indirection_are_rejected(encoding):
    assert (
        cfi._decode_encoded_pointer(
            b"\x00" * 8,
            0,
            8,
            encoding=encoding,
            address_size=4,
            endian="little",
            section_address=0,
        )
        is None
    )


@pytest.mark.parametrize("encoding", [0x23, 0x33, 0x43])
def test_non_pc_relative_pointer_without_a_context_is_rejected(encoding):
    assert (
        cfi._decode_encoded_pointer(
            b"\x10\x00\x00\x00",
            0,
            4,
            encoding=encoding,
            address_size=4,
            endian="little",
            section_address=0,
        )
        is None
    )


def test_alignment_uses_virtual_address_and_personality_pointer_width():
    data = b"___" + (0x1234).to_bytes(4, "little")
    assert cfi._decode_encoded_pointer(
        data,
        0,
        len(data),
        encoding=0x50,
        address_size=4,
        endian="little",
        section_address=0x4001,
    ) == (0x1234, 7)
    assert cfi._encoded_pointer_skip(b"0" * 8, 0, 0, 8, address_size=8) == 8
    for encoding in (1, 9):
        assert cfi._encoded_pointer_skip(b"\x01", 0, encoding, 1) == 1
        assert cfi._encoded_pointer_skip(b"\x80", 0, encoding, 1) is None
    assert cfi._encoded_pointer_skip(b"", 0, 0xFF, 0) == 0
    assert cfi._encoded_pointer_skip(b"", 0, 7, 0) is None


@pytest.mark.parametrize("eh", [False, True])
def test_cie_initial_failure_keeps_known_fde_coverage(eh):
    blob = _section(eh=eh).replace(b"\x0c\x0d\x00", b"\x0f\x0d\x00", 1)
    table = cfi.parse_cfi_section(
        blob, section_address=0, address_size=4, endian="little", is_eh_frame=eh
    )
    assert table.row_for(0x1004).error == "cfi-truncated-expression"
    assert table.row_for(0x2000) is None and table.error is None


@pytest.mark.parametrize("eh", [False, True])
def test_unknown_cie_is_not_silent_absence(eh):
    blob = bytearray(_section(eh=eh))
    fde_start = int.from_bytes(blob[:4], "little") + 4
    blob[fde_start + 4 : fde_start + 8] = b"\xfe\xff\xff\xff"
    table = cfi.parse_cfi_section(
        bytes(blob), section_address=0, address_size=4, endian="little", is_eh_frame=eh
    )
    assert table.error == "cfi-cie-missing"
    assert table.row_for(0x1004) is None


def test_known_fde_with_truncated_augmentation_keeps_range():
    blob = bytearray(_section(eh=True, augmentation=b"zR", aug_data=b"\x00"))
    fde_start = int.from_bytes(blob[:4], "little") + 4
    blob[fde_start + 16] = 0x7F
    table = cfi.parse_cfi_section(
        bytes(blob),
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=True,
    )
    assert table.row_for(0x1004).error == "cfi-malformed-fde"
    assert table.row_for(0x2000) is None and table.error is None


@pytest.mark.parametrize(
    "augmentation,aug_data,reason",
    [
        (b"zX", b"", "cfi-augmentation-unsupported"),
        (b"zL", b"", "cfi-malformed-cie"),
        (b"eh", b"", "cfi-augmentation-unsupported"),
    ],
)
def test_augmentation_failures_are_retained(augmentation, aug_data, reason):
    table = cfi.parse_cfi_section(
        _section(eh=True, augmentation=augmentation, aug_data=aug_data),
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=True,
    )
    assert table.error == reason


def test_budget_and_truncation_do_not_hide_valid_independent_ranges(monkeypatch):
    blob = _section()
    table = cfi.parse_cfi_section(
        blob + b"!",
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=False,
    )
    assert table.error == "cfi-truncated-length"
    assert table.row_for(0x1004).error is None
    monkeypatch.setattr(cfi, "MAX_FDE_COUNT", 0)
    limited = cfi.parse_cfi_section(
        blob, section_address=0, address_size=4, endian="little", is_eh_frame=False
    )
    assert limited.error == "cfi-record-limit"
