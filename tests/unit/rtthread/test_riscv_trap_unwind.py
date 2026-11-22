"""Unit tests for RT-Thread RISC-V trap profile and GDB backtrace orchestration."""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import rtthread.backtrace.unwind_gdb as bridge
from core.backtrace.unwind import SeedFrame, UnwindMetadata
from core.backtrace.unwind_cfi import CfaRule, CfiRow, CfiTable, RegisterRule
from core.backtrace.unwind_riscv import (
    MSTATUS_MPP_MACHINE,
    RiscvSnapshot,
    RiscvTrapFrameLayout,
    RiscvTrapSpan,
)
from core.elf import CodeSymbol, ElfImage, ElfSection
from core.stack_frame_parser import StackContextSelection
from rtthread.adapter import Thread
from rtthread.backtrace.unwind import RISCV32_INTEGER, RISCV64_INTEGER
from rtthread.layout import RtConfig, build_layouts
from rtthread.stack_frame_profile import analyze_riscv_trap_profile


def _trap_span(*, epc_offset: int | None, word: int = 4) -> RiscvTrapSpan:
    layout = RiscvTrapFrameLayout(
        frame_bytes=32 * word,
        total_frame_bytes=32 * word,
        reg_offsets={1: word, **{reg: reg * word for reg in range(4, 32)}},
        epc_offset=epc_offset,
        mstatus_offset=2 * word,
        epc_sync_addend=0,
        frame_pointer_register=8,
    )
    return RiscvTrapSpan(
        entry=0x80001000,
        save_complete=0x80001050,
        call_start=0x80001050,
        call_end=0x80001070,
        exit_end=0x800010B0,
        layout=layout,
        refused_spans=(
            (0x80001002, 0x80001050, "exception-frame-incomplete"),
            (0x80001070, 0x800010B0, "scheduler-transition-unproven"),
        ),
        evidence=("trap=trap_entry@0x80001000",),
    )


