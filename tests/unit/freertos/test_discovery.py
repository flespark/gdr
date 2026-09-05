"""Unit tests for the six-channel object discovery model.

The channels are driven through module-level helpers (``lookup_symbol``,
``read_field``, ``read_path``, ...) so every GDB entry point stays
monkeypatchable outside a GDB session; synthetic addresses stand in for
target memory.
"""

from __future__ import annotations

import types

import pytest

import freertos.adapter as adapter_module
import freertos.navigation as navigation
from freertos.layout import FreeRtosConfig, FreeRtosLayout, build_layout

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeValue:
    """Minimal gdb.Value stand-in: a typedef name, an address, a scalar."""

    def __init__(self, type_name: str, address: int, value: int):
        self.type = types.SimpleNamespace(name=type_name)
        self.address = address
        self.value = value

    def __int__(self) -> int:
        return self.value


class _FakeArrayType:
    def __init__(self, count: int):
        self._count = count

    def strip_typedefs(self):
        return self

    def range(self):
        return (0, self._count - 1)


class _FakePool:
    """xKernelObjectPool stand-in exposing a decodable array bound."""

    def __init__(self, slots):
        self.slots = slots
        self.type = _FakeArrayType(len(slots))

    def __getitem__(self, index):
        return self.slots[index]


@pytest.fixture(autouse=True)
def _fresh_symbol_cache():
    """The symbol-channel cache is module-global; keep tests isolated."""
    navigation.reset_symbol_object_cache()
    yield
    navigation.reset_symbol_object_cache()


# ---------------------------------------------------------------------------
# registry channel
# ---------------------------------------------------------------------------


def _patch_host_reads(monkeypatch, paths: dict):
    """Wire ``navigation.read_field`` for a waiter-host plausibility test.

    ``paths`` keys are raw member-path tuples (the layout resolves logical
    names to those paths); the sibling sentinel paths are reached through
    the List/Mini-list read sequence.
    """

    def fake_read_field(_value, struct_layout, field):
        f = struct_layout.fields.get(field)
        return paths.get(f.path) if f is not None else None

    monkeypatch.setattr(navigation, "read_field", fake_read_field)
    monkeypatch.setattr(navigation, "read_int", lambda value: value)


def _registry_channel(monkeypatch, slots, layout=None):
    """Wire the registry channel against a list of fake slot objects.

    Each slot provides ``name`` (raw windowed string or None) and ``handle``.
    """
    layout = layout or build_layout(
        FreeRtosConfig(queue_registry=True, queue_registry_size=len(slots))
    )
    names = {slot: slot.name for slot in slots}
    handles = {slot: slot.handle for slot in slots}
    table = list(slots)

    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: table)
    monkeypatch.setattr(navigation, "_array_item", lambda value, index: value[index])
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda item, _sl, field: names[item] if field == "name" else handles[item],
    )
    monkeypatch.setattr(navigation, "read_cstring", lambda value, _max_len=256: value)
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    return layout


class _Slot:
    def __init__(self, name, handle):
        self.name = name
        self.handle = handle


def test_iter_registry_entries_scans_holes_after_empty_slot(monkeypatch):
    """A hole in front of a filled slot must not stop the scan.

    vQueueUnregisterQueue can null out an early slot while later slots stay
    registered; stopping at the hole would silently drop those objects.
    """
    slots = [_Slot(None, 0), _Slot("gdr_queue", 0x20001000)]
    layout = _registry_channel(monkeypatch, slots)

    found = list(navigation.iter_registry_entries(layout))

    assert len(found) == 1
    assert found[0].name == "gdr_queue"
    assert found[0].address == 0x20001000
    assert found[0].source == "registry"
    # Registry handles are type-erased: semaphores/mutexes are queues here.
    assert found[0].inferred_kind is True


def test_iter_registry_entries_bounds_name_read(monkeypatch):
    """A windowed name read reaches the consumer NUL-free.

    The truncation contract lives in :func:`gdr.gdb_bridge.read_cstring`
    (covered by the bridge unit tests); the registry consumer only needs to
    pass the bridge result through unchanged.  The injected mock below
    plays the bridge's role -- clamping the 256-byte window at the first
    NUL -- and the registry channel must not widen or re-taint it.
    """
    window = ("TmrQ" + "\x00" + "z" * 300)[:256]
    slots = [_Slot(window, 0x20001000)]
    layout = _registry_channel(monkeypatch, slots)

    found = list(navigation.iter_registry_entries(layout))

    assert len(found) == 1
    assert found[0].name is not None
    assert found[0].name == window
    assert len(found[0].name) <= 256


