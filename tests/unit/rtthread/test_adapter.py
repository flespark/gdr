"""Unit tests for RT-Thread adapter summaries and tables."""

from __future__ import annotations

import pytest

import rtthread.adapter as adapter_module
from gdr.layout import KernelLayout, StructField, StructLayout
from rtthread.adapter import Event, Mailbox, MemoryPool, MessageQueue, Timer


def test_task_table_reads_current_task_once(monkeypatch):
    """RT-Thread does not re-read the current task for every rendered row."""
    current_reads = 0
    converted: list[tuple[object, int]] = []
    values = [object(), object(), object()]
    adapter = adapter_module.RtThreadAdapter(KernelLayout())

    def current_task():
        nonlocal current_reads
        current_reads += 1
        return object()

    def summarize(value, current_address):
        converted.append((value, current_address))
        return (
            adapter_module.Thread(name=f"task-{len(converted)}"),
            "Ready",
            None,
        )

    monkeypatch.setattr(adapter_module, "get_current_thread", current_task)
    monkeypatch.setattr(adapter_module, "_get_addr", lambda _value: 0x1234)
    monkeypatch.setattr(adapter_module, "iter_threads", lambda _layout: iter(values))
    monkeypatch.setattr(adapter, "_task_view", summarize)

    table = adapter.task_table()

    assert current_reads == 1
    assert [row[0] for row in table.rows] == ["task-1", "task-2", "task-3"]
    assert converted == [(value, 0x1234) for value in values]


def test_smp_task_summary_uses_real_oncpu(monkeypatch):
    """The current task reports its actual CPU, not a hardcoded core 0."""
    from rtthread.adapter import Thread

    current_value = object()
    current_thread = Thread(
        name="worker",
        address=0x2000,
        current_priority=20,
        init_priority=20,
        oncpu=1,
        bind_cpu=0,
    )
    adapter = adapter_module.RtThreadAdapter(KernelLayout(cpu_count=2))
    monkeypatch.setattr(
        adapter_module, "value_to_thread", lambda _value, _layout: current_thread
    )

    thread, _state, current_core = adapter._task_view(current_value, 0x2000)

    assert current_core == 1
    assert thread.oncpu == 1
    assert thread.bind_cpu == 0


def test_up_task_summary_keeps_core_zero_marker(monkeypatch):
    """UP targets keep the current marker on core 0 without SMP fields."""
    from rtthread.adapter import Thread

    current_thread = Thread(name="main", address=0x3000, oncpu=None, bind_cpu=None)
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(
        adapter_module, "value_to_thread", lambda _value, _layout: current_thread
    )

    _thread, _state, current_core = adapter._task_view(object(), 0x3000)

    assert current_core == 0


def test_timer_table_symbolizes_and_falls_back_to_addresses(monkeypatch):
    """Timer callbacks preserve symbol, address, and null boundary behavior."""
    timers = [
        Timer(name="symbolized", callback=0x1000),
        Timer(name="unknown", callback=0x2000),
        Timer(name="null", callback=0),
    ]
    looked_up: list[int] = []
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(adapter_module, "iter_timers", lambda _layout: iter(timers))
    monkeypatch.setattr(adapter_module, "value_to_timer", lambda value, _layout: value)
    monkeypatch.setattr(adapter_module, "get_tick", lambda: 123)
    monkeypatch.setattr(
        adapter_module,
        "lookup_symbol_at",
        lambda address: (
            looked_up.append(address)
            or ("test_timer_timeout+0" if address == 0x1000 else None)
        ),
    )

    table = adapter.object_table("timer")

    assert table is not None
    callback_idx = table.headers.index("Callback")
    assert [row[callback_idx] for row in table.rows] == [
        "<test_timer_timeout+0>",
        "0x2000",
        "0x0",
    ]
    assert table.messages == ["Kernel tick: 123"]
    assert looked_up == [0x1000, 0x2000]


