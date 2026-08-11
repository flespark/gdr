"""FreeRTOS task conversion and GDB convenience functions."""

from __future__ import annotations

from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from freertos.navigation import current_tasks, iter_tasks, list_count, system_value
from gdr.adapter_api import ObjectTable, RtosAdapter, SystemSummary, TaskSummary
from gdr.gdb_bridge import read_cstring, read_int
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


class FreeRtosAdapter(RtosAdapter):
    """Expose FreeRTOS scheduler lists through the shared task contract."""

    def __init__(self, layout: FreeRtosLayout) -> None:
        self.layout = layout

    def find_task(self, name: str) -> gdb.Value | None:
        return find_task(name, self.layout)

    def find_object(self, kind: str, name: str) -> gdb.Value | None:  # noqa: ARG002
        # Queue Registry and active-timer traversal are Phase 3 features.
        return None

    def object_counts(self) -> dict[str, int]:
        return {"task": len(list(self.iter_tasks()))}

    def object_table(self, kind: str) -> ObjectTable | None:  # noqa: ARG002
        return None

    def iter_tasks(self):
        for value, _state, _core in iter_tasks(self.layout):
            yield value

    def _locations(self) -> dict[int, tuple[str, int | None]]:
        return {
            _address(value): (state, core)
            for value, state, core in iter_tasks(self.layout)
        }

    def summarize_task(self, value: gdb.Value) -> TaskSummary:
        state, core = self._locations().get(_address(value), ("Unknown", None))
        task = value_to_task(value, state, core, self.layout)
        return TaskSummary(
            name=task.name,
            address=task.address,
            state=task.state,
            priority=task.current_priority,
            base_priority=task.base_priority,
            stack_pointer=task.top_of_stack,
            stack_size=task.stack_size,
            stack_used=task.stack_used,
            high_water_mark=task.high_water_mark,
            entry=task.entry,
            current_core=task.core,
        )

    def system_summary(self) -> SystemSummary:
        tasks = [self.summarize_task(value) for value in self.iter_tasks()]
        current_addresses = {address for _core, address in current_tasks(self.layout)}
        current = next(
            (task.name for task in tasks if task.address in current_addresses), None
        )
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
        return SystemSummary(
            kernel_version=(
                ".".join(map(str, self.layout.version))
                if self.layout.version is not None
                else "unknown"
            ),
            current_task=current,
            task_count=system_value("uxCurrentNumberOfTasks") or len(tasks),
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
            heap_summary="unavailable",
        )
