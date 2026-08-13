"""Unit tests for merged FreeRTOS configuration and layout ownership."""

from __future__ import annotations

import freertos.layout as layout_module
from freertos.layout import FreeRtosConfig, build_layout


def test_detect_config_retains_probed_tcb_capabilities(monkeypatch):
    fields = {
        "pxTopOfStack",
        "pcTaskName",
        "uxBasePriority",
        "ulRunTimeCounter",
        "uxCoreAffinityMask",
        "pxEndOfStack",
    }
    symbols = {"pxCurrentTCBs", "uxTaskGetSystemState", "xQueueRegistry"}
    monkeypatch.setattr(
        layout_module,
        "_fields",
        lambda type_name: (
            fields if type_name == "struct tskTaskControlBlock" else set()
        ),
    )
    monkeypatch.setattr(
        layout_module,
        "lookup_symbol",
        lambda name: object() if name in symbols else None,
    )
    monkeypatch.setattr(layout_module, "lookup_type", lambda _name: None)
    monkeypatch.setattr(layout_module, "_macro_int", lambda _name: 4)
    monkeypatch.setattr(layout_module, "macro_defined", lambda _name: False)

    config = layout_module.detect_config()

    assert config.smp is True
    assert config.number_of_cores == 4
    assert config.tcb_fields == frozenset(fields)
    assert config.stack_end_field == "pxEndOfStack"
    assert config.trace_facility is True
    assert config.queue_registry is True


def test_smp_layout_does_not_fabricate_optional_tcb_fields():
    layout = build_layout(FreeRtosConfig(smp=True), (10, 5, 1))
    fields = layout.structs["struct tskTaskControlBlock"].fields

    assert "run_state" not in fields
    assert "core_affinity" not in fields


def test_layout_adds_only_the_optional_fields_detected_in_dwarf():
    config = FreeRtosConfig(
        smp=True,
        stack_end_field="pxStackEnd",
        tcb_fields=frozenset(
            {"uxBasePriority", "ulRunTimeCounter", "uxCoreAffinityMask"}
        ),
    )

    fields = (
        build_layout(config, (11, 1, 0)).structs["struct tskTaskControlBlock"].fields
    )

    assert fields["base_priority"].path == ("uxBasePriority",)
    assert fields["runtime_counter"].path == ("ulRunTimeCounter",)
    assert fields["stack_end"].path == ("pxStackEnd",)
    assert fields["core_affinity"].path == ("uxCoreAffinityMask",)
    assert "run_state" not in fields
