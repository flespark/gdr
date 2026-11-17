"""Unit tests for merged FreeRTOS configuration and layout ownership."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.layout as printers
import freertos.layout as layout_module
import freertos.layout as probe_module
import freertos.layout as version
from freertos.layout import (
    PROBE_MARKERS,
    SMP_ERA_FIELD,
    FreeRtosConfig,
    build_layout,
)
from tests.support.freertos_fixture_profiles import (
    get_freertos_test_profile,
    list_freertos_boards,
    list_freertos_variants,
)


class _FakeArrayType:
    """Minimal ``gdb.Type``-like array for ``_array_bound`` probing."""

    def __init__(self, last: int):
        self._last = last

    def strip_typedefs(self):
        return self

    def range(self):
        return (0, self._last)


_FAKE_ARCH = type("_Arch", (), {"ptrsize": 4, "endian": "little"})()


class _ArrayValue:
    """Minimal ``gdb.Value``-like array symbol with a decodable bound."""

    def __init__(self, last: int):
        self.type = _FakeArrayType(last)


def _make_config(
    monkeypatch,
    *,
    fields=None,
    symbols=(),
    types=(),
    macro_int=None,
    array_bounds=None,
    symbol_present=(),
):
    """Probe ``detect_config`` against a controlled DWARF/macro stand-in."""
    fields = fields or {}
    array_bounds = array_bounds or {}

    def _field_set(type_name: str):
        return fields.get(type_name, set())

    def _lookup_symbol(name: str):
        if name in array_bounds:
            return _ArrayValue(array_bounds[name])
        return object() if name in symbols else None

    monkeypatch.setattr(layout_module, "_fields", _field_set)
    monkeypatch.setattr(layout_module, "lookup_symbol", _lookup_symbol)
    monkeypatch.setattr(
        layout_module, "lookup_type", lambda name: object() if name in types else None
    )
    # get_arch_info raises outside GDB; the stack-word fallback depends on
    # it being probed, so the fixture supplies a known 32-bit target.
    monkeypatch.setattr(layout_module, "get_arch_info", lambda: _FAKE_ARCH)
    if macro_int is not None:
        monkeypatch.setattr(layout_module, "_macro_int", lambda _name: macro_int)
    monkeypatch.setattr(
        layout_module, "symbol_exists", lambda name: name in symbol_present
    )
    return layout_module.detect_config()


def _tcb_config(monkeypatch, *fields: str) -> FreeRtosConfig:
    return _make_config(monkeypatch, fields={"struct tskTaskControlBlock": set(fields)})


def test_detect_config_retains_probed_tcb_capabilities(monkeypatch):
    """Core capabilities (SMP, cores, registry) are still retained."""
    fields = {
        "pxTopOfStack",
        "pcTaskName",
        "uxBasePriority",
        "ulRunTimeCounter",
        "uxCoreAffinityMask",
        "pxEndOfStack",
    }
    symbols = {"pxCurrentTCBs", "xQueueRegistry"}
    array_bounds = {"pxCurrentTCBs": 3, "xQueueRegistry": 9, "pxReadyTasksLists": 7}
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
        lambda name: (
            _ArrayValue(array_bounds[name])
            if name in array_bounds
            else (object() if name in symbols else None)
        ),
    )
    monkeypatch.setattr(layout_module, "lookup_type", lambda _name: None)
    monkeypatch.setattr(layout_module, "_macro_int", lambda _name: 4)
    monkeypatch.setattr(layout_module, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(layout_module, "get_arch_info", lambda: _FAKE_ARCH)

    config = layout_module.detect_config()

    assert config.smp is True
    assert config.number_of_cores == 4
    assert config.tcb_fields == frozenset(fields)
    assert config.stack_end_field == "pxEndOfStack"
    assert config.queue_registry is True
    assert config.queue_registry_size == 10
    assert config.max_priorities == 8


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


# --- trace_facility / queue_sets (struct QueueDefinition fields) ------------


def test_trace_facility_detected_via_uc_queue_type(monkeypatch):
    cfg = _make_config(monkeypatch, fields={"struct QueueDefinition": {"ucQueueType"}})
    assert cfg.trace_facility is True


def test_trace_facility_falls_back_to_xqueue_typedef(monkeypatch):
    """Some DWARF only exposes the xQUEUE typedef, not the struct tag."""
    cfg = _make_config(monkeypatch, fields={"xQUEUE": {"ucQueueType"}})
    assert cfg.trace_facility is True


def test_trace_facility_absent_without_uc_queue_type(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.trace_facility is False


def test_queue_sets_detected_via_px_queue_set_container(monkeypatch):
    cfg = _make_config(
        monkeypatch, fields={"struct QueueDefinition": {"pxQueueSetContainer"}}
    )
    assert cfg.queue_sets is True


def test_queue_sets_absent_without_px_queue_set_container(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.queue_sets is False


# --- static_allocation / static_and_dynamic ---------------------------------


def test_static_and_dynamic_detected_via_uc_statically_allocated(monkeypatch):
    cfg = _tcb_config(monkeypatch, "ucStaticallyAllocated")
    assert cfg.static_and_dynamic is True
    assert cfg.static_allocation is False


def test_static_allocation_detected_via_x_task_create_static(monkeypatch):
    cfg = _make_config(monkeypatch, symbol_present={"xTaskCreateStatic"})
    assert cfg.static_allocation is True
    assert cfg.static_and_dynamic is False


def test_static_only_has_api_without_tcb_marker(monkeypatch):
    """static-only: xTaskCreateStatic exists, ucStaticallyAllocated does not."""
    cfg = _make_config(monkeypatch, symbol_present={"xTaskCreateStatic"})
    assert cfg.static_allocation is True
    assert cfg.static_and_dynamic is False


def test_static_dynamic_sets_both_flags(monkeypatch):
    cfg = _make_config(
        monkeypatch,
        fields={"struct tskTaskControlBlock": {"ucStaticallyAllocated"}},
        symbol_present={"xTaskCreateStatic"},
    )
    assert cfg.static_allocation is True
    assert cfg.static_and_dynamic is True


def test_dynamic_only_has_neither_static_flag(monkeypatch):
    cfg = _tcb_config(monkeypatch)
    assert cfg.static_allocation is False
    assert cfg.static_and_dynamic is False


# --- TCB optional capability fields -----------------------------------------

_TCB_FIELD_CONFIGS = [
    ("uxTaskAttributes", "task_attributes"),
    ("xPreemptionDisable", "preemption_disable"),
    ("uxCriticalNesting", "critical_nesting_in_tcb"),
    ("iTaskErrno", "posix_errno"),
    ("ucDelayAborted", "delay_abort"),
]


@pytest.mark.parametrize(("field", "attr"), _TCB_FIELD_CONFIGS)
def test_tcb_optional_field_detected(monkeypatch, field, attr):
    cfg = _tcb_config(monkeypatch, field)
    assert getattr(cfg, attr) is True


@pytest.mark.parametrize(("field", "attr"), _TCB_FIELD_CONFIGS)
def test_tcb_optional_field_absent(monkeypatch, field, attr):
    # Reason: ``field`` is only referenced by the param id; the absent case
    # intentionally probes a TCB with no optional members at all.
    del field
    cfg = _tcb_config(monkeypatch)
    assert getattr(cfg, attr) is False


# --- mini_list (List_t::xListEnd presence of pvOwner) -----------------------


class _FakeField:
    def __init__(self, name: str, field_type=None):
        self.name = name
        self.type = field_type


class _FakeStruct:
    def __init__(self, fields):
        self._fields = fields

    def strip_typedefs(self):
        return self

    def fields(self):
        return self._fields


def _mini_list_type(has_owner: bool):
    end_fields = [
        _FakeField("xItemValue"),
        _FakeField("pxNext"),
        _FakeField("pxPrevious"),
    ]
    if has_owner:
        end_fields.append(_FakeField("pvOwner"))
    return _FakeStruct([_FakeField("xListEnd", _FakeStruct(end_fields))])


def _mini_config(monkeypatch, has_owner: bool):
    monkeypatch.setattr(
        layout_module,
        "lookup_type",
        lambda name: _mini_list_type(has_owner) if name == "struct xLIST" else None,
    )
    monkeypatch.setattr(layout_module, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(layout_module, "_fields", lambda _type_name: set())
    monkeypatch.setattr(layout_module, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(layout_module, "get_arch_info", lambda: _FAKE_ARCH)
    return layout_module.detect_config()


def test_mini_list_detected_when_x_list_end_lacks_pv_owner(monkeypatch):
    cfg = _mini_config(monkeypatch, has_owner=False)
    assert cfg.mini_list is True


def test_mini_list_false_when_x_list_end_is_full_list_item(monkeypatch):
    cfg = _mini_config(monkeypatch, has_owner=True)
    assert cfg.mini_list is False


# --- number_of_cores --------------------------------------------------------


def test_number_of_cores_prefers_px_current_tcbs_array_bound(monkeypatch):
    cfg = _make_config(
        monkeypatch, symbols={"pxCurrentTCBs"}, array_bounds={"pxCurrentTCBs": 1}
    )
    assert cfg.smp is True
    assert cfg.number_of_cores == 2


def test_number_of_cores_falls_back_to_macro_when_array_unknown(monkeypatch):
    cfg = _make_config(monkeypatch, symbols={"pxCurrentTCBs"}, macro_int=4)
    assert cfg.smp is True
    assert cfg.number_of_cores == 4


def test_number_of_cores_unary_when_not_smp(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.smp is False
    assert cfg.number_of_cores == 1


# --- max_priorities ---------------------------------------------------------


def test_max_priorities_reads_px_ready_tasks_lists_bound(monkeypatch):
    cfg = _make_config(monkeypatch, array_bounds={"pxReadyTasksLists": 31})
    assert cfg.max_priorities == 32


def test_max_priorities_none_when_list_symbol_missing(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.max_priorities is None


# --- list_item_container_field (pxContainer/pvContainer spelling) ----------


def test_container_field_detects_pv_container_spelling(monkeypatch):
    """Default backward-compat build emits the member as ``pvContainer``."""
    cfg = _make_config(monkeypatch, fields={"struct xLIST_ITEM": {"pvContainer"}})
    assert cfg.list_item_container_field == "pvContainer"
    field = (
        build_layout(cfg, (10, 3, 1)).structs["struct xLIST_ITEM"].fields["container"]
    )
    assert field.path == ("pvContainer",)


def test_container_field_detects_px_container_spelling(monkeypatch):
    """Non-backward-compatible build emits the member as ``pxContainer``."""
    cfg = _make_config(monkeypatch, fields={"struct xLIST_ITEM": {"pxContainer"}})
    assert cfg.list_item_container_field == "pxContainer"
    field = (
        build_layout(cfg, (11, 1, 0)).structs["struct xLIST_ITEM"].fields["container"]
    )
    assert field.path == ("pxContainer",)


def test_container_field_defaults_to_px_container_when_unknown():
    """No probed member name defaults the access path to pxContainer."""
    field = (
        build_layout(FreeRtosConfig(), (10, 3, 1))
        .structs["struct xLIST_ITEM"]
        .fields["container"]
    )
    assert field.path == ("pxContainer",)


# --- list_integrity_check ---------------------------------------------------


def test_list_integrity_check_detected_via_field(monkeypatch):
    cfg = _make_config(
        monkeypatch, fields={"struct xLIST_ITEM": {"xListItemIntegrityValue1"}}
    )
    assert cfg.list_integrity_check is True


def test_list_integrity_check_absent_without_field(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.list_integrity_check is False


# --- event_groups -----------------------------------------------------------


def test_event_groups_detected_via_struct_type(monkeypatch):
    cfg = _make_config(monkeypatch, types={"struct EventGroupDef_t"})
    assert cfg.event_groups is True


def test_event_groups_absent_without_struct(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.event_groups is False


# --- stream_buffers ---------------------------------------------------------


def test_stream_buffers_detected_via_struct_type(monkeypatch):
    cfg = _make_config(monkeypatch, types={"struct StreamBufferDef_t"})
    assert cfg.stream_buffers is True


def test_stream_buffers_absent_without_struct(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.stream_buffers is False


def test_stream_buffer_notification_index_detected_via_field(monkeypatch):
    cfg = _make_config(
        monkeypatch, fields={"struct StreamBufferDef_t": {"uxNotificationIndex"}}
    )
    assert cfg.stream_buffer_notification_index is True


def test_stream_buffer_notification_index_absent(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.stream_buffer_notification_index is False


# --- queue_registry_size ----------------------------------------------------


def test_queue_registry_size_reads_array_bound(monkeypatch):
    cfg = _make_config(monkeypatch, array_bounds={"xQueueRegistry": 9})
    assert cfg.queue_registry_size == 10


def test_queue_registry_size_zero_when_symbol_missing(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.queue_registry_size == 0


# --- mpu_object_pool / heap_protector ---------------------------------------


def test_mpu_object_pool_detected_via_x_kernel_object_pool(monkeypatch):
    cfg = _make_config(monkeypatch, symbol_present={"xKernelObjectPool"})
    assert cfg.mpu_object_pool is True


def test_mpu_object_pool_absent_without_symbol(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.mpu_object_pool is False


@pytest.mark.parametrize(
    "pool,settings", [(False, False), (False, True), (True, False), (True, True)]
)
def test_mpu_saved_context_requires_both_pool_and_tcb_settings(
    monkeypatch, pool, settings
):
    cfg = _make_config(
        monkeypatch,
        symbol_present={"xKernelObjectPool"} if pool else set(),
        fields={"struct tskTaskControlBlock": {"xMPUSettings"} if settings else set()},
    )
    assert cfg.mpu_context is (pool and settings)
    fields = build_layout(cfg).structs["struct tskTaskControlBlock"].fields
    assert ("mpu_context" in fields) is (pool and settings)
    if pool and settings:
        assert fields["mpu_context"].path == ("xMPUSettings", "ulContext")


def test_heap_protector_detected_via_x_heap_canary(monkeypatch):
    cfg = _make_config(monkeypatch, symbol_present={"xHeapCanary"})
    assert cfg.heap_protector is True


def test_heap_protector_absent_without_symbol(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.heap_protector is False


# --- heap_kind --------------------------------------------------------------


def test_heap_kind_identifies_heap5(monkeypatch):
    cfg = _make_config(
        monkeypatch,
        types={"BlockLink_t"},
        symbol_present={"vPortDefineHeapRegions", "pxEnd", "xStart", "xEnd"},
    )
    assert cfg.heap_kind == 5


def test_heap_kind_identifies_heap4(monkeypatch):
    cfg = _make_config(
        monkeypatch, types={"BlockLink_t"}, symbol_present={"pxEnd", "xStart"}
    )
    assert cfg.heap_kind == 4


def test_heap_kind_identifies_heap1(monkeypatch):
    cfg = _make_config(monkeypatch, symbol_present={"xNextFreeByte"})
    assert cfg.heap_kind == 1


def test_heap_kind_identifies_heap2(monkeypatch):
    cfg = _make_config(
        monkeypatch, types={"BlockLink_t"}, symbol_present={"xStart", "xEnd"}
    )
    assert cfg.heap_kind == 2


def test_heap_kind_none_for_heap3_or_no_heap(monkeypatch):
    cfg = _make_config(monkeypatch)
    assert cfg.heap_kind is None


def test_heap_kind_requires_block_link_type_for_heap4(monkeypatch):
    """pxEnd without BlockLink_t is not enough to claim heap_4."""
    cfg = _make_config(monkeypatch, symbol_present={"pxEnd", "xStart"})
    assert cfg.heap_kind is None


def test_heap_kind_ambiguous_overlap_is_unknown(monkeypatch):
    """heap_1's xNextFreeByte next to a BlockLink heap is not a unique match."""
    cfg = _make_config(
        monkeypatch,
        types={"BlockLink_t"},
        symbol_present={"pxEnd", "xNextFreeByte"},
    )
    assert cfg.heap_kind is None


