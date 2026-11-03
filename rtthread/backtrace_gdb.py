"""GDB-facing RT-Thread backtrace orchestration (no target writes).

Pure saved-context and metadata parsing lives in :mod:`rtthread.backtrace`
and :mod:`core.unwind`; this module resolves one thread, reads live or saved
registers through GDB and converts the bounded result to the adapter model.
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
    source_location_at,
    value_address,
)
from core.stack_frame_parser import StackContextSelection
from core.unwind import (
    ElfImage,
    SeedFrame,
    UnwindMetadata,
    unwind_chain,
)
from core.unwind_arm_gdb import (
    cached_unwind_metadata,
)
from core.unwind_arm_gdb import (
    live_seed as _core_live_seed,
)
from rtthread.adapter import value_to_thread
from rtthread.backtrace import decode_saved_context as decode_backtrace_context
from rtthread.layout import RT_THREAD_STACK_FILL
from rtthread.navigation import find_thread, get_current_thread
from rtthread.stack_frame_profile import select_context
from rtthread.stackpc import SavedPcLayout, selected_saved_pc_layout

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


def _unwind_metadata(adapter) -> UnwindMetadata | None:
    """Parse the loaded image's unwind tables once per ELF path."""
    return cached_unwind_metadata(adapter, path_reader=loaded_object_path)


_context_cache: tuple[str, StackContextSelection] | None = None


def _backtrace_context() -> StackContextSelection:
    """Resolve ISA, ABI and the actual compiled libcpu save convention."""
    global _context_cache
    path = loaded_object_path()
    if path is None:
        return StackContextSelection(None, "unknown", None, "port-undetermined")
    if _context_cache is not None and _context_cache[0] == path:
        return _context_cache[1]
    elf = ElfImage.from_path(path)
    if elf is None:
        return StackContextSelection(None, "unknown", None, "port-undetermined")
    result = select_context(elf)
    _context_cache = (path, result)
    return result


def reset_context_cache() -> None:
    """Drop the image-derived save-profile decision when GDB replaces the image."""
    global _context_cache
    _context_cache = None


def saved_pc_setup(adapter, gdb_available: bool) -> tuple[SavedPcLayout, int] | None:
    """Resolve an image-proven saved-PC layout for the adapter session."""
    if not gdb_available:
        return None
    if not adapter._saved_pc_resolved:
        selection = _backtrace_context()
        if selection.isa in ("riscv64", "armv7-a"):
            switch = eval_identifier("rt_hw_context_switch")
            exit_path = (
                eval_identifier("rt_hw_context_switch_exit")
                if selection.isa == "armv7-a"
                else None
            )
            pointer_size = arch_or_default()[0] if selection.isa == "armv7-a" else 8
            adapter._saved_pc = selected_saved_pc_layout(
                selection,
                adapter.version,
                pointer_size,
                value_address(switch) if switch is not None else None,
                has_exit_path=exit_path is not None,
            )
        adapter._saved_pc_resolved = True
    return adapter._saved_pc


def reset_adapter(adapter) -> None:
    """Drop image-derived decisions and unwind metadata for one session."""
    reset_context_cache()
    adapter._saved_pc = None
    adapter._saved_pc_resolved = False
    adapter._unwind_cache = None


def _live_seed(arch: str, root_pc: int | None) -> SeedFrame | None:
    """Read the selected core's live registers without driving the target."""
    return _core_live_seed(
        arch,
        root_pc=root_pc,
        require_exception_registers=False,
        gdb_module=gdb,
    )


def _stack_growth(low: int, high: int) -> bool | None:
    """Infer growth direction from bounded fill sentinels at valid stack edges."""
    size = high - low
    if size <= 0:
        return None
    edge = min(size, 16)
    low_edge = read_bytes(low, edge)
    high_edge = read_bytes(high - edge, edge)
    if (
        low_edge is None
        or high_edge is None
        or len(low_edge) != edge
        or len(high_edge) != edge
    ):
        return None
    fill = bytes([RT_THREAD_STACK_FILL]) * edge
    low_untouched = low_edge == fill
    high_untouched = high_edge == fill
    if low_untouched == high_untouched:
        return None
    return high_untouched


