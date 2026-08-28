"""Unit tests for the queue-family (queue / semaphore / mutex) model.

Classification, discovery refinement, table/detail rendering and the
consistency checks are driven through module-level helpers (``read_path``,
``read_int``, ...) so every GDB entry point stays monkeypatchable outside a
GDB session, following the pattern of ``test_discovery.py``.
"""

from __future__ import annotations

import types
from dataclasses import replace

import pytest

import freertos.adapter as adapter_module
import freertos.details as details_module
import freertos.diagnostics as diagnostics
import freertos.navigation as navigation
from freertos.adapter import FreeRtosQueueObject
from freertos.layout import FreeRtosConfig, build_layout
from freertos.streams import FreeRtosStreamBufferObject


class _FakeValue:
    """Minimal gdb.Value stand-in for symbol-channel tests."""

    def __init__(self, type_name: str, address: int, value: int):
        self.type = types.SimpleNamespace(name=type_name)
        self.address = address
        self.value = value

    def __int__(self) -> int:
        return self.value


def _patch_reads(monkeypatch, module, paths: dict, read_int=None):
    """Wire *module*'s read_path/read_int against a path -> value dict."""
    monkeypatch.setattr(module, "read_path", lambda _value, path: paths.get(path))
    monkeypatch.setattr(
        module, "read_int", (lambda value: value) if read_int is None else read_int
    )


# ---------------------------------------------------------------------------
# classify_queue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected_kind", "recursive", "is_set"),
    [
        (0, "queue", False, False),  # BASE
        (1, "mutex", False, False),  # MUTEX
        (2, "semaphore", False, False),  # COUNTING_SEMAPHORE
        (3, "semaphore", False, False),  # BINARY_SEMAPHORE
        (4, "mutex", True, False),  # RECURSIVE_MUTEX
        (5, "queue", False, True),  # SET
    ],
)
def test_classify_queue_with_trace_facility_covers_six_type_codes(
    monkeypatch, code, expected_kind, recursive, is_set
):
    """ucQueueType is definitive when configUSE_TRACE_FACILITY is on."""
    layout = build_layout(FreeRtosConfig(trace_facility=True))
    monkeypatch.setattr(navigation, "read_path", lambda _v, _path: code)
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    result = navigation.classify_queue(object(), layout)

    assert result is not None
    assert result.kind == expected_kind
    assert result.type_code == code
    assert result.inferred is False
    assert result.recursive is recursive
    assert result.is_set is is_set


def test_classify_queue_without_trace_facility_infers_three_kinds(monkeypatch):
    """Without ucQueueType the pcHead/itemSize chain discriminates the family."""
    layout = build_layout(FreeRtosConfig(trace_facility=False))
    cases = [
        # pcHead == NULL marks a mutex (queueQUEUE_IS_MUTEX).
        ({("pcHead",): 0}, "mutex"),
        # Semaphores carry no storage; pcHead is the object address.
        ({("pcHead",): 0x2000, ("uxItemSize",): 0}, "semaphore"),
        # Anything else is a data queue.
        ({("pcHead",): 0x2000, ("uxItemSize",): 4}, "queue"),
    ]
    for paths, expected in cases:
        _patch_reads(monkeypatch, navigation, paths)
        result = navigation.classify_queue(object(), layout)
        assert result is not None
        assert result.kind == expected
        assert result.type_code is None
        assert result.inferred is True
        assert result.recursive is None
        assert result.is_set is None


def test_classify_queue_treats_unreadable_pc_head_as_mutex(monkeypatch):
    """An unreadable pcHead cannot prove a data queue or a semaphore.

    The discrimination chain terminates on the mutex alternative -- NULL is
    the mutex marker itself (queue.h) and ucQueueType is absent here -- so
    the classification stays decidable rather than unknown.
    """
    layout = build_layout(FreeRtosConfig(trace_facility=True))
    monkeypatch.setattr(
        navigation,
        "read_path",
        lambda _v, _path: None,  # ucQueueType and pcHead both unreadable
    )

    result = navigation.classify_queue(object(), layout)

    assert result is not None
    assert result.kind == "mutex"
    assert result.inferred is True


# ---------------------------------------------------------------------------
# discovery refinement and shared waiter scan
# ---------------------------------------------------------------------------