@pytest.mark.parametrize(
    ("kind", "struct_name", "converted", "headers", "expected_row"),
    (
        (
            "event",
            "struct rt_event",
            Event(name="ready", set=0x3, address=0x1000),
            ["Name", "Set", "Policy", "Waiters", "Addr"],
            ["ready", "0x3", "N/A", "0", "0x1000"],
        ),
        (
            "mailbox",
            "struct rt_mailbox",
            Mailbox(
                name="input",
                entry=2,
                size=8,
                in_offset=3,
                out_offset=1,
                address=0x2000,
            ),
            [
                "Name",
                "Entry",
                "Size",
                "Free",
                "In",
                "Out",
                "Policy",
                "RecvWait",
                "SendWait",
                "Addr",
            ],
            ["input", "2", "8", "6", "3", "1", "N/A", "0", "0", "0x2000"],
        ),
        (
            "msgqueue",
            "struct rt_messagequeue",
            MessageQueue(name="work", entry=3, msg_size=16, max_msgs=8, address=0x3000),
            [
                "Name",
                "Entry",
                "MsgSize",
                "MaxMsgs",
                "Free",
                "Policy",
                "RecvWait",
                "SendWait",
                "Addr",
            ],
            ["work", "3", "16", "8", "5", "N/A", "0", "N/A", "0x3000"],
        ),
        (
            "mempool",
            "struct rt_mempool",
            MemoryPool(
                name="blocks",
                block_size=32,
                block_total_count=10,
                block_free_count=4,
                address=0x4000,
            ),
            ["Name", "BlockSize", "Total", "Free", "Used", "Waiters", "Addr"],
            ["blocks", "32", "10", "4", "6", "0", "0x4000"],
        ),
    ),
)
def test_ipc_object_tables_use_their_own_registry_route(
    kind, struct_name, converted, headers, expected_row, monkeypatch
):
    """Each IPC command converts only its own registered object type."""
    value = object()
    type_code = 7
    layout = KernelLayout(
        structs={struct_name: StructLayout(struct_name)},
        object_codes={kind: type_code},
    )
    calls: list[tuple[int, KernelLayout]] = []
    monkeypatch.setattr(
        adapter_module,
        "iter_objects",
        lambda code, selected: calls.append((code, selected)) or iter((value,)),
    )
    converter_name = {
        "event": "value_to_event",
        "mailbox": "value_to_mailbox",
        "msgqueue": "value_to_messagequeue",
        "mempool": "value_to_mempool",
    }[kind]
    monkeypatch.setattr(
        adapter_module,
        converter_name,
        lambda raw, selected: (
            converted if raw is value and selected is layout else None
        ),
    )

    table = adapter_module.RtThreadAdapter(layout).object_table(kind)

    assert table is not None
    assert table.headers == headers
    assert table.rows == [expected_row]
    assert calls == [(type_code, layout)]


def test_object_detail_returns_none_for_unknown_kind():
    """A kind outside the adapter's tables is reported as not enumerable."""
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    assert adapter.object_detail("device", "whatever") is None


def test_object_detail_reports_not_found(monkeypatch):
    """A missing object yields an explicit ``found=False`` result."""
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(adapter_module, "find_thread", lambda _name, _layout: None)

    detail = adapter.object_detail("task", "missing")

    assert detail is not None
    assert detail.found is False


def test_object_detail_task_uses_thread_builder(monkeypatch):
    """Thread detail includes the public thread fields."""
    thread = object()
    detail_pairs = [("Name", "worker1"), ("Address", "0x1000")]
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(adapter_module, "find_thread", lambda _name, _layout: thread)
    monkeypatch.setattr(
        adapter_module, "value_to_thread", lambda _value, _layout: thread
    )
    monkeypatch.setattr(
        adapter_module.diagnostics, "thread_detail", lambda _thread: detail_pairs
    )

    detail = adapter.object_detail("task", "worker1")

    assert detail is not None
    assert detail.found is True
    assert detail.pairs == detail_pairs