def test_iter_registry_entries_skips_when_disabled(monkeypatch):
    """configQUEUE_REGISTRY_SIZE 0 means no registry symbol at all."""
    layout = build_layout(FreeRtosConfig(queue_registry=False, queue_registry_size=0))
    monkeypatch.setattr(
        navigation,
        "lookup_symbol",
        lambda _name: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert list(navigation.iter_registry_entries(layout)) == []


# ---------------------------------------------------------------------------
# symbol channel
# ---------------------------------------------------------------------------

_INFO_VARIABLES_SAMPLE = """\
All defined variables:

File /fixture/main.c:
50:\tstatic StaticTask_t gdr_idle_tcb;
79:\tstatic SemaphoreHandle_t gdr_mutex;
162:\tQueueRegistryItem_t xQueueRegistry[8];
51:\tstatic StackType_t gdr_idle_stack[128];

Non-debugging symbols:
0x08001234  gdr_stripped_thing
50:\tstatic TaskHandle_t gdr_after_marker;
"""

_MPU_INFO_VARIABLES = """\
All defined variables:

File /fixture/main.c:
60:\tstatic QueueHandle_t gdr_empty_queue;
"""


def test_iter_static_symbol_objects_maps_typedef_names(monkeypatch):
    """Static buffers use their symbol address; handles use their value.

    Mixing the two up would turn .bss pointer slots into fake objects.
    """
    layout = build_layout(FreeRtosConfig())
    monkeypatch.setattr(
        navigation, "_info_variables_text", lambda: _INFO_VARIABLES_SAMPLE
    )
    by_name = {
        "gdr_idle_tcb": _FakeValue("StaticTask_t", 0x20001000, 0x20001000),
        "gdr_mutex": _FakeValue("SemaphoreHandle_t", 0x20001004, 0x20002000),
        "xQueueRegistry": _FakeValue("QueueRegistryItem_t", 0x20001008, 0),
        "gdr_idle_stack": _FakeValue("StackType_t", 0x2000100C, 0),
    }
    monkeypatch.setattr(navigation, "lookup_symbol", by_name.get)

    found = {obj.name: obj for obj in navigation.iter_static_symbol_objects(layout)}

    assert set(found) == {"gdr_idle_tcb", "gdr_mutex"}
    assert found["gdr_idle_tcb"].kind == "task"
    assert found["gdr_idle_tcb"].address == 0x20001000
    assert found["gdr_mutex"].kind == "semaphore"
    assert found["gdr_mutex"].address == 0x20002000
    assert all(obj.source == "symbol" for obj in found.values())


def test_iter_static_symbol_objects_ignores_non_debugging_section(monkeypatch):
    """Entries after ``Non-debugging symbols:`` have no types to classify."""
    monkeypatch.setattr(
        navigation, "_info_variables_text", lambda: _INFO_VARIABLES_SAMPLE
    )

    declared = list(navigation._iter_declared_variables(_INFO_VARIABLES_SAMPLE))

    assert ("TaskHandle_t", "gdr_after_marker") not in declared
    assert ("StaticTask_t", "gdr_idle_tcb") in declared
    assert ("QueueRegistryItem_t", "xQueueRegistry") in declared


def test_iter_static_symbol_objects_caches_scan(monkeypatch):
    """The DWARF scan runs once per session, not once per kind/command."""
    layout = build_layout(FreeRtosConfig())
    calls = {"count": 0}

    def scan() -> str:
        calls["count"] += 1
        return ""

    monkeypatch.setattr(navigation, "_info_variables_text", scan)

    list(navigation.iter_static_symbol_objects(layout))
    list(navigation.iter_static_symbol_objects(layout))
    assert calls["count"] == 1

    navigation.reset_symbol_object_cache()
    list(navigation.iter_static_symbol_objects(layout))
    assert calls["count"] == 2


def test_iter_static_symbol_objects_degrades_on_scan_failure(monkeypatch):
    """An unusable scan caches an empty result instead of raising."""
    layout = build_layout(FreeRtosConfig())
    monkeypatch.setattr(navigation, "_info_variables_text", lambda: "")

    assert list(navigation.iter_static_symbol_objects(layout)) == []
    # The empty result is cached, so a second call does not re-scan.
    monkeypatch.setattr(
        navigation,
        "_info_variables_text",
        lambda: (_ for _ in ()).throw(AssertionError("must not re-scan")),
    )
    assert list(navigation.iter_static_symbol_objects(layout)) == []


def _mpu_symbol_channel(monkeypatch, handle_value, pool_slots, ranges=()):
    """Wire the symbol channel against an MPU build's opaque handles.

    The wrappers v2 external index in a *Handle_t variable is translated
    through xKernelObjectPool[value - 1].xInternalObjectHandle (see
    _opaque_mpu_symbol_address); pool slots expose ``handle`` and
    ``type_code`` attributes read through the same read_path wire the
    mpu-pool channel tests use.
    """
    layout = build_layout(FreeRtosConfig(mpu_object_pool=True))
    monkeypatch.setattr(navigation, "_info_variables_text", lambda: _MPU_INFO_VARIABLES)
    by_name = {
        "gdr_empty_queue": _FakeValue("QueueHandle_t", 0x20001000, handle_value),
        "xKernelObjectPool": _FakePool(pool_slots),
    }
    monkeypatch.setattr(navigation, "lookup_symbol", by_name.get)
    monkeypatch.setattr(navigation, "_array_item", lambda value, index: value[index])
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda item, _sl, field: (
            item.handle if field == "internal_handle" else item.type_code
        ),
    )
    monkeypatch.setattr(navigation, "_pointer_size", lambda: 4)
    monkeypatch.setattr(navigation, "mapped_ranges", lambda: ranges)
    return layout


