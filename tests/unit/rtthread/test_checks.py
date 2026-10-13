"""Unit tests for the explicit RT-Thread check surface (``rtt check``)."""

from __future__ import annotations

import rtthread.checks as checks_module
import rtthread.commands as commands
from gdr.adapter_api import CheckFinding
from gdr.layout import KernelLayout, StructField, StructLayout
from rtthread import diagnostics
from rtthread.adapter import RtThreadAdapter


def _thread_layout() -> KernelLayout:
    layout = KernelLayout()
    layout.structs["struct rt_thread"] = StructLayout(
        "struct rt_thread",
        fields={
            name: StructField(name, (name,))
            for name in (
                "name",
                "type",
                "sp",
                "stack_addr",
                "stack_size",
                "current_priority",
            )
        },
    )
    return layout


def _adapter() -> RtThreadAdapter:
    return RtThreadAdapter(_thread_layout(), heap_type="none")


def test_verdict_statuses_keep_only_checks_and_normalize():
    pairs = [
        ("Name", "work_sem"),
        ("Value", "3"),
        ("OffsetCheck", "ok"),
        ("SlotCheck", "fail: slot unreadable"),
        ("FreeCountCheck", "walked 4, cached 5"),
        ("AlignmentCheck", "N/A"),
    ]
    assert checks_module._verdict_statuses(pairs) == [
        ("OffsetCheck", "ok"),
        ("SlotCheck", "fail: slot unreadable"),
        ("FreeCountCheck", "fail: walked 4, cached 5"),
        ("AlignmentCheck", "skipped: N/A"),
    ]


def test_name_uniqueness_flags_truncated_collisions():
    unique = checks_module._name_uniqueness({0x1000: "a", 0x2000: "b"}, 0x1000, "a")
    assert unique == ("NameUnique", "ok")
    assert checks_module._name_uniqueness({}, 0x1000, "") is None
    collision = checks_module._name_uniqueness(
        {0x1000: "dup", 0x2000: "dup"}, 0x1000, "dup"
    )
    assert collision is not None
    assert collision[0] == "NameUnique"
    assert collision[1].startswith("fail: 2 objects share name 'dup'")
    assert "0x1000" in collision[1] and "0x2000" in collision[1]


def test_thread_checks_cover_sp_bounds_and_priority(monkeypatch):
    layout = _thread_layout()
    fields = {
        "sp": 0x1500,
        "stack_addr": 0x1000,
        "stack_size": 0x1000,
        "current_priority": 5,
    }
    monkeypatch.setattr(
        diagnostics, "read_field", lambda _v, _l, name: fields.get(name)
    )
    monkeypatch.setattr(diagnostics, "read_int", lambda value: value)
    monkeypatch.setattr(diagnostics, "read_macro_int", lambda _name: 32)

    assert diagnostics.thread_checks(object(), layout) == [
        ("SpBounds", "ok"),
        ("PriorityRange", "ok"),
    ]

    fields["sp"] = 0x5000
    assert diagnostics.thread_checks(object(), layout)[0][1].startswith("fail:")

    fields["sp"] = 0x1500
    fields["current_priority"] = 40
    assert diagnostics.thread_checks(object(), layout)[1][1].startswith("fail:")

    monkeypatch.setattr(diagnostics, "read_macro_int", lambda _name: None)
    assert diagnostics.thread_checks(object(), layout)[1][1].startswith("skipped:")


def test_object_identity_checks_type_and_lifetime(monkeypatch):
    layout = _thread_layout()
    monkeypatch.setattr(diagnostics, "read_field", lambda _v, _l, _f: 0x01)
    monkeypatch.setattr(diagnostics, "read_int", lambda value: value)

    assert diagnostics.object_identity_checks(
        object(), layout, "struct rt_thread", 1
    ) == [("ObjectType", "ok"), ("Lifetime", "ok: dynamic object")]

    monkeypatch.setattr(diagnostics, "read_field", lambda _v, _l, _f: 0x81)
    assert diagnostics.object_identity_checks(object(), layout, "struct rt_thread", 1)[
        1
    ] == ("Lifetime", "ok: static object; confirm it outlives its scope")

    monkeypatch.setattr(diagnostics, "read_field", lambda _v, _l, _f: 0x02)
    assert diagnostics.object_identity_checks(
        object(), layout, "struct rt_thread", 1
    ) == [("ObjectType", "fail: type byte 0x2, expected class 0x1")]

    assert diagnostics.object_identity_checks(
        object(), KernelLayout(), "struct rt_thread", 1
    ) == [("ObjectType", "skipped: type field not in debug info")]


