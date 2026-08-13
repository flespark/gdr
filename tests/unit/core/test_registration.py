"""Unit tests for generic adapter and printer registration."""

from __future__ import annotations

import pytest

import gdr.adapter_api as adapter_api
import gdr.functions as functions
import gdr.printers as printers
from gdr.adapter_api import ObjectTable, RtosAdapter
from gdr.layout import KernelLayout


class _FakeGdb:
    """Minimal GDB stand-in with a global printer registry."""

    def __init__(self):
        self.pretty_printers: list[object] = []


class _Adapter(RtosAdapter):
    def find_task(self, name):  # noqa: ARG002
        return None

    def find_object(self, kind, name):  # noqa: ARG002
        return None

    def object_counts(self):
        return {}

    def object_table(self, kind):  # noqa: ARG002
        return None

    def iter_tasks(self):
        return iter(())

    def task_table(self):
        return ObjectTable()

    def system_summary(self):
        raise AssertionError("not called")


def test_register_printers_is_idempotent(monkeypatch):
    fake_gdb = _FakeGdb()
    monkeypatch.setattr(printers, "gdb", fake_gdb)
    printers.register_printers(KernelLayout())
    printers.register_printers(KernelLayout())
    assert len(fake_gdb.pretty_printers) == 1


def test_unregister_printers_preserves_non_gdr_lookups(monkeypatch):
    fake_gdb = _FakeGdb()
    external_lookup = object()
    fake_gdb.pretty_printers.append(external_lookup)
    monkeypatch.setattr(printers, "gdb", fake_gdb)
    printers.register_printers(KernelLayout())
    printers.unregister_printers()
    assert fake_gdb.pretty_printers == [external_lookup]


def test_adapter_api_rejects_replacing_the_active_adapter(monkeypatch):
    first = _Adapter()
    second = _Adapter()
    monkeypatch.setattr(adapter_api, "_active", None)
    adapter_api.register(first)
    adapter_api.register(first)
    with pytest.raises(RuntimeError, match="already initialized"):
        adapter_api.register(second)
    assert adapter_api.active() is first


def test_register_functions_resumes_after_a_partial_failure(monkeypatch):
    calls: list[str] = []
    task_array_attempts = 0

    def register_task_array():
        nonlocal task_array_attempts
        task_array_attempts += 1
        calls.append("gdr_tasks")
        if task_array_attempts == 1:
            raise RuntimeError("registration interrupted")

    monkeypatch.setattr(functions, "gdb", object())
    monkeypatch.setattr(functions, "_registered_functions", set())
    monkeypatch.setattr(
        functions, "GdrTaskFunction", lambda: calls.append("gdr_task"), raising=False
    )
    monkeypatch.setattr(
        functions, "GdrTasksFunction", register_task_array, raising=False
    )
    monkeypatch.setattr(
        functions,
        "GdrObjectFunction",
        lambda: calls.append("gdr_object"),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="registration interrupted"):
        functions.register_functions()

    functions.register_functions()
    functions.register_functions()

    assert calls == ["gdr_task", "gdr_tasks", "gdr_tasks", "gdr_object"]
    assert functions._registered_functions == {
        "gdr_task",
        "gdr_tasks",
        "gdr_object",
    }