def _mpu_pool_of(slots: dict[int, int]) -> list[types.SimpleNamespace]:
    """48-slot pool stub where *slots* maps internal index to handle."""
    return [
        types.SimpleNamespace(handle=slots.get(i, 0xFFFFFFFF), type_code=1)
        for i in range(48)
    ]


def test_symbol_channel_resolves_mpu_external_index(monkeypatch):
    """A wrappers-v2 handle (pool index + 1) resolves to the slot's internal
    object address instead of being read as a raw address."""
    layout = _mpu_symbol_channel(
        monkeypatch, handle_value=3, pool_slots=_mpu_pool_of({2: 0x280005E0})
    )

    found = list(navigation.iter_static_symbol_objects(layout))

    assert len(found) == 1
    assert found[0].address == 0x280005E0
    assert found[0].kind == "queue"
    assert found[0].source == "symbol"


def test_symbol_channel_skips_unresolvable_mpu_index(monkeypatch):
    """An index whose slot carries no object is an honest skip, never an
    address-looking row."""
    layout = _mpu_symbol_channel(
        monkeypatch,
        handle_value=7,
        pool_slots=_mpu_pool_of({}),  # slot 6 is empty (0)
        ranges=((0x10000000, 0x10040000), (0x28000000, 0x28200000)),
    )

    found = list(navigation.iter_static_symbol_objects(layout))

    assert found == []


def test_symbol_channel_skips_pointer_outside_sections_on_mpu_build(monkeypatch):
    """A handle value that is neither a valid index nor inside a mapped
    section cannot be trusted on an MPU build either."""
    layout = _mpu_symbol_channel(
        monkeypatch,
        handle_value=0x20002000,
        pool_slots=_mpu_pool_of({2: 0x280005E0}),
        ranges=((0x28000000, 0x28200000), (0x10000000, 0x10040000)),
    )

    found = list(navigation.iter_static_symbol_objects(layout))

    assert found == []


def test_symbol_channel_keeps_pointer_values_on_mpu_build(monkeypatch):
    """A pointer-sized value inside the mapped sections stays an address."""
    layout = _mpu_symbol_channel(
        monkeypatch,
        handle_value=0x28002000,
        pool_slots=_mpu_pool_of({2: 0x280005E0}),
        ranges=((0x28000000, 0x28200000), (0x10000000, 0x10040000)),
    )

    found = list(navigation.iter_static_symbol_objects(layout))

    assert len(found) == 1
    assert found[0].address == 0x28002000


def test_symbol_channel_unaffected_without_mpu_pool(monkeypatch):
    """On a non-MPU build the pool is never consulted and the existing
    pointer-value semantics stay untouched."""
    layout = build_layout(FreeRtosConfig(mpu_object_pool=False))
    monkeypatch.setattr(navigation, "_info_variables_text", lambda: _MPU_INFO_VARIABLES)
    monkeypatch.setattr(
        navigation,
        "lookup_symbol",
        lambda name: {
            "gdr_empty_queue": _FakeValue("QueueHandle_t", 0x20001000, 0x20002000)
        }.get(name),
    )

    found = list(navigation.iter_static_symbol_objects(layout))

    assert len(found) == 1
    assert found[0].address == 0x20002000


def test_resolve_object_resolves_mpu_handle_via_pool(monkeypatch):
    """The user channel (frt queue <name>) translates an MPU handle too."""
    layout = _mpu_symbol_channel(
        monkeypatch, handle_value=3, pool_slots=_mpu_pool_of({2: 0x280005E0})
    )

    obj = navigation.resolve_object("queue", "gdr_empty_queue", layout)

    assert obj is not None
    assert obj.address == 0x280005E0
    assert obj.source == "user"


# ---------------------------------------------------------------------------
# MPU pool channel
# ---------------------------------------------------------------------------


