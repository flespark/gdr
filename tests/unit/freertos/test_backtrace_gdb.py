"""FreeRTOS orchestration contracts without a live GDB process."""

import struct
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

import freertos.backtrace.unwind_gdb as bridge
import freertos.stack_frame_profile as ports
from core.backtrace.unwind import SeedFrame, UnwindFrame, UnwindResult, unwind_chain
from core.backtrace.unwind_arm import (
    armv7m_exception_return_candidate,
    armv7m_exception_transition,
)
from core.backtrace.unwind_cfi import CfiTable
from core.backtrace.unwind_segments import StackSegment
from core.elf import Cpu
from core.stack_frame_parser import StackContextSelection
from freertos.adapter import FreeRtosTask
from freertos.backtrace.unwind import (
    ARMV6M_INTEGER,
    ARMV7M_CM4F_BASIC,
    ARMV7M_CM4F_FPU,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_FPU,
    ARMV8M_MAINLINE_INTEGER,
    ARMV8M_MAINLINE_MPU_INTEGER,
    ARMV8M_MAINLINE_SMP_INTEGER,
)
from freertos.layout import FreeRtosConfig, build_layout


def _elf(symbols=lambda name: None if name == "vPortEnableVFP" else 0x1000):
    """Untyped ELF double; selection tests stub ``cpu_of`` above it."""
    return cast(Any, SimpleNamespace(symbol_address=symbols))


def _session(monkeypatch, *, isa="armv7-m", smp=False, mpu=False):
    layout = build_layout(FreeRtosConfig(smp=smp, mpu_context=mpu))
    context = {
        ("armv6-m", False, False): ARMV6M_INTEGER,
        ("armv7-m", False, False): ARMV7M_INTEGER,
        ("armv8-m.mainline", False, False): ARMV8M_MAINLINE_INTEGER,
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


class _LiveFrame:
    """Minimal ``gdb.Frame`` stand-in with one optional failing register."""

    def __init__(
        self,
        *,
        missing: str | None = None,
        pc: int = 0x1004,
        sp: int = 0x20004000,
        xpsr: int = 0x01000000,
        msp: int = 0x203FF000,
        psp: int = 0x20004000,
    ) -> None:
        self.missing = missing
        self._pc = pc
        self._registers = {f"r{index}": index for index in range(13)}
        self._registers.update(
            {
                "sp": sp,
                "lr": 0x1001,
                "xpsr": xpsr,
                "msp": msp,
                "psp": psp,
                "psplim": 0,
                "control": 2,
            }
        )

    def pc(self) -> int:
        return self._pc

    def read_register(self, name: str) -> int:
        if name == self.missing:
            raise RuntimeError(f"{name} is unavailable")
        return self._registers[name]


def _live_gdb(frame):
    return SimpleNamespace(
        newest_frame=lambda: frame,
        selected_thread=lambda: SimpleNamespace(num=1),
        error=RuntimeError,
    )


@pytest.mark.parametrize("missing", ["xpsr", "msp", "psp"])
def test_live_arm_seed_refuses_incomplete_exception_registers(monkeypatch, missing):
    monkeypatch.setattr(bridge, "gdb", _live_gdb(_LiveFrame(missing=missing)))
    seed, reason = bridge._live_seed_detail("arm")
    assert seed is None
    assert reason == "exception-registers-unavailable"


@pytest.mark.parametrize("missing", ["xpsr", "msp", "psp"])
def test_incomplete_live_exception_state_reaches_the_report_gate(monkeypatch, missing):
    adapter, task = _session(monkeypatch)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    monkeypatch.setattr(bridge, "gdb", _live_gdb(_LiveFrame(missing=missing)))
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_args, **_kwargs: pytest.fail("unsafe unwind")
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "exception-registers-unavailable"
    assert report.frames == [] and report.source == "current"
    assert not report.complete and not report.truncated


def test_live_arm_thread_seed_reads_every_register(monkeypatch):
    monkeypatch.setattr(bridge, "gdb", _live_gdb(_LiveFrame()))
    seed, reason = bridge._live_seed_detail("arm")
    assert reason == "ok" and seed is not None
    assert seed.pc == 0x1004 and seed.sp == 0x20004000
    assert seed.exception_number == 0 and seed.process_sp == 0x20004000
    assert seed.source == "current"
    assert seed.regs == {
        **{index: index for index in range(13)},
        13: 0x20004000,
        14: 0x1001,
        15: 0x1004,
    }


@pytest.mark.parametrize("ipsr", [24, 25, 511])
def test_live_arm_handler_seed_keeps_a_valid_exception_state(monkeypatch, ipsr):
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000000 | ipsr, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    seed, reason = bridge._live_seed_detail("arm", "armv7-m")
    assert reason == "ok" and seed is not None
    assert seed.exception_number == ipsr and seed.process_sp == 0x20004000


@pytest.mark.parametrize("ipsr", [1, 7, 8, 9, 10, 13])
def test_live_arm_seed_refuses_an_architecturally_invalid_ipsr(monkeypatch, ipsr):
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000000 | ipsr, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    seed, reason = bridge._live_seed_detail("arm", "armv7-m")
    assert seed is None and reason == "exception-number-invalid"


@pytest.mark.parametrize("ipsr", [1, 7, 8, 9, 10, 13])
def test_live_invalid_ipsr_reaches_the_report_gate(monkeypatch, ipsr):
    adapter, task = _session(monkeypatch)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000000 | ipsr, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_args, **_kwargs: pytest.fail("unsafe unwind")
    )
    monkeypatch.setattr(
        bridge,
        "_exception_guard",
        lambda *_args: pytest.fail("invalid IPSR reached the exception guard"),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "exception-number-invalid"
    assert report.frames == [] and report.source == "current"
    assert not report.complete and not report.truncated


@pytest.mark.parametrize("ipsr", [2, 12])
def test_live_system_exceptions_nmi_and_debugmon_are_admitted(monkeypatch, ipsr):
    adapter, task = _session(monkeypatch)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000000 | ipsr, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x203FC000, 0x20400000))
    monkeypatch.setattr(bridge, "_external_exception_limit", lambda: 47)
    monkeypatch.setattr(bridge, "_ccr_stkalign", lambda: True)
    monkeypatch.setattr(bridge, "_vector_entry", lambda _number: 0x1004)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda seed, _meta, **_kwargs: UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "no-unwind-info",
            False,
            False,
            "current",
            "seed",
        ),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "no-unwind-info"
    assert report.source == "current" and len(report.frames) == 1


