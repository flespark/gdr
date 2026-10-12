"""On-demand exhaustive FreeRTOS checks, separate from compact live facts."""

from __future__ import annotations

from freertos.diagnostics import (
    event_checks,
    queue_checks,
    system_checks,
    task_checks,
    timer_checks,
)
from freertos.heap import heap_snapshot
from freertos.navigation import (
    _refine_queue_object,
    discover,
    iter_tasks,
    resolve_object,
    task_name_at,
)
from gdr.adapter_api import active
from gdr.gdb_bridge import info, print_table, value_address, warn

CHECK_KINDS = (
    "system",
    "heap",
    "task",
    "queue",
    "semaphore",
    "mutex",
    "timer",
    "eventgroup",
)


def render_checks(arguments: list[str]) -> None:
    """Print every result for the requested scope, including skips and passes."""
    from freertos.adapter import FreeRtosAdapter, _cast_object

    selected = active()
    if not isinstance(selected, FreeRtosAdapter):
        warn("run `gdr init freertos <version>` first")
        return
    kind = arguments[0].lower() if arguments else "system"
    name = " ".join(arguments[1:])
    if kind not in CHECK_KINDS or (name and kind in ("system", "heap")):
        warn(
            "usage: frt checks [system|heap|task|queue|semaphore|mutex|timer|eventgroup] [name]"
        )
        return
    layout = selected.layout
    rows: list[list[str]] = []

    def append(scope: str, results: list[tuple[str, str]]) -> None:
        for check, status in results:
            verdict, _, reason = status.partition(":")
            rows.append([scope, check, verdict, reason.strip() or "-"])

    if kind == "system":
        append("system", system_checks(layout))
    elif kind == "heap":
        verdict = heap_snapshot(layout).cross_check
        append(
            "heap",
            [
                (
                    "CrossCheck",
                    "ok"
                    if verdict == "ok"
                    else (
                        "skipped: " if verdict.startswith("unavailable") else "fail: "
                    )
                    + verdict,
                )
            ],
        )
    elif kind == "task":
        for value, _state, _core in iter_tasks(layout):
            address = value_address(value)
            task_name = task_name_at(address, layout)
            if name and name != task_name and name != f"{address:#x}":
                continue
            append(
                f"{task_name or '-'}@{address:#x}", task_checks(value, address, layout)
            )
    else:
        objects = discover(kind, layout)
        if name:
            objects = [
                obj
                for obj in objects
                if obj.name == name or f"{obj.address:#x}" == name
            ]
            if not objects:
                obj = resolve_object(kind, name, layout)
                objects = [obj] if obj is not None else []
        for obj in objects:
            if kind in ("queue", "semaphore", "mutex"):
                obj = _refine_queue_object(obj, layout)
                if obj.kind != kind and not obj.inferred_kind:
                    warn(f"{name!r} is a {obj.kind}, not a {kind}")
                    continue
            value = _cast_object(obj.address, kind, layout)
            results = (
                queue_checks(value, obj.kind, layout)
                if kind in ("queue", "semaphore", "mutex")
                else timer_checks(value, layout)
                if kind == "timer"
                else event_checks(value, layout)
            )
            append(f"{obj.name or '-'}@{obj.address:#x}", results)
    if not rows:
        info(f"no {kind} objects matched {name!r}")
        return
    info(
        "checks inspect one halted snapshot; an in-progress kernel update can be transient"
    )
    print_table(rows, ["Object", "Check", "Verdict", "Evidence"])
