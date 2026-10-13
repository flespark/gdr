"""Stop-time watch printing for kernel objects (the CLI watch surface).

This is the RTOS-aware analogue of GDB's ``display``: watched kinds print
their list table and watched singletons print their detail block on every
target stop. Unlike a GDB watchpoint it never stops the target itself.

Both render paths reuse the same collectors the text commands and the API
verbs consume, so a watch block cannot drift from its command output. All
state is session-scoped and dropped by ``gdr reset`` alongside the adapter
caches.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gdr.adapter_api import active
from gdr.commands import (
    collect_object_detail,
    collect_object_listing,
    collect_task_table,
    prefix_candidates,
)
from gdr.gdb_bridge import (
    TARGET_ACCESS_ERRORS,
    info,
    print_detail,
    print_table,
)


@dataclass(frozen=True)
class Watch:
    """One watched kind (``target`` is ``None``) or singleton object."""

    kind: str
    target: str | None
    label: str


@dataclass(frozen=True)
class WatchSpec:
    """The watchable words of one RTOS command tree, for tab completion.

    ``kind_words`` maps a plural command word to its canonical kind and
    ``singular_kinds`` a singular detail word to its kind; both come from
    the tree's own help metadata so completion never drifts from routing.
    """

    kind_words: dict[str, str]
    singular_kinds: dict[str, str]
    aliases: dict[str, str]
    object_names_fn: Callable[[str], list[str]]
    preserve_spaces: bool = False


def complete_watch(spec: WatchSpec, text: str, word: str | None) -> list[str]:
    """Tab-completion for the words after ``watch``/``unwatch``.

    The first word completes against plural kinds and singular details; the
    remainder of a singular detail completes against live object names.
    Shared by the RTOS command trees.
    """
    parts = text.split()
    if len(parts) <= 1:
        return prefix_candidates(word, [*spec.kind_words, *spec.singular_kinds])
    first = spec.aliases.get(parts[1].lower(), parts[1].lower())
    if first in spec.singular_kinds and (len(parts) > 2 or text.endswith((" ", "\t"))):
        return prefix_candidates(word, spec.object_names_fn(spec.singular_kinds[first]))
    return prefix_candidates(word, [*spec.kind_words, *spec.singular_kinds])


_watches: list[Watch] = []


def list_watches() -> list[Watch]:
    """The active watch set, in the order it was added."""
    return list(_watches)


def clear() -> None:
    """Drop every watch (session reset and adapter replacement)."""
    _watches.clear()


def add_watch(label: str, kind: str, target: str | None) -> str | None:
    """Record one watch after validating it against the live target.

    Returns ``None`` when the watch was added, else the reason it was
    refused (unknown kind, missing object, unreadable target). A singleton
    must exist now; it is re-resolved on every stop, so a later detach
    renders as ``gone`` instead of a stale address.
    """
    adapter = active()
    if adapter is None:
        return "run `gdr init <rtos> <version>` first"
    if kind not in adapter.object_counts():
        return f"object kind {kind!r} is not reliably enumerable"
    if target is not None:
        try:
            found = (
                adapter.find_task(target)
                if kind == "task"
                else adapter.find_object(kind, target)
            )
        except TARGET_ACCESS_ERRORS:
            return f"{kind} {target!r}: unreadable (target halted?)"
        if found is None:
            return f"{kind} {target!r}: not found or type not enabled"
    if any(entry.kind == kind and entry.target == target for entry in _watches):
        return f"already watching {label}"
    _watches.append(Watch(kind=kind, target=target, label=label))
    return None


def remove_watch(kind: str, target: str | None) -> bool:
    """Drop one watch; ``False`` when the set never held it."""
    for index, entry in enumerate(_watches):
        if entry.kind == kind and entry.target == target:
            del _watches[index]
            return True
    return False


def render_watch_list() -> None:
    """List the active watch set, or note that it is empty."""
    if not _watches:
        info("no watches (add one with `<rtos> watch <kind>`)")
        return
    for index, entry in enumerate(_watches):
        info(f"[watch {index}] {entry.label}")


def render_watches() -> None:
    """Print every watched kind/object; the stop-hook entry point.

    Never raises: this runs inside GDB's event loop, where a raise would
    surface as raw noise mid-session (the same discipline as the generation
    bump beside it). One unreadable entry must not silence the rest, and an
    empty set costs a single length check.
    """
    try:
        watches = list(_watches)
        if not watches:
            return
        adapter = active()
        if adapter is None:
            return
        for entry in watches:
            # Reason: _render_one never raises (see its guard), so one
            # unreadable entry cannot silence the rest; the entry stays
            # watched and retries on the next stop.
            _render_one(adapter, entry)
    except Exception:  # pragma: no cover - defensive event-loop guard
        return


def _render_one(adapter, entry: Watch) -> None:
    """Print one watch entry's fresh snapshot with a greppable header.

    Never raises: a stop-time read can fail for reasons the next stop heals
    (transient kernel update, dropped remote link).
    """
    try:
        _render_entry(adapter, entry)
    except Exception:  # pragma: no cover - defensive event-loop guard
        return


def _render_entry(adapter, entry: Watch) -> None:
    """One watch entry's fresh snapshot; raises on unreadable target."""
    if entry.kind not in adapter.object_counts():
        info(f"[gdr watch: {entry.label}] skipped: kind not enumerable now")
        return
    if entry.target is None:
        table = (
            collect_task_table(adapter)
            if entry.kind == "task"
            else collect_object_listing(adapter, entry.kind)
        )
        if table is None:
            info(f"[gdr watch: {entry.label}] skipped: kind not enumerable now")
            return
        info(f"[gdr watch: {entry.label}]")
        for message in table.messages:
            info(message)
        print_table(table.rows, table.headers, elastic=table.elastic)
        return
    detail = collect_object_detail(adapter, entry.kind, entry.target)
    if detail is None:
        info(f"[gdr watch: {entry.label}] skipped: kind not enumerable now")
        return
    if not detail.found:
        info(f"[gdr watch: {entry.label}] gone (detached?)")
        return
    info(f"[gdr watch: {entry.label}]")
    print_detail(detail.pairs)
