"""Unit tests for generic adapter and printer registration."""

from __future__ import annotations

import pytest

import gdr.functions as functions
import gdr.printers as printers
import gdr.registry as registry
import rtthread.adapter as adapter
import rtthread.commands as commands
from gdr.adapter_api import RtosAdapter
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

    def summarize_task(self, value):  # noqa: ARG002
        raise AssertionError("not called")

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


def test_registry_preserves_the_first_active_adapter(monkeypatch):
    first = _Adapter()
    second = _Adapter()
    monkeypatch.setattr(registry, "_active", None)
    registry.register(first)
    registry.register(second)
    assert registry.active() is first


def test_rtthread_adapter_owns_its_layout():
    layout = KernelLayout()
    selected = adapter.RtThreadAdapter(layout)

    assert selected.layout is layout
    assert isinstance(selected, adapter.RtThreadAdapter)


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


def test_register_rtthread_commands_resumes_after_alias_failure(monkeypatch):
    calls: list[str] = []
    alias_attempts = 0

    def execute(command: str):
        nonlocal alias_attempts
        alias_attempts += 1
        calls.append(command)
        if alias_attempts == 1:
            raise RuntimeError("alias registration interrupted")

    fake_gdb = type("FakeGdb", (), {"execute": staticmethod(execute)})()
    monkeypatch.setattr(commands, "gdb", fake_gdb)
    monkeypatch.setattr(
        commands,
        "RtThreadCommand",
        lambda: calls.append("command"),
        raising=False,
    )
    monkeypatch.setattr(commands, "info", lambda _message: None)
    monkeypatch.setattr(commands, "_command_registered", False)
    monkeypatch.setattr(commands, "_alias_registered", False)

    with pytest.raises(RuntimeError, match="alias registration interrupted"):
        commands.register_commands()
    commands.register_commands()
    commands.register_commands()

    assert calls == [
        "command",
        "alias rtt = rtthread",
        "alias rtt = rtthread",
    ]
