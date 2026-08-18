"""Unit tests for FreeRTOS command registration."""

from __future__ import annotations

import pytest

import freertos.commands as commands


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
        "FreeRtosCommand",
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
        "alias frt = freertos",
        "alias frt = freertos",
    ]