def _wire_discovery_channels(monkeypatch, **channels):
    """Patch each channel; bare lists are wrapped as one-shot iterators."""
    names = {
        "mpu": "iter_mpu_pool_objects",
        "registry": "iter_registry_entries",
        "active": "iter_active_timer_hosts",
        "symbol": "iter_static_symbol_objects",
        "waiter": "iter_waiter_hosts",
    }

    def empty(_layout):
        return iter(())

    for key, attribute in names.items():
        value = channels.get(key)
        if value is None:
            monkeypatch.setattr(navigation, attribute, empty)
        elif callable(value):
            monkeypatch.setattr(navigation, attribute, value)
        else:
            monkeypatch.setattr(
                navigation, attribute, lambda _layout, value=value: iter(value)
            )


def _refine_with_type_code(monkeypatch, code: int):
    """Make classification definitive via a fake ucQueueType read."""
    layout = build_layout(FreeRtosConfig(trace_facility=True))
    monkeypatch.setattr(navigation, "_queue_value", lambda _address, _l: object())
    monkeypatch.setattr(navigation, "read_path", lambda _v, _path: code)
    monkeypatch.setattr(navigation, "read_int", lambda value: value)
    return layout


def test_discover_refines_registry_queue_into_mutex(monkeypatch):
    """A registry slot whose Queue_t is really a mutex lands in frt mutexes.

    Registry handles are type-erased QueueHandle_t, so the slot starts as
    kind=queue with inferred_kind=True; the refinement must both change the
    kind and clear the inferred flag (ucQueueType is definitive).
    """
    registry_obj = navigation.DiscoveredObject(
        kind="queue",
        address=0x2000,
        name="gdr_mutex",
        source="registry",
        inferred_kind=True,
    )
    _wire_discovery_channels(monkeypatch, registry=[registry_obj])
    layout = _refine_with_type_code(monkeypatch, code=1)

    mutexes = navigation.discover("mutex", layout)
    assert len(mutexes) == 1
    assert mutexes[0].name == "gdr_mutex"
    assert mutexes[0].kind == "mutex"
    assert mutexes[0].inferred_kind is False
    assert navigation.discover("queue", layout) == []


def test_discover_dedups_queue_family_across_kinds(monkeypatch):
    """The same address found by registry and symbol channels counts once.

    Before refinement the registry slot (kind=queue) and the
    SemaphoreHandle_t symbol (kind=semaphore) described the *same* mutex,
    so a naive per-kind dedup double-counted it; the cross-family dedup
    keeps the earlier channel's name and the refined kind.
    """
    registry_obj = navigation.DiscoveredObject(
        kind="queue",
        address=0x2000,
        name="gdr_mutex",
        source="registry",
        inferred_kind=True,
    )
    symbol_obj = navigation.DiscoveredObject(
        kind="semaphore",
        address=0x2000,
        name="gdr_mutex",
        source="symbol",
    )
    _wire_discovery_channels(monkeypatch, registry=[registry_obj], symbol=[symbol_obj])
    layout = _refine_with_type_code(monkeypatch, code=1)

    found = navigation.discover("mutex", layout)
    assert len(found) == 1
    assert found[0].source == "registry"
    assert navigation.discover("queue", layout) == []
    assert navigation.discover("semaphore", layout) == []

    groups = navigation.discover_all(layout)
    family_total = sum(
        len(groups.get(kind, [])) for kind in ("queue", "semaphore", "mutex")
    )
    assert family_total == 1


def test_discover_all_scans_waiter_channel_once(monkeypatch):
    """frt objects must not re-walk task lists once per kind."""
    layout = build_layout(FreeRtosConfig(trace_facility=True))
    calls = {"count": 0}

    def counting_waiter(_layout):
        calls["count"] += 1
        return iter(())

    _wire_discovery_channels(monkeypatch, waiter=counting_waiter)
    monkeypatch.setattr(navigation, "_queue_value", lambda _a, _l: object())
    monkeypatch.setattr(adapter_module, "_kind_enabled", lambda _kind, _layout: True)
    monkeypatch.setattr(
        adapter_module, "iter_converted_tasks", lambda _layout: iter(())
    )

    adapter_module.FreeRtosAdapter(layout).object_summary_rows()

    assert calls["count"] == 1


