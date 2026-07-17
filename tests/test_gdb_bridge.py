"""Unit tests for GDB bridge helpers that do not require a target."""

from __future__ import annotations

import gdr.gdb_bridge as bridge


class _FakeGdb:
    class error(Exception):
        pass

    def __init__(self, output: str):
        self._output = output

    def execute(self, command: str, *, to_string: bool):
        assert command == "info macro ARCH_CPU_STACK_GROWS_UPWARD"
        assert to_string is True
        return self._output


def test_macro_defined_recognizes_gdb_macro_output(monkeypatch):
    """A GDB macro definition marks an architecture-specific config as enabled."""
    monkeypatch.setattr(
        bridge,
        "gdb",
        _FakeGdb("Defined at rtconfig.h:47:\n#define ARCH_CPU_STACK_GROWS_UPWARD\n"),
    )

    assert bridge.macro_defined("ARCH_CPU_STACK_GROWS_UPWARD")


def test_macro_defined_returns_false_when_gdb_has_no_definition(monkeypatch):
    """Unavailable macro debug information does not imply an upward stack."""
    monkeypatch.setattr(
        bridge,
        "gdb",
        _FakeGdb("The symbol has no definition as a C/C++ preprocessor macro.\n"),
    )

    assert not bridge.macro_defined("ARCH_CPU_STACK_GROWS_UPWARD")
