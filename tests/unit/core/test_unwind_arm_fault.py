"""Armv7-M synchronous-fault precision and handler setup (synthetic)."""

from __future__ import annotations

import struct

import pytest

from core.unwind import SeedFrame
from core.unwind_arm import (
    arm_fault_precision,
    armv7m_exception_return_candidate,
    armv7m_exception_transition,
    prepare_arm_handler_unwind,
)
from core.unwind_segments import StackSegment
from tests.unit.core.test_unwind import _Memory

CFSR = 0xE000ED28
HFSR = 0xE000ED2C
MMFAR = 0xE000ED34
BFAR = 0xE000ED38

TASK = StackSegment(
    "task:worker", "worker", 0x2000, 0x2200, "task-recorded", "process", "task"
)
IRQ = StackSegment(
    "exception:3",
    "exception:3",
    0x3000,
    0x4000,
    "linker:irq-stack",
    "main",
    "exception",
)


def _regs(cfsr: int, hfsr: int, mmfar: int = 0, bfar: int = 0):
    return _Memory(
        {
            CFSR: struct.pack("<I", cfsr),
            HFSR: struct.pack("<I", hfsr),
            MMFAR: struct.pack("<I", mmfar),
            BFAR: struct.pack("<I", bfar),
        }
    )


def test_m0_fault_is_imprecise_without_target_reads():
    precision = arm_fault_precision(3, "armv6-m", read=_Memory({}))
    assert not precision.precise
    assert precision.fault == "HardFault"


def test_unreadable_scB_registers_fail_closed():
    precision = arm_fault_precision(3, "armv7-m", read=_Memory({}))
    assert not precision.precise
    assert precision.fault == "unknown"


@pytest.mark.parametrize("number", [3, 4, 5, 6])
def test_vector_fetch_and_debug_event_are_imprecise(number):
    for hfsr, name in ((0x02, "VECTTBL"), (0x80000000, "DEBUGEVT")):
        precision = arm_fault_precision(number, "armv7-m", read=_regs(0, hfsr))
        assert not precision.precise
        assert name in precision.fault


@pytest.mark.parametrize(
    "cfsr",
    [0x10, 0x08, 0x20, 0x1000, 0x0800, 0x2000, 0x0400],
)
def test_stacking_and_async_bus_faults_are_imprecise(cfsr):
    precision = arm_fault_precision(5, "armv7-m", read=_regs(cfsr, 0x40000000))
    assert not precision.precise
    assert precision.fault.startswith("imprecise:")


@pytest.mark.parametrize(
    "cfsr",
    [0x00010000, 0x00020000, 0x00040000, 0x00080000, 0x01000000, 0x02000000],
)
def test_usage_fault_bits_are_precise(cfsr):
    precision = arm_fault_precision(6, "armv7-m", read=_regs(cfsr, 0))
    assert precision.precise
    assert precision.fault.startswith("UsageFault:")


def test_usage_fault_without_cause_is_imprecise():
    precision = arm_fault_precision(6, "armv7-m", read=_regs(0, 0))
    assert not precision.precise


@pytest.mark.parametrize("cfsr", [0x01, 0x02, 0x81, 0x82])
def test_memmanage_access_violations_are_precise(cfsr):
    precision = arm_fault_precision(4, "armv7-m", read=_regs(cfsr, 0, mmfar=0x20001000))
    assert precision.precise
    assert precision.fault.startswith("MemManage:")
    if cfsr & 0x80:
        assert "MMFAR=" in precision.evidence


def test_memmanage_without_cause_is_imprecise():
    precision = arm_fault_precision(4, "armv7-m", read=_regs(0, 0))
    assert not precision.precise