def test_resolve_object_prefers_name_over_decimal_address(monkeypatch):
    """A numeric object name resolves as a symbol before digits are an address."""
    layout = build_layout(FreeRtosConfig())
    by_name = {
        "42": _FakeValue("QueueHandle_t", 0x2000, 0x20002000),
    }
    monkeypatch.setattr(navigation, "lookup_symbol", by_name.get)

    named = navigation.resolve_object("queue", "42", layout)
    assert named is not None
    assert named.name == "42"
    assert named.address == 0x20002000
    assert named.source == "user"

    # The lookup failing (or the number not being a symbol) falls back to a
    # plain decimal address.
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: None)
    decimal = navigation.resolve_object("queue", "536874000", layout)
    assert decimal is not None
    assert decimal.address == 536874000
    assert decimal.name is None


# ---------------------------------------------------------------------------
# value_to_queue_object: mutex-only decoding of the xSemaphore arm
# ---------------------------------------------------------------------------


def test_value_to_queue_object_decodes_mutex_semaphore_arm(monkeypatch):
    """Only a mutex may decode u.xSemaphore; holders and recursion land in the
    model while the queue arm stays untouched."""
    layout = build_layout(FreeRtosConfig(trace_facility=True), (10, 3, 1))
    found = navigation.DiscoveredObject(
        kind="mutex", address=0x2000, name="gdr_mutex", source="registry"
    )
    paths = {
        ("uxLength",): 1,
        ("uxMessagesWaiting",): 0,
        ("uxItemSize",): 0,
        ("ucQueueType",): 1,
        ("cRxLock",): -1,
        ("cTxLock",): -1,
        ("u", "xSemaphore", "xMutexHolder"): 0x3000,
        ("u", "xSemaphore", "uxRecursiveCallCount"): 2,
    }
    _patch_reads(monkeypatch, adapter_module, paths)
    monkeypatch.setattr(adapter_module, "task_name_at", lambda _address, _l: "main")

    obj = adapter_module.value_to_queue_object(object(), found, layout)

    assert obj.kind == "mutex"
    assert obj.holder_address == 0x3000
    assert obj.holder == "main"
    assert obj.recursive_count == 2
    assert obj.type_code == 1
    assert obj.free == 1  # length 1 - count 0


def test_semaphore_detail_never_reads_semaphore_union_arm(monkeypatch):
    """Semaphore rendering never decodes the u.xSemaphore union arm.

    A semaphore's ``u`` member is QueuePointers_t (pcTail/pcReadFrom); read
    as SemaphoreData_t it would fabricate a holder whose address happens to
    be the object itself and a huge recursive count.  The u.xSemaphore paths
    are seeded with sentinels so any wrong read shows up in the rendered
    text and in the recorded read paths.
    """
    layout = build_layout(FreeRtosConfig(trace_facility=True), (10, 3, 1))
    found = navigation.DiscoveredObject(
        kind="semaphore", address=0x2000, name="gdr_semaphore", source="registry"
    )
    paths = {
        ("uxLength",): 3,
        ("uxMessagesWaiting",): 0,
        ("uxItemSize",): 0,
        ("ucQueueType",): 2,
        ("cRxLock",): -1,
        ("cTxLock",): -1,
        ("pcHead",): 0x2000,
        # Seeds that would expose a wrongful xSemaphore read as a holder.
        ("u", "xSemaphore", "xMutexHolder"): 0xDEADBEEF,
        ("u", "xSemaphore", "uxRecursiveCallCount"): 0xDEADBEEF,
    }
    recorded: list[tuple] = []

    def spy_read_path(_value, path):
        recorded.append(path)
        return paths.get(path)

    for module in (adapter_module, details_module, diagnostics):
        monkeypatch.setattr(module, "read_path", spy_read_path)
        monkeypatch.setattr(module, "read_int", lambda value: value)
    monkeypatch.setattr(diagnostics, "value_address", lambda _value: 0x2000)
    monkeypatch.setattr(
        adapter_module, "task_name_at", lambda address, _l: f"0x{address:x}"
    )

    obj = adapter_module.value_to_queue_object(object(), found, layout)
    pairs = details_module.semaphore_detail(obj, object(), layout)

    keys = {key for key, _value in pairs}
    assert "Owner" not in keys
    assert "RecursiveCallCount" not in keys
    text = "\n".join(f"{key}: {value}" for key, value in pairs).lower()
    assert "deadbeef" not in text
    # The pipeline must never have touched the xSemaphore union arm at all.
    assert not any(path[:2] == ("u", "xSemaphore") for path in recorded)