def test_live_handler_seed_requires_profile_evidence(monkeypatch):
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000018, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    seed, reason = bridge._live_seed_detail("arm")
    assert seed is None and reason == "unsupported-exception-model"


def test_live_armv8_seed_does_not_apply_armv7_reserved_numbers(monkeypatch):
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000007, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    seed, reason = bridge._live_seed_detail("arm", "armv8-m.mainline")
    assert reason == "ok" and seed is not None
    assert seed.exception_number == 7


def test_armv8_live_handler_seed_keeps_the_unsupported_profile_gate(monkeypatch):
    adapter, task = _session(monkeypatch, isa="armv8-m.mainline", smp=True)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000007, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_args, **_kwargs: pytest.fail("unsafe unwind")
    )
    monkeypatch.setattr(
        bridge,
        "_external_exception_limit",
        lambda: pytest.fail("Armv8-M seed read the Armv7-M ICTR gate"),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "unsupported-exception-model"
    assert report.frames == [] and report.source == "current"


def _live_handler_report(monkeypatch, ipsr, *, ictr_raw=b"\x00\x00\x00\x00"):
    adapter, task = _session(monkeypatch)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    frame = _LiveFrame(sp=0x203FFFD0, xpsr=0x01000000 | ipsr, msp=0x203FFFD0)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x203FC000, 0x20400000))
    monkeypatch.setattr(bridge, "_ccr_stkalign", lambda: True)
    monkeypatch.setattr(bridge, "_vector_entry", lambda _number: None)
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, _size: ictr_raw if address == 0xE000E004 else None,
    )
    reached = []

    def unwind(seed, _metadata, **_kwargs):
        reached.append(seed.exception_number)
        return UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "task-exit",
            True,
            False,
            seed.source,
            "seed",
        )

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    return bridge.backtrace(adapter, "worker"), reached


@pytest.mark.parametrize("ipsr", [24, 25, 47])
def test_live_implemented_external_ipsr_reaches_unwind(monkeypatch, ipsr):
    report, reached = _live_handler_report(monkeypatch, ipsr)
    assert report.stop_reason == "task-exit"
    assert report.frames and reached == [ipsr]


@pytest.mark.parametrize("ipsr", [48, 511])
def test_live_unimplemented_external_ipsr_stops_before_unwind(monkeypatch, ipsr):
    report, reached = _live_handler_report(monkeypatch, ipsr)
    assert report.stop_reason == "exception-number-unimplemented"
    assert report.frames == [] and reached == []
    assert report.source == "current"


@pytest.mark.parametrize(
    "ictr_raw",
    [None, b"\x10\x00\x00\x00", b"\x00\x00"],
)
def test_live_unusable_ictr_fails_closed(monkeypatch, ictr_raw):
    report, reached = _live_handler_report(monkeypatch, 24, ictr_raw=ictr_raw)
    assert report.stop_reason == "exception-capacity-unavailable"
    assert report.frames == [] and reached == []


@pytest.mark.parametrize(
    "raw,limit",
    [
        (b"\x00\x00\x00\x00", 47),
        (b"\x0f\x00\x00\x00", 511),
        (b"\x10\x00\x00\x00", None),
        (b"\x00\x00", None),
        (None, None),
    ],
)
def test_external_exception_limit_reads_only_ictr(monkeypatch, raw, limit):
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, _size: (
            raw if address == 0xE000E004 else pytest.fail("unexpected target read")
        ),
    )
    assert bridge._external_exception_limit() == limit


@pytest.mark.parametrize("ipsr", [24, 25, 47])
def test_capacity_guard_admits_implemented_external_numbers(ipsr):
    assert bridge._capacity_exception_guard(47)(ipsr, 0x1000) is None


@pytest.mark.parametrize("ipsr", [48, 511])
def test_capacity_guard_refuses_unimplemented_external_numbers(ipsr):
    assert (
        bridge._capacity_exception_guard(47)(ipsr, 0x1000)
        == "exception-number-unimplemented"
    )


def test_capacity_guard_uses_the_supplied_target_limit():
    assert bridge._capacity_exception_guard(79)(79, 0x1000) is None
    assert (
        bridge._capacity_exception_guard(79)(80, 0x1000)
        == "exception-number-unimplemented"
    )


@pytest.mark.parametrize("ipsr", [48, 511])
def test_restored_unimplemented_external_ipsr_stops_before_transition(ipsr):
    task = StackSegment(
        "task:test", "test", 0x2000, 0x2200, "task-recorded", "process", "task"
    )
    irq = StackSegment(
        "exception:24", "exception:24", 0x3000, 0x4000, "linker", "main", "exception"
    )
    frame = struct.pack("<8I", 0, 0, 0, 0, 0, 0xFFFFFFF1, 0x1008, 0x01000000 | ipsr)
    memory = {0x3010: frame}

    def read(address, size):
        raw = memory.get(address)
        return raw[:size] if raw is not None and len(raw) >= size else None

    seed = SeedFrame(
        0x1004,
        0x3010,
        {13: 0x3010, 14: 0xFFFFFFF1},
        source="current",
        process_sp=0x2100,
        at_exception_entry=True,
    )
    result = unwind_chain(
        seed,
        None,
        read=read,
        max_depth=4,
        segments=(task, irq),
        current_segment=irq.ident,
        exception_detector=armv7m_exception_return_candidate,
        exception_hook=armv7m_exception_transition,
        exception_guard=bridge._capacity_exception_guard(47),
    )
    assert [frame.pc for frame in result.frames] == [0x1004]
    assert result.stop_reason == "exception-number-unimplemented"


