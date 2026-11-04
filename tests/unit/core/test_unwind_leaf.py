"""Instruction-derived leaf rules preserve bounds and metadata failure guards."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.unwind import SeedFrame, UnwindMetadata, unwind_chain
from core.unwind_cfi import CfaRule, CfiRow, CfiTable
from core.unwind_leaf import prove_leaf_return
from core.unwind_meta import CodeSymbol, ElfImage, ElfSection, _parse_code_symbols
from tests.unit.core.test_cpus import _attributes
from tests.unit.core.test_unwind import _cfi_bytes, _cfi_metadata, _elf, _Memory


def _image(code=b"\x80\xf3\x10\x88\x70\x47", *, zero=False, thumb=True, isa="armv7-m"):
    riscv = isa.startswith("riscv")
    attributes = _attributes(
        b"riscv" if riscv else b"aeabi",
        [(5, "rv64imac" if isa == "riscv64" else "rv32imac"), (4, 16)]
        if riscv
        else [(6, 10), (7, ord("A") if isa == "armv7-a" else ord("M"))],
    )
    parsed = ElfImage.from_bytes(
        _elf(
            is_64=isa == "riscv64",
            endian="little",
            machine=243 if riscv else 40,
            sections=[
                (".text", 0x1000, code + bytes(0x400 - len(code)), 1, 6),
                (
                    ".riscv.attributes" if riscv else ".ARM.attributes",
                    0,
                    attributes,
                    0x70000003,
                    0,
                ),
            ],
        )
    )
    assert parsed is not None
    symbol = CodeSymbol("vendor_neutral_leaf", 0x1000, 0 if zero else len(code), thumb)
    return replace(
        parsed,
        symbols=(CodeSymbol("caller", 0x1100, 0x20, thumb),)
        if zero
        else (
            symbol,
            CodeSymbol("caller", 0x1100, 0x20, thumb),
        ),
        zero_size_functions=(symbol,) if zero else (),
    )


@pytest.mark.parametrize("zero", [False, True])
def test_thumb_leaf_proof_needs_no_port_name_or_added_cfi(zero):
    proof = prove_leaf_return(_image(zero=zero), 0x1004)
    assert proof is not None and proof.return_register == 14
    assert proof.clobbered == ()
    assert "preserves SP" in " ".join(proof.evidence)
    assert "vendor_neutral_leaf" in " ".join(proof.evidence)


@pytest.mark.parametrize(
    "code",
    [
        b"\x80\xb4\x70\x47",
        b"\x80\xbc\x70\x47",
        b"\x00\xf0\x00\xf8\x70\x47",
        b"\x00\xe0\x70\x47",
        b"\x80\xf3\x08\x88\x70\x47",
        b"\x80\xf3\x09\x88\x70\x47",
        b"\x80\xf3\x14\x88\x70\x47",
        b"\xef\xf3\x10\x8e\x70\x47",
        b"\x00\x47",
        b"\x00\xbf",
    ],
)
def test_thumb_calls_stack_changes_lr_writes_and_unknown_paths_are_not_leaf_proofs(
    code,
):
    assert prove_leaf_return(_image(code), 0x1000) is None


def test_instruction_boundaries_symbol_state_and_prefix_writes_are_preserved():
    assert prove_leaf_return(_image(), 0x1002) is None
    assert prove_leaf_return(_image(thumb=False), 0x1004) is None
    assert prove_leaf_return(replace(_image(), symbols=()), 0x1004) is None
    proof = prove_leaf_return(_image(b"\xef\xf3\x10\x80\x72\xb6\x70\x47"), 0x1006)
    assert proof is not None and proof.clobbered == (0,)


@pytest.mark.parametrize("isa", ["riscv32", "riscv64"])
def test_riscv_scalar_leaf_is_proven_independently_of_a_vendor_port(isa):
    proof = prove_leaf_return(
        _image(b"\x13\x05\x10\x00\x67\x80\x00\x00", thumb=None, isa=isa), 0x1004
    )
    assert proof is not None and proof.return_register == 1 and proof.clobbered == (10,)
    assert (
        prove_leaf_return(
            _image(b"\x13\x01\xc1\xff\x67\x80\x00\x00", thumb=None, isa=isa), 0x1004
        )
        is None
    )
    assert (
        prove_leaf_return(_image(b"\x73\x00\x20\x30", thumb=None, isa=isa), 0x1000)
        is None
    )


def test_arm_instruction_state_uses_the_function_marker_not_cpu_family():
    image = _image(bytes.fromhex("00000fe11eff2fe1"), thumb=False, isa="armv7-a")
    proof = prove_leaf_return(image, 0x1004)
    assert proof is not None and proof.clobbered == (0,)
    assert (
        prove_leaf_return(
            replace(image, symbols=(replace(image.symbols[0], thumb=None),)), 0x1004
        )
        is None
    )


def test_leaf_and_cfi_chain_share_the_normal_bounds_depth_and_pc_checks():
    metadata = replace(_cfi_metadata(_cfi_bytes(start=0x1100)), elf=_image(zero=True))
    memory = _Memory({0x2044: (0x1200).to_bytes(4, "little")})
    seed = SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1105}, root_pc=0x1200)
    result = unwind_chain(
        seed,
        metadata,
        read=memory,
        stack_low=0x2000,
        stack_high=0x2100,
        allow_frame_pointer=False,
    )
    assert [(frame.pc, frame.method) for frame in result.frames] == [
        (0x1004, "seed"),
        (0x1104, "leaf"),
        (0x1200, "cfi"),
    ]
    assert result.complete and result.frames[1].evidence
    limited = unwind_chain(
        seed, metadata, read=memory, max_depth=2, stack_low=0x2000, stack_high=0x2100
    )
    assert limited.truncated and limited.stop_reason == "max-depth"


def test_leaf_return_never_reuses_a_missing_or_clobbered_register():
    metadata = UnwindMetadata.from_elf(_image())
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040}),
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert (
        result.stop_reason == "leaf-return-address-missing" and len(result.frames) == 1
    )


def test_leaf_rules_cannot_override_unknown_or_damaged_cfi_coverage():
    metadata = UnwindMetadata.from_elf(_image())
    broken = CfiTable((), address_size=4, endian="little", error="cfi-malformed-fde")
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1104}),
        replace(metadata, cfi_tables=(broken,)),
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert result.stop_reason == "cfi-malformed-fde" and len(result.frames) == 1


def test_generic_labels_cannot_hide_a_stack_changing_prefix():
    image = replace(
        _image(b"\x80\xb4\x70\x47", zero=True), anchors=(("inner_label", 0x1002),)
    )
    assert prove_leaf_return(image, 0x1002) is None


def test_elf_parser_retains_zero_size_function_type_and_thumb_state():
    import struct

    strings = b"\0leaf\0inner\0"
    functions = struct.pack("<IIIBBH", 1, 0x1001, 0, 0x12, 0, 3)
    functions += struct.pack("<IIIBBH", 6, 0x1003, 0, 0x10, 0, 3)
    data = functions + strings
    headers = [
        (0, 0, 0, 0, 0, 0, 0),
        (0, 2, 0, 0, 0, 32, 2),
        (0, 3, 0, 0, 32, len(strings), 0),
        (0, 1, 6, 0x1000, 0, 4, 0),
    ]
    sized, anchors, zero = _parse_code_symbols(
        data, headers, is_64=False, endian="little", machine=40, section_count=4
    )
    assert sized == () and zero == (CodeSymbol("leaf", 0x1000, 0, True),)
    assert anchors == (("leaf", 0x1000), ("inner", 0x1002))


def test_leaf_clobber_reason_survives_into_the_caller_cfa(monkeypatch):
    metadata = UnwindMetadata.from_elf(_image(b"\xef\xf3\x10\x80\x70\x47"))
    table = CfiTable((), address_size=4, endian="little")
    monkeypatch.setattr(
        CfiTable,
        "row_for",
        lambda _table, pc: CfiRow(CfaRule(0, 0)) if pc == 0x1103 else None,
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {0: 0x2080, 13: 0x2040, 14: 0x1104}),
        replace(metadata, cfi_tables=(table,)),
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert [frame.method for frame in result.frames] == ["seed", "leaf"]
    assert result.stop_reason == "leaf-register-unavailable"


def test_truncated_ehabi_table_cannot_enable_an_instruction_leaf():
    image = _image()
    image = replace(
        image,
        sections=(
            *image.sections,
            ElfSection(".ARM.exidx", 0x1500, b"\0", 0, 0x70000001),
        ),
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1104}),
        UnwindMetadata.from_elf(image),
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert result.stop_reason == "ehabi-truncated-table" and len(result.frames) == 1