# ---------------------------------------------------------------------------
# FIFO item dump
# ---------------------------------------------------------------------------


def test_queue_items_follow_fifo_order_and_wrap(monkeypatch):
    """The Item[i] dump starts past pcReadFrom and wraps.

    pcReadFrom marks the last *consumed* item, so with it on the last slot of
    a 4-slot queue the next unread item sits at pcHead (wraparound at
    pcTail with the kernel's >= comparison).
    """
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    head, tail = 0x2000, 0x2010  # uxLength=4, uxItemSize=4
    paths = {
        ("pcHead",): head,
        ("u", "xQueue", "pcTail"): tail,
        ("u", "xQueue", "pcReadFrom"): tail - 4,  # last slot
        ("uxLength",): 4,
        ("uxMessagesWaiting",): 3,
        ("uxItemSize",): 4,
    }
    _patch_reads(monkeypatch, details_module, paths)
    read_sites: list[int] = []

    def read_bytes(address, size):
        read_sites.append(address)
        return bytes([address & 0xFF] * size)

    monkeypatch.setattr(details_module, "read_bytes", read_bytes)

    items = list(details_module.iter_queue_items(object(), layout))

    assert [address for _index, address, _payload in items] == [0x2000, 0x2004, 0x2008]
    assert read_sites == [0x2000, 0x2004, 0x2008]


