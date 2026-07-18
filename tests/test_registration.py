"""Unit tests for idempotent RT-Thread registration."""

from __future__ import annotations

import gdr.printers as printers
import rtthread.adapter as adapter
import rtthread.commands as commands
from gdr.layout import KernelLayout


class _FakeGdb:
    """Minimal GDB stand-in with a global printer registry."""

    def __init__(self):
        self.pretty_printers: list[object] = []


def test_register_printers_is_idempotent(monkeypatch):
    """Repeated setup leaves one GDR lookup in GDB's global registry."""
    fake_gdb = _FakeGdb()
    monkeypatch.setattr(printers, "gdb", fake_gdb)

    printers.register_printers(KernelLayout())
    printers.register_printers(KernelLayout())

    assert len(fake_gdb.pretty_printers) == 1


def test_unregister_printers_preserves_non_gdr_lookups(monkeypatch):
    """Development reload removes only lookup functions created by GDR."""
    fake_gdb = _FakeGdb()
    external_lookup = object()
    fake_gdb.pretty_printers.append(external_lookup)
    monkeypatch.setattr(printers, "gdb", fake_gdb)
    printers.register_printers(KernelLayout())

    printers.unregister_printers()

    assert fake_gdb.pretty_printers == [external_lookup]


def test_register_adapter_preserves_the_first_layout(monkeypatch):
    """Repeated adapter registration must not recreate convenience functions."""
    registrations: list[str] = []
    monkeypatch.setattr(adapter, "_kl", None)
    monkeypatch.setattr(adapter, "gdb", object())
    for name in ("GdrThreadFunction", "GdrThreadsFunction", "GdrObjectFunction"):
        monkeypatch.setattr(
            adapter,
            name,
            lambda name=name: registrations.append(name),
            raising=False,
        )
    first = KernelLayout()
    second = KernelLayout()

    adapter.register_adapter(first)
    adapter.register_adapter(second)

    assert adapter._kl is first
    assert registrations == [
        "GdrThreadFunction",
        "GdrThreadsFunction",
        "GdrObjectFunction",
    ]


def test_register_commands_preserves_the_first_layout(monkeypatch):
    """Repeated command registration keeps the original command layout."""
    shell_calls: list[None] = []
    messages: list[str] = []
    monkeypatch.setattr(commands, "_kl", None)
    monkeypatch.setattr(
        commands, "register_command_shell", lambda: shell_calls.append(None)
    )
    monkeypatch.setattr(commands, "info", messages.append)
    first = KernelLayout()
    second = KernelLayout()

    commands.register_commands(first)
    commands.register_commands(second)

    assert commands.is_initialized()
    assert commands._kl is first
    assert shell_calls == [None]
    assert messages == ["rtthread commands registered (alias: rtt)"]
