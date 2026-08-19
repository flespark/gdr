"""Unit tests for GDB bridge helpers that do not require a target."""

from __future__ import annotations

from unittest.mock import patch

import pytest

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


class _Pointer:
    def __init__(self, address: int, value=None, *, broken: bool = False):
        self.address = address
        self.value = value
        self.broken = broken

    def __int__(self) -> int:
        if self.broken:
            raise RuntimeError("unreadable pointer")
        return self.address

    def dereference(self):
        if self.broken:
            raise RuntimeError("invalid memory")
        return self.value


def test_safe_value_helpers_contain_unreadable_gdb_values():
    value = object()
    pointer = _Pointer(0x1000, value)

    assert bridge.safe_int(pointer) == 0x1000
    assert bridge.safe_int(_Pointer(1, broken=True)) is None
    assert bridge.value_address(type("Value", (), {"address": pointer})()) == 0x1000
    assert bridge.value_address(object()) == 0
    assert bridge.safe_dereference(pointer) is value
    assert bridge.safe_dereference(_Pointer(0)) is None
    assert bridge.safe_dereference(_Pointer(1, broken=True)) is None


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

    def unused_terminal():
        raise AssertionError("terminal unused")

    # Reason: patch the local helper, not stdlib shutil. pytest -v probes
    # shutil.get_terminal_size while reporting, and a leaked mock crashes CI.
    monkeypatch.setattr(bridge, "_system_columns", unused_terminal)

    assert bridge.terminal_width() == 100


def test_terminal_width_falls_back_to_terminal_columns_when_gdb_width_unlimited(
    monkeypatch,
):
    """Unlimited GDB width delegates to the detected terminal columns."""
    monkeypatch.setattr(bridge, "gdb", _WidthGdb(0))
    monkeypatch.setattr(bridge, "_system_columns", lambda: 80)

    assert bridge.terminal_width() == 80


def test_terminal_width_falls_back_to_terminal_columns_when_gdb_width_missing(
    monkeypatch,
):
    """An unavailable width parameter is treated like ``unlimited``."""
    monkeypatch.setattr(bridge, "gdb", _WidthGdb(None))
    monkeypatch.setattr(bridge, "_system_columns", lambda: 120)

    assert bridge.terminal_width() == 120


def test_terminal_width_defaults_to_120_when_everything_is_unavailable(monkeypatch):
    """Terminal probing failures never crash list rendering (fallback 120)."""
    monkeypatch.setattr(bridge, "gdb", _WidthGdb(0))
    monkeypatch.setattr(bridge, "_system_columns", lambda: None)

    assert bridge.terminal_width() == 120


def test_system_columns_reads_detected_terminal_width():
    """``_system_columns`` forwards a positive shutil column count."""
    with patch.object(
        bridge.shutil, "get_terminal_size", return_value=_TerminalSize(80)
    ):
        assert bridge._system_columns() == 80


def test_system_columns_returns_none_when_terminal_probe_fails():
    """OSError from shutil is contained so list rendering can fall back."""
    with patch.object(
        bridge.shutil, "get_terminal_size", side_effect=OSError("no terminal")
    ):
        assert bridge._system_columns() is None


def test_print_table_honors_explicit_width_with_elastic_columns(monkeypatch):
    """Elastic columns shrink and truncate when the natural width exceeds."""
    fake_gdb = _TableGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    bridge.print_table(
        [["2@worker,logger", "3"]],
        ["Waiters", "Value"],
        elastic=("Waiters",),
        width=14,
    )

    assert fake_gdb.writes == ["Waiters  Value\n-------  -----\n2@wor..  3    \n"]


def test_print_detail_writes_key_value_pairs_once(monkeypatch):
    """Detail output is one write with a colon-aligned ``Key: Value`` layout."""
    fake_gdb = _TableGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    bridge.print_detail([("Name", "worker1"), ("Value", "3")])

    assert fake_gdb.writes == [" Name: worker1\nValue: 3\n"]


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


def test_gdb_command_guard_debug_uses_show_last_exception(monkeypatch):
    """In debug mode an unexpected error prints the full diagnostic, not a one-liner."""
    monkeypatch.setattr(bridge, "gdb", _FakeArchGdb(4, "little endian"))
    monkeypatch.setattr(bridge, "is_debug", lambda: True)
    shown: list[bool] = []
    monkeypatch.setattr(bridge, "show_last_exception", lambda: shown.append(True))
    monkeypatch.setattr(bridge, "propagate_exception", lambda: False)
    errors: list[str] = []
    monkeypatch.setattr(bridge, "err", errors.append)

    @bridge.gdb_command_guard
    def command():
        raise ValueError("boom")

    assert command() is None
    assert shown == [True]
    assert errors == []