@pytest.mark.parametrize(
    ("kind", "expected_pairs"),
    (
        ("semaphore", [("Value", "3")]),
        ("mutex", [("Owner", "main")]),
        ("mailbox", [("Entry", "2")]),
        ("msgqueue", [("MaxMsgs", "8")]),
        ("mempool", [("Free", "4")]),
    ),
)
def test_object_detail_ipc_kinds_use_their_converter_and_builder(
    kind, expected_pairs, monkeypatch
):
    """Every IPC kind converts its value then builds its own detail pairs."""
    layout = KernelLayout()
    adapter = adapter_module.RtThreadAdapter(layout)
    value = object()
    converted = object()
    type_code = 7
    monkeypatch.setattr(
        adapter_module,
        "resolve_object_type_code",
        lambda _kind, _layout: type_code,
    )
    monkeypatch.setattr(
        adapter_module,
        "find_rt_object",
        lambda code, _name, _layout: value if code == type_code else None,
    )
    monkeypatch.setattr(
        adapter_module,
        "_DETAIL_BUILDERS",
        {
            kind: (
                lambda _value, _layout: converted,
                lambda _converted, *_raw: expected_pairs,
            )
        },
    )
    monkeypatch.setattr(adapter_module, "_ipc_detail_pairs", lambda *_args: [])
    monkeypatch.setattr(adapter_module, "_waiter_detail_pairs", lambda *_args: [])

    detail = adapter.object_detail(kind, "obj")

    assert detail is not None
    assert detail.found is True
    assert detail.pairs == expected_pairs


def test_event_detail_appends_waiter_conditions(monkeypatch):
    """Event detail pairs each waiter with its mask and AND/OR/CLEAR mode."""
    layout = KernelLayout()
    adapter = adapter_module.RtThreadAdapter(layout)
    event = object()
    type_code = 7
    base_pairs = [("Name", "ready"), ("Set", "0x3")]

    def fake_waiter_detail(_value, _layout):
        return base_pairs + [
            ("Waiter: t1", "set=0x3 mode=AND"),
            ("Waiter: t2", "set=0x1 mode=OR|CLEAR"),
        ]

    monkeypatch.setattr(
        adapter_module,
        "resolve_object_type_code",
        lambda _kind, _layout: type_code,
    )
    monkeypatch.setattr(
        adapter_module,
        "find_rt_object",
        lambda code, _name, _layout: event if code == type_code else None,
    )
    monkeypatch.setattr(
        adapter_module, "_event_detail_with_waiters", fake_waiter_detail
    )

    detail = adapter.object_detail("event", "ready")

    assert detail is not None
    assert detail.found is True
    assert detail.pairs == base_pairs + [
        ("Waiter: t1", "set=0x3 mode=AND"),
        ("Waiter: t2", "set=0x1 mode=OR|CLEAR"),
    ]


def test_ipc_detail_pairs_include_policy_and_waiters(monkeypatch):
    """Singular IPC detail reuses table policy and waiter diagnostics."""
    layout = KernelLayout(
        structs={
            "struct rt_semaphore": StructLayout(
                "struct rt_semaphore",
                fields={
                    "flag": StructField("flag", ("flag",)),
                    "suspend_thread": StructField(
                        "suspend_thread", ("suspend_thread",)
                    ),
                },
            )
        }
    )
    monkeypatch.setattr(
        adapter_module,
        "read_field",
        lambda _value, _layout, field: 1 if field == "flag" else None,
    )
    monkeypatch.setattr(adapter_module, "read_int", lambda value: value)
    monkeypatch.setattr(
        adapter_module,
        "suspend_thread_names",
        lambda _value, _layout, _struct, _field: ["worker4"],
    )

    pairs = dict(
        adapter_module._ipc_detail_pairs(
            object(),
            layout,
            "struct rt_semaphore",
            (("Waiters", "suspend_thread"),),
        )
    )

    assert pairs == {"Policy": "PRIO", "Waiters": "1@worker4"}


def test_timer_detail_includes_tick_detail(monkeypatch):
    """Timer detail reports the tick snapshot used for its expiry calculation."""
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    value = object()
    timer = Timer(name="heartbeat", active=True, timeout_tick=125)
    monkeypatch.setattr(adapter_module, "iter_timers", lambda _layout: iter((value,)))
    monkeypatch.setattr(adapter_module, "value_to_timer", lambda _v, _l: timer)
    monkeypatch.setattr(adapter_module, "get_tick", lambda: 100)
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "timer_detail",
        lambda _timer: [("Name", "heartbeat")],
    )

    detail = adapter.object_detail("timer", "heartbeat")

    assert detail is not None
    assert dict(detail.pairs)["KernelTick"] == "100"
    assert dict(detail.pairs)["ExpiresIn"] == "25"


