"""FreeRTOS orchestration contracts without a live GDB process."""

from types import SimpleNamespace

import pytest

import freertos.backtrace_gdb as bridge
from core.unwind import SeedFrame, UnwindFrame, UnwindResult
from freertos.adapter import FreeRtosTask
from freertos.layout import FreeRtosConfig, build_layout


def _session(monkeypatch, *, port="cortex-m3", smp=False, mpu=False):
    layout = build_layout(FreeRtosConfig(smp=smp, mpu_context=mpu))
    adapter = SimpleNamespace(layout=layout, _port=lambda: port, _unwind_cache=None)
    task = FreeRtosTask(
        name="worker",
        address=0x4000,
        top_of_stack=0x2080,
        stack_base=0x2000,
        stack_size=0x200,
    )
    value = object()
    monkeypatch.setattr(bridge, "find_task", lambda _target, _layout: value)
    monkeypatch.setattr(bridge, "task_state", lambda _value, _layout: ("Blocked", None))
    monkeypatch.setattr(bridge, "value_to_task", lambda *_args: task)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [])
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (4, "little"))
    monkeypatch.setattr(bridge, "_exit_pc", lambda: 0x1200)
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: None)
    return adapter, task


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing", "task-not-found"),
        ("port", "port-undetermined"),
        ("bounds", "stack-bounds-unavailable"),
        ("m33", "unverified-port"),
    ],
)
def test_unavailable_inputs_never_reach_the_unwinder(monkeypatch, case, reason):
    adapter, task = _session(
        monkeypatch, port="cortex-m33" if case == "m33" else "cortex-m3"
    )
    if case == "missing":
        monkeypatch.setattr(bridge, "find_task", lambda *_args: None)
    elif case == "port":
        adapter._port = lambda: None
    elif case == "bounds":
        task.stack_size = None
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_a, **_k: pytest.fail("unsafe unwind")
    )
    report = bridge.backtrace(adapter, "worker", 3)
    assert report.stop_reason == reason and report.frames == []
    assert not report.complete and not report.truncated


def test_mpu_decode_receives_context_and_task_bounds_without_entry_evidence(
    monkeypatch,
):
    adapter, task = _session(monkeypatch, port="cortex-m33", mpu=True)
    task.top_of_stack = 0x3080
    monkeypatch.setattr(bridge, "saved_context_bounds", lambda *_args: (0x3000, 0x3100))
    seen = {}

    def decode(context, top, read, **kwargs):
        assert context.context_in_tcb and top == 0x3080
        assert read is bridge.read_bytes
        seen.update(kwargs)
        return SeedFrame(0x1004, 0x2100, {13: 0x2100}), "ok"

    def unwind(seed, metadata, **kwargs):
        assert metadata is None
        assert seed.root_pc == 0x1200 and seed.root_reason == "task-exit"
        assert kwargs["stack_low"] == 0x2000 and kwargs["stack_high"] == 0x2200
        assert kwargs["max_depth"] == 3
        return UnwindResult(
            (UnwindFrame(0, 0x1004, "seed"), UnwindFrame(1, 0x1200, "cfi")),
            "task-exit",
            True,
            False,
            "saved",
            "cfi",
        )

    monkeypatch.setattr(bridge, "decode_backtrace_context", decode)
    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda pc: f"function_{pc:x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    report = bridge.backtrace(adapter, "worker", 3)
    assert seen["context_low"] == 0x3000 and seen["context_high"] == 0x3100
    assert seen["stack_low"] == 0x2000 and seen["stack_high"] == 0x2200
    assert "entry_pc" not in seen
    assert report.source == "saved" and report.method == "cfi" and report.complete
    assert [frame.index for frame in report.frames] == [0, 1]


def test_other_running_core_never_uses_selected_core_registers(monkeypatch):
    adapter, task = _session(monkeypatch, port="cortex-m33", smp=True)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(1, task.address)])
    monkeypatch.setattr(
        bridge, "gdb", SimpleNamespace(selected_thread=lambda: SimpleNamespace(num=1))
    )
    monkeypatch.setattr(
        bridge, "_live_seed", lambda _arch: pytest.fail("wrong core registers")
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "smp-no-context" and report.frames == []
    assert report.port == "cortex-m33-smp" and not report.complete


@pytest.mark.parametrize("value", [0, 0x1234, None])
def test_exit_variable_uses_its_value_not_its_storage_address(monkeypatch, value):
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: value if name == "xTaskReturnAddress" else None,
    )
    monkeypatch.setattr(
        bridge,
        "value_address",
        lambda _value: pytest.fail("variable storage is not code"),
    )
    assert bridge._exit_pc() == value


def test_exit_function_uses_its_code_address(monkeypatch):
    function = object()
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: function if name == "prvTaskExitError" else None,
    )
    monkeypatch.setattr(
        bridge, "value_address", lambda value: 0x1234 if value is function else 0
    )
    assert bridge._exit_pc() == 0x1234