def test_precise_bus_fault_requires_preciserr_and_bfarvalid():
    precise = arm_fault_precision(
        5, "armv7-m", read=_regs(0x0200 | 0x8000, 0, bfar=0x20002000)
    )
    assert precise.precise
    assert "BFAR=" in precise.evidence
    for cfsr in (0x0200, 0x8000, 0):
        imprecise = arm_fault_precision(5, "armv7-m", read=_regs(cfsr, 0))
        assert not imprecise.precise


def test_instruction_bus_fault_is_precise_without_bfar():
    precision = arm_fault_precision(5, "armv7-m", read=_regs(0x0100, 0))
    assert precision.precise
    assert precision.fault == "BusFault:IBUSERR"


def test_escalated_hardfault_requires_forced_and_precise_cfsr():
    assert not arm_fault_precision(3, "armv7-m", read=_regs(0x00010000, 0)).precise
    assert not arm_fault_precision(3, "armv7-m", read=_regs(0, 0x40000000)).precise
    precise = arm_fault_precision(3, "armv7-m", read=_regs(0x00010000, 0x40000000))
    assert precise.precise
    assert precise.fault.startswith("HardFault:escalated-UsageFault:")
    mem = arm_fault_precision(3, "armv7-m", read=_regs(0x02, 0x40000000))
    assert mem.precise
    bus = arm_fault_precision(
        3, "armv7-m", read=_regs(0x0200 | 0x8000, 0x40000000, bfar=0x20002000)
    )
    assert bus.precise
    ibus = arm_fault_precision(3, "armv7-m", read=_regs(0x0100, 0x40000000))
    assert ibus.precise


def _fault_seed(number: int = 6):
    return SeedFrame(
        0x1004,
        0x3010,
        {13: 0x3010, 14: 0xFFFFFFFD},
        source="current",
        process_sp=0x2100,
        exception_number=number,
    )


def test_prepare_refuses_imprecise_fault_before_bounds():
    seed = _fault_seed(6)
    result = prepare_arm_handler_unwind(
        seed,
        "armv7-m",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 47,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        read=_Memory({}),
    )
    assert isinstance(result, tuple) and result[0] == "fault-frame-imprecise"


def test_prepare_admits_precise_usage_fault_with_evidence():
    seed = _fault_seed(6)
    read = _regs(0x00010000, 0)
    setup = prepare_arm_handler_unwind(
        seed,
        "armv7-m",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 47,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        read=read,
    )
    assert not isinstance(setup, tuple)
    assert setup.fault is not None and setup.fault.precise
    assert setup.fault.fault.startswith("UsageFault:")
    assert setup.exception_detector is armv7m_exception_return_candidate
    assert setup.exception_hook is armv7m_exception_transition


def test_prepare_refuses_m0_fault_without_cfsr():
    seed = _fault_seed(3)
    result = prepare_arm_handler_unwind(
        seed,
        "armv6-m",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 47,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        read=_Memory({}),
    )
    assert isinstance(result, tuple) and result[0] == "fault-frame-imprecise"


def test_prepare_admits_mainline_fault_only():
    seed = _fault_seed(6)
    setup = prepare_arm_handler_unwind(
        seed,
        "armv8-m.mainline",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 47,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        read=_regs(0x00010000, 0),
    )
    assert not isinstance(setup, tuple)
    assert setup.fault is not None and setup.fault.precise


def test_prepare_admits_mainline_external_irq():
    seed = _fault_seed(24)
    setup = prepare_arm_handler_unwind(
        seed,
        "armv8-m.mainline",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 47,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        read=_Memory({}),
    )
    assert not isinstance(setup, tuple)
    assert setup.fault is None


def test_prepare_refuses_mainline_secure_fault_as_unsupported():
    seed = _fault_seed(7)
    result = prepare_arm_handler_unwind(
        seed,
        "armv8-m.mainline",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 511,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        read=_regs(0, 0),
    )
    assert isinstance(result, tuple) and result[0] == "unsupported-exception-model"
    assert "SecureFault" in result[1]