def test_event_mode_decodes_and_or_clear_bits():
    """Event wait-mode decoding covers AND/OR/CLEAR and unknown values."""
    assert adapter_module._event_mode(0x1) == "AND"
    assert adapter_module._event_mode(0x2) == "OR"
    assert adapter_module._event_mode(0x4) == "CLEAR"
    assert adapter_module._event_mode(0x7) == "AND|OR|CLEAR"
    assert adapter_module._event_mode(0x8) == "0x8"


def test_waiter_summary_leads_with_count(monkeypatch):
    """The ``count@names`` summary keeps the count first for truncation."""
    layout = KernelLayout()
    value = object()
    monkeypatch.setattr(
        adapter_module,
        "suspend_thread_names",
        lambda _v, _l, _s, _f: ["worker1", "worker2"],
    )

    summary = adapter_module._waiter_summary(
        value, layout, "struct rt_semaphore", "suspend_thread"
    )

    assert summary == "2@worker1,worker2"


def test_waiter_summary_shows_zero_for_an_empty_list(monkeypatch):
    """An empty wait list renders as ``0``, never a fabricated N/A."""
    layout = KernelLayout()
    monkeypatch.setattr(
        adapter_module, "suspend_thread_names", lambda _v, _l, _s, _f: []
    )

    summary = adapter_module._waiter_summary(
        object(), layout, "struct rt_semaphore", "suspend_thread"
    )

    assert summary == "0"


def test_waiter_summary_shows_na_when_field_is_unavailable():
    """A missing field (e.g. old MQ sender list) is N/A, not a fake 0."""
    layout = KernelLayout()
    summary = adapter_module._waiter_summary(
        object(),
        layout,
        "struct rt_messagequeue",
        "suspend_sender_thread",
        available=False,
    )

    assert summary == "N/A"


def test_mailbox_table_splits_receiver_and_sender_waiters(monkeypatch):
    """Mailbox waiters distinguish receiver and sender suspend lists."""
    layout = KernelLayout(
        structs={"struct rt_mailbox": StructLayout("struct rt_mailbox")},
        object_codes={"mailbox": 5},
    )
    adapter = adapter_module.RtThreadAdapter(layout)
    value = object()
    monkeypatch.setattr(
        adapter_module,
        "iter_objects",
        lambda _code, _layout: iter((value,)),
    )
    monkeypatch.setattr(
        adapter_module,
        "value_to_mailbox",
        lambda _v, _l: Mailbox(name="input", address=0x2000),
    )
    monkeypatch.setattr(
        adapter_module,
        "suspend_thread_names",
        lambda _v, _l, _struct, field: (
            ["recv1"] if field == "suspend_thread" else ["send1", "send2"]
        ),
    )

    table = adapter.object_table("mailbox")

    assert table is not None
    assert table.headers == [
        "Name",
        "Entry",
        "Size",
        "Free",
        "In",
        "Out",
        "Policy",
        "RecvWait",
        "SendWait",
        "Addr",
    ]
    assert table.rows == [
        ["input", "0", "0", "0", "0", "0", "N/A", "1@recv1", "2@send1,send2", "0x2000"]
    ]


def test_messagequeue_table_honors_sender_list_availability(monkeypatch):
    """MQ sender waiters render as N/A when the field is absent by version."""
    layout = KernelLayout(
        structs={"struct rt_messagequeue": StructLayout("struct rt_messagequeue")},
        object_codes={"msgqueue": 6},
    )
    adapter = adapter_module.RtThreadAdapter(layout)
    value = object()
    monkeypatch.setattr(
        adapter_module,
        "iter_objects",
        lambda _code, _layout: iter((value,)),
    )
    monkeypatch.setattr(
        adapter_module,
        "value_to_messagequeue",
        lambda _v, _l: MessageQueue(name="work", address=0x3000),
    )
    monkeypatch.setattr(
        adapter_module,
        "suspend_thread_names",
        lambda _v, _l, _s, _f: ["recv1"],
    )

    table = adapter.object_table("msgqueue")

    assert table is not None
    assert table.rows == [
        ["work", "0", "0", "0", "0", "N/A", "1@recv1", "N/A", "0x3000"]
    ]


