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


class _TableGdb:
    """Minimal GDB stand-in that records complete writes."""

    def __init__(self):
        self.writes: list[str] = []

    def write(self, text: str):
        self.writes.append(text)


class _FakeArchGdb:
    """GDB stand-in with mutable target architecture metadata."""

    class error(Exception):
        pass

    class MemoryError(Exception):
        pass

    def __init__(
        self,
        ptrsize: int,
        endian_output: str,
        *,
        architecture_available: bool = True,
        memory: bytes = b"",
    ):
        self.ptrsize = ptrsize
        self.endian_output = endian_output
        self.architecture_available = architecture_available
        self.memory = memory
        self.memory_reads: list[tuple[int, int]] = []

    def selected_inferior(self):
        return self

    def architecture(self):
        if not self.architecture_available:
            raise AttributeError("architecture unavailable")
        return self

    def void_type(self):
        return self

    def pointer(self):
        return self

    @property
    def sizeof(self) -> int:
        return self.ptrsize

    def lookup_type(self, name: str):
        assert name == "void"
        return self

    def read_memory(self, addr: int, size: int) -> bytes:
        self.memory_reads.append((addr, size))
        return self.memory[:size]

    def execute(self, command: str, *, to_string: bool) -> str:
        assert command == "show endian"
        assert to_string is True
        return self.endian_output


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


def test_print_table_writes_complete_table_once(monkeypatch):
    """A table is formatted before one GDB write to avoid row interleaving."""
    fake_gdb = _TableGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    bridge.print_table([["worker", "20"], ["idle", "3"]], ["Name", "Prio"])

    assert fake_gdb.writes == [
        "Name    Prio\n------  ----\nworker  20  \nidle    3   \n"
    ]


def test_print_table_writes_empty_table_once(monkeypatch):
    """The empty-table path uses the same single-write isolation."""
    fake_gdb = _TableGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    bridge.print_table([], ["Name"])

    assert fake_gdb.writes == ["(empty)\n"]


class _WidthGdb:
    """GDB stand-in exposing a mutable ``width`` parameter."""

    class error(Exception):
        pass

    def __init__(self, width):
        self.width = width

    def parameter(self, name: str):
        assert name == "width"
        return self.width


class _TerminalSize:
    def __init__(self, columns: int):
        self.columns = columns


def test_terminal_width_uses_explicit_gdb_width(monkeypatch):
    """``set width N`` beats terminal probing (priority 1)."""
    monkeypatch.setattr(bridge, "gdb", _WidthGdb(100))
    monkeypatch.setattr(
        bridge.shutil,
        "get_terminal_size",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("terminal unused")),
    )

    assert bridge.terminal_width() == 100


def test_terminal_width_falls_back_to_terminal_columns_when_gdb_width_unlimited(
    monkeypatch,
):
    """Unlimited GDB width delegates to the detected terminal columns."""
    monkeypatch.setattr(bridge, "gdb", _WidthGdb(0))
    monkeypatch.setattr(
        bridge.shutil, "get_terminal_size", lambda **_kwargs: _TerminalSize(80)
    )

    assert bridge.terminal_width() == 80


def test_terminal_width_falls_back_to_terminal_columns_when_gdb_width_missing(
    monkeypatch,
):
    """An unavailable width parameter is treated like ``unlimited``."""
    monkeypatch.setattr(bridge, "gdb", _WidthGdb(None))
    monkeypatch.setattr(
        bridge.shutil, "get_terminal_size", lambda **_kwargs: _TerminalSize(120)
    )

    assert bridge.terminal_width() == 120


def test_terminal_width_defaults_to_120_when_everything_is_unavailable(monkeypatch):
    """Terminal probing failures never crash list rendering (fallback 120)."""
    monkeypatch.setattr(bridge, "gdb", _WidthGdb(0))

    def no_terminal(*_args, **_kwargs):
        raise OSError("no terminal")

    monkeypatch.setattr(bridge.shutil, "get_terminal_size", no_terminal)

    assert bridge.terminal_width() == 120