# --- entry_field / mpu_wrapper_v2 / xTASK_STATUS removal --------------------


def test_layout_does_not_include_entry_field():
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    assert "entry" not in layout.structs["struct tskTaskControlBlock"].fields


def test_config_has_no_entry_field_attribute():
    assert not hasattr(FreeRtosConfig(), "entry_field")


def test_config_exposes_mpu_object_pool_not_legacy_name():
    assert not hasattr(FreeRtosConfig(), "mpu_wrapper_v2")
    assert hasattr(FreeRtosConfig(), "mpu_object_pool")


def test_layout_does_not_include_task_status_struct():
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    assert "struct xTASK_STATUS" not in layout.structs


# --- pretty-printer summary metadata ----------------------------------------


def test_task_layout_has_summary_fields():
    tcb = build_layout(FreeRtosConfig(), (10, 3, 1)).structs[
        "struct tskTaskControlBlock"
    ]
    assert tcb.fields["name"].summary is True
    assert tcb.fields["name"].kind == "string"
    assert tcb.fields["current_priority"].summary is True


def test_queue_layout_placeholder():
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    assert layout.structs["struct QueueDefinition"].display_name == "Queue"


def test_timer_layout_placeholder():
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    assert layout.structs["struct tmrTimerControl"].display_name == "Timer"