def test_cpu_or_none_preserves_cpu_zero():
    """A legal CPU 0 must never be coerced to -1."""
    assert adapter_module._cpu_or_none(0, 2) == 0
    assert adapter_module._cpu_or_none(1, 2) == 1
    assert adapter_module._cpu_or_none(None, 2) is None
    assert adapter_module._cpu_or_none(2, 2) is None
    assert adapter_module._cpu_or_none(-1, 2) is None
    assert adapter_module._cpu_or_none(0, None) == 0


def test_ipc_policy_decodes_fifo_and_prio():
    """IPC policy decoding covers FIFO/PRIO and unknown flag values."""
    assert adapter_module._ipc_policy(0) == "FIFO"
    assert adapter_module._ipc_policy(0x01) == "PRIO"
    assert adapter_module._ipc_policy(None) == "N/A"
    assert adapter_module._ipc_policy(0x40) == "0x40"


def test_timer_expires_in_wraps_safely():
    """Timer remaining time survives 32-bit tick wraparound."""
    assert adapter_module._timer_expires_in(100, 90) == "10"
    assert adapter_module._timer_expires_in(0xFFFFFFF5, 0xFFFFFFF0) == "5"
    assert adapter_module._timer_expires_in(0x00000005, 0xFFFFFFF0) == "21"
    assert adapter_module._timer_expires_in(100, None) is None


def test_system_summary_copies_heap_snapshot_fields(monkeypatch):
    """System summary carries structured heap snapshot fields, not a display line."""
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(
            algorithm="small_mem", used=4096, total=65536
        ),
    )
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(
        adapter_module.diagnostics, "walk_system_heap", lambda _t, _kl: None
    )

    summary = adapter.system_summary()
    assert summary.heap_allocator == "small_mem"
    assert summary.heap_used == 4096
    assert summary.heap_total == 65536
    assert summary.heap_from_walk is False
    assert summary.heap_truncated is False
    assert summary.heap_corrupt is False


def test_system_summary_keeps_missing_heap_counters(monkeypatch):
    """Missing heap symbols do not fall back to calling ``rt_memory_info``."""
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem"),
    )
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(
        adapter_module.diagnostics, "walk_system_heap", lambda _t, _kl: None
    )

    summary = adapter.system_summary()
    assert summary.heap_allocator == "small_mem"
    assert summary.heap_used is None
    assert summary.heap_total is None


def test_system_summary_keeps_partial_heap_counters(monkeypatch):
    """A one-sided snapshot still exposes the known counter as an int."""
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(
        adapter_module.diagnostics, "walk_system_heap", lambda _t, _kl: None
    )
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem", total=65536),
    )
    summary = adapter.system_summary()
    assert summary.heap_allocator == "small_mem"
    assert summary.heap_used is None
    assert summary.heap_total == 65536
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem", used=0),
    )
    summary = adapter.system_summary()
    assert summary.heap_used == 0
    assert summary.heap_total is None


def test_system_summary_omits_none_heap_allocator(monkeypatch):
    """``heap_type=none`` does not advertise a fake allocator name."""
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="none")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="none"),
    )
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(
        adapter_module.diagnostics, "walk_system_heap", lambda _t, _kl: None
    )

    summary = adapter.system_summary()
    assert summary.heap_allocator is None
    assert summary.heap_used is None
    assert summary.heap_total is None


