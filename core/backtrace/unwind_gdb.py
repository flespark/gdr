"""GDB-facing pre-scheduler and RTOS-agnostic live stack backtraces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ..adapter import BacktraceReport, live_boot_backtrace_report
from ..elf import (
    RISCV_CONVENTION,
    Arch,
    Cpu,
    ElfImage,
    Reader,
    cpu_of,
)
from ..gdb_bridge import (
    linker_symbol_address,
    loaded_object_path,
    lookup_symbol_at,
    read_bytes,
    source_location_at,
)
from .unwind import (
    SeedFrame,
    UnwindFrame,
    UnwindMetadata,
    UnwindResult,
    unwind_chain,
)
from .unwind_segments import SegmentTransition, StackSegment

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

RISCV_LIVE_REGISTERS: dict[int, str] = dict(RISCV_CONVENTION.live_registers)

_boot_metadata_cache: tuple[str, tuple[Cpu, UnwindMetadata | None]] | None = None


def reset_boot_metadata_cache() -> None:
    """Drop cached ELF CPU/metadata used by RTOS-agnostic live backtraces."""
    global _boot_metadata_cache
    _boot_metadata_cache = None


def cached_unwind_metadata(
    adapter: Any,
    *,
    path_reader: Callable[[], str | None] = loaded_object_path,
) -> UnwindMetadata | None:
    """Parse the loaded image's unwind tables once per ELF path."""
    path = path_reader()
    if path is None:
        return None
    cache = getattr(adapter, "_unwind_cache", None)
    if cache is not None and cache[0] == path:
        return cache[1]
    elf = ElfImage.from_path(path)
    metadata = UnwindMetadata.from_elf(elf) if elf is not None else None
    adapter._unwind_cache = (path, metadata)
    return metadata


def _boot_image_info(
    *,
    path_reader: Callable[[], str | None] = loaded_object_path,
) -> tuple[str | None, Cpu | None, UnwindMetadata | None]:
    global _boot_metadata_cache
    path = path_reader()
    if path is None:
        return None, None, None
    if _boot_metadata_cache is not None and _boot_metadata_cache[0] == path:
        cpu, metadata = _boot_metadata_cache[1]
        return path, cpu, metadata
    elf = ElfImage.from_path(path)
    if elf is None:
        return path, None, None
    cpu = cpu_of(elf)
    metadata = UnwindMetadata.from_elf(elf)
    _boot_metadata_cache = (path, (cpu, metadata))
    return path, cpu, metadata


def live_seed_detail(
    arch: str,
    isa: str | None = None,
    *,
    root_pc: int | None = None,
    require_exception_registers: bool = True,
    gdb_module: Any = None,
    snapshot_reader: Callable[[], tuple[Any, str]] | None = None,
) -> tuple[SeedFrame | None, str]:
    """Read the selected core's live registers without driving the target.

    ``isa`` is the selected profile evidence. A live handler seed requires it
    so architecture-specific exception-number rules are applied only to the
    matching profile; an unknown profile is refused rather than assumed.
    """
    active_gdb = gdb if gdb_module is None else gdb_module
    if arch.startswith("arm"):
        from .unwind_arm import arm_live_seed_detail

        return arm_live_seed_detail(
            isa,
            root_pc=root_pc,
            require_exception_registers=require_exception_registers,
            gdb_module=active_gdb,
            snapshot_reader=snapshot_reader,
        )
    if active_gdb is None:
        return None, "live-registers-unavailable"
    mask = (
        (1 << 32) - 1
        if isa == "riscv32"
        else (1 << 64) - 1
        if isa == "riscv64"
        else None
    )
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc())
        sp = int(frame.read_register("sp"))
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    if mask is not None:
        pc &= mask
        sp &= mask
    registers: dict[int, int] = {}
    for register, name in RISCV_LIVE_REGISTERS.items():
        try:
            value = int(frame.read_register(name))
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):
            return None, "live-registers-unavailable"
        registers[register] = value & mask if mask is not None else value
    registers[2] = sp
    return (
        SeedFrame(
            pc=pc,
            sp=sp,
            regs=registers,
            source="current",
            root_pc=root_pc,
        ),
        "ok",
    )