def test_queue_item_payload_truncates_at_64_bytes(monkeypatch):
    """Item payloads longer than 64 bytes render truncated with a marker."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    paths = {
        ("pcHead",): 0x2000,
        ("u", "xQueue", "pcTail"): 0x2080,
        ("u", "xQueue", "pcReadFrom"): 0x2000,  # single-slot queue reset state
        ("pcWriteTo",): 0x2000,
        ("uxLength",): 1,
        ("uxMessagesWaiting",): 1,
        ("uxItemSize",): 128,
    }
    _patch_reads(monkeypatch, details_module, paths)
    _patch_reads(monkeypatch, diagnostics, paths)
    monkeypatch.setattr(details_module, "read_bytes", lambda _a, size: b"\xab" * size)
    obj = FreeRtosQueueObject(
        name="big",
        address=0x2000,
        kind="queue",
        type_code=0,
        count=1,
        length=1,
        item_size=128,
        free=0,
    )

    pairs = details_module.queue_detail(obj, object(), layout)
    item = next((value for key, value in pairs if key.startswith("Item[")), None)

    assert item is not None
    assert item.startswith("@0x2000: ")
    assert item.count("ab") == 64  # only the first 64 bytes are rendered
    assert item.endswith("…(+64 bytes)")


# ---------------------------------------------------------------------------
# consistency checks
# ---------------------------------------------------------------------------


L = ("uxLength",)
W = ("uxMessagesWaiting",)
ITEM = ("uxItemSize",)
H = ("pcHead",)
TAIL = ("u", "xQueue", "pcTail")
READ = ("u", "xQueue", "pcReadFrom")
WRITE = ("pcWriteTo",)
HOLDER = ("u", "xSemaphore", "xMutexHolder")


@pytest.mark.parametrize(
    ("kind", "obj_address", "paths", "check", "expect_ok"),
    [
        (  # Count: queued <= length
            "queue",
            0x2000,
            {L: 4, W: 2, ITEM: 4, H: 0x2000},
            "Count",
            True,
        ),
        (
            "queue",
            0x2000,
            {L: 4, W: 5, ITEM: 4, H: 0x2000},
            "Count",
            False,
        ),
        (  # Storage: pcTail - pcHead == uxLength * uxItemSize
            "queue",
            0x2000,
            {L: 2, W: 0, ITEM: 4, H: 0x2000, WRITE: 0x2004, TAIL: 0x2008, READ: 0x2004},
            "Storage",
            True,
        ),
        (
            "queue",
            0x2000,
            {L: 2, W: 0, ITEM: 4, H: 0x2000, WRITE: 0x2004, TAIL: 0x2010, READ: 0x2004},
            "Storage",
            False,
        ),
        (  # WritePtr: pcHead <= pcWriteTo < pcTail
            "queue",
            0x2000,
            {L: 2, W: 0, ITEM: 4, H: 0x2000, WRITE: 0x2004, TAIL: 0x2008, READ: 0x2004},
            "WritePtr",
            True,
        ),
        (
            "queue",
            0x2000,
            {L: 2, W: 0, ITEM: 4, H: 0x2000, WRITE: 0x2008, TAIL: 0x2008, READ: 0x2004},
            "WritePtr",
            False,
        ),
        (  # ReadPtr: pcHead <= pcReadFrom < pcTail
            "queue",
            0x2000,
            {L: 2, W: 0, ITEM: 4, H: 0x2000, WRITE: 0x2004, TAIL: 0x2008, READ: 0x2004},
            "ReadPtr",
            True,
        ),
        (
            "queue",
            0x2000,
            {L: 4, W: 0, ITEM: 4, H: 0x2000, WRITE: 0x2000, TAIL: 0x2010, READ: 0x2010},
            "ReadPtr",
            False,
        ),
        (  # MutexAccounting: count + (holder != NULL) == 1
            "mutex",
            0x2000,
            {L: 1, W: 0, ITEM: 0, H: 0, HOLDER: 0x3000},  # held
            "MutexAccounting",
            True,
        ),
        (
            "mutex",
            0x2000,
            {L: 1, W: 0, ITEM: 0, H: 0, HOLDER: 0},  # free -> count must be 1
            "MutexAccounting",
            False,
        ),
        (  # SemaphoreSelfHead: pcHead == object address, item size 0
            "semaphore",
            0x2000,
            {L: 3, W: 0, ITEM: 0, H: 0x2000},
            "SemaphoreSelfHead",
            True,
        ),
        (
            "semaphore",
            0x2000,
            {L: 3, W: 0, ITEM: 0, H: 0x3000},
            "SemaphoreSelfHead",
            False,
        ),
        (
            "semaphore",
            0x2000,
            {L: 3, W: 0, ITEM: 4, H: 0x2000},
            "SemaphoreSelfHead",
            False,
        ),
    ],
)
def test_queue_checks_pass_and_fail(
    monkeypatch, kind, obj_address, paths, check, expect_ok
):
    """Each consistency check has one passing and one failing case."""
    _patch_reads(monkeypatch, diagnostics, paths)
    monkeypatch.setattr(diagnostics, "value_address", lambda _value: obj_address)

    results = dict(
        diagnostics.queue_checks(object(), kind, build_layout(FreeRtosConfig()))
    )

    assert check in results
    if expect_ok:
        assert results[check] == "ok", results
    else:
        assert results[check].startswith("fail:"), results


def test_queue_checks_skip_pointer_bounds_for_mutex_and_semaphore(monkeypatch):
    """The pointer invariants only exist for real data queues.

    Applying them to a mutex (pcWriteTo keeps its reset value behind a NULL
    pcHead) or a semaphore (pcReadFrom == pcTail == pcHead) would fabricate
    failures that look like target corruption; they must be stated as
    skipped instead.
    """
    _patch_reads(
        monkeypatch,
        diagnostics,
        {L: 1, W: 1, ITEM: 0, H: 0, HOLDER: 0},  # free mutex: count 1, no holder
    )
    monkeypatch.setattr(diagnostics, "value_address", lambda _value: 0x2000)
    mutex_results = dict(
        diagnostics.queue_checks(object(), "mutex", build_layout(FreeRtosConfig()))
    )
    for name in ("Storage", "WritePtr", "ReadPtr"):
        assert mutex_results[name].startswith("skipped: not a data queue"), (
            mutex_results
        )
    assert mutex_results["MutexAccounting"] == "ok"

    _patch_reads(
        monkeypatch,
        diagnostics,
        {L: 3, W: 0, ITEM: 0, H: 0x2000},
    )
    monkeypatch.setattr(diagnostics, "value_address", lambda _value: 0x2000)
    sem_results = dict(
        diagnostics.queue_checks(object(), "semaphore", build_layout(FreeRtosConfig()))
    )
    # pcReadFrom == pcTail would make ReadPtr permanently fail; it is skipped.
    assert sem_results["ReadPtr"].startswith("skipped: not a data queue"), sem_results
    assert sem_results["SemaphoreSelfHead"] == "ok"
    assert sem_results["MutexAccounting"].startswith("skipped: not a mutex")


# ---------------------------------------------------------------------------
# shared cells
# ---------------------------------------------------------------------------


def test_waiter_summary_keeps_the_count_first():
    """count@names: truncation drops names, never the diagnostic count."""
    assert details_module.waiter_summary(None) == "N/A"
    assert details_module.waiter_summary([]) == "0"
    assert details_module.waiter_summary(["gdr_qsend"]) == "1@gdr_qsend"
    assert details_module.waiter_summary(["a", "b"]) == "2@a,b"


def test_locks_cell_renders_unlocked_dash_and_counts():
    """queueUNLOCKED (-1, or 255 read unsigned) is '-'; other values count."""
    assert details_module.locks_cell(-1, -1) == "-"
    assert details_module.locks_cell(2, 5) == "rx=2 tx=5"
    assert details_module.locks_cell(None, -1) == "N/A"
    assert adapter_module._normalize_lock(-1) == -1
    assert adapter_module._normalize_lock(255) == -1
    assert adapter_module._normalize_lock(3) == 3


def test_held_cell_reflects_the_taken_count():
    """A mutex is held when its count is 0; the holder pointer may lag a
    pre-scheduler take, so the count is the objective state."""
    assert details_module.held_cell(FreeRtosQueueObject(count=0)) == "yes"
    assert details_module.held_cell(FreeRtosQueueObject(count=1)) == "no"
    assert details_module.held_cell(FreeRtosQueueObject(count=None)) == "N/A"


# ---------------------------------------------------------------------------
# converter edge branches and detail routing
# ---------------------------------------------------------------------------


def test_value_to_queue_object_with_unreadable_value_keeps_found_fields():
    """A cast failure must not fabricate scalars: the model carries the
    discovery provenance only."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    found = navigation.DiscoveredObject(
        kind="semaphore", address=0x2000, name="gdr_semaphore", source="registry"
    )

    obj = adapter_module.value_to_queue_object(None, found, layout)

    assert obj.name == "gdr_semaphore"
    assert obj.address == 0x2000
    assert obj.kind == "semaphore"
    assert obj.source == "registry"
    assert obj.length is None
    assert obj.count is None
    assert obj.free is None


