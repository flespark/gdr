"""FreeRTOS orchestration contracts without a live GDB process."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

import freertos.backtrace_gdb as bridge
import freertos.stack_frame_profile as ports
from core.cpus import Cpu
from core.stack_frame_parser import StackContextSelection
from core.unwind import SeedFrame, UnwindFrame, UnwindResult
from freertos.adapter import FreeRtosTask
from freertos.backtrace import (
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_MPU_INTEGER,
    ARMV8M_MAINLINE_SMP_INTEGER,
)
from freertos.layout import FreeRtosConfig, build_layout


def _session(monkeypatch, *, isa="armv7-m", smp=False, mpu=False):
    layout = build_layout(FreeRtosConfig(smp=smp, mpu_context=mpu))
    context = {
        ("armv7-m", False, False): ARMV7M_INTEGER,
        ("armv8-m.mainline", False, True): ARMV8M_MAINLINE_MPU_INTEGER,
        ("armv8-m.mainline", True, False): ARMV8M_MAINLINE_SMP_INTEGER,
    }.get((isa, smp, mpu))
    selection = StackContextSelection(
        isa,
        "riscv" if isa.startswith("riscv") else "arm",
        context,
        "ok" if context is not None else "unverified-port",
    )
    adapter = SimpleNamespace(
        layout=layout, _context_selection=lambda: selection, _unwind_cache=None
    )
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
    monkeypatch.setattr(bridge, "value_to_task", lambda *_args, **_kwargs: task)
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
        monkeypatch, isa="armv8-m.mainline" if case == "m33" else "armv7-m"
    )
    if case == "missing":
        monkeypatch.setattr(bridge, "find_task", lambda *_args: None)
    elif case == "port":
        adapter._context_selection = lambda: StackContextSelection(
            None, "unknown", None, "port-undetermined"
        )
    elif case == "bounds":
        task.stack_size = None
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_a, **_k: pytest.fail("unsafe unwind")
    )
    report = bridge.backtrace(adapter, "worker", 3)
    assert report.stop_reason == reason and report.frames == []
    assert not report.complete and not report.truncated


def test_m33_profile_requires_explicit_smp_or_mpu_configuration(monkeypatch):
    monkeypatch.setattr(
        ports,
        "cpu_of",
        lambda _elf: Cpu("armv8-m.mainline", "arm", "ok"),
    )
    elf = SimpleNamespace(symbol_address=lambda _name: 0x1000)

    single_core = ports.select_context(elf)
    macros = {"configENABLE_FPU": "0", "configENABLE_TRUSTZONE": "0"}
    smp = ports.select_context(elf, smp=True, macros=macros | {"configENABLE_MPU": "0"})
    mpu = ports.select_context(
        elf,
        mpu=True,
        macros=macros | {"configENABLE_MPU": "1", "configUSE_MPU_WRAPPERS_V1": "0"},
    )

    assert single_core.context is None and single_core.reason == "unverified-port"
    assert smp.context is ARMV8M_MAINLINE_SMP_INTEGER and smp.reason == "ok"
    assert mpu.context is ARMV8M_MAINLINE_MPU_INTEGER and mpu.reason == "ok"
    assert smp.evidence == (
        ".ARM.attributes Tag_CPU_arch/profile",
        "port symbols",
        "kernel configuration",
    )


@pytest.mark.parametrize(
    "mpu,missing",
    [
        (mpu, name)
        for mpu in (False, True)
        for name in ("configENABLE_FPU", "configENABLE_TRUSTZONE", "configENABLE_MPU")
    ]
    + [(True, "configUSE_MPU_WRAPPERS_V1")],
)
def test_m33_missing_required_configuration_never_selects_a_profile(
    monkeypatch, mpu, missing
):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu("armv8-m.mainline", "arm", "ok", "aapcs32")
    )
    macros = {
        "configENABLE_FPU": "0",
        "configENABLE_TRUSTZONE": "0",
        "configENABLE_MPU": str(int(mpu)),
        "configUSE_MPU_WRAPPERS_V1": "0",
    }
    del macros[missing]
    selected = ports.select_context(
        SimpleNamespace(symbol_address=lambda _name: 0x1000),
        smp=not mpu,
        mpu=mpu,
        macros=macros,
    )
    assert (
        selected.context is None and selected.reason == "port-configuration-unavailable"
    )
    assert selected.isa == "armv8-m.mainline" and selected.abi == "aapcs32"


@pytest.mark.parametrize(
    "name,value,reason",
    [
        ("configENABLE_FPU", "1", "unsupported-extension-context"),
        ("configENABLE_MVE", "1", "unsupported-extension-context"),
        ("configENABLE_TRUSTZONE", "1", "port-configuration-unavailable"),
        ("configENABLE_MPU", "1", "port-configuration-conflict"),
    ],
)
def test_m33_conflicting_configuration_remains_rejected(
    monkeypatch, name, value, reason
):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu("armv8-m.mainline", "arm", "ok")
    )
    macros = {
        "configENABLE_FPU": "0",
        "configENABLE_TRUSTZONE": "0",
        "configENABLE_MPU": "0",
        name: value,
    }
    selected = ports.select_context(
        SimpleNamespace(symbol_address=lambda _name: 0x1000), smp=True, macros=macros
    )
    assert selected.context is None and selected.reason == reason


@pytest.mark.parametrize("validation", ["synthetic", "file-only"])
def test_nonlive_profiles_are_rejected_before_port_evidence(monkeypatch, validation):
    monkeypatch.setattr(ports, "cpu_of", lambda _elf: Cpu("armv7-m", "arm", "ok"))
    monkeypatch.setattr(
        ports, "ARMV7M_INTEGER", replace(ARMV7M_INTEGER, validation=validation)
    )
    elf = SimpleNamespace(symbol_address=lambda _name: pytest.fail("nonlive port read"))
    result = ports.select_context(elf)
    assert result.context is None and result.reason == "unverified-port"


@pytest.mark.parametrize("missing", ["PendSV_Handler", "pxPortInitialiseStack"])
def test_arm_profile_requires_each_port_symbol(monkeypatch, missing):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu("armv7-m", "arm", "ok", "aapcs32")
    )
    selected = ports.select_context(
        SimpleNamespace(symbol_address=lambda name: None if name == missing else 0x1000)
    )
    assert (
        selected.context is None
        and selected.reason == "unrecognizable-stack-frame-layout"
    )
    assert selected.isa == "armv7-m" and selected.abi == "aapcs32"


def test_arm_evidence_does_not_claim_compiled_riscv_proof(monkeypatch):
    monkeypatch.setattr(ports, "cpu_of", lambda _elf: Cpu("armv7-m", "arm", "ok"))
    selected = ports.select_context(
        SimpleNamespace(symbol_address=lambda _name: 0x1000)
    )
    assert selected.evidence == (".ARM.attributes Tag_CPU_arch/profile", "port symbols")


def test_mpu_wrapper_conflict_is_not_a_valid_context(monkeypatch):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu("armv8-m.mainline", "arm", "ok")
    )
    macros = {
        "configENABLE_FPU": "0",
        "configENABLE_TRUSTZONE": "0",
        "configENABLE_MPU": "1",
        "configUSE_MPU_WRAPPERS_V1": "1",
    }
    selected = ports.select_context(
        SimpleNamespace(symbol_address=lambda _name: 0x1000), mpu=True, macros=macros
    )
    assert selected.context is None and selected.reason == "port-configuration-conflict"


def test_unverified_profile_selection_precedes_stack_watermark_reads(monkeypatch):
    adapter, _task = _session(monkeypatch, isa="armv8-m.mainline")
    adapter._context_selection = lambda: StackContextSelection(
        "armv8-m.mainline", "arm", None, "unsupported-extension-context"
    )
    monkeypatch.setattr(
        bridge,
        "value_to_task",
        lambda *_args, **kwargs: (
            pytest.fail("task stack metrics were read")
            if kwargs.get("collect_stack_metrics")
            else FreeRtosTask(name="worker", address=0x4000)
        ),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "unsupported-extension-context"


def test_mpu_decode_receives_context_and_task_bounds_without_entry_evidence(
    monkeypatch,
):
    adapter, task = _session(monkeypatch, isa="armv8-m.mainline", mpu=True)
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
    adapter, task = _session(monkeypatch, isa="armv8-m.mainline", smp=True)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(1, task.address)])
    monkeypatch.setattr(
        bridge, "gdb", SimpleNamespace(selected_thread=lambda: SimpleNamespace(num=1))
    )
    monkeypatch.setattr(
        bridge, "_live_seed", lambda _arch: pytest.fail("wrong core registers")
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "smp-no-context" and report.frames == []
    assert report.architecture == "armv8-m.mainline" and not report.complete


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