def live_seed(
    arch: str,
    isa: str | None = None,
    *,
    root_pc: int | None = None,
    require_exception_registers: bool = True,
    gdb_module: Any = None,
) -> SeedFrame | None:
    """Compatibility wrapper returning only the decoded live seed."""
    seed, _reason = live_seed_detail(
        arch,
        isa,
        root_pc=root_pc,
        require_exception_registers=require_exception_registers,
        gdb_module=gdb_module,
    )
    return seed


@dataclass(frozen=True)
class SchedulerHandlerContext:
    """Live handler MSP frame context captured at a verified scheduler stage."""

    pc: int
    sp: int
    registers: dict[int, int]
    exception_number: int
    exception_bounds: tuple[int, int]
    return_pc: int | None
    stage_name: str
    transition_value: int


def build_arm_scheduler_handler(
    snapshot: Any,
    *,
    stage_name: str,
    bounds: tuple[int, int],
    return_pc: int | None = None,
) -> SchedulerHandlerContext:
    """Capture active M-profile handler MSP state for scheduler-stage unwinding."""
    from .unwind_arm import XPSR_IPSR_MAX_MASK

    lr_val = snapshot.registers.get(14, 0) & 0xFFFFFFFF
    exc_value = (
        lr_val
        if lr_val
        in (0xFFFFFFE1, 0xFFFFFFE9, 0xFFFFFFED, 0xFFFFFFF1, 0xFFFFFFF9, 0xFFFFFFFD)
        else 0xFFFFFFFD
    )
    return SchedulerHandlerContext(
        pc=snapshot.pc & ~1,
        sp=snapshot.sp,
        registers=dict(snapshot.registers),
        exception_number=snapshot.xpsr & XPSR_IPSR_MAX_MASK,
        exception_bounds=bounds,
        return_pc=return_pc,
        stage_name=stage_name,
        transition_value=exc_value,
    )


