"""Explicit checks retain full coverage and locate object-level evidence."""

from types import SimpleNamespace

from freertos import adapter, checks, commands
from freertos.layout import FreeRtosConfig, build_layout


def _selected(monkeypatch):
    selected = adapter.FreeRtosAdapter(build_layout(FreeRtosConfig()))
    monkeypatch.setattr(checks, "active", lambda: selected)
    monkeypatch.setattr(checks, "info", lambda _message: None)
    captured = []
    monkeypatch.setattr(
        checks, "print_table", lambda rows, headers: captured.append((rows, headers))
    )
    return captured


def test_checks_route_preserves_scope_and_space_name(monkeypatch):
    captured = []
    monkeypatch.setattr(commands, "render_checks", captured.append)
    commands._invoke_command("check task Tmr Svc")
    assert captured == [["task", "Tmr", "Svc"]]


def test_legacy_checks_alias_shares_the_check_route(monkeypatch):
    """The pre-rename `checks` spelling still reaches the check renderer."""
    captured = []
    monkeypatch.setattr(commands, "render_checks", captured.append)
    commands._invoke_command("checks heap")
    assert captured == [["heap"]]


def test_checks_system_keeps_passes_and_skips(monkeypatch):
    captured = _selected(monkeypatch)
    monkeypatch.setattr(
        checks,
        "system_checks",
        lambda _layout: [
            ("TaskCount", "ok"),
            ("NextUnblockTime", "skipped: scheduler not started"),
        ],
    )
    checks.render_checks([])
    assert captured[0][0] == [
        ["system", "TaskCount", "ok", "-"],
        ["system", "NextUnblockTime", "skipped", "scheduler not started"],
    ]


def test_checks_task_unpacks_scheduler_records_and_keeps_address(monkeypatch):
    captured = _selected(monkeypatch)
    monkeypatch.setattr(
        checks, "iter_tasks", lambda _layout: [(object(), "Ready", None)]
    )
    monkeypatch.setattr(checks, "value_address", lambda _value: 0x2000)
    monkeypatch.setattr(checks, "task_name_at", lambda *_args: "Tmr Svc")
    monkeypatch.setattr(
        checks,
        "task_checks",
        lambda *_args: [("ItemOwner", "fail: owner=0x3000, expected 0x2000")],
    )
    checks.render_checks(["task", "Tmr", "Svc"])
    assert captured[0][0][0][0] == "Tmr Svc@0x2000"
    assert captured[0][0][0][2] == "fail"


def test_checks_queue_names_and_pointer_evidence(monkeypatch):
    captured = _selected(monkeypatch)
    found = SimpleNamespace(name="q", address=0x2000, kind="queue", inferred_kind=False)
    monkeypatch.setattr(checks, "discover", lambda *_args: [found])
    monkeypatch.setattr(checks, "_refine_queue_object", lambda obj, _layout: obj)
    monkeypatch.setattr(adapter, "_cast_object", lambda *_args: object())
    monkeypatch.setattr(
        checks,
        "queue_checks",
        lambda *_args: [
            ("WritePtr", "fail: pcWriteTo=0x3000 outside [0x2100, 0x2200)")
        ],
    )
    checks.render_checks(["queue", "q"])
    assert captured[0][0][0][0] == "q@0x2000"
    assert "0x3000" in captured[0][0][0][3]


def test_checks_heap_ok_remains_explicit(monkeypatch):
    captured = _selected(monkeypatch)
    monkeypatch.setattr(
        checks, "heap_snapshot", lambda _layout: SimpleNamespace(cross_check="ok")
    )
    checks.render_checks(["heap"])
    assert captured[0][0] == [["heap", "CrossCheck", "ok", "-"]]


def test_checks_wrong_kind_refuses_union_decode(monkeypatch):
    captured = _selected(monkeypatch)
    found = SimpleNamespace(name="m", address=0x2000, kind="mutex", inferred_kind=False)
    warnings = []
    monkeypatch.setattr(checks, "warn", warnings.append)
    monkeypatch.setattr(checks, "discover", lambda *_args: [])
    monkeypatch.setattr(checks, "resolve_object", lambda *_args: found)
    monkeypatch.setattr(checks, "_refine_queue_object", lambda obj, _layout: obj)
    checks.render_checks(["semaphore", "m"])
    assert warnings == ["'m' is a mutex, not a semaphore"]
    assert not captured