def test_gdb_command_guard_debug_propagates_when_configured(monkeypatch):
    """``GDR_PROPAGATE_EXCEPTION`` re-raises after a debug diagnostic."""
    monkeypatch.setattr(bridge, "gdb", _FakeArchGdb(4, "little endian"))
    monkeypatch.setattr(bridge, "is_debug", lambda: True)
    monkeypatch.setattr(bridge, "show_last_exception", lambda: None)
    monkeypatch.setattr(bridge, "propagate_exception", lambda: True)

    @bridge.gdb_command_guard
    def command():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        command()


def test_is_debug_reflects_the_config_constant(monkeypatch):
    """Verbose diagnostics mirror the ``GDR_DEBUG`` setting in constants."""
    monkeypatch.setattr(bridge, "GDR_DEBUG", True)
    assert bridge.is_debug() is True

    monkeypatch.setattr(bridge, "GDR_DEBUG", False)
    assert bridge.is_debug() is False


def test_propagate_exception_reflects_the_config_constant(monkeypatch):
    """The re-raise toggle mirrors ``GDR_PROPAGATE_EXCEPTION`` in constants."""
    monkeypatch.setattr(bridge, "GDR_PROPAGATE_EXCEPTION", True)
    assert bridge.propagate_exception() is True

    monkeypatch.setattr(bridge, "GDR_PROPAGATE_EXCEPTION", False)
    assert bridge.propagate_exception() is False


class _FunctionGdb:
    """Minimal GDB stand-in exposing the error types ``gdb.Function`` relies on."""

    class error(Exception):
        pass

    class MemoryError(Exception):
        pass

    class GdbError(Exception):
        pass


def test_gdb_function_guard_preserves_success(monkeypatch):
    """The function guard is transparent on a successful lookup."""
    monkeypatch.setattr(bridge, "gdb", _FunctionGdb())

    @bridge.gdb_function_guard
    def function():
        return 42

    assert function() == 42


def test_gdb_function_guard_converts_target_errors(monkeypatch):
    """Target failures become ``gdb.GdbError`` instead of raw Python noise."""
    monkeypatch.setattr(bridge, "gdb", _FunctionGdb())
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    @bridge.gdb_function_guard
    def function():
        raise _FunctionGdb.MemoryError("unmapped memory")

    with pytest.raises(_FunctionGdb.GdbError) as excinfo:
        function()
    assert "function: MemoryError: unmapped memory" in str(excinfo.value)


def test_gdb_function_guard_converts_unexpected_errors(monkeypatch):
    """An unexpected exception surfaces as ``gdb.GdbError`` with a clean message."""
    monkeypatch.setattr(bridge, "gdb", _FunctionGdb())
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    @bridge.gdb_function_guard
    def function():
        raise ValueError("corrupt task list")

    with pytest.raises(_FunctionGdb.GdbError) as excinfo:
        function()
    assert "function: ValueError: corrupt task list" in str(excinfo.value)


def test_gdb_function_guard_passes_through_user_gdberror(monkeypatch):
    """An explicit ``gdb.GdbError`` from the body is re-raised unchanged."""
    fake_gdb = _FunctionGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    @bridge.gdb_function_guard
    def function():
        raise fake_gdb.GdbError("explicit user-facing message")

    with pytest.raises(fake_gdb.GdbError) as excinfo:
        function()
    assert "explicit user-facing message" in str(excinfo.value)


def test_show_last_exception_renders_a_full_diagnostic(monkeypatch):
    """The debug diagnostic shows banner, stacktrace, and runtime environment."""
    fake_gdb = _TableGdb()
    monkeypatch.setattr(bridge, "gdb", fake_gdb)

    def explode():
        raise ValueError("bad value")

    try:
        explode()
    except ValueError:
        bridge.show_last_exception()

    output = "".join(fake_gdb.writes)
    assert " Exception raised " in output
    assert "ValueError: bad value" in output
    assert " Detailed stacktrace " in output
    assert "test_show_last_exception_renders_a_full_diagnostic" in output
    assert " Runtime environment " in output
    assert "GDB: unknown" in output
    assert "Python:" in output


def test_format_exception_returns_a_one_line_diagnostic():
    """The one-liner keeps message-only output stable regardless of debug state."""
    error = ValueError("boom")
    assert bridge.format_exception(error) == "ValueError: boom"
    assert "\n" not in bridge.format_exception(error)


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


class _Symbol:
    """Minimal ``gdb.Symbol`` that records whether ``value()`` was read."""

    def __init__(self, value, *, fail: bool = False):
        self._value = value
        self.fail = fail
        self.value_reads = 0

    def value(self):
        self.value_reads += 1
        if self.fail:
            raise RuntimeError("no selected frame")
        return self._value