@pytest.mark.parametrize(
    "msp,psp",
    [(0, 0x20004000), (0x203FF000, 0)],
)
def test_live_arm_handler_seed_rejects_an_empty_stack_pointer(monkeypatch, msp, psp):
    frame = _LiveFrame(sp=msp, xpsr=0x01000018, msp=msp, psp=psp)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    seed, reason = bridge._live_seed_detail("arm")
    assert seed is None and reason == "live-registers-unavailable"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing", "task-not-found"),
        ("port", "port-undetermined"),
        ("bounds", "stack-bounds-unavailable"),
        ("m23", "unverified-port"),
    ],
)
def test_unavailable_inputs_never_reach_the_unwinder(monkeypatch, case, reason):
    adapter, task = _session(
        monkeypatch, isa="armv8-m.baseline" if case == "m23" else "armv7-m"
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


def test_m33_fpu_profile_and_live_saved_gates(monkeypatch):
    for abi in ("aapcs32-vfp", "aapcs32"):
        monkeypatch.setattr(
            ports,
            "cpu_of",
            lambda _elf, _abi=abi: Cpu("armv8-m.mainline", "arm", "ok", abi=_abi),
        )
        macros = {
            "configENABLE_FPU": "1",
            "configENABLE_TRUSTZONE": "0",
            "configENABLE_MPU": "0",
            "configENABLE_MVE": "0",
        }
        selected = ports.select_context(_elf(), macros=macros)
        assert selected.reason == "ok"
        assert selected.context == ARMV8M_MAINLINE_INTEGER
        assert selected.alt_context == ARMV8M_MAINLINE_FPU

        missing_sym = ports.select_context(
            _elf(
                lambda name: (
                    None if name in ("vPortEnableVFP", "prvSetupFPU") else 0x1000
                )
            ),
            macros=macros,
        )
        assert missing_sym.context is None
        assert missing_sym.reason == "unrecognizable-stack-frame-layout"

    adapter, task = _session(monkeypatch, isa="armv8-m.mainline")
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    monkeypatch.setattr(
        bridge,
        "gdb",
        _live_gdb(
            _LiveFrame(
                pc=0x1004, sp=0x2080, xpsr=0x01000000, msp=0x203FF000, psp=0x2080
            )
        ),
    )
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_a, **_k: pytest.fail("unsafe unwind")
    )

    monkeypatch.setattr(bridge, "_read_armv8m_regs", lambda: (0x2004, 0x02))
    bad_psplim = bridge.backtrace(adapter, "worker")
    assert bad_psplim.stop_reason == "unsupported-extension-context"

    monkeypatch.setattr(bridge, "_read_armv8m_regs", lambda: (0x2000, 0x03))
    unpriv = bridge.backtrace(adapter, "worker")
    assert unpriv.stop_reason == "unsupported-extension-context"

    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [])
    adapter._context_selection = lambda: StackContextSelection(
        "armv8-m.mainline",
        "arm",
        ARMV8M_MAINLINE_INTEGER,
        "ok",
        alt_context=ARMV8M_MAINLINE_FPU,
    )
    called: dict[str, Any] = {}

    def fake_cm33(top, _read, **kwargs):
        called["top"] = top
        called["basic"] = kwargs["basic"]
        called["fpu"] = kwargs["fpu"]
        return SeedFrame(0x1004, 0x2150, {13: 0x2150}), "ok"

    monkeypatch.setattr(bridge, "decode_cm33_saved_context", fake_cm33)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda pc: f"fn_{pc:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda seed, _meta, **_kwargs: UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "task-exit",
            True,
            False,
            "saved",
            "cfi",
        ),
    )
    saved = bridge.backtrace(adapter, "worker")
    assert saved.complete and saved.source == "saved"
    assert called == {
        "top": task.top_of_stack,
        "basic": ARMV8M_MAINLINE_INTEGER,
        "fpu": ARMV8M_MAINLINE_FPU,
    }


def test_m33_profile_requires_explicit_configuration(monkeypatch):
    monkeypatch.setattr(
        ports,
        "cpu_of",
        lambda _elf: Cpu("armv8-m.mainline", "arm", "ok"),
    )
    elf = _elf()

    missing_macros = ports.select_context(elf)
    macros = {"configENABLE_FPU": "0", "configENABLE_TRUSTZONE": "0"}
    single_core = ports.select_context(elf, macros=macros | {"configENABLE_MPU": "0"})
    smp = ports.select_context(elf, smp=True, macros=macros | {"configENABLE_MPU": "0"})
    mpu = ports.select_context(
        elf,
        mpu=True,
        macros=macros | {"configENABLE_MPU": "1", "configUSE_MPU_WRAPPERS_V1": "0"},
    )

    assert (
        missing_macros.context is None
        and missing_macros.reason == "port-configuration-unavailable"
    )
    assert single_core.context is ARMV8M_MAINLINE_INTEGER and single_core.reason == "ok"
    assert smp.context is ARMV8M_MAINLINE_SMP_INTEGER and smp.reason == "ok"
    assert mpu.context is ARMV8M_MAINLINE_MPU_INTEGER and mpu.reason == "ok"
    assert single_core.evidence == (
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
        _elf(),
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
    selected = ports.select_context(_elf(), smp=True, macros=macros)
    assert selected.context is None and selected.reason == reason


@pytest.mark.parametrize("validation", ["synthetic", "file-only"])
def test_nonlive_profiles_are_rejected_before_port_evidence(monkeypatch, validation):
    monkeypatch.setattr(ports, "cpu_of", lambda _elf: Cpu("armv7-m", "arm", "ok"))
    monkeypatch.setattr(
        ports, "ARMV7M_INTEGER", replace(ARMV7M_INTEGER, validation=validation)
    )
    elf = _elf(lambda _name: pytest.fail("nonlive port read"))
    result = ports.select_context(elf)
    assert result.context is None and result.reason == "unverified-port"


@pytest.mark.parametrize("missing", ["PendSV_Handler", "pxPortInitialiseStack"])
def test_arm_profile_requires_each_port_symbol(monkeypatch, missing):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu("armv7-m", "arm", "ok", "aapcs32")
    )
    selected = ports.select_context(
        _elf(lambda name: None if name in (missing, "vPortEnableVFP") else 0x1000)
    )
    assert (
        selected.context is None
        and selected.reason == "unrecognizable-stack-frame-layout"
    )
    assert selected.isa == "armv7-m" and selected.abi == "aapcs32"


