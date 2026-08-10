"""Safe traversal of FreeRTOS scheduler lists and system globals."""

from __future__ import annotations

from collections.abc import Iterator

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from gdr.gdb_bridge import lookup_symbol, read_int, warn


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _addr(value) -> int:
    try:
        address = value.address
        return _safe_int(address) or 0
    except Exception:
        return 0


def _deref(pointer):
    try:
        return pointer.dereference() if pointer is not None and int(pointer) else None
    except Exception:
        return None


def _owner_task(pointer):
    """Cast ListItem.pvOwner (void *) to the DWARF TCB type."""
    try:
        if pointer is None or not int(pointer) or gdb is None:
            return None
        typ = gdb.lookup_type("struct tskTaskControlBlock").pointer()
        return pointer.cast(typ).dereference()
    except Exception:
        return None


def _array_item(value, index):
    try:
        return value[index]
    except Exception:
        return None


def _list_value(name: str, index: int | None = None):
    value = lookup_symbol(name)
    if value is None:
        return None
    return _array_item(value, index) if index is not None else value


def _iter_list(head, max_count: int = 4096) -> Iterator:
    """Yield TCBs from a List_t, stopping on corruption or a bounded count."""
    if head is None:
        return
    try:
        end = head["xListEnd"]
        end_addr = _addr(end)
        node = end["pxNext"]
        seen: set[int] = set()
        for _ in range(max_count):
            node_addr = _safe_int(node)
            if not node_addr or node_addr == end_addr:
                return
            if node_addr in seen:
                warn(f"FreeRTOS list traversal stopped at repeated node {node_addr:#x}")
                return
            seen.add(node_addr)
            item = _deref(node)
            if item is None:
                warn(f"FreeRTOS list traversal stopped at invalid node {node_addr:#x}")
                return
            owner = _owner_task(item["pvOwner"])
            if owner is not None:
                yield owner
            node = item["pxNext"]
        warn(f"FreeRTOS list traversal truncated after {max_count} nodes")
    except Exception:
        warn("FreeRTOS list traversal stopped because list data is unreadable")


def _ready_heads(layout: FreeRtosLayout) -> Iterator:
    table = lookup_symbol(layout.lists["ready"])
    if table is None:
        return
    try:
        bounds = table.type.strip_typedefs().range()
        first, last = int(bounds[0]), int(bounds[1])
    except Exception:
        first, last = 0, 256
    for index in range(first, min(last + 1, first + 256)):
        item = _array_item(table, index)
        if item is None:
            return
        yield item


def _head(name: str, layout: FreeRtosLayout):
    value = lookup_symbol(layout.lists[name])
    if name in ("delayed_current", "delayed_overflow"):
        return _deref(value)
    return value


def iter_tasks(layout: FreeRtosLayout) -> Iterator[tuple[object, str, int | None]]:
    """Yield ``(TCB, list-state, core)`` exactly once per target address."""
    seen: set[int] = set()
    sources: list[tuple[Iterator, str]] = []
    sources.extend((_iter_list(head), "Ready") for head in _ready_heads(layout))
    for key, state in (
        ("delayed_1", "Blocked"),
        ("delayed_2", "Blocked"),
        ("delayed_current", "Blocked"),
        ("delayed_overflow", "Blocked"),
        ("pending", "Pending"),
        ("suspended", "Suspended"),
        ("termination", "Deleted/Pending"),
    ):
        head = _head(key, layout)
        if head is not None:
            sources.append((_iter_list(head), state))
    current = current_tasks(layout)
    current_addrs = {address: core for core, address in current}
    for iterator, state in sources:
        for task in iterator:
            address = _addr(task)
            if not address or address in seen:
                continue
            seen.add(address)
            core = current_addrs.get(address)
            yield task, "Running" if core is not None else state, core


def current_tasks(layout: FreeRtosLayout) -> list[tuple[int, int]]:
    if layout.config.smp:
        value = lookup_symbol("pxCurrentTCBs")
        result = []
        for core in range(layout.config.number_of_cores):
            pointer = _array_item(value, core)
            task = _deref(pointer)
            if task is not None:
                result.append((core, _addr(task)))
        return result
    task = _deref(lookup_symbol("pxCurrentTCB"))
    return [(0, _addr(task))] if task is not None else []


def system_value(name: str) -> int | None:
    return read_int(lookup_symbol(name))


def list_count(name: str, layout: FreeRtosLayout) -> int | None:
    head = _head(name, layout)
    if head is None:
        return None
    try:
        return read_int(head["uxNumberOfItems"])
    except Exception:
        return None