class _LookupGdb:
    """GDB stand-in for identifier-only symbol lookup."""

    class error(Exception):
        pass

    def __init__(
        self,
        *,
        block=None,
        globals_=None,
        static=None,
        block_raises: bool = False,
    ):
        self.block = block or {}
        self.globals_ = globals_ or {}
        self.static = static or {}
        self.block_raises = block_raises
        self.lookups: list[tuple[str, str]] = []

    def lookup_symbol(self, name: str):
        self.lookups.append(("block", name))
        if self.block_raises:
            raise self.error("no frame")
        symbol = self.block.get(name)
        return symbol, False

    def lookup_global_symbol(self, name: str):
        self.lookups.append(("global", name))
        return self.globals_.get(name)

    def lookup_static_symbol(self, name: str):
        self.lookups.append(("static", name))
        return self.static.get(name)


class _EvalGdb:
    """GDB stand-in for identifier-only ``parse_and_eval``."""

    class error(Exception):
        pass

    def __init__(self, values: dict[str, object]):
        self.values = values
        self.parsed: list[str] = []

    def parse_and_eval(self, expression: str):
        self.parsed.append(expression)
        if expression not in self.values:
            raise self.error(f'No symbol "{expression}"')
        return self.values[expression]


def test_eval_identifier_rejects_call_and_index_expressions(monkeypatch):
    """The wrap checker never forwards ``foo()`` to ``parse_and_eval``."""
    fake = _EvalGdb({"rt_tick": 1})
    monkeypatch.setattr(bridge, "gdb", fake)

    assert bridge.eval_identifier("rt_tick_get()") is None
    assert bridge.eval_identifier("_timer_list[0]") is None
    assert bridge.eval_identifier("$pc") is None
    assert fake.parsed == []


def test_read_macro_int_uses_checked_parse_and_eval(monkeypatch):
    """GDB expands identifier macros, including packed version arithmetic."""
    fake = _EvalGdb(
        {
            "RT_CPUS_NR": 2,
            "configNUMBER_OF_CORES": 4,
            "RT_VER_NUM": 40005,
        }
    )
    monkeypatch.setattr(bridge, "gdb", fake)

    assert bridge.read_macro_int("RT_CPUS_NR") == 2
    assert bridge.read_macro_int("configNUMBER_OF_CORES") == 4
    assert bridge.read_macro_int("RT_VER_NUM") == 40005
    assert bridge.read_macro_int("MISSING") is None
    assert bridge.read_macro_int("foo()") is None
    assert fake.parsed == [
        "RT_CPUS_NR",
        "configNUMBER_OF_CORES",
        "RT_VER_NUM",
        "MISSING",
    ]


def test_production_code_does_not_evaluate_gdb_expressions():
    """Only ``eval_identifier`` in gdb_bridge may call ``gdb.parse_and_eval``."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "gdr.py",
        *root.glob("gdr/*.py"),
        *root.glob("rtthread/*.py"),
        *root.glob("freertos/*.py"),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "eval_safe" not in text, path
        if path.name == "gdb_bridge.py":
            assert "gdb.parse_and_eval(name)" in text
            continue
        assert "parse_and_eval" not in text, path


def test_lookup_symbol_rejects_call_and_index_expressions(monkeypatch):
    """Kernel collection must not evaluate expressions that can resume the MCU."""
    fake = _LookupGdb()
    monkeypatch.setattr(bridge, "gdb", fake)

    assert bridge.lookup_symbol("rt_tick_get()") is None
    assert bridge.lookup_symbol("_timer_list[0]") is None
    assert bridge.lookup_symbol("rt_cpu_index(0)") is None
    assert bridge.lookup_symbol("$pc") is None
    assert not bridge.symbol_exists("rt_sem_init()")
    assert fake.lookups == []


def test_lookup_symbol_falls_back_to_file_static(monkeypatch):
    """File-static heap counters are visible through ``lookup_static_symbol``."""
    symbol = _Symbol(4096)
    fake = _LookupGdb(static={"used_mem": symbol})
    monkeypatch.setattr(bridge, "gdb", fake)

    assert bridge.lookup_symbol("used_mem") == 4096
    assert fake.lookups == [
        ("block", "used_mem"),
        ("global", "used_mem"),
        ("static", "used_mem"),
    ]


def test_symbol_exists_does_not_read_function_values(monkeypatch):
    """Config probes detect ``rt_sem_init`` without requiring a selected frame."""
    symbol = _Symbol(None, fail=True)
    fake = _LookupGdb(globals_={"rt_sem_init": symbol}, block_raises=True)
    monkeypatch.setattr(bridge, "gdb", fake)

    assert bridge.symbol_exists("rt_sem_init") is True
    assert symbol.value_reads == 0
    assert bridge.lookup_symbol("rt_sem_init") is None
    assert symbol.value_reads == 1