def test_iter_mpu_pool_objects_skips_empty_and_reserved(monkeypatch):
    """Empty (0) and reserved (~0) handles are skipped; type 5 is a timer."""
    layout = build_layout(FreeRtosConfig(mpu_object_pool=True))
    slots = [
        types.SimpleNamespace(handle=0, type_code=1),
        types.SimpleNamespace(handle=0xFFFFFFFF, type_code=1),
        types.SimpleNamespace(handle=0x20003000, type_code=5),
        types.SimpleNamespace(handle=0x20003004, type_code=0),
    ]
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: _FakePool(slots))
    monkeypatch.setattr(navigation, "_array_item", lambda value, index: value[index])
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda item, _sl, field: item.type_code if field == "type" else item.handle,
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    monkeypatch.setattr(navigation, "_pointer_size", lambda: 4)

    found = list(navigation.iter_mpu_pool_objects(layout))

    assert len(found) == 1
    assert found[0].address == 0x20003000
    assert found[0].kind == "timer"
    assert found[0].source == "mpu-pool"
    assert found[0].inferred_kind is False


def test_iter_mpu_pool_objects_marks_queue_kind_inferred(monkeypatch):
    """KERNEL_OBJECT_TYPE_QUEUE also covers semaphores and queue sets."""
    layout = build_layout(FreeRtosConfig(mpu_object_pool=True))
    slots = [types.SimpleNamespace(handle=0x20004000, type_code=1)]
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: _FakePool(slots))
    monkeypatch.setattr(navigation, "_array_item", lambda value, index: value[index])
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda item, _sl, field: item.type_code if field == "type" else item.handle,
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    monkeypatch.setattr(navigation, "_pointer_size", lambda: 4)

    found = list(navigation.iter_mpu_pool_objects(layout))

    assert found[0].kind == "queue"
    assert found[0].inferred_kind is True


def test_iter_mpu_pool_objects_disabled_by_config(monkeypatch):
    layout = build_layout(FreeRtosConfig(mpu_object_pool=False))
    monkeypatch.setattr(
        navigation,
        "lookup_symbol",
        lambda _name: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert list(navigation.iter_mpu_pool_objects(layout)) == []


# ---------------------------------------------------------------------------
# waiter channel
# ---------------------------------------------------------------------------


def test_iter_waiter_hosts_rejects_scheduler_lists(monkeypatch):
    """A container pointing at a scheduler list is not an object host.

    xPendingReadyList holds the xEventListItems of unblocked-but-not-yet-
    ready tasks; reading it as a queue host would fabricate objects.
    """
    layout = build_layout(FreeRtosConfig())
    tcb = object()
    event_item = object()
    scheduler_addr = 0x2000F000
    monkeypatch.setattr(
        navigation, "iter_tasks", lambda _layout: iter([(tcb, "Ready", None)])
    )
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda _value, _struct_layout, field_name: (
            event_item if field_name == "event_list_item" else scheduler_addr
        ),
    )
    monkeypatch.setattr(navigation, "safe_int", lambda value: int(value))
    monkeypatch.setattr(
        navigation,
        "_next_global",
        lambda name, deref=False: 0x2000F000 if name == "xPendingReadyList" else None,  # noqa: ARG005 (deref keyword kept for the call shape)
    )
    monkeypatch.setattr(
        navigation,
        "_host_from_container",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert list(navigation.iter_waiter_hosts(layout)) == []


def test_waiter_host_rejects_implausible_queue_fields(monkeypatch):
    """A candidate whose waiting count exceeds the length is not a host."""
    layout = build_layout(FreeRtosConfig())
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingToReceive",): member_list,
        ("uxLength",): 2,
        ("uxMessagesWaiting",): 5,  # more waiting than the queue can hold
        ("uxItemSize",): 4,
        ("pcHead",): 0x20000D38,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x20001000)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec("struct QueueDefinition", "recv_waiters", "queue"),
        0x20001000,
        0x20002000,
        layout,
    )

    assert plausible is False


def test_waiter_host_accepts_consistent_queue_fields(monkeypatch):
    """A real queue host passes membership and field consistency checks."""
    layout = build_layout(FreeRtosConfig())
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingToReceive",): member_list,
        ("xTasksWaitingToSend",): member_list,
        ("uxLength",): 2,
        ("uxMessagesWaiting",): 0,
        ("uxItemSize",): 4,
        ("pcHead",): 0x20000D38,
        ("xListEnd",): object(),
        ("xItemValue",): 0xFFFFFFFF,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x20001000)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec("struct QueueDefinition", "recv_waiters", "queue"),
        0x20001000,
        0x20002000,
        layout,
    )

    assert plausible is True


