"""GDB-facing FreeRTOS backtrace orchestration (no target writes).

The pure context layout and ELF/CFI/EHABI parsers live in
:mod:`freertos.backtrace` and :mod:`core.unwind`; this module only resolves
the task, reads live/saved registers through GDB and turns one bounded
:class:`core.unwind.UnwindResult` into the adapter presentation model.
"""

from __future__ import annotations

from dataclasses import replace

from core.adapter import BacktraceFrame, BacktraceReport
from core.gdb_bridge import (
    arch_or_default,
    eval_identifier,
    loaded_object_path,
    lookup_symbol_at,
    read_bytes,
    read_int,
    source_location_at,
    value_address,
)
from core.unwind import ElfImage, SeedFrame, UnwindMetadata, unwind_chain
from freertos.adapter import FreeRtosTask, find_task, value_to_task
from freertos.backtrace import decode_saved_context as decode_backtrace_context
from freertos.navigation import current_tasks, saved_context_bounds, task_state

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


def _unwind_metadata(adapter) -> UnwindMetadata | None:
    """Parse this image's unwind tables once per loaded ELF path."""
    path = loaded_object_path()
    if path is None:
        return None
    if adapter._unwind_cache is not None and adapter._unwind_cache[0] == path:
        return adapter._unwind_cache[1]
    elf = ElfImage.from_path(path)
    metadata = UnwindMetadata.from_elf(elf) if elf is not None else None
    adapter._unwind_cache = (path, metadata)
    return metadata


def _exit_pc() -> int | None:
    """The port's task-return sentinel address, when the image exports it."""
    value = eval_identifier("prvTaskExitError")
    if value is not None:
        return value_address(value) or None
    return read_int(eval_identifier("xTaskReturnAddress"))


