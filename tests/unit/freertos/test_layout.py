"""Unit tests for merged FreeRTOS configuration and layout ownership."""

from __future__ import annotations

import pytest

import freertos.layout as layout_module
from freertos.layout import FreeRtosConfig, build_layout


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