def test_waiter_host_rejects_pointer_like_event_group_bits(monkeypatch):
    """EventBits_t keeps the high byte for control bits; a pointer-looking
    value means the "host" is really memory inside another object."""
    layout = build_layout(FreeRtosConfig())
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingForBits",): member_list,
        ("uxEventBits",): 0x20001000,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x20001000)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec("struct EventGroupDef_t", "waiting", "eventgroup"),
        0x20001000,
        0x20002000,
        layout,
    )

    assert plausible is False


def test_waiter_host_rejects_address_in_map_on_64_bit_tick(monkeypatch):
    """On a 64-bit tick the (1 << 56) control-byte threshold admits every RAM
    pointer, so the loadable-section test must do the rejection: a
    pointer-looking uxEventBits inside a mapped range is not a bit field."""
    layout = build_layout(FreeRtosConfig(tick_bits=64))
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingForBits",): member_list,
        ("uxEventBits",): 0x80009BF0,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x80009DE0)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    monkeypatch.setattr(
        navigation, "mapped_ranges", lambda: ((0x80000000, 0x80010000),)
    )

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec(
            "struct EventGroupDef_t", "xTasksWaitingForBits", "eventgroup"
        ),
        # Reason: the container must equal the stubbed value_address so the
        # flow passes the identity guard and actually reaches the event-bits
        # branch -- an earlier revision passed the ghost address here and the
        # test passed vacuously at the first guard (verified by deleting the
        # mapped-ranges rejection and seeing it stay green).
        0x80009DE0,
        0x80009E00,
        layout,
    )

    assert plausible is False


def test_waiter_host_accepts_small_bits_on_64_bit_tick(monkeypatch):
    """A genuine bit field (small value, outside every loadable section)
    still confirms the host on a 64-bit tick."""
    layout = build_layout(FreeRtosConfig(tick_bits=64))
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingForBits",): member_list,
        ("uxEventBits",): 0x5,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x80009DE0)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    monkeypatch.setattr(
        navigation, "mapped_ranges", lambda: ((0x80000000, 0x80010000),)
    )

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec("struct EventGroupDef_t", "waiting", "eventgroup"),
        0x80009DE0,
        0x80009E00,
        layout,
    )

    assert plausible is True


def test_waiter_host_accepts_zero_bits_even_when_flash_maps_low(monkeypatch):
    """mps2 boards load flash at 0x00000000, so a quiescent event group
    (uxEventBits == 0) lies inside the first mapped range; the address test
    must not reject values below the 24 user event bits: 0 means 'no bits
    set', never an address."""
    layout = build_layout(FreeRtosConfig())
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingForBits",): member_list,
        ("uxEventBits",): 0x0,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x20001000)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    monkeypatch.setattr(
        navigation, "mapped_ranges", lambda: ((0x00000000, 0x00040000),)
    )

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec("struct EventGroupDef_t", "waiting", "eventgroup"),
        0x20001000,
        0x20002000,
        layout,
    )

    assert plausible is True


def test_waiter_host_rejects_null_head_with_items(monkeypatch):
    """A queue with items must own a storage buffer (non-null pcHead).

    A null pcHead next to a non-zero item size means the "host" is really
    memory inside a neighbour object (observed for the event-group waiter's
    send-list candidate).
    """
    layout = build_layout(FreeRtosConfig())
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingToSend",): member_list,
        ("uxLength",): 33,
        ("uxMessagesWaiting",): 0,
        ("uxItemSize",): 1,
        ("pcHead",): 0,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x20001000)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec("struct QueueDefinition", "send_waiters", "queue"),
        0x20001000,
        0x20002000,
        layout,
    )

    assert plausible is False