def test_timer_fields_gate_timer_number():
    """The detail fields exist, and uxTimerNumber only under the trace facility.

    The trace-off variant's DWARF has no uxTimerNumber member (timers.c), so
    an unconditional registration would fabricate a stable-looking but
    imaginary timer id on that build.
    """
    on = build_layout(FreeRtosConfig(trace_facility=True), (10, 3, 1))
    off = build_layout(FreeRtosConfig(trace_facility=False), (10, 3, 1))
    on_timer = on.structs["struct tmrTimerControl"]
    off_timer = off.structs["struct tmrTimerControl"]
    for name, path in (
        ("id", ("pvTimerID",)),
        ("callback", ("pxCallbackFunction",)),
        ("status", ("ucStatus",)),
        ("list_item", ("xTimerListItem",)),
    ):
        assert on_timer.fields[name].path == path
    assert "number" in on_timer.fields
    assert on_timer.fields["number"].path == ("uxTimerNumber",)
    assert "number" not in off_timer.fields


def test_stream_buffer_summary_uses_only_dwarf_backed_fields():
    """StreamBuffer summary must only name members that exist in DWARF.

    Upstream ``struct StreamBufferDef_t`` (stream_buffer.c) has no
    item/byte-count member, so the fold must not emit a ``count`` key that
    would always render ``N/A``; its length member is ``xLength`` (not the
    queue's ``uxLength``), so a copy-paste of the queue path would also
    render ``size=N/A``.
    """
    sb = build_layout(FreeRtosConfig(), (10, 3, 1)).structs["struct StreamBufferDef_t"]
    assert sb.fields["size"].summary is True
    assert sb.fields["size"].path == ("xLength",)
    assert "count" not in sb.fields
    for field in sb.fields.values():
        assert "uxItemsStored" not in field.path


