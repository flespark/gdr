"""Unit tests for shared kernel object reference resolution (name/symbol/address)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import core.commands as commands_module
import core.gdb_bridge as gdb_bridge
import core.object_ref as object_ref
from core.adapter import BacktraceReport, ObjectDetail
from core.gdb_bridge import lookup_symbols
from core.object_ref import (
    ObjectCandidate,
    ObjectResolution,
    resolve_object_ref,
    symbol_value_address,
)


def test_resolve_object_ref_follows_name_symbol_address_tiers():
    first = ObjectCandidate(0x20001000, "obj1", "worker")
    second = ObjectCandidate(0x20002000, "obj2", None)
    symbols = {"g_worker": [0x20001000], "g_anon_handle": [0x20002000]}

    by_name = resolve_object_ref(
        "task", "worker", [first, second], symbol_addresses=lambda s: symbols.get(s, [])
    )
    assert by_name.status == "found"
    assert by_name.value == "obj1"
    assert by_name.matched_by == "name"

    by_sym = resolve_object_ref(
        "task",
        "g_anon_handle",
        [first, second],
        symbol_addresses=lambda s: symbols.get(s, []),
    )
    assert by_sym.status == "found"
    assert by_sym.value == "obj2"
    assert by_sym.matched_by == "symbol"

    by_hex = resolve_object_ref("task", "0x20002000", [first, second])
    assert by_hex.status == "found"
    assert by_hex.value == "obj2"
    assert by_hex.matched_by == "address"

    by_dec = resolve_object_ref("task", str(0x20002000), [first, second])
    assert by_dec.status == "found"
    assert by_dec.value == "obj2"
    assert by_dec.matched_by == "address"


def test_resolve_object_ref_reports_duplicate_name_and_duplicate_symbol():
    dup1 = ObjectCandidate(0x20001000, "a", "worker")
    dup2 = ObjectCandidate(0x20002000, "b", "worker")
    res_name = resolve_object_ref("task", "worker", [dup1, dup2])
    assert res_name.status == "ambiguous"
    assert res_name.ambiguous is True
    assert res_name.matched_by == "name"
    assert res_name.message is not None
    assert "0x20001000" in res_name.message
    assert "0x20002000" in res_name.message
    assert "unique symbol or address" in res_name.message

    a = ObjectCandidate(0x20003000, "a", "t1")
    b = ObjectCandidate(0x20004000, "b", "t2")
    res_sym = resolve_object_ref(
        "semaphore",
        "lock",
        [a, b],
        symbol_addresses=lambda s: [0x20003000, 0x20004000] if s == "lock" else [],
    )
    assert res_sym.status == "ambiguous"
    assert res_sym.matched_by == "symbol"
    assert res_sym.message is not None
    assert "0x20003000" in res_sym.message
    assert "0x20004000" in res_sym.message
    assert "explicit address" in res_sym.message


def test_symbol_value_address_distinguishes_null_pointer_struct_and_scalar(monkeypatch):
    fake_gdb = SimpleNamespace(
        TYPE_CODE_PTR=1,
        TYPE_CODE_STRUCT=2,
        TYPE_CODE_UNION=3,
        TYPE_CODE_INT=4,
        error=RuntimeError,
    )
    monkeypatch.setattr(object_ref, "gdb", fake_gdb)

    def _val(code: int, int_val: int | None, addr: int):
        vtype = SimpleNamespace(
            code=code, strip_typedefs=lambda: SimpleNamespace(code=code)
        )
        return SimpleNamespace(type=vtype, _int=int_val, _addr=addr)

    null_ptr = _val(1, 0, 0x20009000)
    live_ptr = _val(1, 0x20001000, 0x20009000)
    static_struct = _val(2, None, 0x20002000)
    plain_int = _val(4, 0x20001000, 0x20009000)

    def _get_int(v: Any) -> int | None:
        return v._int

    def _get_addr(v: Any) -> int:
        return v._addr

    assert (
        symbol_value_address(null_ptr, safe_int_fn=_get_int, value_address_fn=_get_addr)
        is None
    )
    assert (
        symbol_value_address(live_ptr, safe_int_fn=_get_int, value_address_fn=_get_addr)
        == 0x20001000
    )
    assert (
        symbol_value_address(
            static_struct, safe_int_fn=_get_int, value_address_fn=_get_addr
        )
        == 0x20002000
    )
    assert (
        symbol_value_address(
            plain_int, safe_int_fn=_get_int, value_address_fn=_get_addr
        )
        is None
    )


def test_lookup_symbols_collects_multiple_file_static_symbols(monkeypatch):
    sym1 = SimpleNamespace(value=lambda: "v1")
    sym2 = SimpleNamespace(value=lambda: "v2")
    fake_gdb = SimpleNamespace(
        error=RuntimeError,
        lookup_global_symbol=lambda _name: None,
        lookup_static_symbols=lambda name: [sym1, sym2] if name == "lock" else [],
    )
    monkeypatch.setattr(gdb_bridge, "gdb", fake_gdb)
    assert lookup_symbols("lock") == ["v1", "v2"]
    assert lookup_symbols("bad()") == []


def test_commands_surface_ambiguity_in_backtrace_watch_and_render(monkeypatch):
    warnings: list[str] = []
    infos: list[str] = []
    monkeypatch.setattr(commands_module, "warn", warnings.append)
    monkeypatch.setattr(commands_module, "info", infos.append)
    monkeypatch.setattr(commands_module, "is_debug", lambda: False)

    ambiguous_msg = (
        "ambiguous task name 'worker' matches 2 objects (0x20001000, 0x20002000); "
        "specify a unique symbol or address"
    )
    report = BacktraceReport(
        task="worker",
        address=0,
        architecture="unknown",
        method="none",
        source="none",
        complete=False,
        stop_reason="ambiguous-target",
        truncated=False,
        max_depth=32,
        bounds="unknown",
        frames=[],
        messages=[ambiguous_msg],
    )
    commands_module.render_backtrace_report(report)
    assert warnings == [ambiguous_msg]

    class _Adapter:
        def object_counts(self):
            return {"task": 2}

        def resolve_target(self, _kind: str, _target: str):
            return ObjectResolution(status="ambiguous", message=ambiguous_msg)

        def object_detail(self, _kind: str, _target: str):
            return ObjectDetail(found=False, message=ambiguous_msg)

    monkeypatch.setattr(commands_module, "active", lambda: _Adapter())
    commands_module.clear()
    refusal = commands_module.add_watch("frt task worker", "task", "worker")
    assert refusal == ambiguous_msg

    commands_module._render_entry(
        _Adapter(), commands_module.Watch("task", "worker", "frt task worker")
    )
    assert infos[-1] == f"[gdr watch: frt task worker] {ambiguous_msg}"