def test_waiter_host_accepts_null_head_without_items(monkeypatch):
    """Zero-size queues (semaphores/mutexes) legitimately have a null pcHead."""
    layout = build_layout(FreeRtosConfig())
    host = object()
    member_list = object()
    paths = {
        ("xTasksWaitingToReceive",): member_list,
        ("xTasksWaitingToSend",): member_list,
        ("uxLength",): 1,
        ("uxMessagesWaiting",): 0,
        ("uxItemSize",): 0,
        ("pcHead",): 0,
        ("xListEnd",): object(),
        ("xItemValue",): 0xFFFFFFFF,
    }
    _patch_host_reads(monkeypatch, paths)
    monkeypatch.setattr(navigation, "value_address", lambda _value: 0x20001000)
    monkeypatch.setattr(
        navigation, "_list_contains_item", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    plausible = navigation._plausible_waiter_host(
        host,
        navigation._WaiterSpec("struct QueueDefinition", "recv_waiters", "queue"),
        0x20001000,
        0x20002000,
        layout,
    )

    assert plausible is True


def test_iter_waiter_hosts_yields_confirmed_host(monkeypatch):
    """A confirmed host is yielded with kind/source provenance."""
    layout = build_layout(FreeRtosConfig())
    tcb = object()
    event_item = object()
    host = object()
    container = 0x2000102C
    monkeypatch.setattr(
        navigation, "iter_tasks", lambda _layout: iter([(tcb, "Blocked", None)])
    )
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda _value, _struct_layout, field_name: (
            event_item if field_name == "event_list_item" else container
        ),
    )
    monkeypatch.setattr(navigation, "safe_int", lambda value: value)
    monkeypatch.setattr(
        navigation,
        "value_address",
        lambda value: 0x20002000 if value is event_item else 0x20001000,
    )
    monkeypatch.setattr(
        navigation,
        "_host_from_container",
        # Only the receive-list candidate resolves to a host; the send and
        # event-group candidates fail the probe for this container.
        lambda _c, _s, member, _l: host if member == "recv_waiters" else None,
    )
    monkeypatch.setattr(navigation, "_plausible_waiter_host", lambda *_a, **_k: True)
    monkeypatch.setattr(navigation, "_scheduler_list_addresses", lambda _l: set())

    found = list(navigation.iter_waiter_hosts(layout))

    assert len(found) == 1
    assert found[0].address == 0x20001000
    assert found[0].source == "waiter"
    assert found[0].kind == "queue"
    assert found[0].inferred_kind is True
    assert found[0].name is None


# ---------------------------------------------------------------------------
# aggregation: discover / resolve_object
# ---------------------------------------------------------------------------


def test_discover_dedups_by_priority(monkeypatch):
    """The first channel to find an address keeps its name and source."""
    layout = build_layout(FreeRtosConfig())
    symbol_obj = navigation.DiscoveredObject(
        kind="queue", address=0x20001000, name="gdr_queue", source="symbol"
    )
    waiter_obj = navigation.DiscoveredObject(
        kind="queue", address=0x20001000, name=None, source="waiter"
    )
    monkeypatch.setattr(navigation, "iter_mpu_pool_objects", lambda _l: iter(()))
    monkeypatch.setattr(navigation, "iter_registry_entries", lambda _l: iter(()))
    monkeypatch.setattr(navigation, "iter_active_timer_hosts", lambda _l: iter(()))
    monkeypatch.setattr(
        navigation, "iter_static_symbol_objects", lambda _l: iter([symbol_obj])
    )
    monkeypatch.setattr(navigation, "iter_waiter_hosts", lambda _l: iter([waiter_obj]))

    found = navigation.discover("queue", layout)

    assert len(found) == 1
    assert found[0].address == symbol_obj.address
    assert found[0].source == "symbol"
    assert found[0].name == "gdr_queue"
    # The later channel is corroboration, not a duplicate: it is recorded as
    # an extra source so provenance can render "symbol+waiter".
    assert found[0].extra_sources == ("waiter",)
    assert navigation.source_label(found[0].source, found[0].extra_sources) == (
        "symbol+waiter"
    )


def test_discover_registry_beats_symbol(monkeypatch):
    """A registry-named queue keeps its name over the same symbol entry."""
    layout = build_layout(FreeRtosConfig())
    registry_obj = navigation.DiscoveredObject(
        kind="queue", address=0x20001000, name="gdr_queue", source="registry"
    )
    symbol_obj = navigation.DiscoveredObject(
        kind="queue", address=0x20001000, name="gdr_registered_queue", source="symbol"
    )
    monkeypatch.setattr(navigation, "iter_mpu_pool_objects", lambda _l: iter(()))
    monkeypatch.setattr(
        navigation, "iter_registry_entries", lambda _l: iter([registry_obj])
    )
    monkeypatch.setattr(navigation, "iter_active_timer_hosts", lambda _l: iter(()))
    monkeypatch.setattr(
        navigation, "iter_static_symbol_objects", lambda _l: iter([symbol_obj])
    )
    monkeypatch.setattr(navigation, "iter_waiter_hosts", lambda _l: iter(()))

    found = navigation.discover("queue", layout)

    assert len(found) == 1
    assert found[0].source == "registry"
    assert found[0].name == "gdr_queue"


def test_discover_filters_by_kind(monkeypatch):
    """Channels yield every kind; discover keeps only the requested one."""
    layout = build_layout(FreeRtosConfig())
    monkeypatch.setattr(navigation, "iter_mpu_pool_objects", lambda _l: iter(()))
    monkeypatch.setattr(navigation, "iter_registry_entries", lambda _l: iter(()))
    monkeypatch.setattr(navigation, "iter_active_timer_hosts", lambda _l: iter(()))
    monkeypatch.setattr(
        navigation,
        "iter_static_symbol_objects",
        lambda _l: iter(
            [
                navigation.DiscoveredObject(
                    kind="queue", address=0x1, name="q", source="symbol"
                ),
                navigation.DiscoveredObject(
                    kind="timer", address=0x2, name="t", source="symbol"
                ),
            ]
        ),
    )
    monkeypatch.setattr(navigation, "iter_waiter_hosts", lambda _l: iter(()))

    found = navigation.discover("timer", layout)

    assert len(found) == 1
    assert found[0].kind == "timer"
    assert found[0].address == 0x2


