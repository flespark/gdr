"""RT-Thread orchestration rejects unverified ports and missing bounds."""

from types import SimpleNamespace

import pytest

import rtthread.backtrace_gdb as bridge
from core.stack_frame_parser import StackContextSelection
from core.unwind import SeedFrame, UnwindFrame, UnwindResult
from rtthread.adapter import Thread
from rtthread.backtrace import (
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_INTEGER,
    RISCV32_INTEGER,
    RISCV64_INTEGER,
)
from rtthread.layout import RT_THREAD_STACK_FILL


@pytest.mark.parametrize(
    "isa,context",
    [
        ("armv8-m.mainline", ARMV8M_MAINLINE_INTEGER),
        ("riscv32", RISCV32_INTEGER),
    ],
)
def test_unverified_context_never_reads_target_stack(monkeypatch, isa, context):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(name="worker", address=0x4000),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection(
            isa,
            "riscv" if isa.startswith("riscv") else "arm",
            context,
            "unverified-port",
        ),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unverified context read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.architecture == isa and report.stop_reason == "unverified-port"
    assert report.frames == [] and not report.complete


@pytest.mark.parametrize("base,size", [(0, 1024), (0x2000, 0)])
def test_verified_port_still_needs_task_bounds(monkeypatch, base, size):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(
            name="worker", address=0x4000, stack_addr=base, stack_size=size
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("riscv64", "riscv", RISCV64_INTEGER, "ok"),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unbounded read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.stop_reason == "stack-bounds-unavailable"
    assert report.frames == [] and not report.complete


def test_unverified_port_selection_precedes_stack_watermark_reads(monkeypatch):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(name="worker", address=0x4000),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("armv7-m", "arm", None, "unverified-port"),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unverified stack read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.stop_reason == "unverified-port"


def test_verified_arm_port_selection_precedes_stack_watermark_reads(monkeypatch):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(name="worker", address=0x4000),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection(
            "armv8-m.mainline", "arm", None, "unverified-port"
        ),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("stack watermark read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.stop_reason == "unverified-port"


@pytest.mark.parametrize(
    "low_edge,high_edge,expected",
    [
        (bytes([RT_THREAD_STACK_FILL]) * 16, b"\0" * 16, False),
        (b"\0" * 16, bytes([RT_THREAD_STACK_FILL]) * 16, True),
        (b"\0" * 16, b"\0" * 16, None),
    ],
)
def test_stack_direction_inference_reads_only_recorded_edges(
    monkeypatch, low_edge, high_edge, expected
):
    reads = []

    def read(address, size):
        reads.append((address, size))
        return low_edge if address == 0x2000 else high_edge

    monkeypatch.setattr(bridge, "read_bytes", read)
    assert bridge._stack_growth(0x2000, 0x2100) is expected
    assert reads == [(0x2000, 16), (0x20F0, 16)]


def test_symbols_and_source_use_lookup_pc_without_changing_resume_pc(monkeypatch):
    thread = Thread(
        name="worker",
        address=0x4000,
        stack_addr=0x2000,
        stack_size=0x200,
        stack_grows_up=False,
    )
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(bridge, "value_to_thread", lambda *_args, **_kwargs: thread)
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("riscv64", "riscv", RISCV64_INTEGER, "ok"),
    )
    monkeypatch.setattr(bridge, "get_current_thread", lambda: None)
    monkeypatch.setattr(bridge, "eval_identifier", lambda _name: None)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (8, "little"))
    monkeypatch.setattr(
        bridge,
        "decode_backtrace_context",
        lambda *_args, **_kwargs: (SeedFrame(0x1004, 0x2040, {2: 0x2040}), "ok"),
    )
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: None)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda *_args, **_kwargs: UnwindResult(
            (UnwindFrame(0, 0x1004, "seed"), UnwindFrame(1, 0x1100, "cfi", 0x10FF)),
            "cfi-return-address-undefined",
            False,
            False,
            "saved",
            "cfi",
        ),
    )
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda pc: f"function_{pc:x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda pc: f"source_{pc:x}")
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert [frame.pc for frame in report.frames] == [0x1004, 0x1100]
    assert [frame.symbol for frame in report.frames] == [
        "function_1004",
        "function_10ff",
    ]
    assert [frame.source for frame in report.frames] == ["source_1004", "source_10ff"]


