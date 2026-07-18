"""Unit tests for RT-Thread navigation fallbacks."""

from __future__ import annotations

import rtthread.navigation as navigation


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
