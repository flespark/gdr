"""Unit tests for layout-driven FreeRTOS scheduler navigation."""

from __future__ import annotations

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
        lambda pointer: task if pointer is owner_pointer else None,
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


def test_iter_list_warns_when_truncated(monkeypatch):
    layout, head, task, _calls = _list_walk(monkeypatch, [_Pointer(1), _Pointer(2)])
    warnings: list[str] = []
    monkeypatch.setattr(navigation, "warn", warnings.append)

    assert list(navigation._iter_list(head, layout, max_count=1)) == [task]
    assert warnings == ["FreeRTOS list traversal truncated after 1 nodes"]


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
