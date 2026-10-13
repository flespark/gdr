"""Stable RTOS-neutral GDB convenience functions."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.adapter_api import active
from gdr.api import chunk_response, dump_response, transport_response
from gdr.gdb_bridge import gdb_function_guard, make_pointer_array, read_cstring

_registered_functions: set[str] = set()
_registered_api_functions: set[str] = set()


if gdb is not None:

    class GdrTaskFunction(gdb.Function):
        """Return a target-native task value by name, address or symbol.

        Usage: ``p $gdr_task("worker")`` or ``p $gdr_task("0x20000000")``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_task")

        @gdb_function_guard
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

        @gdb_function_guard
        def invoke(self):
            adapter = active()
            return (
                make_pointer_array(list(adapter.iter_tasks()))
                if adapter
                else gdb.Value(0)
            )

    class GdrObjectFunction(gdb.Function):
        """Return a target-native object by semantic kind and target.

        Usage: ``p $gdr_object("semaphore", "work_sem")`` or
        ``p $gdr_object("semaphore", "0x20000000")``. An adapter
        returns zero when that kind is not reliably enumerable on its RTOS.
        """

        def __init__(self) -> None:
            super().__init__("gdr_object")

        @gdb_function_guard
        def invoke(self, kind, name):
            adapter = active()
            if adapter is None:
                return gdb.Value(0)
            result = adapter.find_object(
                read_cstring(kind) or "", read_cstring(name) or ""
            )
            return result if result is not None else gdb.Value(0)

    class GdrJsonFunction(gdb.Function):
        """Return one GDR API envelope for a verb as a JSON string.

        Usage: ``p $gdr_json("tasks")``. Small envelopes ride inline;
        larger ones return the chunked-transport meta frame, which the
        caller feeds to ``$gdr_json_chunk``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_json")

        @gdb_function_guard
        def invoke(self, verb, *rest):
            args = [read_cstring(arg) or "" for arg in rest]
            return gdb.Value(transport_response(read_cstring(verb) or "", *args))

    class GdrJsonChunkFunction(gdb.Function):
        """Return one base64 frame of a GDR API envelope.

        Usage: ``p $gdr_json_chunk(0, "tasks")`` for the meta frame, then
        ``p $gdr_json_chunk(1, ...)`` up to the meta's ``total``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_json_chunk")

        @gdb_function_guard
        def invoke(self, index, verb, *rest):
            args = [read_cstring(arg) or "" for arg in rest]
            try:
                chunk_index = int(index)
            except (TypeError, ValueError):
                chunk_index = -1
            return gdb.Value(
                chunk_response(chunk_index, read_cstring(verb) or "", *args)
            )

    class GdrDumpFunction(gdb.Function):
        """Write one GDR API envelope to a file.

        Usage: ``p $gdr_dump("/tmp/gdr-probe.json", "probe")``. Returns the
        number of bytes written, or -1 when the file could not be written: a
        scalar result is what a plugin's file transport needs, because a plugin
        that cannot carry a string through ``evaluate`` can still read a file.
        """

        def __init__(self) -> None:
            super().__init__("gdr_dump")

        @gdb_function_guard
        def invoke(self, path, verb, *rest):
            args = [read_cstring(arg) or "" for arg in rest]
            return gdb.Value(
                dump_response(read_cstring(path) or "", read_cstring(verb) or "", *args)
            )


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


def register_api_functions() -> None:
    """Register the machine-readable API transport functions.

    Kept separate from :func:`register_functions` so the human convenience
    functions and the plugin transport have independent registration
    state; both are called once per adapter setup.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    functions = (
        ("gdr_json", GdrJsonFunction),
        ("gdr_json_chunk", GdrJsonChunkFunction),
        ("gdr_dump", GdrDumpFunction),
    )
    for name, function_type in functions:
        if name in _registered_api_functions:
            continue
        function_type()
        _registered_api_functions.add(name)
