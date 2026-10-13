"""On-demand exhaustive RT-Thread checks, separate from compact live facts."""

from __future__ import annotations

from gdr.adapter_api import active, verdict_severity
from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    print_table,
    read_cstring,
    value_address,
    warn,
)
from gdr.layout import read_field
from rtthread import diagnostics
from rtthread.navigation import (
    active_timer_addresses,
    find_object,
    iter_objects,
    iter_threads,
    iter_timers,
)

CHECK_SCOPES = (
    "system",
    "heap",
    "thread",
    "semaphore",
    "mutex",
    "event",
    "mailbox",
    "messagequeue",
    "mempool",
    "timer",
)

_IPC_STRUCTS = {
    "semaphore": "struct rt_semaphore",
    "mutex": "struct rt_mutex",
    "event": "struct rt_event",
    "mailbox": "struct rt_mailbox",
    "msgqueue": "struct rt_messagequeue",
    "mempool": "struct rt_mempool",
}


def _verdict_statuses(
    pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep only verdict rows, normalized to ``ok``/``fail:``/``skipped:``.

    Detail builders render failures as bare reasons (``OffsetCheck``) or
    verdict-prefixed values; both are normalized here so the table's Verdict
    column always carries the bare verdict word.
    """
    statuses: list[tuple[str, str]] = []
    for key, text in pairs:
        severity = verdict_severity(key, text)
        if severity == "info":
            continue
        if severity == "ok":
            statuses.append((key, "ok"))
            continue
        head = text.partition(":")[0].strip().lower()
        if head in ("fail", "skipped"):
            statuses.append((key, text))
        else:
            statuses.append((key, f"{severity}: {text}"))
    return statuses


def _name_uniqueness(
    names: dict[int, str], address: int, name: str
) -> tuple[str, str] | None:
    """One ``NameUnique`` row flagging truncated-name collisions, if any.

    ``RT_NAME_MAX`` truncates (8 by default), so two live objects can share
    one name; the check names every address so the ``<name-or-address>``
    selector stays usable. Nameless rows carry no name to collide on.
    """
    if not name:
        return None
    dupes = sorted(
        addr for addr, other in names.items() if other == name and addr != address
    )
    if not dupes:
        return ("NameUnique", "ok")
    shared = ", ".join(f"{addr:#x}" for addr in [address, *dupes])
    return (
        "NameUnique",
        f"fail: {len(dupes) + 1} objects share name {name!r}: {shared}",
    )


@gdb_command_guard
def render_check(arguments: list[str]) -> None:
    """Print every result for the requested scope, including skips and passes."""
    from rtthread.adapter import RtThreadAdapter, value_to_timer
    from rtthread.layout import resolve_object_type_code

    selected = active()
    if not isinstance(selected, RtThreadAdapter):
        warn("run `gdr init rtthread <version>` first")
        return
    scope = arguments[0].lower() if arguments else "system"
    if scope == "task":
        # Reason: the machine/API kind is ``task`` while the ``rtt`` command
        # vocabulary names it ``thread``; accept both spellings here rather
        # than failing a user who guessed the wrong one.
        scope = "thread"
    target = " ".join(arguments[1:])
    if scope not in CHECK_SCOPES or (target and scope in ("system", "heap")):
        warn(
            "usage: rtt check "
            "[system|heap|thread|semaphore|mutex|event|mailbox|"
            "messagequeue|mempool|timer] [target]"
        )
        return
    layout = selected.layout
    rows: list[list[str]] = []

    def append(scope_label: str, results: list[tuple[str, str]]) -> None:
        for check, status in results:
            verdict, _, reason = status.partition(":")
            rows.append([scope_label, check, verdict, reason.strip() or "-"])

    if scope == "system":
        append("system", diagnostics.system_checks(layout))
    elif scope == "heap":
        for finding in selected._heap_findings(
            selected.heap_info() if selected.heap_type != "none" else None
        ):
            status = (
                finding.verdict
                if finding.verdict == "ok" or not finding.detail
                else f"{finding.verdict}: {finding.detail}"
            )
            append("heap", [(finding.name, status)])
    elif scope == "thread":
        thread_code = layout.object_codes.get("thread")
        names = {value_address(value): name for value, name in _thread_names(layout)}
        for value in iter_threads(layout):
            address = value_address(value)
            name = names.get(address, "")
            if target and target != name and target != f"{address:#x}":
                continue
            results = diagnostics.thread_checks(value, layout)
            results += diagnostics.object_identity_checks(
                value, layout, "struct rt_thread", thread_code
            )
            unique = _name_uniqueness(names, address, name)
            if unique is not None:
                results.append(unique)
            append(f"{name or '-'}@{address:#x}", results)
    elif scope == "timer":
        active_addrs = active_timer_addresses(layout)
        timer_code = layout.object_codes.get("timer")
        registered_addrs = (
            {value_address(value) for value in iter_objects(timer_code, layout)}
            if timer_code is not None
            else set()
        )
        names = {}
        for value in iter_timers(layout):
            names[value_address(value)] = value_to_timer(value, layout).name
        for value in iter_timers(layout):
            address = value_address(value)
            name = names.get(address, "")
            if target and target != name and target != f"{address:#x}":
                continue
            in_active: bool | None = address in active_addrs
            if address not in active_addrs and address not in registered_addrs:
                # Reason: the two walks raced a live update (a detach between
                # them); failing here would indict a transient, so the
                # membership verdict is skipped instead of guessed.
                in_active = None
            results = diagnostics.timer_checks(value, layout, in_active)
            results += diagnostics.object_identity_checks(
                value, layout, "struct rt_timer", timer_code
            )
            unique = _name_uniqueness(names, address, name)
            if unique is not None:
                results.append(unique)
            append(f"{name or '-'}@{address:#x}", results)
    else:
        type_code = resolve_object_type_code(scope, layout)
        if type_code is None:
            warn(f"object kind {scope!r} is not reliably enumerable")
            return
        struct_name = _IPC_STRUCTS[scope]
        sl = layout.structs.get(struct_name)
        names: dict[int, str] = {}
        if sl is not None:
            for value in iter_objects(type_code, layout):
                names[value_address(value)] = (
                    read_cstring(read_field(value, sl, "name")) or ""
                )
        objects = list(iter_objects(type_code, layout))
        if target:
            objects = [
                obj
                for obj in objects
                if names.get(value_address(obj), "") == target
                or f"{value_address(obj):#x}" == target
            ]
            if not objects:
                obj = find_object(type_code, target, layout)
                objects = [obj] if obj is not None else []
        for obj in objects:
            address = value_address(obj)
            name = names.get(address, "")
            pairs = selected._detail_pairs_for(scope, obj) or []
            results = _verdict_statuses(pairs)
            results += diagnostics.object_identity_checks(
                obj, layout, struct_name, type_code
            )
            unique = _name_uniqueness(names, address, name)
            if unique is not None:
                results.append(unique)
            append(f"{name or '-'}@{address:#x}", results)
    if not rows:
        info(f"no {scope} objects matched {target!r}")
        return
    info(
        "check inspects one halted snapshot; an in-progress kernel update can be transient"
    )
    print_table(rows, ["Object", "Check", "Verdict", "Evidence"])


def _thread_names(layout) -> list[tuple[object, str]]:
    """Pairs of raw thread values with their registry names."""
    thread_layout = layout.structs.get("struct rt_thread")
    if thread_layout is None:
        return []
    return [
        (value, read_cstring(read_field(value, thread_layout, "name")) or "")
        for value in iter_threads(layout)
    ]