# --- array-bound probing ----------------------------------------------------


def test_unbounded_array_declaration_is_unknown_not_zero(monkeypatch):
    """``extern T sym[]`` reports range (0, -1); that is unknown, not 0.

    A zero core count would make the running-task loop iterate zero times and
    silently report no running task, so the macro fallback must win instead.
    """
    cfg = _make_config(
        monkeypatch,
        symbols={"pxCurrentTCBs"},
        array_bounds={"pxCurrentTCBs": -1, "pxReadyTasksLists": -1},
        macro_int=3,
    )

    assert cfg.smp is True
    assert cfg.number_of_cores == 3
    assert cfg.max_priorities is None


def test_unbounded_core_array_without_macro_falls_back_to_two(monkeypatch):
    """With neither a usable bound nor the macro, SMP still means >= 2 cores."""
    monkeypatch.setattr(layout_module, "_macro_int", lambda _name: None)
    cfg = _make_config(
        monkeypatch,
        symbols={"pxCurrentTCBs"},
        array_bounds={"pxCurrentTCBs": -1},
    )

    assert cfg.number_of_cores == 2


@pytest.mark.parametrize(
    "value",
    ("10.3.0", "10.3.1", "10.4.0", "10.5.0", "11.1.0", "11.2.0", "11.3.0", "11.3.1"),
)
def test_validate_version_accepts_supported_ranges(value, monkeypatch):
    monkeypatch.setattr(version, "warn", lambda _message: None)
    assert version.validate_version(value) == tuple(map(int, value.split(".")))


