"""Stable RTOS-neutral GDB convenience functions."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.adapter_api import active
from gdr.gdb_bridge import make_pointer_array, read_cstring

_registered_functions: set[str] = set()


if gdb is not None:

    class GdrTaskFunction(gdb.Function):
        """Return a target-native task value by name.

        Usage: ``p $gdr_task("worker")``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_task")

        def invoke(self, name):
            adapter = active()
            if adapter is None:
                return gdb.Value(0)
            return adapter.find_task(read_cstring(name) or "") or gdb.Value(0)

    class GdrTasksFunction(gdb.Function):
        """Return a target-native pointer array for all task values.

        Usage: ``p $gdr_tasks()[0]``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_tasks")

        def invoke(self):
            adapter = active()
            return (
                make_pointer_array(list(adapter.iter_tasks()))
                if adapter
                else gdb.Value(0)
            )

    class GdrObjectFunction(gdb.Function):
        """Return a target-native object by semantic kind and name.

        Usage: ``p $gdr_object("semaphore", "work_sem")``. An adapter
        returns zero when that kind is not reliably enumerable on its RTOS.
        """

        def __init__(self) -> None:
            super().__init__("gdr_object")

        def invoke(self, kind, name):
            adapter = active()
            if adapter is None:
                return gdb.Value(0)
            result = adapter.find_object(
                read_cstring(kind) or "", read_cstring(name) or ""
            )
            return result if result is not None else gdb.Value(0)


def register_functions() -> None:
    """Register generic function names, resuming after a partial failure."""
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    functions = (
        ("gdr_task", GdrTaskFunction),
        ("gdr_tasks", GdrTasksFunction),
        ("gdr_object", GdrObjectFunction),
    )
    for name, function_type in functions:
        if name in _registered_functions:
            continue
        function_type()
        _registered_functions.add(name)