def test_saved_pc_setup_reset_and_live_riscv_seed(monkeypatch):
    from core.unwind import (
        cached_unwind_metadata,
        live_seed,
    )
    from core.unwind_arm import (
        arm_ccr_stkalign,
        arm_cpuid,
        arm_external_exception_limit,
        armv6m_vector_entry,
        armv7m_vector_entry,
    )

    adapter = SimpleNamespace(version="4.0.5")
    bridge.reset_adapter(adapter)
    monkeypatch.setattr(bridge, "loaded_object_path", lambda: None)
    assert bridge._backtrace_context().reason == "port-undetermined"
    assert bridge.saved_pc_setup(adapter, False) is None
    assert bridge.saved_pc_setup(adapter, True) is None
    assert bridge._unwind_metadata(adapter) is None

    regs = {"sp": 0x2080, "ra": 0x1004, "s0": 0x2090}
    for index in range(1, 12):
        regs[f"s{index}"] = 0x2000 + index
    frame = SimpleNamespace(
        pc=lambda: 0x1000,
        read_register=lambda name: regs[name],
    )
    fake_gdb = SimpleNamespace(error=RuntimeError, newest_frame=lambda: frame)
    seed = live_seed("riscv", "riscv64", root_pc=0x900, gdb_module=fake_gdb)
    assert seed is not None and seed.pc == 0x1000 and seed.sp == 0x2080

    arm_regs = {f"r{i}": i for i in range(13)} | {
        "sp": 0x2000,
        "lr": 0x1005,
    }
    arm_frame = SimpleNamespace(
        pc=lambda: 0x1001,
        read_register=lambda name: arm_regs[name],
    )
    fake_arm_gdb = SimpleNamespace(error=RuntimeError, newest_frame=lambda: arm_frame)
    arm_seed = live_seed(
        "arm",
        "armv7-a",
        root_pc=0x800,
        require_exception_registers=False,
        gdb_module=fake_arm_gdb,
    )
    assert arm_seed is not None and arm_seed.pc == 0x1000 and arm_seed.sp == 0x2000

    mem = {
        0xE000ED14: (1 << 9).to_bytes(4, "little"),
        0xE000E004: (1).to_bytes(4, "little"),
        0xE000ED00: (0x410CC200).to_bytes(4, "little"),
        0xE000ED08: (0x08000000).to_bytes(4, "little"),
        0x08000000 + 4 * 15: (0x08000101).to_bytes(4, "little"),
        4 * 15: (0x00000201).to_bytes(4, "little"),
    }

    def reader(addr: int, _size: int) -> bytes | None:
        return mem.get(addr)

    assert arm_ccr_stkalign(read=reader) is True
    assert arm_external_exception_limit(read=reader) == 79
    assert arm_cpuid(read=reader) == 0x410CC200
    assert armv6m_vector_entry(15, read=reader) == 0x200
    assert armv7m_vector_entry(15, read=reader) == 0x08000100
    adapter = SimpleNamespace()
    assert cached_unwind_metadata(adapter, path_reader=lambda: None) is None


def _arm_current(monkeypatch, *, pending=0, exception=24, psp=0x2080):
    thread = Thread(
        name="worker",
        address=0x4000,
        entry=0x1200,
        stack_addr=0x2000,
        stack_size=0x400,
        stack_grows_up=False,
    )
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(bridge, "value_to_thread", lambda *_args, **_kwargs: thread)
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("armv7-m", "arm", ARMV7M_INTEGER, "ok"),
    )
    monkeypatch.setattr(bridge, "get_current_thread", lambda: 0x4000)
    monkeypatch.setattr(bridge, "value_address", lambda value: value)
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: pending if name == "rt_thread_switch_interrupt_flag" else None,
    )
    monkeypatch.setattr(bridge, "read_int", lambda value: value)
    seed = SeedFrame(
        0x1100,
        0x3080,
        {13: 0x3080, 14: 0xFFFFFFFD},
        source="current",
        process_sp=psp,
        exception_number=exception,
    )
    monkeypatch.setattr(bridge, "_live_seed_detail", lambda *_args: (seed, "ok"))
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: None)
    symbols = {"__StackLimit": 0x3000, "__StackTop": 0x3400}
    words = {0xE000ED08: 0x1000, 0xE000E004: 0, 0xE000ED14: 1 << 9, 0x1060: 0x1101}
    monkeypatch.setattr(bridge, "linker_symbol_address", symbols.get)
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, size: words.get(address, 0).to_bytes(size, "little"),
    )
    return SimpleNamespace(layout=object())


@pytest.mark.parametrize(
    "pending,reason",
    [(None, "scheduler-state-unavailable"), (1, "scheduler-transition-unproven")],
)
def test_pending_arm_switch_cannot_select_a_task_seed(monkeypatch, pending, reason):
    adapter = _arm_current(monkeypatch, pending=pending)
    monkeypatch.setattr(
        bridge, "_live_seed_detail", lambda *_args: pytest.fail("unstable owner read")
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == reason and report.frames == []


@pytest.mark.parametrize(
    "exception,psp,reason",
    [
        (24, 0x2500, "exception-task-context-conflict"),
        (15, 0x2080, "exception-number-unverified"),
        (14, 0x2080, "scheduler-transition-unproven"),
    ],
)
def test_arm_irq_ownership_and_exception_policy_precede_unwinding(
    monkeypatch, exception, psp, reason
):
    adapter = _arm_current(monkeypatch, exception=exception, psp=psp)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda *_args, **_kwargs: pytest.fail("unproven exception unwind"),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == reason and report.frames == []


def test_arm_irq_supplies_shared_segments_hooks_and_vector_entry_proof(monkeypatch):
    adapter = _arm_current(monkeypatch)
    seen = {}

    def unwind(seed, metadata, **kwargs):
        assert metadata is None and seed.at_exception_entry
        seen.update(kwargs)
        return UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "max-depth",
            False,
            True,
            "current",
            "seed",
        )

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    report = bridge.backtrace(adapter, "worker")
    assert report.truncated and seen["current_segment"] == "exception:24"
    assert [
        (segment.kind, segment.low, segment.high) for segment in seen["segments"]
    ] == [
        ("task", 0x2000, 0x2400),
        ("exception", 0x3000, 0x3400),
    ]
    assert seen["exception_detector"](0xFFFFFFFD)
    assert callable(seen["exception_hook"]) and callable(seen["exception_guard"])
    assert seen["stkalign"] is True