def test_baseline_without_lane_is_refused(monkeypatch):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu("armv8-m.baseline", "arm", "ok")
    )
    selected = ports.select_context(_elf(), macros={})
    assert selected.context is None
    assert selected.reason == "unverified-port"


def test_arm_evidence_does_not_claim_compiled_riscv_proof(monkeypatch):
    monkeypatch.setattr(ports, "cpu_of", lambda _elf: Cpu("armv7-m", "arm", "ok"))
    selected = ports.select_context(_elf())
    assert selected.evidence == (".ARM.attributes Tag_CPU_arch/profile", "port symbols")


@pytest.mark.parametrize("isa", ["armv7-m", "armv7e-m"])
def test_cm4f_fpu_port_selects_the_live_pair(monkeypatch, isa):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu(isa, "arm", "ok", abi="aapcs32-vfp")
    )
    selected = ports.select_context(
        _elf(lambda name: 0x1000 if name == "vPortEnableVFP" else None),
        macros={},
    )
    assert selected.reason == "ok"
    assert selected.context is not None and selected.context.profile_id.endswith(
        "/basic"
    )
    assert selected.alt_context is not None and selected.alt_context.fpu
    assert selected.evidence == ("aapcs32-vfp ABI", "port symbols")


@pytest.mark.parametrize("isa", ["armv7-m", "armv7e-m"])
def test_soft_abi_with_fpu_macro_does_not_select_cm4f(monkeypatch, isa):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu(isa, "arm", "ok", abi="aapcs32")
    )
    selected = ports.select_context(_elf(), macros={"configENABLE_FPU": "1"})
    assert selected.alt_context is None
    assert selected.context is None or "cm4f" not in selected.context.profile_id


def test_cm4f_on_mainline_selects_the_live_pair(monkeypatch):
    monkeypatch.setattr(
        ports,
        "cpu_of",
        lambda _elf: Cpu("armv8-m.mainline", "arm", "ok", abi="aapcs32-vfp"),
    )
    selected = ports.select_context(
        _elf(
            lambda name: (
                0x1000 if name in ("vPortEnableVFP", "xPortSysTickHandler") else None
            )
        ),
        macros={},
    )
    assert selected.reason == "ok"
    assert selected.context is not None and selected.alt_context is not None


@pytest.mark.parametrize("isa", ["armv7-m", "armv7e-m"])
def test_softfp_cm4f_port_selects_the_live_pair(monkeypatch, isa):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu(isa, "arm", "ok", abi="aapcs32")
    )
    selected = ports.select_context(
        _elf(lambda name: 0x1000 if name == "vPortEnableVFP" else None),
        macros={},
    )
    assert selected.reason == "ok"
    assert selected.context is ARMV7M_CM4F_BASIC
    assert selected.alt_context is ARMV7M_CM4F_FPU
    assert selected.evidence == ("aapcs32 ABI", "port symbols")


def test_armv7em_soft_cm3_port_selects_integer_profile(monkeypatch):
    monkeypatch.setattr(
        ports, "cpu_of", lambda _elf: Cpu("armv7e-m", "arm", "ok", abi="aapcs32")
    )
    selected = ports.select_context(_elf(), macros={})
    assert selected.reason == "ok"
    assert selected.context is ARMV7M_INTEGER
    assert selected.alt_context is None


def test_mainline_vfp_without_cm4f_port_symbol_is_refused(monkeypatch):
    monkeypatch.setattr(
        ports,
        "cpu_of",
        lambda _elf: Cpu("armv8-m.mainline", "arm", "ok", abi="aapcs32-vfp"),
    )
    selected = ports.select_context(_elf(lambda _name: None), macros={})
    assert selected.context is None
    assert selected.reason == "unsupported-extension-context"


def _cm4f_live_selection():
    basic = replace(ARMV7M_CM4F_BASIC, validation="live")
    fpu = replace(ARMV7M_CM4F_FPU, validation="live")
    return StackContextSelection(
        "armv7-m", "arm", basic, "ok", ("test",), "aapcs32", fpu
    )


def _cm4f_saved_report(monkeypatch, *, fpu_frame: bool):
    adapter, task = _session(monkeypatch)
    adapter._context_selection = lambda: _cm4f_live_selection()
    top = task.top_of_stack
    memory = _WordMemory()
    for index in range(8):
        memory[top + index * 4] = 0x40 + index
    memory[top + 32] = 0xFFFFFFED if fpu_frame else 0xFFFFFFFD
    base = top + (100 if fpu_frame else 36)
    if fpu_frame:
        for index in range(16):
            memory[top + 36 + index * 4] = 0x55000000 + index
    memory[base] = 0
    memory[base + 4] = 0x2000
    memory[base + 8] = 0x100
    memory[base + 12] = 0x200
    memory[base + 16] = 0x300
    memory[base + 20] = 0x1200
    memory[base + 24] = 0x1000
    memory[base + 28] = 0x01000000
    monkeypatch.setattr(bridge, "read_bytes", memory.read)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [])
    seeds = []

    def unwind(seed, _metadata, **_kwargs):
        seeds.append(seed)
        return UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "max-depth",
            False,
            False,
            seed.source,
            "seed",
        )

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    return bridge.backtrace(adapter, "worker"), seeds


