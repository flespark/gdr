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
    section_payloads,
    source_location_at,
    value_address,
)
from core.unwind import (
    ElfImage,
    SeedFrame,
    UnwindMetadata,
    cpu_name_from_attributes,
    unwind_chain,
)
from rtthread.adapter import value_to_thread
from rtthread.backtrace import context_for as context_for_backtrace
from rtthread.backtrace import decode_saved_context as decode_backtrace_context
from rtthread.navigation import find_thread, get_current_thread

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


def _unwind_metadata(adapter) -> UnwindMetadata | None:
    """Parse the loaded image's unwind tables once per ELF path."""
    path = loaded_object_path()
    if path is None:
        return None
    if adapter._unwind_cache is not None and adapter._unwind_cache[0] == path:
        return adapter._unwind_cache[1]
    elf = ElfImage.from_path(path)
    metadata = UnwindMetadata.from_elf(elf) if elf is not None else None
    adapter._unwind_cache = (path, metadata)
    return metadata


def _backtrace_port() -> tuple[str | None, str]:
    """Return ``(port, architecture)`` from ELF machine and ARM attributes."""
    path = loaded_object_path()
    if path is None:
        return None, "unknown"
    elf = ElfImage.from_path(path)
    if elf is None:
        return None, "unknown"
    if elf.machine == 243:
        return ("riscv32" if elf.pointer_size == 4 else "riscv64"), "riscv"
    if elf.machine != 40:
        return None, "unknown"
    try:
        with open(path, "rb") as handle:
            blob = section_payloads(handle.read(), (".ARM.attributes",)).get(
                ".ARM.attributes", b""
            )
    except OSError:
        return None, "arm"
    name = cpu_name_from_attributes(blob)
    return {
        "6-M": "cortex-m0",
        "6S-M": "cortex-m0",
        "7-M": "cortex-m3",
        "8-M.MAIN": "cortex-m33",
    }.get(name or ""), "arm"


def _live_seed(arch: str, root_pc: int | None) -> SeedFrame | None:
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
        for register, name in {
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
        }.items():
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
        root_pc=root_pc,
    )


def backtrace(adapter, target: str, max_depth: int = 32) -> BacktraceReport:
    """Unwind one RT-Thread thread from its saved or live context."""
    value = find_thread(target, adapter.layout)
    if value is None:
        return BacktraceReport(
            task=target,
            address=0,
            port="unknown",
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
    thread = value_to_thread(value, adapter.layout)
    port, arch = _backtrace_port()
    if port is None:
        return BacktraceReport(
            task=thread.name or target,
            address=thread.address,
            port="unknown",
            architecture=arch,
            method="none",
            source="none",
            complete=False,
            stop_reason="port-undetermined",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=["the libcpu port could not be identified from image evidence"],
        )
    context = context_for_backtrace(port)
    if context is None or not context.verified:
        return BacktraceReport(
            task=thread.name or target,
            address=thread.address,
            port=port,
            architecture=arch,
            method="none",
            source="none",
            complete=False,
            stop_reason="unverified-port",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=[
                f"the {port} saved-context layout has no closed-loop fixture; "
                "this port is not claimed as verified"
            ],
        )
    if thread.stack_grows_up:
        return BacktraceReport(
            task=thread.name or target,
            address=thread.address,
            port=port,
            architecture=arch,
            method="none",
            source="none",
            complete=False,
            stop_reason="unsupported-stack-direction",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=[
                "stack growth direction is upward; only downward stacks are supported"
            ],
        )
    low = thread.stack_addr if thread.stack_addr else None
    high = low + thread.stack_size if low is not None and thread.stack_size else None
    if low is None or high is None:
        return BacktraceReport(
            task=thread.name or target,
            address=thread.address,
            port=port,
            architecture=arch,
            method="none",
            source="none",
            complete=False,
            stop_reason="stack-bounds-unavailable",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=["the thread records no usable stack address and size"],
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
                port=port,
                architecture=arch,
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
            )
        seed = _live_seed(arch, thread.entry or None)
        if seed is None:
            return BacktraceReport(
                task=thread.name or target,
                address=thread.address,
                port=port,
                architecture=arch,
                method="none",
                source="current",
                complete=False,
                stop_reason="live-registers-unavailable",
                truncated=False,
                max_depth=max_depth,
                bounds="recorded",
                frames=[],
                messages=["the selected core's registers are not readable"],
            )
    else:
        if thread.oncpu is not None:
            return BacktraceReport(
                task=thread.name or target,
                address=thread.address,
                port=port,
                architecture=arch,
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
                port=port,
                architecture=arch,
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
            symbol=lookup_symbol_at(item.pc),
            source=source_location_at(item.pc),
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
        port=port,
        architecture=arch,
        method=method,
        source=seed.source,
        complete=result.complete,
        stop_reason=result.stop_reason,
        truncated=result.truncated,
        max_depth=max_depth,
        bounds="recorded",
        frames=frames,
        messages=messages,
    )