def test_validate_version_rejects_invalid_and_unsupported_values(monkeypatch):
    """Policy failures warn and return None; SystemExit would kill GDB."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)

    assert version.validate_version("10.3") is None
    assert version.validate_version("11.4.0") is None
    assert version.validate_version("12.0.0") is None

    assert "invalid FreeRTOS version" in warnings[0]
    assert (
        sum(1 for message in warnings if "unsupported FreeRTOS version" in message) == 2
    )


def _stub_cu_fallback(monkeypatch) -> None:
    """Keep CU-scoped fallback off unless a test is exercising it."""
    monkeypatch.setattr(version, "read_macro_text_in_source", lambda *_a, **_k: None)


def test_detect_version_from_macro_triplet(monkeypatch):
    """The tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD int macros win when present."""
    values = {
        "tskKERNEL_VERSION_MAJOR": 10,
        "tskKERNEL_VERSION_MINOR": 3,
        "tskKERNEL_VERSION_BUILD": 1,
    }
    monkeypatch.setattr(version, "read_macro_int", lambda name: values.get(name))
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_switches_to_kernel_source_context(monkeypatch):
    """A non-kernel CU is repaired by selecting the kernel source context."""
    values = {
        "tskKERNEL_VERSION_MAJOR": "11",
        "tskKERNEL_VERSION_MINOR": "1",
        "tskKERNEL_VERSION_BUILD": "0",
    }
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(
        version,
        "read_macro_text_in_source",
        lambda name, source: values[name] if source == "vTaskStartScheduler" else None,
    )
    monkeypatch.setattr(version, "read_macro_text", lambda _name: None)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == (11, 1, 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("V11.1.0+", (11, 1, 0)),
        ("V10.3.1", (10, 3, 1)),
        ("10.5.0", (10, 5, 0)),
    ],
)
def test_detect_version_from_version_number_string(monkeypatch, text, expected):
    """The tskKERNEL_VERSION_NUMBER string macro is parsed with the V/+ forms."""
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: text)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == expected


def test_detect_version_rejects_invalid_string(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: "unknown")
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() is None


def test_detect_version_from_gdr_symbol_decimal_fallback(monkeypatch):
    """gdr_freertos_version_num (decimal MMmmpp) is the last-resort escape hatch."""
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(
        version,
        "lookup_symbol",
        lambda name: 100301 if name == "gdr_freertos_version_num" else None,
    )

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_from_gdr_symbol_packed_hex_fallback(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(
        version,
        "lookup_symbol",
        lambda name: 0xA0301 if name == "gdr_freertos_version_num" else None,
    )

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_returns_none_when_nothing_exported(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() is None


def test_check_target_rejects_a_target_mismatch(monkeypatch):
    """A mismatch refuses registration; the GDB session keeps running."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "detect_target_version", lambda: (10, 5, 0))
    monkeypatch.setattr(version, "symbol_exists", lambda _name: True)

    verdict = version.check_target("10.3.1")

    assert not verdict.accepted
    assert verdict.code == "version_mismatch"
    assert "version mismatch" in warnings[-1]


