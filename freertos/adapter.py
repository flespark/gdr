"""FreeRTOS task conversion and GDB convenience functions."""

from __future__ import annotations

from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from freertos.navigation import iter_tasks, list_count, system_value
from gdr.adapter_api import (
    ObjectDetail,
    ObjectTable,
    RtosAdapter,
    SystemSummary,
)
from gdr.formatting import format_address, format_optional_int
from gdr.gdb_bridge import read_cstring, read_int, value_address
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
    core_affinity: int | None = None


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
        address=value_address(value),
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
        core_affinity=read_int(read_field(value, sl, "core_affinity")),
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


class FreeRtosAdapter(RtosAdapter):
    """Expose FreeRTOS scheduler lists through the shared task contract."""

    def __init__(self, layout: FreeRtosLayout) -> None:
        self.layout = layout

    def find_task(self, name: str) -> gdb.Value | None:
        return find_task(name, self.layout)

    def find_object(self, kind: str, name: str) -> gdb.Value | None:  # noqa: ARG002
        # Queue Registry and active-timer traversal are not implemented.
        return None

    def object_counts(self) -> dict[str, int]:
        return {"task": len(list(self.iter_tasks()))}

    def object_table(self, kind: str) -> ObjectTable | None:  # noqa: ARG002
        return None

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None:  # noqa: ARG002
        # Queue/timer enumeration and object detail are not implemented.
        return None

    def iter_tasks(self):
        for value, _state, _core in iter_tasks(self.layout):
            yield value

    def task_table(self) -> ObjectTable:
        fields = self.layout.structs["struct tskTaskControlBlock"].fields
        show_base_priority = "base_priority" in fields
        show_stack = "stack_end" in fields
        show_runtime = "runtime_counter" in fields
        show_smp = self.layout.config.smp
        show_affinity = "core_affinity" in fields

        headers = ["Name", "State", "Prio"]
        if show_base_priority:
            headers.append("BasePrio")
        headers.append("SP")
        if show_stack:
            headers += ["Stack", "Used"]
        if show_runtime:
            headers.append("Runtime")
        if show_smp:
            headers.append("CPU")
        if show_affinity:
            headers.append("Affinity")
        headers.append("Addr")

        rows = []
        for task in iter_converted_tasks(self.layout):
            row = [
                task.name + (" *" if task.core is not None else ""),
                task.state,
                str(task.current_priority),
            ]
            if show_base_priority:
                row.append(str(task.base_priority))
            row.append(format_address(task.top_of_stack))
            if show_stack:
                row += [
                    format_optional_int(task.stack_size),
                    format_optional_int(task.stack_used),
                ]
            if show_runtime:
                row.append(format_optional_int(task.runtime_counter))
            if show_smp:
                row.append(format_optional_int(task.core))
            if show_affinity:
                row.append(format_optional_int(task.core_affinity))
            row.append(format_address(task.address))
            rows.append(row)
        return ObjectTable(headers=headers, rows=rows, elastic=("Name",))

    def system_summary(self) -> SystemSummary:
        tasks = list(iter_converted_tasks(self.layout))
        current = next((task.name for task in tasks if task.core is not None), None)
        delayed = [list_count(key, self.layout) for key in ("delayed_1", "delayed_2")]
        counts = {
            "Ready": list_count("ready", self.layout),
            "Delayed": (
                sum(value for value in delayed if value is not None)
                if any(value is not None for value in delayed)
                else None
            ),
            "Pending": list_count("pending", self.layout),
            "Suspended": list_count("suspended", self.layout),
            "Termination": list_count("termination", self.layout),
        }
        scheduler = system_value("xSchedulerRunning")
        total = system_value("uxCurrentNumberOfTasks")
        return SystemSummary(
            kernel_version=(
                ".".join(map(str, self.layout.version))
                if self.layout.version is not None
                else "unknown"
            ),
            current_task=current,
            task_count=total if total is not None else len(tasks),
            tick_count=system_value("xTickCount"),
            scheduler_state=(
                "running"
                if scheduler
                else "not-running"
                if scheduler is not None
                else "unavailable"
            ),
            state_counts={
                name: value for name, value in counts.items() if value is not None
            },
            object_counts={"task": len(tasks)},
            heap_allocator="unimplemented",
        )
