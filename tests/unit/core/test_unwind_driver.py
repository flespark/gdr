"""Driver completion, progress, fallback and memory-bound regressions."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import core.backtrace.unwind as driver
import core.backtrace.unwind_cfi as cfi
from core.backtrace.unwind import SeedFrame, unwind_chain
from core.backtrace.unwind_cfi import CfaRule, CfiRow, CfiTable, RegisterRule
from core.backtrace.unwind_ehabi import EhabiTable, _ExidxEntry
from core.elf import CodeSymbol
from tests.unit.core.test_unwind import _cfi_bytes, _cfi_metadata, _Memory


@pytest.mark.parametrize(
    "pc,sp,reason",
    [
        (0, 0x2040, "seed-pc-invalid"),
        (0x1004, 0x1FFF, "seed-stack-underflow"),
        (0x1004, 0x2101, "seed-stack-overflow"),
        (0x1800, 0x2040, "seed-pc-outside-code"),
    ],
)
def test_invalid_seeds_return_no_frames(pc, sp, reason):
    result = unwind_chain(
        SeedFrame(pc, sp, {}),
        _cfi_metadata(_cfi_bytes()),
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert result.frames == () and result.stop_reason == reason
    assert not result.complete and not result.truncated


def test_same_sp_can_reach_a_caller_but_repeating_pc_sp_is_not_a_root():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x0c\x0d\x00"))
    seed = SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1008})
    result = unwind_chain(
        seed, metadata, read=_Memory({}), stack_low=0x2000, stack_high=0x2100
    )
    assert [frame.pc for frame in result.frames] == [0x1004, 0x1008]
    assert result.stop_reason == "loop" and not result.complete


def test_failed_metadata_does_not_fall_back_to_a_plausible_frame_pointer():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x10\x0e\x01\x9c"))
    memory = _Memory(
        {0x2040: (0x2080).to_bytes(4, "little") + (0x1100).to_bytes(4, "little")}
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2000, {7: 0x2040, 13: 0x2000}),
        metadata,
        read=memory,
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert len(result.frames) == 1
    assert result.stop_reason == "cfi-expression-forbidden"


@pytest.mark.parametrize(
    "instructions",
    [
        b"\x0f\x03\x51\x31\x22",
        b"\x10\x0e\x03\x51\x31\x22",
        b"\x16\x0e\x03\x51\x31\x22",
    ],
)
def test_register_location_expression_failure_does_not_fall_back(instructions):
    metadata = _cfi_metadata(_cfi_bytes(instructions=instructions))
    memory = _Memory(
        {0x2040: (0x2080).to_bytes(4, "little") + (0x1100).to_bytes(4, "little")}
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2000, {7: 0x2040, 13: 0x2000}),
        metadata,
        read=memory,
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert len(result.frames) == 1
    assert result.stop_reason == "cfi-expression-register-location"


def test_task_root_and_depth_have_distinct_completeness():
    seed = SeedFrame(0x1004, 0x2040, {}, root_pc=0x1004)
    metadata = _cfi_metadata(_cfi_bytes())
    result = unwind_chain(seed, metadata, read=_Memory({}))
    assert result.complete and result.stop_reason == "task-entry"
    result = unwind_chain(
        replace(seed, root_pc=None), metadata, read=_Memory({}), max_depth=0
    )
    assert len(result.frames) == 1 and result.truncated and not result.complete


@pytest.mark.parametrize(
    "root,complete,reason",
    [
        (None, False, "return-address-zero"),
        (0x1100, False, "return-address-zero"),
        (0, True, "task-exit"),
    ],
)
def test_zero_return_requires_an_independently_recorded_exit_sentinel(
    root, complete, reason
):
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x0c\x0d\x00"))
    seed = SeedFrame(
        0x1004, 0x2040, {13: 0x2040, 14: 0}, root_pc=root, root_reason="task-exit"
    )
    result = unwind_chain(
        seed, metadata, read=_Memory({}), stack_low=0x2000, stack_high=0x2100
    )
    assert len(result.frames) == 1
    assert result.complete is complete and result.stop_reason == reason
    assert not result.truncated


def test_missing_return_register_cannot_become_a_zero_exit_sentinel():
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x0c\x0d\x00"))
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040}, root_pc=0), metadata, read=_Memory({})
    )
    assert not result.complete and result.stop_reason == "cfi-return-address-missing"


@pytest.mark.parametrize(
    "rule,expected",
    [
        (RegisterRule("same"), (None, None)),
        (RegisterRule("undefined"), (None, "cfi-register-undefined")),
        (RegisterRule("register", register=4), (0x1234, None)),
        (RegisterRule("register", register=5), (None, "cfi-register-source-missing")),
        (RegisterRule("val_offset", value=8), (0x2088, None)),
    ],
)
def test_cfi_register_recovery_rules(rule, expected):
    assert (
        driver._rule_value(
            rule,
            cfa=0x2080,
            regs={4: 0x1234},
            read=_Memory({}),
            ptr_size=4,
            endian="little",
            stack_low=0x2000,
            stack_high=0x2100,
        )
        == expected
    )


@pytest.mark.parametrize(
    "row,sp,regs,reason",
    [
        (CfiRow(error="bad-row"), 0x2040, {}, "bad-row"),
        (CfiRow(), 0x2040, {}, "cfi-no-cfa"),
        (CfiRow(CfaRule(7, 0)), 0x2040, {}, "cfi-cfa-register-missing"),
        (CfiRow(CfaRule(13, -4)), 0x2040, {13: 0x2040}, "cfi-no-progress"),
        (CfiRow(CfaRule(13, 0)), 0x1FF0, {13: 0x1FF0}, "cfi-stack-underflow"),
        (CfiRow(CfaRule(13, 0x200)), 0x2040, {13: 0x2040}, "cfi-stack-overflow"),
        (
            CfiRow(CfaRule(13, 8), {14: RegisterRule("undefined")}),
            0x2040,
            {13: 0x2040},
            "cfi-return-address-undefined",
        ),
    ],
)
def test_cfi_failures_do_not_manufacture_caller_registers(row, sp, regs, reason):
    assert (
        driver._unwind_cfi(
            row,
            regs=regs,
            sp=sp,
            read=_Memory({}),
            ptr_size=4,
            endian="little",
            return_register=14,
            sp_register=13,
            stack_low=0x2000,
            stack_high=0x2100,
        )
        == reason
    )


def test_matching_ehabi_frame_updates_sp_for_the_following_cfi_step():
    metadata = _cfi_metadata(_cfi_bytes(start=0x1100, instructions=b"\x0e\x08\x8e\x01"))
    elf = replace(metadata.elf, symbols=(CodeSymbol("callee", 0x1000, 0x100),))
    table = EhabiTable(
        (_ExidxEntry(0x1000, None, False, 0x8000B0B0),), lambda _a, _n: None, 0x1100
    )
    metadata = replace(metadata, elf=elf, ehabi=table)
    memory = _Memory({0x2048: (0x1200).to_bytes(4, "little")})
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {13: 0x2040, 14: 0x1104}, root_pc=0x1200),
        metadata,
        read=memory,
        stack_low=0x2000,
        stack_high=0x2100,
    )
    assert [(frame.pc, frame.method) for frame in result.frames] == [
        (0x1004, "seed"),
        (0x1104, "ehabi"),
        (0x1200, "cfi"),
    ]
    assert result.complete


@pytest.mark.parametrize(
    "address,size,low,high",
    [
        (0, 4, None, None),
        (0x2000, 0, None, None),
        (0x2000, 9, None, None),
        (0x2000, 4, 0x2004, None),
        (0x2000, 4, None, 0x2002),
    ],
)
def test_slot_reader_bounds_precede_memory_access(address, size, low, high):
    reads = []
    assert (
        driver.read_slot(
            lambda a, n: reads.append((a, n)),
            address,
            size,
            "little",
            stack_low=low,
            stack_high=high,
        )
        is None
    )
    assert reads == []


def test_block_reader_checks_size_bounds_and_short_reads():
    for address, size, low, high in [
        (0, 4, None, None),
        (0x2000, 0, None, None),
        (0x2000, 4097, None, None),
        (0x2000, 4, 0x2004, None),
        (0x2000, 4, None, 0x2002),
    ]:
        assert (
            driver.read_block(
                _Memory({}), address, size, stack_low=low, stack_high=high
            )
            is None
        )
    assert driver.read_block(lambda _a, _n: b"x", 0x2000, 4) is None
    assert driver.read_block(lambda _a, _n: b"abcd", 0x2000, 4) == b"abcd"


@pytest.mark.parametrize(
    "rule,reason",
    [
        (RegisterRule("undefined"), "cfi-register-undefined"),
        (RegisterRule("offset", -4), "cfi-memory-error"),
        (RegisterRule("register", register=8), "cfi-register-source-missing"),
    ],
)
def test_unavailable_register_drops_old_value_and_retains_failure(rule, reason):
    options: dict[str, Any] = dict(
        read=_Memory({}),
        ptr_size=4,
        endian="little",
        return_register=14,
        sp_register=13,
        stack_low=0x2000,
        stack_high=0x2200,
    )
    original = {7: 0x2080, 13: 0x2040, 14: 0x1104}
    result = driver._unwind_cfi(
        CfiRow(CfaRule(13, 8), {7: rule}), regs=original, sp=0x2040, **options
    )
    assert isinstance(result, tuple)
    regs, sp, _pc = result
    assert 7 not in regs and original[7] == 0x2080
    assert (
        driver._unwind_cfi(CfiRow(CfaRule(7, 8)), regs=regs, sp=sp, **options) == reason
    )
    assert driver._rule_value(
        RegisterRule("register", register=7),
        cfa=sp,
        regs=regs,
        **{
            key: value
            for key, value in options.items()
            if key not in ("return_register", "sp_register")
        },
    ) == (None, reason)
    assert (
        driver._unwind_frame_pointer(
            arch="arm",
            regs=regs,
            sp=sp,
            frame_pointer_register=7,
            **{
                key: value
                for key, value in options.items()
                if key not in ("return_register", "sp_register")
            },
        )
        == reason
    )
    restored = driver._unwind_cfi(
        CfiRow(CfaRule(13, 0), {7: RegisterRule("val_offset", 8)}),
        regs=regs,
        sp=sp,
        **options,
    )
    assert isinstance(restored, tuple)
    assert restored[0][7] == sp + 8
    assert 7 not in restored[0].unavailable


def test_register_failures_survive_multiple_driver_steps(monkeypatch):
    rows = {
        0x1004: CfiRow(
            CfaRule(13, 8),
            {7: RegisterRule("undefined"), 14: RegisterRule("val_offset", -0x1040)},
        ),
        0x1007: CfiRow(CfaRule(13, 8), {14: RegisterRule("val_offset", -0x1044)}),
        0x100B: CfiRow(CfaRule(7, 8)),
    }
    monkeypatch.setattr(CfiTable, "row_for", lambda _self, pc: rows.get(pc))
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {7: 0x2080, 13: 0x2040, 14: 0x1200}),
        _cfi_metadata(_cfi_bytes()),
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert [frame.pc for frame in result.frames] == [0x1004, 0x1008, 0x100C]
    assert result.stop_reason == "cfi-register-undefined" and not result.complete


@pytest.mark.parametrize(
    "patch,reason",
    [
        (lambda blob: blob[:13] + b"\x0f" + blob[14:], "cfi-truncated-expression"),
        (lambda blob: blob[:13] + b"\x17" + blob[14:], "cfi-opcode-unsupported"),
        (lambda blob: blob[:8] + b"\x09" + blob[9:], "cfi-cie-version-unsupported"),
        (lambda blob: blob[:20] + b"\xfe\xff\xff\xff" + blob[24:], "cfi-cie-missing"),
        (lambda blob: blob[:-1], "cfi-truncated-unit"),
    ],
)
def test_parse_failures_cannot_enable_frame_pointer(patch, reason):
    metadata = _cfi_metadata(patch(_cfi_bytes()))
    reads = []
    result = unwind_chain(
        SeedFrame(0x1004, 0x2000, {7: 0x2040, 13: 0x2000}),
        metadata,
        read=lambda a, n: reads.append((a, n)),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert len(result.frames) == 1 and result.stop_reason == reason
    assert reads == [] and not result.complete


@pytest.mark.parametrize("has_independent_cfi", [False, True])
def test_unknown_coverage_error_allows_only_independent_metadata(has_independent_cfi):
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x0c\x0d\x00"))
    broken = CfiTable((), address_size=4, endian="little", error="cfi-malformed-fde")
    metadata = replace(
        metadata,
        cfi_tables=(broken, *metadata.cfi_tables) if has_independent_cfi else (broken,),
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2040, {7: 0x2080, 13: 0x2040, 14: 0x1104}, root_pc=0x1104),
        metadata,
        read=_Memory({}),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    if has_independent_cfi:
        assert result.complete and result.frames[-1].method == "cfi"
    else:
        assert result.stop_reason == "cfi-malformed-fde" and len(result.frames) == 1


def test_clean_uncovered_pc_still_allows_bounded_frame_pointer():
    metadata = _cfi_metadata(_cfi_bytes(start=0x1200))
    memory = _Memory(
        {0x2040: (0x2080).to_bytes(4, "little") + (0x1100).to_bytes(4, "little")}
    )
    result = unwind_chain(
        SeedFrame(0x1004, 0x2000, {7: 0x2040, 13: 0x2000}, root_pc=0x1100),
        metadata,
        read=memory,
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert result.complete and result.frames[-1].method == "frame-pointer"


@pytest.mark.parametrize(
    "budget,limit,reason",
    [
        ("MAX_CIE_COUNT", 0, "cfi-record-limit"),
        ("MAX_FDE_COUNT", 0, "cfi-record-limit"),
        ("MAX_CFI_OPCODES", 1, "cfi-opcode-limit"),
    ],
)
def test_parser_budget_failures_cannot_enable_frame_pointer(
    monkeypatch, budget, limit, reason
):
    monkeypatch.setattr(cfi, budget, limit)
    metadata = _cfi_metadata(_cfi_bytes())
    assert metadata.cfi_tables[0].error == reason
    reads = []
    result = unwind_chain(
        SeedFrame(0x1004, 0x2000, {7: 0x2040, 13: 0x2000}),
        metadata,
        read=lambda address, size: reads.append((address, size)),
        stack_low=0x2000,
        stack_high=0x2200,
    )
    assert result.stop_reason == reason and len(result.frames) == 1
    assert reads == [] and not result.complete