def test_resolve_object_accepts_addresses_and_symbols(monkeypatch):
    """Hex, decimal and symbol forms resolve; garbage forms return None."""
    layout = build_layout(FreeRtosConfig())
    by_name = {
        "gdr_queue_handle": _FakeValue("QueueHandle_t", 0x20001000, 0x20002000),
        "gdr_task_buf": _FakeValue("StaticTask_t", 0x20003000, 0x20003000),
    }
    monkeypatch.setattr(navigation, "lookup_symbol", by_name.get)

    hex_obj = navigation.resolve_object("queue", "0x20001000", layout)
    dec_obj = navigation.resolve_object("queue", "536874000", layout)
    handle_obj = navigation.resolve_object("queue", "gdr_queue_handle", layout)
    static_obj = navigation.resolve_object("task", "gdr_task_buf", layout)

    assert hex_obj is not None and hex_obj.address == 0x20001000
    assert hex_obj.source == "user"
    assert dec_obj is not None and dec_obj.address == 536874000
    assert handle_obj is not None and handle_obj.address == 0x20002000
    assert static_obj is not None and static_obj.address == 0x20003000
    assert navigation.resolve_object("queue", "0xZZ", layout) is None
    assert navigation.resolve_object("queue", "", layout) is None
    assert navigation.resolve_object("queue", "not an identifier", layout) is None


# ---------------------------------------------------------------------------
# adapter protocol: find_object / object_counts / summary
# ---------------------------------------------------------------------------


def test_find_object_matches_a_discovered_name(monkeypatch):
    """A registry name found by discover() is cast and returned."""
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))
    monkeypatch.setattr(
        adapter_module,
        "discover",
        lambda _kind, _layout: [
            navigation.DiscoveredObject(
                kind="queue", address=0x1000, name="gdr_queue", source="registry"
            )
        ],
    )
    cast_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        adapter_module,
        "_cast_object",
        lambda address, kind, _layout: cast_calls.append((address, kind)) or object(),
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_object",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unused")),
    )

    result = adapter.find_object("queue", "gdr_queue")

    assert result is not None
    assert cast_calls == [(0x1000, "queue")]


def test_find_object_accepts_name_hex_and_symbol(monkeypatch):
    """Name, ``0x`` address and symbol inputs each return a native value."""
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))
    monkeypatch.setattr(adapter_module, "discover", lambda _kind, _layout: [])
    monkeypatch.setattr(
        adapter_module,
        "resolve_object",
        lambda kind, text, _layout: (
            navigation.DiscoveredObject(kind=kind, address=int(text, 0), source="user")
            if not text.startswith("gdr_")
            else navigation.DiscoveredObject(
                kind=kind, address=0x20003000, source="user"
            )
        ),
    )
    monkeypatch.setattr(
        adapter_module, "_cast_object", lambda _address, _kind, _layout: object()
    )

    assert adapter.find_object("queue", "gdr_registered_queue") is not None
    assert adapter.find_object("queue", "0x20001000") is not None
    assert adapter.find_object("queue", "536874000") is not None


