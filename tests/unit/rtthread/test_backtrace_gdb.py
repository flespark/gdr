"""RT-Thread orchestration rejects unverified ports and missing bounds."""

from types import SimpleNamespace

import pytest

import rtthread.backtrace_gdb as bridge
from core.stack_frame_parser import StackContextSelection
from core.unwind import SeedFrame, UnwindFrame, UnwindResult
from rtthread.adapter import Thread
from rtthread.backtrace import (
    ARMV6M_INTEGER,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_INTEGER,
    RISCV32_INTEGER,
    RISCV64_INTEGER,
)
from rtthread.layout import RT_THREAD_STACK_FILL


@pytest.mark.parametrize(
    "isa,context",
    [
        ("armv6-m", ARMV6M_INTEGER),
        ("armv7-m", ARMV7M_INTEGER),
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
        lambda: StackContextSelection(
            "armv7-m", "arm", ARMV7M_INTEGER, "unverified-port"
        ),
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
    from core.unwind_arm_gdb import (
        arm_ccr_stkalign,
        arm_cpuid,
        arm_external_exception_limit,
        armv6m_vector_entry,
        armv7m_vector_entry,
        cached_unwind_metadata,
        live_seed,
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