def test_system_summary_falls_back_to_a_closed_heap_walk(monkeypatch):
    """A complete small_mem/memheap walk fills missing used/total and is marked."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem"),
    )
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "walk_system_heap",
        lambda _t, _kl: HeapWalk(
            used_blocks=2,
            free_blocks=1,
            hole_sizes=[64],
            occupancy=[],
            used_bytes=160,
            total_bytes=256,
        ),
    )

    summary = adapter.system_summary()
    assert summary.heap_used == 160
    assert summary.heap_total == 256
    assert summary.heap_from_walk is True
    assert summary.heap_truncated is False
    assert summary.heap_corrupt is False


def test_system_summary_does_not_use_truncated_or_slab_estimates(monkeypatch):
    """Truncated walks and page-level slab counts must not invent used/total."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem"),
    )
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "walk_system_heap",
        lambda _t, _kl: HeapWalk(
            used_blocks=2,
            free_blocks=1,
            hole_sizes=[64],
            occupancy=[],
            truncated=True,
            used_bytes=160,
            total_bytes=256,
        ),
    )

    summary = adapter.system_summary()
    assert summary.heap_used is None
    assert summary.heap_total is None
    assert summary.heap_from_walk is False
    assert summary.heap_truncated is True


def test_system_summary_reports_a_corrupt_heap_walk(monkeypatch):
    """Corrupt walks surface on the system summary without filling counters."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(
            algorithm="small_mem", used=4096, total=65536
        ),
    )
    monkeypatch.setattr(adapter, "_task_views", lambda: [])
    monkeypatch.setattr(adapter_module, "get_tick", lambda: None)
    monkeypatch.setattr(adapter, "object_counts", lambda: {})
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "walk_system_heap",
        lambda _t, _kl: HeapWalk(
            used_blocks=1,
            free_blocks=0,
            hole_sizes=[],
            occupancy=[],
            corrupt=True,
        ),
    )

    summary = adapter.system_summary()
    assert summary.heap_used == 4096
    assert summary.heap_total == 65536
    assert summary.heap_from_walk is False
    assert summary.heap_corrupt is True


def test_holes_line_formats_free_blocks():
    """The Holes line follows ``N free, largest, three smallest`` exactly."""
    assert adapter_module._holes_line([2048, 4, 16, 16]) == (
        "4 free, largest: 2048, smallest: 4, 16, 16"
    )
    assert adapter_module._holes_line([64]) == "1 free, largest: 64, smallest: 64"
    assert adapter_module._holes_line([]) == "0 free"


def test_heap_basic_pairs_report_na_missing_values(monkeypatch):
    """Basics render missing counters as N/A and MEMTRACE status explicitly."""
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="none")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="none"),
    )
    monkeypatch.setattr(
        adapter_module.RtThreadAdapter, "_memtrace_enabled", lambda _s: False
    )

    pairs = dict(adapter.heap_basic_pairs())

    assert pairs == {
        "Algorithm": "none",
        "TotalSize": "N/A",
        "UsedSize": "N/A",
        "MaxUsed": "N/A",
        "MemTrace": "unavailable",
    }


def test_heap_basic_pairs_report_enabled_memtrace(monkeypatch):
    """MEMTRACE renders ``enabled`` when a block header carries owner names."""
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem", total=100),
    )
    monkeypatch.setattr(
        adapter_module.diagnostics, "walk_system_heap", lambda _t, _kl: None
    )
    monkeypatch.setattr(
        adapter_module.RtThreadAdapter, "_memtrace_enabled", lambda _s: True
    )

    pairs = dict(adapter.heap_basic_pairs())

    assert pairs["MemTrace"] == "enabled"
    assert pairs["TotalSize"] == "100"
    assert pairs["UsedSize"] == "N/A"
    assert "Source" not in pairs


def test_heap_basic_pairs_fall_back_to_a_closed_walk(monkeypatch):
    """Missing snapshot counters take exact walk bytes and mark Source=walk."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem", max_used=200),
    )
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "walk_system_heap",
        lambda _t, _kl: HeapWalk(
            used_blocks=2,
            free_blocks=1,
            hole_sizes=[64],
            occupancy=[],
            used_bytes=160,
            total_bytes=256,
        ),
    )
    monkeypatch.setattr(
        adapter_module.RtThreadAdapter, "_memtrace_enabled", lambda _s: False
    )

    pairs = dict(adapter.heap_basic_pairs())

    assert pairs["TotalSize"] == "256"
    assert pairs["UsedSize"] == "160"
    assert pairs["MaxUsed"] == "200"
    assert pairs["Source"] == "walk"