@pytest.mark.parametrize("fpu_frame,sp_offset", [(False, 68), (True, 204)])
def test_cm4f_wiring_dispatches_saved_tasks_by_exc_return_slot(
    monkeypatch, fpu_frame, sp_offset
):
    report, seeds = _cm4f_saved_report(monkeypatch, fpu_frame=fpu_frame)
    assert len(seeds) == 1
    assert seeds[0].sp == 0x2080 + sp_offset
    assert seeds[0].source == "saved"
    assert [frame.pc for frame in report.frames] == [0x1000]
    assert report.stop_reason == "max-depth" and not report.complete


def test_cm4f_wiring_refuses_foreign_exc_return_slot(monkeypatch):
    adapter, task = _session(monkeypatch)
    adapter._context_selection = lambda: _cm4f_live_selection()
    memory = _WordMemory()
    memory[task.top_of_stack + 32] = 0x12345678
    monkeypatch.setattr(bridge, "read_bytes", memory.read)
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [])
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_args, **_kwargs: pytest.fail("unsafe unwind")
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "saved-context-exc-return-unproven"
    assert report.frames == [] and not report.complete


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
    selected = ports.select_context(_elf(), mpu=True, macros=macros)
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
            (UnwindFrame(0, 0x1004, "seed"), UnwindFrame(1, 0x1200, "cfi", 0x11FF)),
            "task-exit",
            True,
            False,
            "saved",
            "cfi",
        )

    monkeypatch.setattr(bridge, "decode_backtrace_context", decode)
    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda pc: f"function_{pc:x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda pc: f"source_{pc:x}")
    report = bridge.backtrace(adapter, "worker", 3)
    assert seen["context_low"] == 0x3000 and seen["context_high"] == 0x3100
    assert seen["stack_low"] == 0x2000 and seen["stack_high"] == 0x2200
    assert "entry_pc" not in seen
    assert report.source == "saved" and report.method == "cfi" and report.complete
    assert [frame.index for frame in report.frames] == [0, 1]
    assert [frame.pc for frame in report.frames] == [0x1004, 0x1200]
    assert [frame.symbol for frame in report.frames] == [
        "function_1004",
        "function_11ff",
    ]
    assert [frame.source for frame in report.frames] == ["source_1004", "source_11ff"]


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


def test_irq_bounds_require_both_explicit_linker_symbols(monkeypatch):
    values = {
        "__gdr_irq_stack_low": 0x203FC000,
        "__gdr_irq_stack_top": 0x20400000,
    }
    monkeypatch.setattr(bridge, "linker_symbol_address", lambda name: values.get(name))
    assert bridge._irq_stack_bounds() == (0x203FC000, 0x20400000)
    del values["__gdr_irq_stack_top"]
    assert bridge._irq_stack_bounds() is None


def test_irq_bounds_agnostic_tiers_resolve_cmsis_sections_and_vector_msp(monkeypatch):
    # Tier 2: CMSIS / GCC standard stack symbols (__StackLimit..__StackTop).
    symbols = {"__StackLimit": 0x2001C000, "__StackTop": 0x20020000}
    monkeypatch.setattr(bridge, "linker_symbol_address", lambda name: symbols.get(name))
    assert bridge._irq_stack_bounds() == (0x2001C000, 0x20020000)

    # Tier 3: Dedicated ELF stack section (.stack) when only _estack is exported.
    symbols = {"_estack": 0x20020000}
    elf = _elf(None)
    elf.symbol_address = lambda _name: None
    elf.section_bounds = lambda name: (
        (0x2001E000, 0x20020000) if name == ".stack" else None
    )
    assert bridge._irq_stack_bounds(elf=elf) == (0x2001E000, 0x20020000)

    # Tier 4a: Vector-table word 0 (via VTOR) + standard low symbol (_sstack).
    symbols = {"_sstack": 0x2001F000}
    memory = _WordMemory({0xE000ED08: 0x08000000, 0x08000000: 0x20020000})
    monkeypatch.setattr(bridge, "read_bytes", memory.read)
    assert bridge._irq_stack_bounds(isa="armv7-m") == (0x2001F000, 0x20020000)

    # Tier 4b: Vector-table word 0 + halted live_msp with non-overlapping task stack.
    symbols = {}
    assert bridge._irq_stack_bounds(
        isa="armv7-m",
        live_msp=0x2001FF80,
        task_bounds=(0x20001000, 0x20002000),
    ) == (0x2001FF80, 0x20020000)

    # Armv6-M vector word 0 at 0x0 + halted live_msp.
    memory_v6m = _WordMemory({0x0: 0x20008000})
    monkeypatch.setattr(bridge, "read_bytes", memory_v6m.read)
    assert bridge._irq_stack_bounds(
        isa="armv6-m",
        live_msp=0x20007F00,
        task_bounds=(0x20001000, 0x20002000),
    ) == (0x20007F00, 0x20008000)

    # Overlap with task_bounds must fail closed.
    assert (
        bridge._irq_stack_bounds(
            isa="armv7-m",
            live_msp=0x2001FF80,
            task_bounds=(0x2001F000, 0x20020000),
        )
        is None
    )


def _handler_session(monkeypatch, *, exception_number=24, isa="armv7-m"):
    adapter, task = _session(monkeypatch, isa=isa, smp=isa == "armv8-m.mainline")
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    seed = SeedFrame(
        0x1004,
        0x203FFFD0,
        {13: 0x203FFFD0, 14: 0xFFFFFFFD},
        source="current",
        process_sp=0x20004000,
        exception_number=exception_number,
    )
    monkeypatch.setattr(
        bridge, "_live_seed_detail", lambda _arch, _isa=None: (seed, "ok")
    )
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_a, **_k: pytest.fail("unproven")
    )
    return adapter, task


@pytest.mark.parametrize(
    "isa,reason",
    [
        ("armv8-m.baseline", "unverified-port"),
    ],
)
def test_handler_mode_refuses_unverified_isa_profiles(monkeypatch, isa, reason):
    adapter, _task = _handler_session(monkeypatch, isa=isa)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == reason and report.frames == []