def unwind_scheduler_stage(
    handler: SchedulerHandlerContext,
    *,
    task_seed: SeedFrame,
    task_segment: StackSegment,
    metadata: UnwindMetadata | None,
    read: Reader,
    max_depth: int,
    allow_frame_pointer: bool = True,
    evidence: str = "",
    unwinder: Callable[..., UnwindResult] = unwind_chain,
) -> UnwindResult:
    """Unwind active scheduler handler frame(s) and bridge into the task stack."""
    exception_segment = StackSegment(
        ident=f"exception:{handler.exception_number}",
        owner=f"exception:{handler.exception_number}",
        low=handler.exception_bounds[0],
        high=handler.exception_bounds[1],
        source="scheduler-msp",
        stack="main",
        kind="exception",
    )
    segments: tuple[StackSegment, ...] = (task_segment, exception_segment)
    if max_depth <= 0:
        return UnwindResult(
            (),
            "max-depth",
            False,
            True,
            task_seed.source,
            "seed",
            segments=segments,
        )
    if handler.return_pc is None:
        if (
            handler.pc <= 0
            or (metadata is not None and not metadata.elf.is_code(handler.pc))
            or not exception_segment.contains(handler.sp, 0)
        ):
            return UnwindResult(
                (),
                "seed-pc-outside-code"
                if handler.pc <= 0
                or (metadata is not None and not metadata.elf.is_code(handler.pc))
                else "seed-stack-underflow",
                False,
                False,
                task_seed.source,
                "seed",
                segments=segments,
            )
        handler_frames: tuple[UnwindFrame, ...] = (
            UnwindFrame(0, handler.pc, "seed", segment=exception_segment.ident),
        )
        handler_method = "seed"
    else:
        handler_seed = SeedFrame(
            pc=handler.pc,
            sp=handler.sp,
            regs=dict(handler.registers),
            source="current",
            root_pc=handler.return_pc,
            root_reason="scheduler-switch-return",
        )
        handler_result = unwinder(
            handler_seed,
            metadata,
            read=read,
            max_depth=max_depth,
            stack_low=handler.exception_bounds[0],
            stack_high=handler.exception_bounds[1],
            allow_frame_pointer=allow_frame_pointer,
            segments=(exception_segment,),
            current_segment=exception_segment.ident,
        )
        if not handler_result.complete:
            return UnwindResult(
                frames=handler_result.frames,
                stop_reason=handler_result.stop_reason,
                complete=False,
                truncated=handler_result.truncated,
                seed_source=task_seed.source,
                method=handler_result.method,
                segments=segments,
                transitions=handler_result.transitions,
            )
        handler_frames = handler_result.frames
        handler_method = (
            handler_result.frames[1].method
            if len(handler_result.frames) > 1
            else handler_result.method
        )

    remaining = max_depth - len(handler_frames)
    if remaining <= 0:
        return UnwindResult(
            frames=handler_frames,
            stop_reason="max-depth",
            complete=False,
            truncated=True,
            seed_source=task_seed.source,
            method=handler_method,
            segments=segments,
        )

    task_result = unwinder(
        task_seed,
        metadata,
        read=read,
        max_depth=remaining,
        stack_low=task_segment.low,
        stack_high=task_segment.high,
        allow_frame_pointer=allow_frame_pointer,
        segments=(task_segment,),
        current_segment=task_segment.ident,
    )
    if not task_result.frames:
        return UnwindResult(
            frames=handler_frames,
            stop_reason=task_result.stop_reason,
            complete=False,
            truncated=task_result.truncated,
            seed_source=task_seed.source,
            method=handler_method,
            segments=segments,
        )
    offset = len(handler_frames)
    first_task = task_result.frames[0]
    bridged_first = UnwindFrame(
        index=offset,
        pc=first_task.pc,
        method="exception",
        lookup_pc=first_task.pc,
        segment=first_task.segment or task_segment.ident,
        evidence=first_task.evidence,
    )
    tail_frames = tuple(
        replace(frame, index=offset + frame.index) for frame in task_result.frames[1:]
    )
    transition = SegmentTransition(
        from_segment=exception_segment.ident,
        to_segment=task_segment.ident,
        method="exception",
        source=f"scheduler-{handler.stage_name}",
        pc=first_task.pc,
        sp=task_seed.sp,
        value=handler.transition_value,
        exact_pc=True,
        evidence=evidence,
    )
    return UnwindResult(
        frames=(*handler_frames, bridged_first, *tail_frames),
        stop_reason=task_result.stop_reason,
        complete=task_result.complete,
        truncated=task_result.truncated,
        seed_source=task_seed.source,
        method="exception",
        segments=segments,
        transitions=(transition, *task_result.transitions),
    )


def unwind_task_or_scheduler_stage(
    seed: SeedFrame,
    metadata: UnwindMetadata | None,
    *,
    read: Reader,
    max_depth: int,
    stack_low: int,
    stack_high: int,
    scheduler_handler: SchedulerHandlerContext | None,
    segments: tuple[StackSegment, ...] | None,
    current_segment: str | None,
    allow_frame_pointer: bool = True,
    exception_detector: Any = None,
    exception_hook: Any = None,
    exception_guard: Any = None,
    stkalign: bool = True,
    evidence: str = "",
    unwinder: Callable[..., UnwindResult] = unwind_chain,
) -> UnwindResult:
    """Dispatch task unwind either through a live scheduler handler bridge or directly."""
    if scheduler_handler is not None and segments is not None:
        return unwind_scheduler_stage(
            scheduler_handler,
            task_seed=seed,
            task_segment=segments[0],
            metadata=metadata,
            read=read,
            max_depth=max_depth,
            allow_frame_pointer=allow_frame_pointer,
            evidence=evidence,
            unwinder=unwinder,
        )
    return unwinder(
        seed,
        metadata,
        read=read,
        max_depth=max_depth,
        stack_low=stack_low,
        stack_high=stack_high,
        allow_frame_pointer=allow_frame_pointer,
        segments=segments,
        current_segment=current_segment,
        exception_detector=exception_detector,
        exception_hook=exception_hook,
        exception_guard=exception_guard,
        stkalign=stkalign,
    )