def test_value_to_queue_object_inferred_config_leaves_type_code_none(monkeypatch):
    """Without the trace facility or queue sets neither optional field reads."""
    layout = build_layout(
        FreeRtosConfig(trace_facility=False, queue_sets=False), (10, 3, 1)
    )
    found = navigation.DiscoveredObject(
        kind="queue",
        address=0x2000,
        name="gdr_queue",
        source="registry",
        inferred_kind=True,
    )
    _patch_reads(
        monkeypatch,
        adapter_module,
        {
            ("uxLength",): 4,
            ("uxMessagesWaiting",): 2,
            ("uxItemSize",): 4,
            ("cRxLock",): -1,
            ("cTxLock",): -1,
        },
    )

    obj = adapter_module.value_to_queue_object(object(), found, layout)

    assert obj.free == 2
    assert obj.type_code is None
    assert obj.set_container is None
    assert obj.holder is None
    assert obj.recursive_count is None


def test_waiter_names_reads_task_names_from_wait_list(monkeypatch):
    """Send/receive wait lists are walked like scheduler lists for names."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    value, head = object(), object()
    task_a, task_b = object(), object()
    monkeypatch.setattr(
        adapter_module,
        "read_path",
        lambda _v, path: head if path == ("xTasksWaitingToReceive",) else None,
    )
    monkeypatch.setattr(
        adapter_module, "_iter_list", lambda _head, _l: iter([task_a, task_b])
    )
    names_by_id = {id(task_a): "gdr_qrecv", id(task_b): ""}
    monkeypatch.setattr(
        adapter_module, "read_field", lambda task, _sl, f: task if f == "name" else None
    )
    monkeypatch.setattr(
        adapter_module, "read_cstring", lambda v: names_by_id.get(id(v)) or "-"
    )

    names = adapter_module.waiter_names(value, layout, "receive")
    send = adapter_module.waiter_names(value, layout, "send")

    assert names == ["gdr_qrecv", "-"]
    assert send is None  # unreadable head


def test_object_detail_routes_queue_family_kinds(monkeypatch):
    """frt queue/semaphore/mutex each reach their own detail builder."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    adapter = adapter_module.FreeRtosAdapter(layout)
    found = navigation.DiscoveredObject(
        kind="queue", address=0x2000, name="gdr_queue", source="registry"
    )

    def fake_find_named_object(kind, name):
        if name == "no_such_queue":
            return None, None
        # Reason: the real lookup returns an object of the resolved kind, and
        # object_detail refuses to render a mismatched kind, so the stub must
        # mirror that instead of always claiming "queue".
        return replace(found, kind=kind), object()

    monkeypatch.setattr(adapter, "_find_named_object", fake_find_named_object)
    monkeypatch.setattr(
        adapter_module,
        "value_to_queue_object",
        lambda _v, found_obj, _l: adapter_module.FreeRtosQueueObject(
            name=found_obj.name
        ),
    )
    monkeypatch.setattr(
        adapter_module,
        "queue_detail",
        lambda _o, _v, _l: [("Name", _o.name), ("Items", "0")],
    )
    monkeypatch.setattr(
        adapter_module, "semaphore_detail", lambda _o, _v, _l: [("Count", "0")]
    )
    monkeypatch.setattr(
        adapter_module, "mutex_detail", lambda _o, _v, _l: [("Owner", "main")]
    )
    monkeypatch.setattr(
        adapter_module,
        "value_to_stream_buffer_object",
        lambda _v, found_obj, _l: FreeRtosStreamBufferObject(name=found_obj.name),
    )
    monkeypatch.setattr(
        adapter_module,
        "stream_buffer_detail",
        lambda _o, _v, _l: [("Capacity", "31")],
    )

    queue_pairs = adapter.object_detail("queue", "gdr_queue")
    sem_pairs = adapter.object_detail("semaphore", "gdr_semaphore")
    mtx_pairs = adapter.object_detail("mutex", "gdr_mutex")
    missing = adapter.object_detail("queue", "no_such_queue")

    assert queue_pairs.pairs == [("Name", "gdr_queue"), ("Items", "0")]
    assert sem_pairs.pairs == [("Count", "0")]
    assert mtx_pairs.pairs == [("Owner", "main")]
    assert missing.found is False
    # Event groups and stream buffers reach their own detail builders now;
    # only an unknown kind degrades to the not-enumerable None.
    eg_pairs = adapter.object_detail("eventgroup", "gdr_evw")
    assert eg_pairs is not None
    assert eg_pairs.pairs[0] == ("Name", "gdr_queue")
    sb_pairs = adapter.object_detail("streambuffer", "gdr_sb")
    assert sb_pairs is not None
    assert sb_pairs.pairs == [("Capacity", "31")]
    assert adapter.object_detail("bogus", "x") is None