def test_handler_mode_refuses_missing_interrupt_bounds(monkeypatch):
    adapter, _task = _handler_session(monkeypatch)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: None)
    monkeypatch.setattr(
        bridge,
        "_external_exception_limit",
        lambda: pytest.fail("capacity read without an IRQ stack span"),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "exception-stack-bounds-unavailable"
    assert report.frames == []


def test_handler_mode_refuses_svc_and_pendsv_ownership(monkeypatch):
    adapter, _task = _handler_session(monkeypatch, exception_number=14)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "scheduler-transition-unproven"
    assert report.frames == []


def test_handler_mode_refuses_synchronous_fault_ownership(monkeypatch):
    adapter, _task = _handler_session(monkeypatch, exception_number=3)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "fault-frame-imprecise"
    assert report.frames == []


@pytest.mark.parametrize(
    "complete,raw_reason,expected_reason",
    [
        (True, "task-exit", "synchronous-fault"),
        (False, "max-depth", "max-depth"),
    ],
)
def test_handler_mode_precise_fault_preserves_failure_or_marks_synchronous_fault(
    monkeypatch, complete, raw_reason, expected_reason
):
    adapter, _task = _handler_session(monkeypatch, exception_number=6)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x203FC000, 0x20400000))
    monkeypatch.setattr(bridge, "_external_exception_limit", lambda: 47)
    monkeypatch.setattr(bridge, "_ccr_stkalign", lambda: True)
    monkeypatch.setattr(bridge, "_vector_entry", lambda _n: None)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    words = {0xE000ED28: 0x00010000, 0xE000ED2C: 0}
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, size: words.get(address, 0).to_bytes(size, "little"),
    )
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda seed, *_a, **_k: UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            raw_reason,
            complete,
            not complete,
            "current",
            "seed",
        ),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.complete is complete
    assert report.stop_reason == expected_reason
    assert any("synchronous fault UsageFault:UNDEFINSTR" in m for m in report.messages)


@pytest.mark.parametrize("ipsr", [1, 7, 8, 9, 10, 13])
def test_exception_guard_refuses_architecturally_invalid_numbers(ipsr):
    assert bridge._exception_guard(ipsr, 0x1000) == "exception-number-invalid"


@pytest.mark.parametrize("ipsr", [2, 12, 15, 16, 24, 25, 511])
def test_exception_guard_admits_system_and_the_architectural_external_range(ipsr):
    assert bridge._exception_guard(ipsr, 0x1000) is None


def test_exception_guard_refuses_debugmon_on_armv6m():
    assert bridge._exception_guard(2, 0x1000, "armv6-m") is None
    assert bridge._exception_guard(12, 0x1000, "armv6-m") == "exception-number-invalid"


def test_exception_guard_preserves_scheduler_and_fault_refusals():
    assert bridge._exception_guard(14, 0x1000) == "scheduler-transition-unproven"
    assert bridge._exception_guard(11, 0x1000) == "scheduler-transition-unproven"
    assert bridge._exception_guard(3, 0x1000) == "exception-frame-type-unproven"
    assert bridge._exception_guard(6, 0x1000) == "exception-frame-type-unproven"
    assert bridge._exception_guard(0, 0x1000) is None


def test_handler_mode_refuses_an_unreadable_scheduler_state(monkeypatch):
    adapter, _task = _handler_session(monkeypatch)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: None)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "scheduler-state-unavailable"
    assert report.frames == []


def test_handler_mode_refuses_a_stopped_scheduler(monkeypatch):
    adapter, _task = _handler_session(monkeypatch)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: False)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "scheduler-not-running"
    assert report.frames == []


def test_handler_mode_refuses_an_msp_outside_the_linker_span(monkeypatch):
    adapter, _task = _handler_session(monkeypatch)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x1000, 0x2000))
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "exception-stack-outside-bounds"
    assert report.frames == []


def test_handler_mode_refuses_unreadable_stkalign(monkeypatch):
    adapter, _task = _handler_session(monkeypatch)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x203FC000, 0x20400000))
    monkeypatch.setattr(bridge, "_external_exception_limit", lambda: 47)
    monkeypatch.setattr(bridge, "_ccr_stkalign", lambda: None)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == "exception-alignment-unavailable"
    assert report.frames == []


class _WordMemory(dict[int, int]):
    def read(self, address: int, size: int) -> bytes | None:
        values = []
        for offset in range(0, size, 4):
            value = self.get(address + offset)
            if value is None:
                return None
            values.append(value.to_bytes(4, "little"))
        return b"".join(values)


def _scheduler_profile():
    return SimpleNamespace(
        profile_id="test/profile",
        fingerprint="test",
        pendsv_start=0x3000,
        pendsv_end=0x3100,
        svc_start=0x4000,
        svc_end=0x4100,
        start_first_start=0x5000,
        start_first_end=0x5100,
        switch_start=0x6000,
        switch_end=0x6100,
        pendsv_entry=0x3000,
        pendsv_save_pending=0x3004,
        pendsv_save_written=0x3008,
        pendsv_writeback=0x300C,
        pendsv_switch_call=0x3010,
        pendsv_after_switch=0x3014,
        pendsv_select=0x3018,
        pendsv_saved_sp=0x301C,
        pendsv_restored_pending_psp=0x3020,
        pendsv_psp_written=0x3024,
        pendsv_exception_return=0x3028,
        svc_entry=0x4000,
        svc_saved_sp=0x4004,
        svc_restored_pending_psp=0x4008,
        svc_psp_written=0x400C,
        svc_exception_return=0x4010,
        start_first_svc=0x5000,
        switch_entry=0x6000,
        switch_update=0x6004,
        switch_after_update=0x6008,
        switch_return=0x3014,
        px_current_tcb=0x7000,
    )