def arm_boot_state(
    isa: str | None,
    *,
    metadata: UnwindMetadata | None = None,
    scheduler_symbols: frozenset[str] = frozenset(),
    snapshot_reader: Callable[[], tuple[Any, str]] | None = None,
    gdb_module: Any = None,
) -> str:
    """Classify an Arm M-profile stop as ``scheduler-symbol``, ``pre-first-task``, or ``other``."""
    if isa not in (
        "armv6-m",
        "armv7-m",
        "armv7e-m",
        "armv8-m.baseline",
        "armv8-m.mainline",
    ):
        return "other"
    from .unwind_arm import XPSR_IPSR_MAX_MASK, arm_snapshot

    active_gdb = gdb if gdb_module is None else gdb_module
    snapshot, _reason = (
        snapshot_reader()
        if snapshot_reader is not None
        else arm_snapshot(gdb_module=active_gdb)
    )
    if snapshot is None:
        return "other"
    if metadata is not None and scheduler_symbols:
        symbol = metadata.elf.function_containing(snapshot.pc & ~1)
        if symbol is not None and symbol.name in scheduler_symbols:
            return "scheduler-symbol"
    if (
        not (snapshot.xpsr & XPSR_IPSR_MAX_MASK)
        and snapshot.msp > 0
        and snapshot.sp == snapshot.msp
        and snapshot.psp <= 0
    ):
        return "pre-first-task"
    return "other"


def _resolve_boot_riscv_bounds(
    *,
    seed: SeedFrame,
    elf: ElfImage | None,
    hart_id: int,
    symbol_lookup: Callable[[str], int | None],
) -> tuple[int, int, str] | None:
    from .unwind_arm import MAX_INFERRED_IRQ_STACK_BYTES
    from .unwind_riscv import resolve_riscv_irq_stack_bounds_detail

    bounds = resolve_riscv_irq_stack_bounds_detail(
        symbol_lookup=symbol_lookup,
        elf=elf,
        hart_id=hart_id,
    )
    if bounds is not None:
        return bounds
    for name in (
        "_stack_top",
        "_estack",
        "__stack_end__",
        "__freertos_irq_stack_top",
    ):
        top = symbol_lookup(name)
        if top is None and elf is not None:
            top = elf.symbol_address(name)
        if (
            top is not None
            and 0 < seed.sp <= top
            and top - seed.sp <= MAX_INFERRED_IRQ_STACK_BYTES
        ):
            return seed.sp, top, f"sp:live..{name}"
    return None