def _systick_seed(number: int = 15):
    return SeedFrame(
        0x1004,
        0x3010,
        {13: 0x3010, 14: 0xFFFFFFFD},
        source="current",
        process_sp=0x2100,
        exception_number=number,
    )


def test_prepare_admits_mainline_systick_with_default_guard():
    from core.unwind_arm import arm_exception_guard

    setup = prepare_arm_handler_unwind(
        _systick_seed(),
        "armv8-m.mainline",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 47,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        guard_fn=arm_exception_guard,
        read=_Memory({}),
    )
    assert not isinstance(setup, tuple)
    assert setup.fault is None
    assert setup.exception_detector is armv7m_exception_return_candidate


def test_prepare_refuses_mainline_systick_when_guard_rejects():
    result = prepare_arm_handler_unwind(
        _systick_seed(),
        "armv8-m.mainline",
        scheduler_running=lambda: True,
        irq_stack_bounds=lambda: (0x3000, 0x4000),
        capacity_reader=lambda: 47,
        stkalign_reader=lambda: True,
        vector_entry_reader=lambda _number: None,
        guard_fn=lambda *_a: "unsupported-exception-model",
        read=_Memory({}),
    )
    assert isinstance(result, tuple) and result[0] == "unsupported-exception-model"


def test_baseline_table_has_no_configurable_faults():
    from core.unwind_arm import armv8m_baseline_exception_number_kind as kind

    assert kind(0) == "thread"
    assert kind(2) == "system" and kind(15) == "system"
    assert kind(3) == "fault"
    assert kind(11) == "scheduler" and kind(14) == "scheduler"
    for number in (1, 4, 5, 6, 7, 8, 9, 10, 12, 13, 48, 511):
        assert kind(number) == "invalid", number
    for number in (16, 24, 47):
        assert kind(number) == "external", number


def test_mainline_table_keeps_secure_fault_as_fault_for_seed_validity():
    from core.unwind_arm import armv8m_mainline_exception_number_kind as kind

    assert kind(0) == "thread"
    assert kind(7) == "fault"
    for number in (3, 4, 5, 6):
        assert kind(number) == "fault", number
    assert kind(2) == "system" and kind(12) == "system" and kind(15) == "system"
    assert kind(11) == "scheduler" and kind(14) == "scheduler"
    for number in (1, 8, 9, 10, 13, 512):
        assert kind(number) == "invalid", number
    for number in (16, 24, 511):
        assert kind(number) == "external", number


def test_dispatcher_routes_each_isa_to_its_own_table():
    from core.unwind_arm import arm_exception_number_kind as dispatch

    assert dispatch("armv6-m", 4) == "invalid"
    assert dispatch("armv7-m", 4) == "fault"
    assert dispatch("armv7e-m", 6) == "fault"
    assert dispatch("armv8-m.baseline", 4) == "invalid"
    assert dispatch("armv8-m.baseline", 3) == "fault"
    assert dispatch("armv8-m.mainline", 7) == "fault"
    assert dispatch("armv8-m.mainline", 13) == "invalid"
    assert dispatch(None, 24) == "invalid"
    assert dispatch("armv7-a", 24) == "invalid"
    assert dispatch("riscv", 24) == "invalid"


def test_exception_guard_refuses_nested_fault_and_admits_systick_and_external_irq():
    from core.unwind_arm import arm_exception_guard

    assert arm_exception_guard(3, 0x1000, "armv7-m") == "exception-frame-type-unproven"
    assert (
        arm_exception_guard(7, 0x1000, "armv8-m.mainline")
        == "exception-frame-type-unproven"
    )
    assert arm_exception_guard(15, 0x1000, "armv7-m") is None
    assert arm_exception_guard(15, 0x1000, "armv8-m.mainline") is None
    assert arm_exception_guard(24, 0x1000, "armv8-m.mainline") is None
    assert arm_exception_guard(24, 0x1000, "armv7-m") is None