def test_find_named_object_falls_back_to_resolve_object(monkeypatch):
    """Name match first, then the explicit resolve fallback; neither -> None."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    adapter = adapter_module.FreeRtosAdapter(layout)
    resolved = navigation.DiscoveredObject(
        kind="mutex", address=0x6000, name=None, source="user"
    )
    monkeypatch.setattr(adapter_module, "discover", lambda _k, _l: [])
    monkeypatch.setattr(adapter_module, "resolve_object", lambda _k, _n, _l: resolved)
    monkeypatch.setattr(adapter_module, "_cast_object", lambda _a, _k, _l: object())

    found, value = adapter._find_named_object("mutex", "0x6000")
    assert found is resolved
    assert value is not None

    monkeypatch.setattr(adapter_module, "resolve_object", lambda _k, _n, _l: None)
    assert adapter._find_named_object("mutex", "nope") == (None, None)


def test_kind_enabled_uses_config_capabilities():
    """timer/eventgroup/streambuffer gates follow their config probes."""
    layout = build_layout(
        FreeRtosConfig(timers=True, event_groups=True, stream_buffers=True)
    )
    assert adapter_module._kind_enabled("task", layout) is True
    assert adapter_module._kind_enabled("timer", layout) is True
    assert adapter_module._kind_enabled("eventgroup", layout) is True
    assert adapter_module._kind_enabled("streambuffer", layout) is True


def test_queue_table_reports_inferred_kind_message():
    """The trace-off build says why the Type cells carry '?'."""
    layout = build_layout(FreeRtosConfig(trace_facility=False), (10, 3, 1))
    table = adapter_module.FreeRtosAdapter(layout)._queue_table([])

    assert any("configUSE_TRACE_FACILITY" in message for message in table.messages)


def test_queue_type_label_marks_inferred_kinds():
    from freertos.layout import queue_type_label

    assert queue_type_label("mutex", 1, False) == "mutex"
    assert queue_type_label("mutex", 4, False) == "recursive-mutex"
    assert queue_type_label("queue", 5, False) == "queue-set"
    assert queue_type_label("semaphore", 2, False) == "counting-sem"
    # No trace facility: only the family name with the inference marker.
    assert queue_type_label("mutex", None, True) == "mutex?"
    assert queue_type_label("semaphore", None, True) == "semaphore?"
    assert queue_type_label("queue", None, True) == "queue?"


def test_checks_pairs_summarises_a_healthy_object():
    """All-ok checks collapse to one verdict row with no problem rows."""
    pairs = details_module.checks_pairs(
        [("Count", "ok"), ("Storage", "ok"), ("WritePtr", "ok")]
    )

    assert pairs == [("Checks", "ok (3 verified)")]


def test_checks_pairs_counts_inapplicable_checks_without_naming_them():
    """Structural skips stay counted so 'not checked' is never read as 'ok'."""
    pairs = details_module.checks_pairs(
        [
            ("Count", "ok"),
            ("Storage", "skipped: not a data queue"),
            ("WritePtr", "skipped: not a data queue"),
        ]
    )

    assert pairs == [("Checks", "ok (1 verified, 2 n/a)")]


def test_checks_pairs_promotes_failures_to_their_own_row():
    """A failure is the actionable output, so it gets a row of its own."""
    pairs = details_module.checks_pairs(
        [
            ("Count", "ok"),
            ("MutexAccounting", "fail: 0 + holder(False) != 1 accounting"),
            ("SemaphoreSelfHead", "skipped: not a semaphore"),
        ]
    )

    assert pairs == [
        ("Checks", "1 failed (1 verified, 1 n/a)"),
        ("Check[MutexAccounting]", "0 + holder(False) != 1 accounting"),
    ]


def test_checks_pairs_reports_unreadable_separately_from_inapplicable():
    """An unreadable field means the check could not run, not that it passed."""
    pairs = details_module.checks_pairs(
        [("Count", "skipped: unreadable"), ("Storage", "skipped: not a data queue")]
    )

    assert pairs == [
        ("Checks", "ok (0 verified, 1 n/a)"),
        ("Check[Count]", "skipped: unreadable"),
    ]


def test_object_detail_redirects_when_the_name_is_another_kind(monkeypatch):
    """`frt semaphore <a mutex>` points at the right command, not a bad table.

    The address/symbol fallback stamps the requested kind on whatever it
    resolved, so without this check the semaphore detail rendered with every
    consistency check failing.
    """
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    adapter = adapter_module.FreeRtosAdapter(layout)
    resolved = navigation.DiscoveredObject(
        kind="semaphore", address=0x2000, name="gdr_mutex", source="user"
    )
    monkeypatch.setattr(
        adapter, "_find_named_object", lambda _k, _n: (resolved, object())
    )
    monkeypatch.setattr(
        adapter_module,
        "_refine_queue_object",
        lambda obj, _l: replace(obj, kind="mutex", inferred_kind=False),
    )

    detail = adapter.object_detail("semaphore", "gdr_mutex")

    assert detail.found is False
    assert detail.message == (
        "'gdr_mutex' is a mutex, not a semaphore; try `freertos mutex gdr_mutex`"
    )


def test_object_detail_renders_an_inferred_kind_instead_of_refusing(monkeypatch):
    """Without configUSE_TRACE_FACILITY a guessed kind must not block the view.

    The pointer-chain discriminator cannot tell a binary semaphore from a
    counting one, so refusing on an inferred mismatch would hide readable
    state behind a wrong-kind error.
    """
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    adapter = adapter_module.FreeRtosAdapter(layout)
    resolved = navigation.DiscoveredObject(
        kind="semaphore", address=0x2000, name="gdr_thing", source="user"
    )
    monkeypatch.setattr(
        adapter, "_find_named_object", lambda _k, _n: (resolved, object())
    )
    monkeypatch.setattr(
        adapter_module,
        "_refine_queue_object",
        lambda obj, _l: replace(obj, kind="mutex", inferred_kind=True),
    )
    monkeypatch.setattr(
        adapter_module,
        "value_to_queue_object",
        lambda _v, found_obj, _l: adapter_module.FreeRtosQueueObject(
            name=found_obj.name
        ),
    )
    monkeypatch.setattr(
        adapter_module, "semaphore_detail", lambda _o, _v, _l: [("Count", "0")]
    )

    detail = adapter.object_detail("semaphore", "gdr_thing")

    assert detail.found is True
    assert detail.pairs == [("Count", "0")]