def _setup_rtt_session(
    monkeypatch,
    *,
    xlen: int = 32,
    epc_offset: int | None = None,
    seed_pc: int = 0x80002010,
    ra: int = 0x80001060,
    nest_count: int | None = 1,
    switch_flag: int = 0,
    is_current: bool = True,
    from_thread: int = 0,
    mcause: int | None = None,
):
    word = xlen // 8
    isa = "riscv64" if xlen == 64 else "riscv32"
    ctx_prof = RISCV64_INTEGER if xlen == 64 else RISCV32_INTEGER
    layout = build_layouts(RtConfig())
    selection = StackContextSelection(isa, "riscv", ctx_prof, "ok")
    adapter = SimpleNamespace(
        layout=layout,
        version=(4, 0, 5),
        _unwind_cache=None,
    )
    thread = Thread(
        name="bt",
        address=0x80010000,
        sp=0x80011E00,
        stack_addr=0x80011000,
        stack_size=0x1000,
        stack_grows_up=False,
        entry=0x80004000,
        oncpu=0 if is_current else None,
    )
    cfi = CfiTable.from_rows(
        [
            (
                0x80002000,
                0x80002040,
                CfiRow(CfaRule(2, 16), {1: RegisterRule("offset", -word)}),
            ),
            (
                0x80003000,
                0x80003040,
                CfiRow(CfaRule(2, 32), {1: RegisterRule("offset", -word)}),
            ),
        ],
        return_register=1,
        address_size=word,
    )
    elf = ElfImage(
        pointer_size=word,
        endian="little",
        machine=243,
        sections=(ElfSection(".text", 0x80001000, b"\x00" * 0x4000, 0x4, 1),),
        symbols=(
            CodeSymbol("trap_entry", 0x80001000, 0xB0),
            CodeSymbol("handle_trap", 0x80002000, 0x40),
            CodeSymbol("bt_leaf", 0x80003000, 0x40),
            CodeSymbol("bt_entry", 0x80004000, 0x40),
        ),
    )
    meta = UnwindMetadata(
        elf=elf,
        arch="riscv",
        cfi_tables=(cfi,),
        ehabi=None,
        return_register=1,
        frame_pointer_register=8,
    )
    frame_addr = 0x80011D00
    fmt = "<Q" if word == 8 else "<I"
    mem: dict[int, bytes] = {}
    trap_words = [0] * 32
    if epc_offset is not None:
        trap_words[0] = 0x80003010
    trap_words[1] = 0x80003999
    trap_words[2] = MSTATUS_MPP_MACHINE | 0x80
    for idx, val in enumerate(trap_words):
        mem[frame_addr + idx * word] = struct.pack(fmt, val)
    # tick_isr frame on IRQ stack: size 16 -> caller ra at sp + 16 - word
    mem[0x8001FFE0 + 16 - word] = struct.pack(fmt, ra)
    # task leaf frame at frame_addr + total_frame_bytes (size 32) -> ra at cfa - word
    mem[frame_addr + 32 * word + 32 - word] = struct.pack(fmt, 0x80004000)

    def _read_bytes(addr: int, size: int) -> bytes | None:
        out = bytearray()
        cursor = addr
        while len(out) < size:
            chunk = mem.get(cursor)
            if chunk is None:
                return None
            take = min(size - len(out), len(chunk))
            out.extend(chunk[:take])
            cursor += take
        return bytes(out)

    snap = RiscvSnapshot(
        xlen=xlen,
        pc=seed_pc,
        sp=0x8001FFE0,
        mstatus=MSTATUS_MPP_MACHINE,
        mcause=((1 << (xlen - 1)) | 7) if mcause is None else mcause,
        mepc=0x80003010,
        mtvec=0x80001000,
        registers={1: ra, 2: 0x8001FFE0, 8: frame_addr},
    )
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(bridge, "value_to_thread", lambda *_args, **_kwargs: thread)
    monkeypatch.setattr(bridge, "_backtrace_context", lambda: selection)
    monkeypatch.setattr(
        bridge, "get_current_thread", lambda: object() if is_current else None
    )
    monkeypatch.setattr(
        bridge,
        "value_address",
        lambda val: thread.address if is_current and val is not None else 0,
    )
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: meta)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (word, "little"))
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: None if name in ("rt_thread_exit", "_thread_exit") else name,
    )
    monkeypatch.setattr(
        bridge,
        "read_int",
        lambda expr: {
            "rt_interrupt_nest": nest_count,
            "rt_thread_switch_interrupt_flag": switch_flag,
            "rt_interrupt_from_thread": from_thread,
        }.get(expr),
    )
    monkeypatch.setattr(bridge, "read_bytes", _read_bytes)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda addr: f"sym_{addr:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _addr: None)
    monkeypatch.setattr(
        bridge,
        "_riscv_irq_stack_bounds",
        lambda _elf, *, hart_id=0: (0x8001F000, 0x80020000, "symbol:irq"),  # noqa: ARG005
    )
    monkeypatch.setattr(
        bridge,
        "analyze_riscv_trap_profile",
        lambda _elf: _trap_span(epc_offset=epc_offset, word=word),
    )
    monkeypatch.setattr(
        bridge,
        "_live_seed_detail",
        lambda _arch, _isa, _root: (
            SeedFrame(
                seed_pc,
                0x8001FFE0,
                {1: ra, 2: 0x8001FFE0, 8: frame_addr},
                source="current",
            ),
            "ok",
        ),
    )
    monkeypatch.setattr(bridge, "_riscv_snapshot", lambda *, xlen=32: (snap, "ok"))  # noqa: ARG005
    return adapter, layout


def test_rtthread_riscv_e310_deferred_mepc_and_virt64_stacked_epc(monkeypatch):
    adapter32, _ = _setup_rtt_session(
        monkeypatch, xlen=32, epc_offset=None, nest_count=1
    )
    report32 = bridge.backtrace(adapter32, "bt")
    assert report32.complete is True
    assert report32.stop_reason == "task-entry"
    assert [f.pc for f in report32.frames] == [0x80002010, 0x80003010, 0x80004000]
    assert report32.frames[0].segment.startswith("exception:")
    assert report32.frames[1].segment == "task:0x80010000"

    adapter64, _ = _setup_rtt_session(monkeypatch, xlen=64, epc_offset=0, nest_count=1)
    report64 = bridge.backtrace(adapter64, "bt")
    assert report64.complete is True
    assert report64.stop_reason == "task-entry"
    assert [f.pc for f in report64.frames] == [0x80002010, 0x80003010, 0x80004000]


