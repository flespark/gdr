"""RT-Thread orchestration rejects unverified ports and missing bounds."""

from types import SimpleNamespace

import pytest

import rtthread.backtrace_gdb as bridge
from rtthread.adapter import Thread


@pytest.mark.parametrize("port", ["cortex-m0", "cortex-m3", "cortex-m33", "riscv32"])
def test_unverified_context_never_reads_target_stack(monkeypatch, port):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge, "value_to_thread", lambda *_args: Thread(name="worker", address=0x4000)
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_port",
        lambda: (port, "riscv" if port.startswith("riscv") else "arm"),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unverified context read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.port == port and report.stop_reason == "unverified-port"
    assert report.frames == [] and not report.complete


@pytest.mark.parametrize("base,size", [(0, 1024), (0x2000, 0)])
def test_verified_port_still_needs_task_bounds(monkeypatch, base, size):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args: Thread(
            name="worker", address=0x4000, stack_addr=base, stack_size=size
        ),
    )
    monkeypatch.setattr(bridge, "_backtrace_port", lambda: ("riscv64", "riscv"))
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unbounded read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.stop_reason == "stack-bounds-unavailable"
    assert report.frames == [] and not report.complete