def test_print_table_honors_explicit_width_with_elastic_columns(monkeypatch):
    """Elastic columns shrink and truncate when the natural width exceeds."""
    fake_gdb = _TableGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    bridge.print_table(
        [["2:worker,logger", "3"]],
        ["Waiters", "Value"],
        elastic=("Waiters",),
        width=14,
    )

    assert fake_gdb.writes == ["Waiters  Value\n-------  -----\n2:wor..  3    \n"]


def test_print_detail_writes_key_value_pairs_once(monkeypatch):
    """Detail output is one write with a stable ``Key: Value`` layout."""
    fake_gdb = _TableGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    bridge.print_detail([("Name", "worker1"), ("Value", "3")])

    assert fake_gdb.writes == ["Name: worker1\nValue: 3\n"]


def test_gdb_command_guard_warns_for_target_errors(monkeypatch):
    """Expected target failures are downgraded to concise warnings."""
    fake_gdb = _FakeArchGdb(4, "The target is set to little endian.")
    warnings: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(bridge, "gdb", fake_gdb)
    monkeypatch.setattr(bridge, "warn", warnings.append)
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    @bridge.gdb_command_guard
    def read_target(error: Exception):
        raise error

    assert read_target(fake_gdb.error("target unavailable")) is None
    assert read_target(fake_gdb.MemoryError("unmapped memory")) is None
    assert warnings == [
        "read_target: error: target unavailable",
        "read_target: MemoryError: unmapped memory",
    ]
    assert errors == []


def test_gdb_command_guard_reports_unexpected_errors(monkeypatch):
    """Unexpected command failures use the error channel without escaping."""
    errors: list[str] = []
    monkeypatch.setattr(bridge, "gdb", _FakeArchGdb(4, "little endian"))
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    @bridge.gdb_command_guard
    def render_command():
        raise ValueError("invalid task state")

    assert render_command() is None
    assert errors == ["render_command: ValueError: invalid task state"]


def test_gdb_command_guard_preserves_successful_results(monkeypatch):
    """The guard is transparent when a command body succeeds."""
    monkeypatch.setattr(bridge, "gdb", _FakeArchGdb(4, "little endian"))

    @bridge.gdb_command_guard
    def command(value: int) -> int:
        return value + 1

    assert command(41) == 42
    assert command.__name__ == "command"


def test_get_arch_info_reports_a_fresh_target_snapshot(monkeypatch):
    """Architecture changes must not reuse stale pointer or endian metadata."""
    fake_gdb = _FakeArchGdb(
        4,
        "The target endianness is set automatically (currently little endian).",
    )
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    assert bridge.get_arch_info() == bridge.ArchInfo(ptrsize=4, endian="little")

    fake_gdb.ptrsize = 8
    fake_gdb.endian_output = "The target is set to big endian."

    assert bridge.get_arch_info() == bridge.ArchInfo(ptrsize=8, endian="big")


def test_get_arch_info_falls_back_when_inferior_architecture_is_unavailable(
    monkeypatch,
):
    """Older GDB bindings can still supply the pointer width through ``void``."""
    fake_gdb = _FakeArchGdb(
        8,
        "The target is set to big endian.",
        architecture_available=False,
    )
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    assert bridge.get_arch_info() == bridge.ArchInfo(ptrsize=8, endian="big")


def test_get_arch_info_returns_none_for_ambiguous_endian(monkeypatch):
    """Unrecognized GDB output must not silently assume a byte order."""
    fake_gdb = _FakeArchGdb(4, "Target might be little endian or big endian.")
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    assert bridge.get_arch_info() is None


def test_read_bytes_preserves_target_memory_order(monkeypatch):
    """Raw memory is not reversed when the target is big-endian."""
    fake_gdb = _FakeArchGdb(
        8,
        "The target is set to big endian.",
        memory=b"\x12\x34",
    )
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    assert bridge.read_bytes(0x1000, 2) == b"\x12\x34"
    assert fake_gdb.memory_reads == [(0x1000, 2)]