def test_find_object_task_routes_via_scheduler_lists(monkeypatch):
    """$gdr_object("task", name) agrees with the task table lookup."""
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))
    raw = object()
    monkeypatch.setattr(adapter_module, "find_task", lambda _name, _layout: raw)
    monkeypatch.setattr(
        adapter_module,
        "discover",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert adapter.find_object("task", "IDLE") is raw


def test_object_counts_only_includes_present_types(monkeypatch):
    """Kinds whose DWARF type is absent never appear in the counts."""
    layout = build_layout(
        FreeRtosConfig(timers=True, event_groups=False, stream_buffers=False)
    )
    monkeypatch.setattr(
        adapter_module, "iter_converted_tasks", lambda _layout: iter(())
    )
    monkeypatch.setattr(adapter_module, "_queue_type_present", lambda _layout: True)
    monkeypatch.setattr(adapter_module, "discover_all", lambda _layout: {})

    counts = adapter_module.FreeRtosAdapter(layout).object_counts()

    assert set(counts) == {"task", "queue", "semaphore", "mutex", "timer"}
    assert "eventgroup" not in counts
    assert "streambuffer" not in counts


def test_object_summary_rows_break_down_sources(monkeypatch):
    """Rows carry a per-channel ``source=count`` breakdown."""
    layout = build_layout(FreeRtosConfig(timers=True))
    monkeypatch.setattr(
        adapter_module,
        "iter_converted_tasks",
        lambda _layout: iter([object(), object()]),
    )
    monkeypatch.setattr(adapter_module, "_queue_type_present", lambda _layout: True)
    monkeypatch.setattr(
        adapter_module,
        "discover_all",
        lambda _layout: {
            "queue": [
                navigation.DiscoveredObject(
                    kind="queue", address=0x1, name="a", source="registry"
                ),
                navigation.DiscoveredObject(
                    kind="queue", address=0x2, name="b", source="symbol"
                ),
                navigation.DiscoveredObject(
                    kind="queue", address=0x3, name=None, source="symbol"
                ),
            ],
            "timer": [
                navigation.DiscoveredObject(
                    kind="timer", address=0x4, name="t", source="active"
                )
            ],
        },
    )

    rows = adapter_module.FreeRtosAdapter(layout).object_summary_rows()
    row_map = {kind: (count, sources) for kind, count, sources in rows}

    assert row_map["task"] == (2, "scheduler=2")
    assert row_map["queue"] == (3, "registry=1 symbol=2")
    assert row_map["timer"] == (1, "active=1")


def test_object_summary_table_reports_disabled_registry(monkeypatch):
    """A build without a queue registry explains it in the messages."""
    layout = build_layout(FreeRtosConfig(queue_registry=False))
    monkeypatch.setattr(
        adapter_module, "iter_converted_tasks", lambda _layout: iter(())
    )
    monkeypatch.setattr(adapter_module, "_kind_enabled", lambda _kind, _layout: False)

    table = adapter_module.FreeRtosAdapter(layout).object_summary_table()

    assert table.headers == ["Kind", "Count", "Sources"]
    assert any("queue registry" in message for message in table.messages)


# ---------------------------------------------------------------------------
# active timer channel
# ---------------------------------------------------------------------------


def test_iter_active_timer_hosts_yields_timers(monkeypatch):
    """The active channel walks the timer lists with the timer owner cast."""
    layout = build_layout(FreeRtosConfig(timers=True))
    timer = object()
    timer_name = object()
    received: dict[str, object] = {}

    def fake_lookup(name):
        received["lookup"] = name
        return object() if name == "pxCurrentTimerList" else None

    def fake_iter_list(_head, _layout, _max_count=..., owner_converter=None):
        received["owner_converter"] = owner_converter
        return iter([timer])

    monkeypatch.setattr(navigation, "lookup_symbol", fake_lookup)
    monkeypatch.setattr(
        navigation,
        "safe_dereference",
        lambda value: None if value is None else object(),
    )
    monkeypatch.setattr(navigation, "_iter_list", fake_iter_list)
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda _timer, _sl, field_name: timer_name if field_name == "name" else None,
    )
    monkeypatch.setattr(
        navigation,
        "read_cstring",
        lambda value: "gdr_active" if value is timer_name else None,
    )
    monkeypatch.setattr(navigation, "value_address", lambda _timer: 0x20005000)

    found = list(navigation.iter_active_timer_hosts(layout))

    assert len(found) == 1
    assert found[0].kind == "timer"
    assert found[0].name == "gdr_active"
    assert found[0].source == "active"
    assert found[0].address == 0x20005000
    assert received["owner_converter"] is navigation._owner_timer


def test_iter_active_timer_hosts_skipped_without_timers(monkeypatch):
    layout = build_layout(FreeRtosConfig(timers=False))
    monkeypatch.setattr(
        navigation,
        "lookup_symbol",
        lambda _name: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert list(navigation.iter_active_timer_hosts(layout)) == []


# ---------------------------------------------------------------------------
# list membership walk
# ---------------------------------------------------------------------------


def test_list_contains_item_finds_and_misses(monkeypatch):
    """The membership walk finds the item and stops at the list end."""
    layout = build_layout(FreeRtosConfig())
    head, end = object(), object()
    end_address = 0xFF00

    def make_walk(nodes):
        it = iter(nodes)

        def read_field(value, _struct_layout, field_name):
            if value is head and field_name == "end":
                return end
            return next(it)

        monkeypatch.setattr(navigation, "read_field", read_field)
        monkeypatch.setattr(navigation, "value_address", lambda _value: end_address)
        monkeypatch.setattr(navigation, "safe_int", lambda value: int(value))

    make_walk([0x2000, 0x3000, end_address])
    assert navigation._list_contains_item(head, 0x2000, layout) is True

    make_walk([0x2000, 0x3000, end_address])
    assert navigation._list_contains_item(head, 0x9999, layout) is False