def test_heap_basic_pairs_do_not_use_truncated_walk_sizes(monkeypatch):
    """Truncated walks must not fill ``rtt heap`` TotalSize/UsedSize."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem"),
    )
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "walk_system_heap",
        lambda _t, _kl: HeapWalk(
            used_blocks=2,
            free_blocks=1,
            hole_sizes=[64],
            occupancy=[],
            truncated=True,
            used_bytes=160,
            total_bytes=256,
        ),
    )
    monkeypatch.setattr(
        adapter_module.RtThreadAdapter, "_memtrace_enabled", lambda _s: False
    )

    pairs = dict(adapter.heap_basic_pairs())

    assert pairs["TotalSize"] == "N/A"
    assert pairs["UsedSize"] == "N/A"
    assert "Source" not in pairs


def test_heap_report_walks_the_heap_once(monkeypatch):
    """``rtt heap`` shares one snapshot+walk with the size fallback."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="small_mem")
    walks = {"count": 0}

    def walk(_heap_type, _kl):
        walks["count"] += 1
        return HeapWalk(
            used_blocks=2,
            free_blocks=1,
            hole_sizes=[64],
            occupancy=[("main", 2, 160)],
            used_bytes=160,
            total_bytes=256,
        )

    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem"),
    )
    monkeypatch.setattr(adapter_module.diagnostics, "walk_system_heap", walk)
    monkeypatch.setattr(
        adapter_module.RtThreadAdapter, "_memtrace_enabled", lambda _s: False
    )

    pairs, detail = adapter.heap_report()
    by_label = dict(pairs)

    assert walks["count"] == 1
    assert by_label["TotalSize"] == "256"
    assert by_label["UsedSize"] == "160"
    assert by_label["Source"] == "walk"
    assert detail is not None
    assert detail.pairs[0] == ("Blocks", "2 used, 1 free, 3 total")
    assert detail.occupancy == [["main", "2", "160"]]


def test_heap_detail_formats_blocks_holes_and_occupancy(monkeypatch):
    """The walk assembly keeps Blocks/Holes pairs plus sorted occupancy rows."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    layout = KernelLayout()
    adapter = adapter_module.RtThreadAdapter(layout, heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem"),
    )
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "walk_system_heap",
        lambda _heap_type, _kl: HeapWalk(
            used_blocks=12,
            free_blocks=5,
            hole_sizes=[2048, 4, 16, 16, 8],
            occupancy=[("main", 8, 4096), ("worker1", 4, 1024)],
        ),
    )

    result = adapter.heap_detail()

    assert result is not None
    assert result.pairs == [
        ("Blocks", "12 used, 5 free, 17 total"),
        ("Holes", "5 free, largest: 2048, smallest: 4, 8, 16"),
    ]
    assert result.occupancy == [
        ["main", "8", "4096"],
        ["worker1", "4", "1024"],
    ]


def test_heap_detail_is_none_when_walk_unresolvable(monkeypatch):
    """An unresolvable block chain keeps the snapshot but no walk result."""
    from rtthread.navigation import HeapSnapshot

    layout = KernelLayout()
    adapter = adapter_module.RtThreadAdapter(layout, heap_type="small_mem")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="small_mem"),
    )
    monkeypatch.setattr(
        adapter_module.diagnostics, "walk_system_heap", lambda _t, _kl: None
    )

    assert adapter.heap_detail() is None


def test_heap_detail_marks_corrupt_walks(monkeypatch):
    """Corrupt walks report the corrupt suffix on the Blocks line."""
    from rtthread.diagnostics import HeapWalk
    from rtthread.navigation import HeapSnapshot

    adapter = adapter_module.RtThreadAdapter(KernelLayout(), heap_type="memheap")
    monkeypatch.setattr(
        adapter_module,
        "get_heap_snapshot",
        lambda _type, _layout: HeapSnapshot(algorithm="memheap"),
    )
    monkeypatch.setattr(
        adapter_module.diagnostics,
        "walk_system_heap",
        lambda _t, _kl: HeapWalk(
            used_blocks=1, free_blocks=0, hole_sizes=[], occupancy=[], corrupt=True
        ),
    )

    result = adapter.heap_detail()

    assert result is not None
    assert result.pairs[0][1] == "1 used, 0 free, 1 total (corrupt)"
