"""Unit tests for generic adapter and printer registration."""

from __future__ import annotations

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


def test_rtthread_adapter_registers_the_generic_contract(monkeypatch):
    registrations: list[object] = []
    monkeypatch.setattr(adapter, "_kl", None)
    monkeypatch.setattr(adapter, "gdb", object())
    monkeypatch.setattr(adapter, "register", registrations.append)
    layout = KernelLayout()
    adapter.register_adapter(layout)
    assert adapter._kl is layout
    assert len(registrations) == 1
    assert isinstance(registrations[0], adapter.RtThreadAdapter)


def test_register_rtthread_commands_is_idempotent(monkeypatch):
    calls: list[str] = []
    fake_gdb = type(
        "FakeGdb", (), {"execute": staticmethod(lambda command: calls.append(command))}
    )()
    monkeypatch.setattr(commands, "gdb", fake_gdb)
    monkeypatch.setattr(
        commands,
        "RtThreadCommand",
        lambda: calls.append("command"),
        raising=False,
    )
    monkeypatch.setattr(commands, "info", lambda _message: None)
    monkeypatch.setattr(commands, "_registered", False)

    commands.register_commands()
    commands.register_commands()

    assert calls == ["command", "alias rtt = rtthread"]