def live_boot_unwind(
    arch: Arch,
    isa: str | None,
    metadata: UnwindMetadata | None,
    *,
    max_depth: int = 32,
    hart_id: int = 0,
    gdb_module: Any = None,
    read: Reader | None = None,
    symbol_lookup: Callable[[str], int | None] | None = None,
    arm_snapshot_reader: Callable[[], tuple[Any, str]] | None = None,
) -> tuple[UnwindResult | None, str, str, str]:
    """Unwind the halted core's current pre-scheduler or bare-metal stack."""
    from .unwind_arm import (
        XPSR_IPSR_MAX_MASK,
        arm_snapshot,
        resolve_arm_irq_stack_bounds_detail,
    )

    active_gdb = gdb if gdb_module is None else gdb_module
    reader = read_bytes if read is None else read
    lookup = linker_symbol_address if symbol_lookup is None else symbol_lookup
    elf = metadata.elf if metadata is not None else None
    m_profile = isa in (
        "armv6-m",
        "armv7-m",
        "armv7e-m",
        "armv8-m.baseline",
        "armv8-m.mainline",
    )

    if arch == "arm" and m_profile:
        snapshot, snap_reason = (
            arm_snapshot_reader()
            if arm_snapshot_reader is not None
            else arm_snapshot(gdb_module=active_gdb)
        )
        if snapshot is None:
            return (
                None,
                snap_reason,
                "the selected core's exception/stack registers are unavailable",
                "unknown",
            )
        if snapshot.xpsr & XPSR_IPSR_MAX_MASK:
            return (
                None,
                "scheduler-not-running",
                "an M-profile exception handler is active before the scheduler is started",
                "unknown",
            )
        if snapshot.msp <= 0 or snapshot.sp != snapshot.msp:
            return (
                None,
                "live-stack-selection-invalid",
                "the live pre-scheduler SP is not the active MSP",
                "unknown",
            )
        seed = SeedFrame(
            pc=snapshot.pc & ~1,
            sp=snapshot.sp,
            regs=dict(snapshot.registers),
            source="current",
        )
        bounds = resolve_arm_irq_stack_bounds_detail(
            symbol_lookup=lookup,
            read=reader,
            isa=isa,
            elf=elf,
            live_msp=seed.sp,
        )
    elif arch == "arm":
        seed_or_none, seed_reason = live_seed_detail(
            "arm",
            isa,
            require_exception_registers=False,
            gdb_module=active_gdb,
        )
        if seed_or_none is None:
            return (
                None,
                seed_reason,
                "the selected core's live registers are unavailable",
                "unknown",
            )
        if isa == "armv7-a":
            if active_gdb is None:
                return (
                    None,
                    "live-registers-unavailable",
                    "the selected core's CPSR is unavailable",
                    "unknown",
                )
            try:
                cpsr = int(active_gdb.newest_frame().read_register("cpsr"))
            except (active_gdb.error, AttributeError, RuntimeError, TypeError):
                return (
                    None,
                    "live-registers-unavailable",
                    "the selected core's CPSR is unavailable",
                    "unknown",
                )
            if cpsr & 0x1F not in (0x13, 0x1F):
                return (
                    None,
                    "unsupported-exception-model",
                    "pre-scheduler Armv7-A unwind requires SVC or System mode",
                    "unknown",
                )
        seed = seed_or_none
        bounds = resolve_arm_irq_stack_bounds_detail(
            symbol_lookup=lookup,
            read=reader,
            isa=isa,
            elf=elf,
            live_msp=seed.sp,
        )
    elif arch == "riscv":
        seed_or_none, seed_reason = live_seed_detail(
            "riscv",
            isa,
            gdb_module=active_gdb,
        )
        if seed_or_none is None:
            return (
                None,
                seed_reason,
                "the selected core's live registers are unavailable",
                "unknown",
            )
        seed = seed_or_none
        bounds = _resolve_boot_riscv_bounds(
            seed=seed,
            elf=elf,
            hart_id=hart_id,
            symbol_lookup=lookup,
        )
    else:
        return (
            None,
            "unsupported-architecture",
            f"pre-scheduler live unwind is unsupported for {arch!r}",
            "unknown",
        )

    if bounds is None:
        return (
            None,
            "stack-bounds-unavailable",
            "live boot stack bounds could not be resolved",
            "unknown",
        )
    low, high, _ = bounds
    result = unwind_chain(
        seed,
        metadata,
        read=reader,
        max_depth=max_depth,
        stack_low=low,
        stack_high=high,
        allow_frame_pointer=isa != "armv7-a",
    )
    return result, result.stop_reason, "", "recorded"


def live_backtrace(
    max_depth: int = 32,
    *,
    path_reader: Callable[[], str | None] = loaded_object_path,
) -> BacktraceReport:
    """Unwind the halted core from ELF attributes, metadata, and live registers."""
    path, cpu, metadata = _boot_image_info(path_reader=path_reader)
    if path is None or cpu is None or metadata is None:
        message = (
            "no symbol file loaded; run `file <elf>` first"
            if path is None
            else f"cannot read ELF metadata from {path}"
        )
        return BacktraceReport(
            task="<current>",
            address=0,
            architecture="unknown",
            method="none",
            source="current",
            complete=False,
            stop_reason="no-unwind-info",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=[message],
            abi=None,
        )
    return live_boot_backtrace_report(
        architecture=cpu.isa or "unknown",
        abi=cpu.abi,
        family=cpu.family,
        max_depth=max_depth,
        metadata=metadata,
        symbol_lookup=lookup_symbol_at,
        source_lookup=source_location_at,
        linker_lookup=linker_symbol_address,
        read=read_bytes,
        hart_id=0,
    )
