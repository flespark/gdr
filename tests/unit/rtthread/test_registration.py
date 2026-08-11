"""Unit tests for RT-Thread adapter and command registration."""

from __future__ import annotations

import pytest

import rtthread.adapter as adapter_module
import rtthread.commands as commands
from gdr.layout import KernelLayout


def test_adapter_owns_its_layout():
    layout = KernelLayout()
    selected = adapter_module.RtThreadAdapter(layout)

    assert selected.layout is layout
    assert isinstance(selected, adapter_module.RtThreadAdapter)


def test_register_commands_resumes_after_alias_failure(monkeypatch):
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
