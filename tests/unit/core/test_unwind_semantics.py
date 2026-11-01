"""Return-column and resume-PC contracts for bounded integer unwinding."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

import core.unwind as driver
from core.unwind import SeedFrame, UnwindFrame, unwind_chain
from core.unwind_cfi import CfaRule, CfiRow, RegisterRule, parse_cfi_section
from core.unwind_meta import CodeSymbol
from tests.unit.core.test_unwind import _cfi_bytes, _cfi_metadata, _Memory
from tests.unit.core.test_unwind_cfi import _section


@pytest.mark.parametrize("column", [5, 14, 128])
@pytest.mark.parametrize("sp_register,width", [(13, 4), (2, 8)])
def test_return_column_never_selects_the_architectural_sp(column, sp_register, width):
    row = CfiRow(
        CfaRule(sp_register, 8),
        {column: RegisterRule("offset", -width)},
        return_register=column,
    )
    result = driver._unwind_cfi(
        row,
        regs={sp_register: 0x2040},
        sp=0x2040,
        read=_Memory({0x2048 - width: (0x1105).to_bytes(width, "little")}),
        ptr_size=width,
        endian="little",
        return_register=1 if sp_register == 2 else 14,
        sp_register=sp_register,
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert isinstance(result, tuple)
    regs, sp, pc = result
    assert regs[sp_register] == sp == 0x2048 and pc == 0x1105
    assert column not in regs if column >= 32 else regs[column] == 0x1105


@pytest.mark.parametrize("eh", [False, True])
@pytest.mark.parametrize("column", [5, 128])
def test_parsed_cie_return_column_reaches_the_driver(eh, column):
    blob = _section(
        version=3,
        eh=eh,
        instructions=b"\x05"
        + bytes([column])
        + (b"\x01" if column == 128 else b"")
        + b"\x01",
    )
    offset = blob.index(b"\x01\x7c\x0e") + 2
    encoded = b"\x80\x01" if column == 128 else bytes([column])
    if column == 128:
        old_length = int.from_bytes(blob[:4], "little")
        blob = (
            (old_length + 1).to_bytes(4, "little")
            + blob[4:offset]
            + encoded
            + blob[offset + 1 :]
        )
        if eh:
            fde = old_length + 5
            blob = blob[: fde + 4] + (fde + 4).to_bytes(4, "little") + blob[fde + 8 :]
    else:
        blob = blob[:offset] + encoded + blob[offset + 1 :]
    table = parse_cfi_section(
        blob, section_address=0, address_size=4, endian="little", is_eh_frame=eh
    )
    assert table.error is None
    assert table.row_for(0x1004).return_register == column
    metadata = replace(_cfi_metadata(_cfi_bytes()), cfi_tables=(table,))
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040}, root_pc=0x1104),
        metadata,
        read=_Memory({0x203C: (0x1104).to_bytes(4, "little")}),
        stack_low=0x2000,
        stack_high=0x2200,
        allow_frame_pointer=False,
    )
    assert result.complete and result.frames[-1].pc == 0x1104


@pytest.mark.parametrize(
    "row,reason",
    [
        (
            CfiRow(CfaRule(13, 0), return_register=128),
            "cfi-return-register-unsupported",
        ),
        (CfiRow(CfaRule(13, 0), return_register=13), "cfi-return-register-unsupported"),
        (CfiRow(CfaRule(99, 0)), "cfi-cfa-register-unsupported"),
        (
            CfiRow(CfaRule(13, 0), {99: RegisterRule("same")}),
            "cfi-register-unsupported",
        ),
        (
            CfiRow(CfaRule(13, 0), {14: RegisterRule("register", register=99)}),
            "cfi-register-unsupported",
        ),
        (
            CfiRow(CfaRule(13, 0), {14: RegisterRule("undefined")}),
            "cfi-return-address-undefined",
        ),
    ],
)
def test_unsupported_registers_and_undefined_return_are_explicit(row, reason):
    result = driver._unwind_cfi(
        row,
        regs={13: 0x2040, 14: 0x1104},
        sp=0x2040,
        read=_Memory({}),
        ptr_size=4,
        endian="little",
        return_register=14,
        sp_register=13,
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert result == reason


@pytest.mark.parametrize("signal", [False, True])
@pytest.mark.parametrize("machine", ["arm", "riscv"])
def test_raw_resume_pc_and_metadata_lookup_are_distinct(monkeypatch, signal, machine):
    metadata = _cfi_metadata(_cfi_bytes())
    if machine == "riscv":
        metadata = replace(
            metadata, arch="riscv", return_register=1, frame_pointer_register=8
        )
    sp, ra = (13, 14) if machine == "arm" else (2, 1)
    queries = []

    def row_for(_table, pc):
        queries.append(pc)
        if len(queries) == 1:
            return CfiRow(
                CfaRule(sp, 8),
                {ra: RegisterRule("same")},
                return_register=ra,
                signal_frame=signal,
            )
        return CfiRow(
            CfaRule(sp, 0), {ra: RegisterRule("undefined")}, return_register=ra
        )

    monkeypatch.setattr(type(metadata.cfi_tables[0]), "row_for", row_for)
    result = unwind_chain(
        SeedFrame(
            0x1004, 0x2040, {sp: 0x2040, ra: 0x1101 if machine == "arm" else 0x1100}
        ),
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert queries == [0x1004, 0x1100 if signal else 0x10FF]
    assert result.frames[1].pc == 0x1100
    assert result.frames[1].address_in_block == queries[-1]
    assert result.stop_reason == "cfi-return-address-undefined" and not result.complete


@pytest.mark.parametrize("signal", [False, True])
def test_signal_augmentation_survives_row_execution(signal):
    table = parse_cfi_section(
        _section(eh=True, augmentation=b"zS" if signal else b"z"),
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=True,
    )
    row = table.row_for(0x1004)
    assert row.signal_frame is signal and row.return_register == 14


def test_undefined_return_never_claims_a_task_root():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x07\x0e"))
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0}, root_pc=0),
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert result.stop_reason == "cfi-return-address-undefined"
    assert not result.complete and not result.truncated and len(result.frames) == 1
    assert UnwindFrame(0, 0x1004, "seed").address_in_block == 0x1004


@pytest.mark.parametrize("machine", ["arm", "riscv"])
@pytest.mark.parametrize("root_reason", ["task-entry", "task-exit"])
def test_proven_task_root_uses_its_exact_address_without_decoding_another_row(
    monkeypatch, machine, root_reason
):
    metadata = _cfi_metadata(_cfi_bytes())
    sp, ra = (13, 14) if machine == "arm" else (2, 1)
    metadata = replace(
        metadata,
        arch=machine,
        return_register=ra,
        elf=replace(
            metadata.elf,
            symbols=(
                CodeSymbol("previous", 0x1000, 0x100),
                CodeSymbol("root", 0x1100, 0x100),
            ),
        ),
    )
    queries = []

    def row_for(_table, pc):
        queries.append(pc)
        assert pc == 0x1004
        return CfiRow(CfaRule(sp, 8), {ra: RegisterRule("same")}, return_register=ra)

    monkeypatch.setattr(type(metadata.cfi_tables[0]), "row_for", row_for)
    result = unwind_chain(
        SeedFrame(
            0x1004,
            0x2040,
            {sp: 0x2040, ra: 0x1101 if machine == "arm" else 0x1100},
            root_pc=0x1101 if machine == "arm" else 0x1100,
            root_reason=root_reason,
        ),
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert result.complete and result.stop_reason == root_reason
    assert queries == [0x1004]
    assert result.frames[-1].pc == result.frames[-1].address_in_block == 0x1100
    assert (
        metadata.elf.function_containing(result.frames[-1].address_in_block).name
        == "root"
    )


def test_unproven_entry_shaped_return_does_not_become_an_exact_task_root(monkeypatch):
    metadata = _cfi_metadata(_cfi_bytes())
    metadata = replace(
        metadata,
        elf=replace(
            metadata.elf,
            symbols=(
                CodeSymbol("previous", 0x1000, 0x100),
                CodeSymbol("entry", 0x1100, 0x100),
            ),
        ),
    )
    queries = []

    def row_for(_table, pc):
        queries.append(pc)
        if pc == 0x1104:
            return CfiRow(CfaRule(13, 8), {14: RegisterRule("same")})
        return CfiRow(CfaRule(13, 0x1000))

    monkeypatch.setattr(type(metadata.cfi_tables[0]), "row_for", row_for)
    result = unwind_chain(
        SeedFrame(0x1104, 0x2040, {13: 0x2040, 14: 0x1101}),
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert queries == [0x1104, 0x10FF]
    assert (
        result.frames[-1].pc == 0x1100 and result.frames[-1].address_in_block == 0x10FF
    )
    assert result.stop_reason == "cfi-stack-overflow" and not result.complete


@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("damage", ["record", "section"])
def test_independent_valid_cfi_survives_a_broken_table(monkeypatch, bad_first, damage):
    good = _cfi_metadata(_cfi_bytes(instructions=b"\x0e\x08\x8e\x01"))
    bad = parse_cfi_section(
        _cfi_bytes(instructions=b"\x17") if damage == "record" else b"\x03",
        section_address=0,
        address_size=4,
        endian="little",
        is_eh_frame=False,
    )
    tables = (bad, *good.cfi_tables) if bad_first else (*good.cfi_tables, bad)
    monkeypatch.setattr(
        driver,
        "_unwind_frame_pointer",
        lambda **_kwargs: pytest.fail("fallback masked CFI"),
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040}, root_pc=0x1104),
        replace(good, cfi_tables=tables),
        read=_Memory({0x2044: (0x1104).to_bytes(4, "little")}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert result.complete and result.stop_reason == "task-entry"
    assert result.frames[-1].pc == 0x1104 and result.method == "cfi"


@pytest.mark.parametrize("kind", ["same", "undefined", "register", "val_offset"])
def test_virtual_return_column_requires_a_rule_and_is_not_a_machine_register(kind):
    row = CfiRow(
        CfaRule(13, 8),
        {128: RegisterRule(kind, -0xF44, 14 if kind == "register" else None)},
        return_register=128,
    )
    result = driver._unwind_cfi(
        row,
        regs={13: 0x2040, 14: 0x1104, 128: 0xDEAD},
        sp=0x2040,
        read=_Memory({}),
        ptr_size=4,
        endian="little",
        return_register=14,
        sp_register=13,
        stack_low=0x2000,
        stack_high=0x2200,
    )
    if kind == "same":
        assert result == "cfi-return-register-unsupported"
    elif kind == "undefined":
        assert result == "cfi-return-address-undefined"
    else:
        assert isinstance(result, tuple)
        regs, sp, pc = result
        assert 128 not in regs and pc == 0x1104 and regs[13] == sp == 0x2048


def test_ehabi_and_cfi_share_the_ordinary_caller_lookup_address(monkeypatch):
    metadata = _cfi_metadata(_cfi_bytes())
    queries = []

    def entry_for(pc):
        queries.append(("ehabi", pc))
        return SimpleNamespace(start=pc)

    def row_for(_table, pc):
        queries.append(("cfi", pc))
        if pc == 0x1004:
            return CfiRow(CfaRule(13, 8), {14: RegisterRule("same")})
        return CfiRow(CfaRule(13, 0), {14: RegisterRule("undefined")})

    monkeypatch.setattr(
        type(metadata.elf),
        "function_containing",
        lambda _elf, pc: SimpleNamespace(start=pc),
    )
    monkeypatch.setattr(type(metadata.cfi_tables[0]), "row_for", row_for)
    monkeypatch.setattr(
        driver, "unwind_ehabi", lambda *_args, **_kwargs: "ehabi-cant-unwind"
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1101}),
        replace(metadata, ehabi=SimpleNamespace(entry_for=entry_for)),
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert queries == [
        ("ehabi", 0x1004),
        ("cfi", 0x1004),
        ("ehabi", 0x10FF),
        ("cfi", 0x10FF),
    ]
    assert result.frames[1].pc == 0x1100 and result.frames[1].method == "cfi"
    assert result.stop_reason == "cfi-return-address-undefined" and not result.complete


@pytest.mark.parametrize("signal", [False, True])
def test_executable_range_end_is_valid_only_as_an_ordinary_return(monkeypatch, signal):
    metadata = _cfi_metadata(_cfi_bytes())
    monkeypatch.setattr(
        type(metadata.cfi_tables[0]),
        "row_for",
        lambda _table, _pc: CfiRow(
            CfaRule(13, 8), {14: RegisterRule("same")}, signal_frame=signal
        ),
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1401}),
        metadata,
        read=_Memory({}),
        max_depth=2,
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert len(result.frames) == (1 if signal else 2)
    assert result.stop_reason == (
        "return-address-outside-code" if signal else "max-depth"
    )
