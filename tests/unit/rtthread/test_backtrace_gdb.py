"""RT-Thread orchestration rejects unverified ports and missing bounds."""

from types import SimpleNamespace

import pytest

import rtthread.backtrace_gdb as bridge
from core.stack_frame_parser import StackContextSelection
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
