"""Unit tests for RT-Thread navigation fallbacks."""

from __future__ import annotations

import rtthread.navigation as navigation
from gdr.layout import StructLayout


class _FakeType:
    """Minimal GDB type stand-in for CPU table shape tests."""

    def __init__(self, code: int):
        self.code = code

    def strip_typedefs(self):
        """Return the unaliased type used by navigation."""
        return self


class _FakeValue:
    """Minimal GDB value stand-in with array-style indexing."""

    def __init__(self, type_code: int, entries: list[object], address: int = 1):
        self.type = _FakeType(type_code)
        self._entries = entries
        self._address = address

    def __getitem__(self, index: int) -> object:
        return self._entries[index]

    def __int__(self) -> int:
        return self._address


class _FakeGdb:
    """GDB constants and exceptions used by ``_cpu_from_table``."""

    TYPE_CODE_ARRAY = 1
    TYPE_CODE_PTR = 2

    class error(Exception):
        pass

    class MemoryError(Exception):
        pass


def test_cpu_table_fallback_accepts_array_and_pointer_shapes(monkeypatch):
    """RT-Thread branches may expose either CPU table representation."""
    monkeypatch.setattr(navigation, "gdb", _FakeGdb)

    array_entries = [object(), object()]
    pointer_entries = [object(), object()]
    array_table = _FakeValue(_FakeGdb.TYPE_CODE_ARRAY, array_entries)
    pointer_table = _FakeValue(_FakeGdb.TYPE_CODE_PTR, pointer_entries)

    assert navigation._cpu_from_table(array_table, 1) is array_entries[1]
    assert navigation._cpu_from_table(pointer_table, 1) is pointer_entries[1]
    assert navigation._cpu_from_table(None, 0) is None


def test_find_thread_uses_the_active_layout_object_code(monkeypatch):
    """3.1.0's thread code is resolved from the selected version profile."""
    calls: list[tuple[int, str]] = []
    layout = navigation.KernelLayout(object_codes={"thread": 0})

    def find_object(type_code, name, _layout):
        calls.append((type_code, name))
        return "thread-value"

    monkeypatch.setattr(navigation, "find_object", find_object)

    assert navigation.find_thread("worker1", layout) == "thread-value"
    assert calls == [(0, "worker1")]


def test_suspend_thread_names_recovers_thread_names_via_tlist(monkeypatch):
    """Waiters are recovered through ``struct rt_thread.tlist`` nodes."""
    layout = navigation.KernelLayout(
        structs={
            "struct rt_semaphore": StructLayout("struct rt_semaphore"),
            "struct rt_thread": StructLayout("struct rt_thread"),
        }
    )
    head = object()
    waiter_values = [object(), object()]
    read_calls: list[str] = []

    def fake_read_field(value, sl, field_name):
        read_calls.append(field_name)
        if sl is layout.structs["struct rt_semaphore"]:
            return head
        return value

    monkeypatch.setattr(navigation, "read_field", fake_read_field)
    monkeypatch.setattr(
        navigation,
        "iter_suspend_threads",
        lambda _head, _layout: iter(waiter_values),
    )
    monkeypatch.setattr(
        navigation,
        "read_cstring",
        lambda value: f"name-{id(value)}",
    )

    names = navigation.suspend_thread_names(
        object(), layout, "struct rt_semaphore", "suspend_thread"
    )

    assert names == [f"name-{id(value)}" for value in waiter_values]
    assert read_calls == ["suspend_thread", "name", "name"]


def test_suspend_thread_names_renders_invalid_for_unreadable_names(monkeypatch):
    """An unreadable waiter name renders as ``<invalid>``, never crashes."""
    layout = navigation.KernelLayout(
        structs={
            "struct rt_semaphore": StructLayout("struct rt_semaphore"),
            "struct rt_thread": StructLayout("struct rt_thread"),
        }
    )
    monkeypatch.setattr(navigation, "read_field", lambda _v, _sl, _f: object())
    monkeypatch.setattr(
        navigation, "iter_suspend_threads", lambda _head, _layout: iter([object()])
    )
    monkeypatch.setattr(navigation, "read_cstring", lambda _value: None)

    names = navigation.suspend_thread_names(
        object(), layout, "struct rt_semaphore", "suspend_thread"
    )

    assert names == ["<invalid>"]


def test_suspend_thread_names_returns_empty_when_layout_missing():
    """A missing struct or head field degrades to an empty waiter list."""
    layout = navigation.KernelLayout()
    assert (
        navigation.suspend_thread_names(
            object(), layout, "struct rt_semaphore", "suspend_thread"
        )
        == []
    )


def test_iter_suspend_threads_uses_tlist_container_hook(monkeypatch):
    """The suspend hook recovers threads from their embedded ``tlist`` node."""
    layout = navigation.KernelLayout(
        structs={"struct rt_thread": StructLayout("struct rt_thread")}
    )
    head = object()
    captured: list[tuple[object, object]] = []

    def fake_iter_list(head_value, hook, max_count):
        captured.append((head_value, hook))
        assert hook.node_path == ("tlist",)
        assert hook.container_type == "struct rt_thread"
        assert hook.next_path == ("next",)
        assert max_count == navigation.MAX_SUSPEND_THREADS
        return iter([object()])

    monkeypatch.setattr(navigation, "iter_list", fake_iter_list)

    result = list(navigation.iter_suspend_threads(head, layout))

    assert len(result) == 1
    assert captured == [(head, object())] or len(captured) == 1
