"""Driver completion, progress, fallback and memory-bound regressions."""

from __future__ import annotations

from dataclasses import replace

import pytest

import core.unwind as driver
from core.unwind import SeedFrame, unwind_chain
from core.unwind_cfi import CfaRule, CfiRow, RegisterRule
from core.unwind_ehabi import EhabiTable, _ExidxEntry
from core.unwind_meta import CodeSymbol
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
    metadata = _cfi_metadata(_cfi_bytes(instructions=b"\x10\x0e\x00"))
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
    assert result.stop_reason == "cfi-expression-unsupported"


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
            "cfi-register-undefined",
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