def test_rtthread_riscv_e310_refuses_nested_and_missing_nest(monkeypatch):
    adapter, _ = _setup_rtt_session(monkeypatch, xlen=32, epc_offset=None, nest_count=2)
    report = bridge.backtrace(adapter, "bt")
    assert not report.complete
    assert report.stop_reason == "nested-trap-epc-unproven"

    adapter_none, _ = _setup_rtt_session(
        monkeypatch, xlen=32, epc_offset=None, nest_count=None
    )
    report_none = bridge.backtrace(adapter_none, "bt")
    assert not report_none.complete
    assert report_none.stop_reason == "exception-context-unproven"


def test_rtthread_riscv_refuses_switch_flag_and_from_thread(monkeypatch):
    adapter, _layout = _setup_rtt_session(
        monkeypatch, xlen=32, epc_offset=None, nest_count=1, switch_flag=1
    )
    report = bridge.backtrace(adapter, "bt")
    assert not report.complete
    assert report.stop_reason == "scheduler-profile-unsupported-image"

    monkeypatch.setattr(bridge, "member_offset", lambda *_args: 12)
    sp_off = 12
    adapter_from, _ = _setup_rtt_session(
        monkeypatch,
        xlen=32,
        is_current=False,
        switch_flag=1,
        from_thread=0x80010000 + sp_off,
    )
    # Stub decode_saved_context so reaching unwind without refusal would be visible.
    monkeypatch.setattr(
        bridge,
        "decode_backtrace_context",
        lambda *_a, **_kw: (SeedFrame(0x80003010, 0x80011E00, {}), "ok"),
    )
    report_from = bridge.backtrace(adapter_from, "bt")
    assert not report_from.complete
    assert report_from.stop_reason == "scheduler-profile-unsupported-image"


def test_rtthread_riscv_precise_sync_fault_and_unmodeled_refusal(monkeypatch):
    adapter, _ = _setup_rtt_session(monkeypatch, xlen=64, epc_offset=0, mcause=5)
    report = bridge.backtrace(adapter, "bt")
    assert report.complete is True
    assert report.stop_reason == "synchronous-fault"
    assert [f.pc for f in report.frames] == [0x80002010, 0x80003010, 0x80004000]
    assert any("Exception:LoadAccessFault" in msg for msg in report.messages)

    adapter_bad, _ = _setup_rtt_session(monkeypatch, xlen=64, epc_offset=0, mcause=24)
    refused = bridge.backtrace(adapter_bad, "bt")
    assert refused.complete is False
    assert refused.stop_reason == "exception-frame-type-unproven"


@pytest.mark.parametrize(
    "elf_path,expected_epc",
    [
        ("ci/.cache/fixtures/rtthread/rv32/4.0.5/rtthread.elf", None),
        ("ci/.cache/fixtures/rtthread/rv64/4.0.4/rtthread.elf", 0),
        ("ci/.cache/fixtures/rtthread/rv64/4.0.5/rtthread.elf", 0),
        ("ci/.cache/fixtures/rtthread/rv64/4.1.0/rtthread.elf", 0),
        ("ci/.cache/fixtures/rtthread/rv64/4.1.1/rtthread.elf", 0),
    ],
)
def test_cached_rtthread_riscv_elfs_produce_verified_trap_span(elf_path, expected_epc):
    path = Path(elf_path)
    if not path.is_file():
        pytest.skip(f"{elf_path} not cached")
    elf = ElfImage.from_bytes(path.read_bytes())
    assert elf is not None
    span = analyze_riscv_trap_profile(elf)
    assert span is not None
    assert span.layout.epc_offset is expected_epc
    assert span.call_start < span.call_end < span.exit_end