def test_check_target_refuses_a_target_without_any_marker(monkeypatch):
    """No FreeRTOS symbol means no adapter: every read would come back empty."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)
    monkeypatch.setattr(version, "loaded_object_path", lambda: "/fw/freertos.elf")

    verdict = version.check_target("10.3.1")

    assert not verdict.accepted
    assert verdict.code == "no_target_evidence"
    assert "--allow-unverified" in warnings[-1]


def test_check_target_without_a_symbol_file_names_file(monkeypatch):
    """`load` without `file` gets the actionable hint, not the load hint."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)
    monkeypatch.setattr(version, "loaded_object_path", lambda: None)

    verdict = version.check_target("10.3.1")

    assert not verdict.accepted
    assert verdict.code == "no_target_evidence"
    assert "`file <elf>`" in warnings[-1]


def test_allow_unverified_keeps_the_escape_hatch_open(monkeypatch):
    """A modified or stripped kernel must still be debuggable on request."""
    monkeypatch.setattr(version, "warn", lambda _message: None)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)

    verdict = version.check_target("10.3.1", allow_unverified=True)

    assert verdict.accepted
    assert verdict.version == (10, 3, 1)
    assert verdict.note is not None and "--allow-unverified" in verdict.note


def test_check_target_accepts_markers_without_an_exported_version(monkeypatch):
    """The user asserts the version here; that is the plugin's confirm flow."""
    monkeypatch.setattr(version, "warn", lambda _message: None)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: True)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)

    verdict = version.check_target("10.3.1")

    assert verdict.accepted
    assert verdict.note is not None and "unverified" in verdict.note
    assert verdict.markers == version.PROBE_MARKERS