def _scheduler_snapshot(pc, *, xpsr=0x0100000E, lr=0xFFFFFFFD, psp=0x2080, r0=None):
    registers = {index: 0x5000 + index for index in range(13)}
    registers.update({13: 0x203FF000, 14: lr, 15: pc})
    if r0 is not None:
        registers[0] = r0
    return bridge._ArmSnapshot(
        pc=pc,
        sp=0x203FF000,
        msp=0x203FF000,
        psp=psp,
        xpsr=xpsr,
        registers=registers,
    )


def _scheduler_report(
    monkeypatch,
    *,
    pc,
    snapshot,
    memory=None,
    entry_pc=0x1000,
    running=True,
    bounds=(0x203FC000, 0x20400000),
    stkalign=True,
    task_top=0x2080,
):
    del pc
    adapter, task = _session(monkeypatch)
    task.top_of_stack = task_top
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    monkeypatch.setattr(
        bridge, "build_scheduler_profile", lambda _elf: (_scheduler_profile(), "ok")
    )
    monkeypatch.setattr(
        bridge,
        "_arm_snapshot",
        lambda: (
            (snapshot, "ok")
            if snapshot is not None
            else (
                None,
                "live-registers-unavailable",
            )
        ),
    )
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: running)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: bounds)
    monkeypatch.setattr(bridge, "_ccr_stkalign", lambda: stkalign)
    monkeypatch.setattr(
        bridge, "read_bytes", memory.read if memory is not None else lambda *_a: None
    )
    monkeypatch.setattr(bridge, "eval_identifier", lambda _name: 1)
    monkeypatch.setattr(bridge, "read_int", lambda _value: task.address)
    monkeypatch.setattr(bridge, "_exit_pc", lambda: 0x1200)
    elf = SimpleNamespace(
        function_containing=lambda address: (
            SimpleNamespace(start=entry_pc) if address == entry_pc else None
        )
    )
    monkeypatch.setattr(
        bridge, "_unwind_metadata", lambda _adapter: SimpleNamespace(elf=elf)
    )
    monkeypatch.setattr(
        bridge, "unwind_chain", lambda *_args, **_kwargs: pytest.fail("unsafe unwind")
    )
    return bridge.backtrace(adapter, "worker"), task


def test_scheduler_report_refuses_missing_live_registers_before_unwind(monkeypatch):
    report, _task = _scheduler_report(monkeypatch, pc=0x3000, snapshot=None)
    assert report.stop_reason == "live-registers-unavailable", report
    assert report.frames == [] and not report.complete


def test_scheduler_report_refuses_nonthumb_before_unwind(monkeypatch):
    pc = 0x3000
    report, _task = _scheduler_report(
        monkeypatch,
        pc=pc,
        snapshot=_scheduler_snapshot(pc, xpsr=0x0000000E),
    )
    assert report.stop_reason == "scheduler-frame-not-thumb", report
    assert report.frames == [] and not report.complete


def test_scheduler_report_refuses_f9_at_pendsv_entry(monkeypatch):
    pc = 0x3000
    report, _task = _scheduler_report(
        monkeypatch,
        pc=pc,
        snapshot=_scheduler_snapshot(pc, lr=0xFFFFFFF9),
    )
    assert report.stop_reason == "scheduler-stack-selection-unproven", report
    assert report.frames == [] and not report.complete


@pytest.mark.parametrize("running", [None, False])
def test_scheduler_report_refuses_missing_or_stopped_scheduler(monkeypatch, running):
    pc = 0x3000
    report, _task = _scheduler_report(
        monkeypatch,
        pc=pc,
        snapshot=_scheduler_snapshot(pc),
        running=running,
    )
    assert report.stop_reason == (
        "scheduler-state-unavailable" if running is None else "scheduler-not-running"
    ), report
    assert report.frames == []


@pytest.mark.parametrize(
    "bounds,stkalign,reason",
    [
        (None, True, "exception-stack-bounds-unavailable"),
        ((0x20400000, 0x20500000), True, "exception-stack-outside-bounds"),
        ((0x203FC000, 0x20400000), None, "exception-alignment-unavailable"),
    ],
)
def test_scheduler_report_refuses_missing_stack_or_alignment(
    monkeypatch, bounds, stkalign, reason
):
    pc = 0x3000
    report, _task = _scheduler_report(
        monkeypatch,
        pc=pc,
        snapshot=_scheduler_snapshot(pc),
        bounds=bounds,
        stkalign=stkalign,
    )
    assert report.stop_reason == reason, report
    assert report.frames == []


@pytest.mark.parametrize(
    "stage_name,stage_pc",
    [
        ("svc-entry", 0x4000),
        ("svc-restored-pending-psp", 0x4008),
        ("svc-psp-written", 0x400C),
        ("svc-exception-return", 0x4010),
    ],
)
def test_scheduler_report_refuses_first_start_without_entry_evidence(
    monkeypatch, stage_name, stage_pc
):
    memory = _WordMemory()
    top = 0x2080
    for index, value in enumerate(
        [0xA5A5A5A5] * 8 + [0, 0, 0, 0, 0, 0x1200, 0x1002, 0x01000000]
    ):
        memory[top + index * 4] = value
    registers = {index: 0x5000 + index for index in range(13)}
    registers.update({13: 0x203FF000, 14: 0xFFFFFFF9, 15: stage_pc})
    registers.update({index: 0xA5A5A5A5 for index in range(4, 12)})
    psp = 0
    if stage_name in {"svc-restored-pending-psp", "svc-psp-written"}:
        registers[0] = top + 0x20
    if stage_name in {"svc-psp-written", "svc-exception-return"}:
        psp = top + 0x20
    if stage_name == "svc-exception-return":
        registers[14] = 0xFFFFFFFD
    snapshot = bridge._ArmSnapshot(
        pc=stage_pc,
        sp=0x203FF000,
        msp=0x203FF000,
        psp=psp,
        xpsr=0x0100000B,
        registers=registers,
    )
    report, _task = _scheduler_report(
        monkeypatch,
        pc=stage_pc,
        snapshot=snapshot,
        memory=memory,
        entry_pc=0x1000,
    )
    assert report.stop_reason == "scheduler-first-start-entry-unproven", report
    assert report.frames == [] and not report.complete


