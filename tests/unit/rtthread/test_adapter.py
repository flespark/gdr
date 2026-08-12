"""Unit tests for RT-Thread adapter summaries and tables."""

from __future__ import annotations

import pytest

import rtthread.adapter as adapter_module
from gdr.abstractions import Event, Mailbox, MemoryPool, MessageQueue, Timer
from gdr.adapter_api import TaskSummary
from gdr.layout import KernelLayout, StructLayout


def test_task_summaries_read_current_task_once(monkeypatch):
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
        return TaskSummary(name=f"task-{len(converted)}")

    monkeypatch.setattr(adapter_module, "get_current_thread", current_task)
    monkeypatch.setattr(adapter_module, "_get_addr", lambda _value: 0x1234)
    monkeypatch.setattr(adapter_module, "iter_threads", lambda _layout: iter(values))
    monkeypatch.setattr(adapter, "_summarize_task", summarize)

    summaries = list(adapter.iter_task_summaries())

    assert current_reads == 1
    assert [summary.name for summary in summaries] == ["task-1", "task-2", "task-3"]
    assert converted == [(value, 0x1234) for value in values]


def test_smp_task_summary_uses_real_oncpu(monkeypatch):
    """The current task reports its actual CPU, not a hardcoded core 0."""
    from gdr.abstractions import Thread

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

    summary = adapter._summarize_task(current_value, 0x2000)

    assert summary.current_core == 1
    assert summary.oncpu == 1
    assert summary.bind_cpu == 0


def test_up_task_summary_keeps_core_zero_marker(monkeypatch):
    """UP targets keep the current marker on core 0 without SMP fields."""
    from gdr.abstractions import Thread

    current_thread = Thread(name="main", address=0x3000, oncpu=None, bind_cpu=None)
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(
        adapter_module, "value_to_thread", lambda _value, _layout: current_thread
    )

    summary = adapter._summarize_task(object(), 0x3000)

    assert summary.current_core == 0
    assert summary.oncpu is None
    assert summary.bind_cpu is None


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
        adapter_module.rt_detail, "thread_detail", lambda _thread: detail_pairs
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
                lambda _converted: expected_pairs,
            )
        },
    )

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


def test_event_mode_decodes_and_or_clear_bits():
    """Event wait-mode decoding covers AND/OR/CLEAR and unknown values."""
    assert adapter_module._event_mode(0x1) == "AND"
    assert adapter_module._event_mode(0x2) == "OR"
    assert adapter_module._event_mode(0x4) == "CLEAR"
    assert adapter_module._event_mode(0x7) == "AND|OR|CLEAR"
    assert adapter_module._event_mode(0x8) == "0x8"


def test_waiter_summary_leads_with_count(monkeypatch):
    """The ``count:names`` summary keeps the count first for truncation."""
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

    assert summary == "2:worker1,worker2"


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
        ["input", "0", "0", "0", "0", "0", "N/A", "1:recv1", "2:send1,send2", "0x2000"]
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
        ["work", "0", "0", "0", "0", "N/A", "1:recv1", "N/A", "0x3000"]
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
