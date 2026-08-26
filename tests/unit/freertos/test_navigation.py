"""Unit tests for layout-driven FreeRTOS scheduler navigation."""

from __future__ import annotations

import pytest

import freertos.navigation as navigation
from freertos.layout import FreeRtosConfig, build_layout


class _Pointer:
    def __init__(self, address: int):
        self.address = address

    def __int__(self) -> int:
        return self.address


def _list_walk(monkeypatch, next_nodes: list[_Pointer], *, dereference=True):
    layout = build_layout(FreeRtosConfig())
    head = object()
    end = object()
    item = object()
    owner_pointer = object()
    task = object()
    calls: list[tuple[object, str]] = []
    nodes = iter(next_nodes)

    def read_field(value, struct_layout, field_name):
        calls.append((struct_layout, field_name))
        if value is head and field_name == "end":
            return end
        if value is end and field_name == "next":
            return next(nodes)
        if value is item and field_name == "owner":
            return owner_pointer
        if value is item and field_name == "next":
            return next(nodes)
        raise AssertionError(f"unexpected logical field read: {field_name}")

    monkeypatch.setattr(navigation, "read_field", read_field)
    monkeypatch.setattr(
        navigation, "value_address", lambda value: 0xFF if value is end else 0
    )
    monkeypatch.setattr(
        navigation,
        "safe_dereference",
        lambda _pointer: item if dereference else None,
    )
    monkeypatch.setattr(
        navigation,
        "_owner_task",
        lambda pointer, _layout: task if pointer is owner_pointer else None,
    )
    return layout, head, task, calls


def test_iter_list_reads_only_logical_layout_fields(monkeypatch):
    layout, head, task, calls = _list_walk(monkeypatch, [_Pointer(1), _Pointer(0xFF)])

    assert list(navigation._iter_list(head, layout)) == [task]
    assert calls == [
        (layout.structs["struct xLIST"], "end"),
        (layout.structs["struct xMINI_LIST_ITEM"], "next"),
        (layout.structs["struct xLIST_ITEM"], "owner"),
        (layout.structs["struct xLIST_ITEM"], "next"),
    ]


def test_iter_list_stops_on_repeated_node(monkeypatch):
    layout, head, task, _calls = _list_walk(monkeypatch, [_Pointer(1), _Pointer(1)])
    warnings: list[str] = []
    monkeypatch.setattr(navigation, "warn", warnings.append)

    assert list(navigation._iter_list(head, layout)) == [task]
    assert warnings == ["FreeRTOS list traversal stopped at repeated node 0x1"]


def test_iter_list_stops_on_invalid_pointer(monkeypatch):
    layout, head, _task, _calls = _list_walk(
        monkeypatch, [_Pointer(1)], dereference=False
    )
    warnings: list[str] = []
    monkeypatch.setattr(navigation, "warn", warnings.append)

    assert list(navigation._iter_list(head, layout)) == []
    assert warnings == ["FreeRTOS list traversal stopped at invalid node 0x1"]


def test_iter_list_stops_on_out_of_range_node(monkeypatch):
    """A next pointer outside every mapped section ends the walk."""
    layout, head, _task, _calls = _list_walk(monkeypatch, [_Pointer(1)])
    warnings: list[str] = []
    monkeypatch.setattr(navigation, "warn", warnings.append)
    monkeypatch.setattr(
        navigation, "_mapped_ranges", lambda: ((0x20000000, 0x20010000),)
    )

    assert list(navigation._iter_list(head, layout)) == []
    assert warnings == ["FreeRTOS list traversal stopped at out-of-range node 0x1"]


def test_iter_list_warns_when_truncated(monkeypatch):
    layout, head, task, _calls = _list_walk(monkeypatch, [_Pointer(1), _Pointer(2)])
    warnings: list[str] = []
    monkeypatch.setattr(navigation, "warn", warnings.append)

    assert list(navigation._iter_list(head, layout, max_count=1)) == [task]
    assert warnings == ["FreeRTOS list traversal truncated after 1 nodes"]


def test_iter_list_propagates_unexpected_errors(monkeypatch):
    """Only expected traversal failures are contained; others bubble to a guard."""
    layout = build_layout(FreeRtosConfig())
    head = object()

    def boom(_value):
        raise RuntimeError("unexpected traversal failure")

    monkeypatch.setattr(navigation, "value_address", boom)

    with pytest.raises(RuntimeError, match="unexpected traversal failure"):
        list(navigation._iter_list(head, layout))

    warnings: list[str] = []
    monkeypatch.setattr(navigation, "warn", warnings.append)
    assert warnings == []


def test_list_count_reads_the_layout_count_field(monkeypatch):
    layout = build_layout(FreeRtosConfig())
    head = object()
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(navigation, "_head", lambda _name, _layout: head)
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda _value, struct_layout, field_name: (
            calls.append((struct_layout, field_name)) or 3
        ),
    )
    monkeypatch.setattr(navigation, "read_int", int)

    assert navigation.list_count("suspended", layout) == 3
    assert calls == [(layout.structs["struct xLIST"], "count")]


def test_list_count_sums_every_ready_priority_list(monkeypatch):
    """pxReadyTasksLists is an array: the count must cover all priorities.

    GDB resolves a struct-member access on an array value to element 0, so
    reading the ready table like a plain ``List_t`` reports only the
    priority-0 count and silently under-reports every higher-priority ready
    task in ``frt system``.
    """
    layout = build_layout(FreeRtosConfig(max_priorities=4))
    counts = {0: 1, 1: 0, 2: 2, 3: 1}
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: counts)
    monkeypatch.setattr(navigation, "_array_item", lambda table, index: table[index])
    monkeypatch.setattr(
        navigation, "read_field", lambda value, _struct_layout, _field: value
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    assert navigation.list_count("ready", layout) == 4


def test_list_count_ready_is_unknown_without_a_priority_count(monkeypatch):
    """An unknown priority count degrades to None, never a partial sum."""
    layout = build_layout(FreeRtosConfig(max_priorities=None))
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: {0: 3})
    monkeypatch.setattr(navigation, "warn", lambda _message: None)

    assert navigation.list_count("ready", layout) is None