def _scheduler_frame_memory(top, *, xpsr, core):
    memory = _WordMemory()
    words = list(core) + [0, 0, 0, 0, 0, 0x1200, 0x1002, xpsr]
    for index, value in enumerate(words):
        memory[top + index * 4] = value
    return memory


@pytest.mark.parametrize(
    "stage_pc,task_top,psp,frame_xpsr,stkalign,r0",
    [
        (0x3000, 0x2084, 0x20A4, 0x01000000, True, None),
        (0x3000, 0x2080, 0x20A0, 0x01000200, False, None),
        (0x300C, 0x2084, 0x20A4, 0x01000000, True, None),
        (0x300C, 0x2080, 0x20A0, 0x01000200, False, None),
        (0x3020, 0x2084, 0x20A4, 0x01000000, True, 0x20A4),
        (0x3020, 0x2080, 0x20A0, 0x01000200, False, 0x20A0),
        (0x3024, 0x2084, 0x20A4, 0x01000000, True, None),
        (0x3024, 0x2080, 0x20A0, 0x01000200, False, None),
    ],
)
def test_scheduler_report_refuses_inconsistent_frame_alignment_before_unwind(
    monkeypatch, stage_pc, task_top, psp, frame_xpsr, stkalign, r0
):
    core = tuple(0x5000 + index for index in range(4, 12))
    memory = _scheduler_frame_memory(task_top, xpsr=frame_xpsr, core=core)
    snapshot = _scheduler_snapshot(stage_pc, psp=psp, r0=r0)
    report, _task = _scheduler_report(
        monkeypatch,
        pc=stage_pc,
        snapshot=snapshot,
        memory=memory,
        stkalign=stkalign,
        task_top=task_top,
    )
    assert report.stop_reason == "exception-frame-alignment-invalid", report
    assert report.frames == [] and not report.complete


def test_current_pc_backtrace_delegates_to_running_task_and_handles_boot(monkeypatch):
    from core.backtrace.unwind import UnwindFrame, UnwindMetadata, UnwindResult
    from core.backtrace.unwind_arm import ArmSnapshot

    adapter, task = _session(monkeypatch)
    current_val = object()
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [(0, task.address)])
    monkeypatch.setattr(
        bridge, "iter_tasks", lambda _layout: [(current_val, "Running", 0)]
    )
    monkeypatch.setattr(bridge, "value_address", lambda _val: task.address)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda pc: f"fn_{pc:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    frame = _LiveFrame(
        pc=0x1004, sp=0x20004000, xpsr=0x01000000, msp=0x203FF000, psp=0x20004000
    )
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda seed, _meta, **_kwargs: UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "task-exit",
            True,
            False,
            "current",
            "seed",
        ),
    )
    report = bridge.backtrace(adapter)
    assert report.task == "worker" and report.address == task.address
    assert report.source == "current" and report.complete is True

    # Running scheduler with NULL current TCB fails closed.
    monkeypatch.setattr(bridge, "current_tasks", lambda _layout: [])
    null_tcb = bridge.backtrace(adapter)
    assert not null_tcb.complete
    assert null_tcb.stop_reason == "scheduler-current-tcb-unavailable"

    # Pre-scheduler boot unwinds current MSP within boot stack bounds.
    from core.backtrace.unwind_cfi import CfaRule, CfiRow, RegisterRule
    from core.elf import EM_ARM, CodeSymbol, ElfImage, ElfSection

    monkeypatch.setattr(bridge, "_scheduler_running", lambda: False)
    boot_elf = ElfImage(
        pointer_size=4,
        endian="little",
        machine=EM_ARM,
        sections=(
            ElfSection(
                name=".text",
                address=0x1000,
                data=b"\x70\x47\x00\x00\x70\x47",
                flags=0x6,
                section_type=1,
            ),
        ),
        symbols=(CodeSymbol("boot_leaf", 0x1004, 2),),
    )
    boot_row = CfiRow(
        cfa=CfaRule(13, 4),
        registers={14: RegisterRule("offset", -4)},
        return_register=14,
    )
    meta = UnwindMetadata(
        elf=boot_elf,
        arch="arm",
        cfi_tables=(
            cast(CfiTable, SimpleNamespace(row_for=lambda _pc: boot_row, error=None)),
        ),
        ehabi=None,
        return_register=14,
        frame_pointer_register=7,
    )
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: meta)
    boot_regs = {index: index for index in range(13)} | {
        13: 0x20001FE0,
        14: 0x0,
        15: 0x1004,
    }
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, size: (
            b"\x00\x00\x00\x00" if address == 0x20001FE0 and size == 4 else None
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_arm_snapshot",
        lambda: (
            ArmSnapshot(0x1004, 0x20001FE0, 0x20001FE0, 0, 0x01000000, boot_regs),
            "ok",
        ),
    )
    monkeypatch.setattr(
        bridge,
        "linker_symbol_address",
        lambda name: {"_sstack": 0x20001000, "_estack": 0x20002000}.get(name),
    )
    boot_report = bridge.backtrace(adapter)
    assert boot_report.task == "<current>"
    assert boot_report.source == "current"
    assert boot_report.bounds == "recorded"
    assert [frame.pc for frame in boot_report.frames] == [0x1004]
    assert boot_report.stop_reason == "return-address-zero"

    # Pre-scheduler active handler refuses rather than guessing an exception boundary.
    monkeypatch.setattr(
        bridge,
        "_arm_snapshot",
        lambda: (
            ArmSnapshot(0x1004, 0x20001FE0, 0x20001FE0, 0, 0x01000003, boot_regs),
            "ok",
        ),
    )
    irq_boot = bridge.backtrace(adapter)
    assert not irq_boot.complete and irq_boot.stop_reason == "scheduler-not-running"