def _stub(monkeypatch, *, present: tuple[str, ...], version, tcb_fields=()):
    """Resolve only *present* markers, pin the version, pin the TCB fields."""
    monkeypatch.setattr(probe_module, "symbol_exists", lambda name: name in present)
    monkeypatch.setattr(probe_module, "detect_target_version", lambda: version)
    monkeypatch.setattr(
        probe_module, "type_field_names", lambda _type_name: frozenset(tcb_fields)
    )


def test_probe_returns_none_when_no_marker_matches(monkeypatch):
    """An adapter with nothing to claim must not appear in the report."""
    _stub(monkeypatch, present=(), version=None)

    assert probe_module.probe() is None


def test_probe_reports_an_exact_version_with_its_evidence(monkeypatch):
    """A resolvable version is ``exact`` and names the macros behind it."""
    _stub(monkeypatch, present=PROBE_MARKERS[:2], version=(10, 3, 1))

    candidate = probe_module.probe()

    assert candidate is not None
    assert candidate.confidence == "exact"
    assert candidate.version == "10.3.1"
    assert "tskKERNEL_VERSION_MAJOR" in candidate.evidence[-1]


def test_probe_narrows_the_era_when_the_version_macros_are_absent(monkeypatch):
    """A -g2 build still yields a range, with the fingerprint as evidence."""
    _stub(
        monkeypatch,
        present=PROBE_MARKERS[:1],
        version=None,
        tcb_fields=(SMP_ERA_FIELD,),
    )

    candidate = probe_module.probe()

    assert candidate is not None
    assert candidate.confidence == "range"
    assert candidate.version is None
    assert any(SMP_ERA_FIELD in line for line in candidate.evidence)
    assert "confirmed by the user" in candidate.detail

    _stub(monkeypatch, present=PROBE_MARKERS[:1], version=None)

    pre_smp = probe_module.probe()

    assert pre_smp is not None
    assert pre_smp.confidence == "range"
    assert any("absent" in line for line in pre_smp.evidence)


_REPO = Path(__file__).resolve().parents[3]


def test_variant_names_are_unique():
    names = list_freertos_variants()
    assert len(names) == len(set(names))
    assert names


def test_every_variant_has_config_directory():
    for variant in list_freertos_variants():
        path = _REPO / "ci" / "fixture" / "freertos" / "profiles" / "config" / variant
        assert path.is_dir(), path
        assert (path / "FreeRTOSConfig.h").is_file(), path


def test_every_board_has_port_directory():
    for board in list_freertos_boards():
        path = _REPO / "ci" / "fixture" / "freertos" / "profiles" / "board" / board
        assert path.is_dir(), path
        assert (path / "gdr_board.h").is_file(), path


def test_version_ranges_monotonic_with_capabilities():
    """Later kernel versions never drop notification-array / mini-list / batching."""
    earlier = get_freertos_test_profile("base", "10.3.1")
    mid = get_freertos_test_profile("base", "10.4.6")
    later = get_freertos_test_profile("base", "10.5.1")
    v11 = get_freertos_test_profile("base", "11.1.0")
    assert earlier.notification_array is False
    assert mid.notification_array is True
    assert later.notification_array is True
    assert later.mini_list_item is True
    assert v11.batching_buffer is True
    assert earlier.batching_buffer is False


def test_static_only_and_static_dynamic_are_distinct():
    only = get_freertos_test_profile("static-only", "10.3.1")
    both = get_freertos_test_profile("static-dynamic", "10.3.1")
    assert only.static_allocation is True
    assert only.static_and_dynamic is False
    assert both.static_allocation is True
    assert both.static_and_dynamic is True


def test_heap_variants_map_to_independent_kinds():
    assert get_freertos_test_profile("heap-1", "10.3.1").heap_kind == 1
    assert get_freertos_test_profile("heap-2", "10.3.1").heap_kind == 2
    assert get_freertos_test_profile("heap-3", "10.3.1").heap_kind is None
    assert get_freertos_test_profile("base", "10.3.1").heap_kind == 4
    assert get_freertos_test_profile("heap-5", "10.3.1").heap_kind == 5