def backtrace(adapter, target: str, max_depth: int = 32) -> BacktraceReport:
    """Unwind one RT-Thread thread from its saved or live context."""
    value = find_thread(target, adapter.layout)
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
            messages=[f"thread {target!r} was not found in the registry"],
        )
    selection = _backtrace_context()
    thread = value_to_thread(value, adapter.layout, collect_stack_metrics=False)
    context = selection.context
    architecture = selection.isa or "unknown"
    arch = selection.family
    if context is None or not context.verified:
        return BacktraceReport(
            task=thread.name or target,
            address=thread.address,
            architecture=architecture,
            method="none",
            source="none",
            complete=False,
            stop_reason=selection.reason,
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=["no verified libcpu save profile matches the image evidence"],
            abi=selection.abi,
        )
    low = thread.stack_addr if thread.stack_addr else None
    high = low + thread.stack_size if low is not None and thread.stack_size else None
    if low is None or high is None:
        return BacktraceReport(
            task=thread.name or target,
            address=thread.address,
            architecture=architecture,
            method="none",
            source="none",
            complete=False,
            stop_reason="stack-bounds-unavailable",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=["the thread records no usable stack address and size"],
            abi=selection.abi,
        )
    grows_up = thread.stack_grows_up
    if grows_up is None:
        grows_up = _stack_growth(low, high)
    if grows_up is not False:
        reason = (
            "unsupported-stack-direction" if grows_up else "stack-direction-unavailable"
        )
        message = (
            "stack growth direction is upward; only downward stacks are supported"
            if grows_up
            else "stack growth direction cannot be proven from configuration or fill sentinels"
        )
        return BacktraceReport(
            task=thread.name or target,
            address=thread.address,
            architecture=architecture,
            method="none",
            source="none",
            complete=False,
            stop_reason=reason,
            truncated=False,
            max_depth=max_depth,
            bounds="recorded",
            frames=[],
            messages=[message],
            abi=selection.abi,
        )
    current = get_current_thread()
    is_current = current is not None and value_address(current) == thread.address
    if is_current:
        selected = 0
        if gdb is not None:
            try:
                selected = gdb.selected_thread().num - 1
            except (gdb.error, AttributeError, RuntimeError):
                selected = 0
        if thread.oncpu is not None and thread.oncpu != selected:
            return BacktraceReport(
                task=thread.name or target,
                address=thread.address,
                architecture=architecture,
                method="none",
                source="current",
                complete=False,
                stop_reason="smp-no-context",
                truncated=False,
                max_depth=max_depth,
                bounds="recorded",
                frames=[],
                messages=[
                    f"thread runs on core {thread.oncpu}; the selected core "
                    f"{selected} has no saved context for it"
                ],
                abi=selection.abi,
            )
        seed = _live_seed(arch, thread.entry or None)
        if seed is None:
            return BacktraceReport(
                task=thread.name or target,
                address=thread.address,
                architecture=architecture,
                method="none",
                source="current",
                complete=False,
                stop_reason="live-registers-unavailable",
                truncated=False,
                max_depth=max_depth,
                bounds="recorded",
                frames=[],
                messages=["the selected core's registers are not readable"],
                abi=selection.abi,
            )
    else:
        if thread.oncpu is not None:
            return BacktraceReport(
                task=thread.name or target,
                address=thread.address,
                architecture=architecture,
                method="none",
                source="saved",
                complete=False,
                stop_reason="smp-no-context",
                truncated=False,
                max_depth=max_depth,
                bounds="recorded",
                frames=[],
                messages=[
                    f"thread is running on core {thread.oncpu} and has no saved context"
                ],
                abi=selection.abi,
            )
        exit_pc = None
        exit_value = eval_identifier("rt_thread_exit")
        if exit_value is not None:
            exit_pc = value_address(exit_value)
        seed, reason = decode_backtrace_context(
            context,
            thread.sp,
            read_bytes,
            endian=arch_or_default()[1],
            entry=thread.entry or None,
            stack_low=low,
            stack_high=high,
        )
        if seed is None:
            return BacktraceReport(
                task=thread.name or target,
                address=thread.address,
                architecture=architecture,
                method="none",
                source="saved",
                complete=False,
                stop_reason=reason,
                truncated=False,
                max_depth=max_depth,
                bounds="recorded",
                frames=[],
                messages=[
                    "the saved context is not readable or not proven"
                    + (f" (exit {exit_pc:#x})" if exit_pc else "")
                ],
                abi=selection.abi,
            )
    exit_value = None
    for exit_name in ("rt_thread_exit", "_thread_exit"):
        exit_value = eval_identifier(exit_name)
        if exit_value is not None:
            break
    if seed.source != "unstarted" and exit_value is not None:
        seed = replace(
            seed,
            root_pc=value_address(exit_value),
            root_reason="task-exit",
        )
    metadata = _unwind_metadata(adapter)
    result = unwind_chain(
        seed,
        metadata,
        read=read_bytes,
        max_depth=max_depth,
        stack_low=low,
        stack_high=high,
        allow_frame_pointer=True,
    )
    frames = [
        BacktraceFrame(
            index=item.index,
            pc=item.pc,
            method=item.method,
            symbol=lookup_symbol_at(item.address_in_block),
            source=source_location_at(item.address_in_block),
        )
        for item in result.frames
    ]
    method = next(
        (item.method for item in result.frames if item.method != "seed"), "seed"
    )
    messages = []
    if metadata is None:
        messages.append("no ELF unwind metadata could be read")
    return BacktraceReport(
        task=thread.name or target,
        address=thread.address,
        architecture=architecture,
        method=method,
        source=seed.source,
        complete=result.complete,
        stop_reason=result.stop_reason,
        truncated=result.truncated,
        max_depth=max_depth,
        bounds="recorded",
        frames=frames,
        messages=messages,
        abi=selection.abi,
    )