def _live_seed(arch: str) -> SeedFrame | None:
    """Read the selected core's live registers without driving the target."""
    if gdb is None:
        return None
    try:
        frame = gdb.newest_frame()
        pc = int(frame.pc())
        sp = int(frame.read_register("sp"))
    except (gdb.error, AttributeError, RuntimeError, TypeError):
        return None
    registers: dict[int, int] = {}
    if arch == "arm":
        for register in range(13):
            try:
                registers[register] = int(frame.read_register(f"r{register}"))
            except (gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
                return None
        registers[13] = sp
        try:
            registers[14] = int(frame.read_register("lr"))
            registers[15] = pc
        except (gdb.error, AttributeError, RuntimeError, TypeError):
            return None
    else:
        names = {
            1: "ra",
            2: "sp",
            8: "s0",
            9: "s1",
            18: "s2",
            19: "s3",
            20: "s4",
            21: "s5",
            22: "s6",
            23: "s7",
            24: "s8",
            25: "s9",
            26: "s10",
            27: "s11",
        }
        for register, name in names.items():
            try:
                registers[register] = int(frame.read_register(name))
            except (gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
                return None
        registers[2] = sp
    return SeedFrame(
        pc=pc & ~1 if arch == "arm" else pc,
        sp=sp,
        regs=registers,
        source="current",
    )


def _task_bounds(task: FreeRtosTask) -> tuple[int | None, int | None, str]:
    """Return ``(low, high, provenance)`` for this task's stack."""
    if task.stack_base <= 0:
        return None, None, "unknown"
    if task.stack_size is not None and task.stack_size > 0:
        return task.stack_base, task.stack_base + task.stack_size, "recorded"
    return task.stack_base, None, "unknown"


def backtrace(adapter, target: str, max_depth: int = 32) -> BacktraceReport:
    """Unwind one FreeRTOS task from its saved or live context."""
    value = find_task(target, adapter.layout)
    if value is None:
        return BacktraceReport(
            task=target,
            address=0,
            architecture="unknown",
            method="none",
            source="none",
            complete=False,
            stop_reason="task-not-found",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=[f"task {target!r} was not found in the scheduler lists"],
        )
    selection = adapter._context_selection()
    _state, core = task_state(value, adapter.layout)
    task = value_to_task(
        value, _state, core, adapter.layout, collect_stack_metrics=False
    )
    context = selection.context
    if context is None or not context.verified:
        return BacktraceReport(
            task=task.name or target,
            address=task.address,
            architecture=selection.isa or "unknown",
            method="none",
            source="none",
            complete=False,
            stop_reason=selection.reason,
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=[
                "no verified save profile matches the image/configuration evidence"
            ],
            abi=selection.abi,
        )
    arch = selection.family
    architecture = selection.isa or "unknown"
    low, high, bounds = _task_bounds(task)
    if low is None or high is None:
        return BacktraceReport(
            task=task.name or target,
            address=task.address,
            architecture=architecture,
            method="none",
            source="none",
            complete=False,
            stop_reason="stack-bounds-unavailable",
            truncated=False,
            max_depth=max_depth,
            bounds=bounds,
            frames=[],
            messages=[
                "the task records no usable stack span; enable configRECORD_STACK_HIGH_ADDRESS"
            ],
            abi=selection.abi,
        )
    running = {address: core for core, address in current_tasks(adapter.layout)}
    current_core = running.get(task.address)
    messages: list[str] = []
    if current_core is not None:
        selected = None
        if gdb is not None:
            try:
                selected = gdb.selected_thread().num - 1
            except (gdb.error, AttributeError, RuntimeError):
                selected = None
        if selected is not None and selected != current_core:
            return BacktraceReport(
                task=task.name or target,
                address=task.address,
                architecture=architecture,
                method="none",
                source="current",
                complete=False,
                stop_reason="smp-no-context",
                truncated=False,
                max_depth=max_depth,
                bounds=bounds,
                frames=[],
                messages=[
                    f"task runs on core {current_core}; the selected core "
                    f"{selected} has no saved context for it"
                ],
                abi=selection.abi,
            )
        seed = _live_seed(arch)
        if seed is None:
            return BacktraceReport(
                task=task.name or target,
                address=task.address,
                architecture=architecture,
                method="none",
                source="current",
                complete=False,
                stop_reason="live-registers-unavailable",
                truncated=False,
                max_depth=max_depth,
                bounds=bounds,
                frames=[],
                messages=["the selected core's registers are not readable"],
                abi=selection.abi,
            )
    else:
        context_low, context_high = (
            saved_context_bounds(value, adapter.layout)
            if context.context_in_tcb
            else (None, None)
        )
        seed, reason = decode_backtrace_context(
            context,
            task.top_of_stack,
            read_bytes,
            endian=arch_or_default()[1],
            stack_low=low,
            stack_high=high,
            context_low=context_low,
            context_high=context_high,
            exit_pc=_exit_pc(),
        )
        if seed is None:
            return BacktraceReport(
                task=task.name or target,
                address=task.address,
                architecture=architecture,
                method="none",
                source="saved",
                complete=False,
                stop_reason=reason,
                truncated=False,
                max_depth=max_depth,
                bounds=bounds,
                frames=[],
                messages=["the saved context is not readable or not proven"],
                abi=selection.abi,
            )
    exit_pc = _exit_pc()
    if seed.source != "unstarted" and exit_pc is not None:
        seed = replace(seed, root_pc=exit_pc, root_reason="task-exit")
    metadata = _unwind_metadata(adapter)
    if metadata is None:
        messages.append("no ELF unwind metadata could be read")
    result = unwind_chain(
        seed,
        metadata,
        read=read_bytes,
        max_depth=max_depth,
        stack_low=low,
        stack_high=high,
        allow_frame_pointer=True,
    )
    frames = []
    for item in result.frames:
        symbol = lookup_symbol_at(item.pc)
        source = source_location_at(item.pc)
        frames.append(
            BacktraceFrame(
                index=item.index,
                pc=item.pc,
                method=item.method,
                symbol=symbol,
                source=source,
            )
        )
    method = next(
        (item.method for item in result.frames if item.method != "seed"), "seed"
    )
    if result.stop_reason == "no-unwind-info" and metadata is not None:
        messages.append(
            "no EHABI/CFI entry covers the stop PC and the frame-pointer "
            "chain did not validate"
        )
    return BacktraceReport(
        task=task.name or target,
        address=task.address,
        architecture=architecture,
        method=method,
        source=seed.source,
        complete=result.complete,
        stop_reason=result.stop_reason,
        truncated=result.truncated,
        max_depth=max_depth,
        bounds=bounds,
        frames=frames,
        messages=messages,
        abi=selection.abi,
    )