def test_static_only_has_no_heap_manager():
    """static-only links no MemMang source, so detection reports None."""
    assert get_freertos_test_profile("static-only", "10.3.1").heap_kind is None


def test_mpu_pool_has_a_live_variant():
    """The mpu variant (single-core CM33 with configENABLE_MPU) is registered
    and boots live on mps2-an521 (the xKernelObjectPool channel's first live
    fixture); it is not the old 'mpu-pool' name -- the pool symbol is reached
    through the mpu variant's build.  The MPU wrappers v2 TCB declares
    ucStaticallyAllocated whenever dynamic allocation is on, so the probed
    static_and_dynamic is True even though the fixture never creates a
    static task."""
    assert "mpu" in list_freertos_variants()
    assert "mpu-pool" not in list_freertos_variants()
    profile = get_freertos_test_profile("mpu", "11.3.1", "mps2-an521")
    assert profile.static_and_dynamic is True
    assert profile.static_allocation is False


def test_tick_widths_by_variant():
    """Only tick16 declares a 16-bit tick; rv64 is 64-bit (RISC-V port)."""
    assert get_freertos_test_profile("tick16", "10.3.1").tick_bits == 16
    assert get_freertos_test_profile("tick16", "11.1.0", "mps2-an385").tick_bits == 16
    assert get_freertos_test_profile("rv64", "11.1.0", "qemu-virt-rv64").tick_bits == 64
    for variant in list_freertos_variants():
        if variant in {"tick16", "rv64"}:
            continue
        profile = get_freertos_test_profile(variant, "11.1.0")
        assert profile.tick_bits == 32, variant


def test_stack_watermark_is_off_only_without_trace_facility():
    """Only trace-off drops the 0xa5 prefill, hence the HighWater column."""
    assert get_freertos_test_profile("trace-off", "10.3.1").stack_watermark is False
    for variant in list_freertos_variants():
        if variant == "trace-off":
            continue
        profile = get_freertos_test_profile(variant, "10.3.1")
        assert profile.stack_watermark is True, variant


def _fold(
    monkeypatch,
    struct_name: str,
    values: dict[str, object],
    config: FreeRtosConfig | None = None,
) -> str:
    """Render the one-line fold for *struct_name* with fake field values."""
    layout = build_layout(config or FreeRtosConfig(), (10, 3, 1))
    sl = layout.structs[struct_name]

    monkeypatch.setattr(printers, "read_field", lambda _v, _l, f: values.get(f))
    monkeypatch.setattr(printers, "read_cstring", lambda value: value)
    return printers.LayoutPrinter(object(), sl).to_string()


def test_task_fold_is_nonempty_and_names_expected_fields(monkeypatch):
    text = _fold(
        monkeypatch,
        "struct tskTaskControlBlock",
        {"name": "worker", "current_priority": 4},
    )
    assert text.startswith("Task(")
    assert "name=" in text
    assert "current_priority=" in text
    assert "worker" in text
    assert text != "Task()"


def test_queue_fold_is_nonempty_and_names_expected_fields(monkeypatch):
    text = _fold(
        monkeypatch,
        "struct QueueDefinition",
        {"length": 10, "count": 3, "type": 2},
        config=FreeRtosConfig(trace_facility=True),
    )
    assert text.startswith("Queue(")
    assert "length=" in text
    assert "count=" in text
    assert "type=counting-sem" in text
    assert text != "Queue()"


def test_queue_fold_omits_the_type_key_without_trace_facility(monkeypatch):
    """``ucQueueType`` is absent when configUSE_TRACE_FACILITY is 0.

    Describing it anyway would fold every queue as ``type=N/A`` on the default
    (trace-off) configuration instead of simply not claiming a type.
    """
    text = _fold(
        monkeypatch,
        "struct QueueDefinition",
        {"length": 10, "count": 3},
        config=FreeRtosConfig(trace_facility=False),
    )
    assert text == "Queue(length=10, count=3)"


def test_timer_fold_is_nonempty_and_names_expected_fields(monkeypatch):
    text = _fold(
        monkeypatch,
        "struct tmrTimerControl",
        {"name": "blink", "period": 1000},
    )
    assert text.startswith("Timer(")
    assert "name=" in text
    assert "period=" in text
    assert text != "Timer()"
