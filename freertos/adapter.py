"""FreeRTOS task conversion and GDB convenience functions."""

from __future__ import annotations

from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from freertos.navigation import iter_tasks
from gdr.gdb_bridge import make_pointer_array, read_cstring, read_int
from gdr.layout import read_field


@dataclass
class FreeRtosTask:
    name: str = ""
    address: int = 0
    state: str = "Unknown"
    current_priority: int = 0
    base_priority: int = 0
    top_of_stack: int = 0
    stack_base: int = 0
    stack_end: int = 0
    stack_size: int | None = None
    stack_used: int | None = None
    high_water_mark: int | None = None
    runtime_counter: int | None = None
    entry: int = 0
    core: int | None = None


_layout: FreeRtosLayout | None = None


def _address(value) -> int:
    try:
        return int(value.address)
    except Exception:
        return 0


def _ptr(value) -> int:
    return read_int(value) or 0


def value_to_task(
    value, state: str, core: int | None, layout: FreeRtosLayout
) -> FreeRtosTask:
    sl = layout.structs["struct tskTaskControlBlock"]
    top = _ptr(read_field(value, sl, "top_of_stack"))
    base = _ptr(read_field(value, sl, "stack_base"))
    end = _ptr(read_field(value, sl, "stack_end"))
    size = end - base if end and base and end >= base else None
    high = None
    runtime = read_int(read_field(value, sl, "runtime_counter"))
    entry = _ptr(read_field(value, sl, "entry"))
    return FreeRtosTask(
        name=read_cstring(read_field(value, sl, "name")) or "",
        address=_address(value),
        state=state,
        current_priority=read_int(read_field(value, sl, "current_priority")) or 0,
        base_priority=read_int(read_field(value, sl, "base_priority")) or 0,
        top_of_stack=top,
        stack_base=base,
        stack_end=end,
        stack_size=size,
        stack_used=(end - top if end and top and end >= top else None),
        high_water_mark=high,
        runtime_counter=runtime,
        entry=entry,
        core=core,
    )


def iter_converted_tasks(layout: FreeRtosLayout):
    for value, state, core in iter_tasks(layout):
        yield value_to_task(value, state, core, layout)


def find_task(name: str, layout: FreeRtosLayout):
    for value, state, core in iter_tasks(layout):
        task = value_to_task(value, state, core, layout)
        if task.name == name:
            return value
    return None


if gdb is not None:

    class GdrFreeRtosTaskFunction(gdb.Function):
        def __init__(self):
            super().__init__("gdr_freertos_task")

        def invoke(self, name):
            if _layout is None:
                return gdb.Value(0)
            value = find_task(read_cstring(name) or "", _layout)
            return value if value is not None else gdb.Value(0)

    class GdrFreeRtosTasksFunction(gdb.Function):
        def __init__(self):
            super().__init__("gdr_freertos_tasks")

        def invoke(self):
            if _layout is None:
                return gdb.Value(0)
            return make_pointer_array([v for v, _, _ in iter_tasks(_layout)])


def register_adapter(layout: FreeRtosLayout) -> None:
    global _layout
    if _layout is not None:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    GdrFreeRtosTaskFunction()
    GdrFreeRtosTasksFunction()
    _layout = layout