def test_timer_checks_flag_against_list_membership(monkeypatch):
    layout = KernelLayout()
    layout.structs["struct rt_timer"] = StructLayout(
        "struct rt_timer", fields={"flag": StructField("flag", ("flag",))}
    )
    monkeypatch.setattr(diagnostics, "read_field", lambda _v, _l, _f: 0x1)
    monkeypatch.setattr(diagnostics, "read_int", lambda value: value)

    assert diagnostics.timer_checks(object(), layout, True) == [
        ("ListMembership", "ok")
    ]
    assert diagnostics.timer_checks(object(), layout, False)[0][1].startswith("fail:")
    assert diagnostics.timer_checks(object(), layout, None)[0][1].startswith("skipped:")


def test_render_check_needs_an_rtthread_adapter(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(checks_module, "active", lambda: None)
    monkeypatch.setattr(checks_module, "warn", warnings.append)

    checks_module.render_check([])

    assert warnings == ["run `gdr init rtthread <version>` first"]


def test_render_check_rejects_scope_and_target_combinations(monkeypatch):
    adapter = _adapter()
    warnings: list[str] = []
    monkeypatch.setattr(checks_module, "active", lambda: adapter)
    monkeypatch.setattr(checks_module, "warn", warnings.append)

    checks_module.render_check(["bogus"])
    checks_module.render_check(["heap", "extra"])

    assert all(warning.startswith("usage: rtt check") for warning in warnings)
    assert len(warnings) == 2


def test_render_check_system_reports_passes_and_skips(monkeypatch):
    adapter = _adapter()
    captured: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(checks_module, "active", lambda: adapter)
    monkeypatch.setattr(checks_module, "info", lambda _message: None)
    monkeypatch.setattr(
        checks_module,
        "print_table",
        lambda rows, headers: captured.append((rows, headers)),
    )
    monkeypatch.setattr(
        diagnostics, "system_checks", lambda _layout: [("Tick", "ok: 100")]
    )

    checks_module.render_check([])

    assert captured[0][1] == ["Object", "Check", "Verdict", "Evidence"]
    assert captured[0][0] == [["system", "Tick", "ok", "100"]]


def test_render_check_task_alias_routes_to_threads(monkeypatch):
    adapter = _adapter()
    captured: list[tuple[list[list[str]], list[str]]] = []
    value = object()
    monkeypatch.setattr(checks_module, "active", lambda: adapter)
    monkeypatch.setattr(checks_module, "info", lambda _message: None)
    monkeypatch.setattr(
        checks_module,
        "print_table",
        lambda rows, headers: captured.append((rows, headers)),
    )
    monkeypatch.setattr(checks_module, "iter_threads", lambda _layout: iter([value]))
    monkeypatch.setattr(
        checks_module, "_thread_names", lambda _layout: [(value, "worker")]
    )
    monkeypatch.setattr(checks_module, "value_address", lambda _value: 0x1000)
    monkeypatch.setattr(
        diagnostics, "thread_checks", lambda _value, _layout: [("SpBounds", "ok")]
    )
    monkeypatch.setattr(
        diagnostics,
        "object_identity_checks",
        lambda _v, _l, _s, _c: [("ObjectType", "ok")],
    )

    checks_module.render_check(["task"])

    assert captured[0][0][0][:3] == ["worker@0x1000", "SpBounds", "ok"]


def test_render_check_heap_reuses_adapter_findings(monkeypatch):
    adapter = _adapter()
    captured: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(checks_module, "active", lambda: adapter)
    monkeypatch.setattr(checks_module, "info", lambda _message: None)
    monkeypatch.setattr(
        checks_module,
        "print_table",
        lambda rows, headers: captured.append((rows, headers)),
    )
    monkeypatch.setattr(
        adapter,
        "_heap_findings",
        lambda _info: [CheckFinding("HeapChain", "fail", "corrupt", "system")],
    )

    checks_module.render_check(["heap"])

    assert captured[0][0] == [["heap", "HeapChain", "fail", "corrupt"]]


def test_check_command_reaches_the_renderer(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(commands, "render_check", calls.append)

    commands._invoke_command("check thread worker")

    assert calls == [["thread", "worker"]]


def test_check_help_topic_documents_usage(capsys):
    commands._invoke_command("help check")
    output = capsys.readouterr().out
    assert "rtt check [system|heap|thread|" in output
