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
    linker_symbol_address,
    loaded_object_path,
    lookup_symbol_at,
    read_bytes,
    read_int,
    source_location_at,
    value_address,
)
from core.layout import member_offset
from core.stack_frame_parser import StackContextSelection
from core.unwind import (
    ElfImage,
    SeedFrame,
    UnwindMetadata,
    cached_unwind_metadata,
    unwind_chain,
)
from core.unwind import (
    live_seed as _core_live_seed,
)
from core.unwind import live_seed_detail as _core_live_seed_detail
from core.unwind_arm import (
    ArmSnapshot,
    arm_ccr_stkalign,
    arm_exception_guard,
    arm_external_exception_limit,
    armv6m_vector_entry,
    armv7m_vector_entry,
    prepare_arm_handler_unwind,
    resolve_arm_irq_stack_bounds,
    resolve_arm_irq_stack_bounds_detail,
    resolve_armv6m_vector_limit,
)
from core.unwind_arm import (
    arm_snapshot as _core_arm_snapshot,
)
from core.unwind_cfi import CfaRule, CfiRow, CfiTable, RegisterRule
from core.unwind_segments import (
    ExceptionHook,
    ExceptionTransitionContext,
    ExceptionTransitionResult,
    StackSegment,
)
from rtthread.adapter import value_to_thread
from rtthread.backtrace import (
    decode_armv7em_saved_context,
    decode_armv8m_mainline_saved_context,
)
from rtthread.backtrace import decode_saved_context as decode_backtrace_context
from rtthread.layout import RT_THREAD_STACK_FILL
from rtthread.navigation import find_thread, get_current_thread
from rtthread.scheduler_seed import SchedulerSeed, build_stage_seed
from rtthread.scheduler_switch import (
    build_profile as build_scheduler_profile,
)
from rtthread.scheduler_switch import (
    classify_stage as classify_scheduler_stage,
)
from rtthread.stack_frame_profile import analyze_hardfault_trampoline, select_context
from rtthread.stackpc import SavedPcLayout, selected_saved_pc_layout

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


def _systick_aware_guard(
    exception_number: int, pc: int, isa: str = "armv7-m"
) -> str | None:
    """Admit SysTick as a proven periodic interrupt; other numbers keep core policy.

    SysTick shares the ordinary handler proof (scheduler ownership, IRQ stack
    bounds, vector entry): its handler is a simple tick increment with no
    scheduling entanglement at entry. FreeRTOS keeps the core refusal because
    its SysTick belongs to the scheduler.
    """
    if exception_number == 15:
        return None
    return arm_exception_guard(exception_number, pc, isa)


def _systick_capacity_guard(max_exception: int, isa: str = "armv7-m"):
    """Capacity guard that admits SysTick alongside the core policy."""

    def guard(exception_number: int, pc: int) -> str | None:
        reason = _systick_aware_guard(exception_number, pc, isa)
        if reason is not None:
            return reason
        if exception_number >= 16 and exception_number > max_exception:
            return "exception-number-unimplemented"
        return None

    return guard


def _apply_hardfault_trampoline(
    seed: SeedFrame,
    metadata: UnwindMetadata | None,
    exception_ident: str,
    base_hook: ExceptionHook,
) -> tuple[UnwindMetadata | None, ExceptionHook]:
    """Attach HardFault_Handler CFI rows and PSP software-frame compensation."""
    if seed.exception_number != 3 or metadata is None:
        return metadata, base_hook
    trampoline = analyze_hardfault_trampoline(metadata.elf)
    if trampoline is None:
        return metadata, base_hook
    unpushed = CfiRow(cfa=CfaRule(register=13, offset=0), return_register=14)
    pushed = CfiRow(
        cfa=CfaRule(register=13, offset=4),
        registers={14: RegisterRule(kind="offset", value=-4)},
        return_register=14,
    )
    trampoline_table = CfiTable.from_rows(
        (
            (trampoline.start, trampoline.push_lr_after_pc, unpushed),
            (trampoline.push_lr_after_pc, trampoline.pop_lr_after_pc, pushed),
            (trampoline.pop_lr_after_pc, trampoline.end, unpushed),
        ),
        address_size=metadata.elf.pointer_size,
        endian=metadata.elf.endian,
    )
    updated_metadata = replace(
        metadata,
        cfi_tables=(trampoline_table, *metadata.cfi_tables),
    )

    def hook(
        context: ExceptionTransitionContext,
    ) -> ExceptionTransitionResult | str | None:
        if (
            context.current_segment.ident != exception_ident
            or context.process_sp is None
            or trampoline.start <= (seed.pc & ~1) < trampoline.msr_psp_after_pc
        ):
            return base_hook(context)
        task_segments = [
            segment
            for segment in context.segments.values()
            if segment.stack == "process" and segment.kind == "task"
        ]
        if len(task_segments) != 1:
            return "exception-target-segment-missing"
        if not task_segments[0].contains(context.process_sp, trampoline.software_bytes):
            return "exception-frame-outside-bounds"
        if trampoline.software_bytes > context.remaining_exception_bytes:
            return "exception-read-budget-exceeded"
        raw = context.read(context.process_sp, trampoline.software_bytes)
        if raw is None or len(raw) != trampoline.software_bytes:
            return "exception-frame-unreadable"
        marker = int.from_bytes(raw[:4], "little")
        if marker != (context.value & 0xFFFFFFFF):
            return "exception-context-unproven"
        saved_r4_r11 = {
            4 + index: int.from_bytes(
                raw[
                    trampoline.r4_offset + 4 * index : trampoline.r4_offset
                    + 4 * (index + 1)
                ],
                "little",
            )
            for index in range(8)
        }
        adjusted = replace(
            context,
            registers={**context.registers, **saved_r4_r11},
            process_sp=context.process_sp + trampoline.software_bytes,
            remaining_exception_bytes=(
                context.remaining_exception_bytes - trampoline.software_bytes
            ),
        )
        result = base_hook(adjusted)
        if isinstance(result, ExceptionTransitionResult):
            return replace(
                result,
                bytes_read=result.bytes_read + trampoline.software_bytes,
                evidence=(
                    *result.evidence,
                    f"rtthread-hardfault-software-frame={trampoline.software_bytes}B",
                ),
            )
        return result

    return updated_metadata, hook


def _apply_switch_return_cfi(
    metadata: UnwindMetadata | None,
    scheduler_decision: SchedulerSeed | None,
) -> UnwindMetadata | None:
    """Attach return-via-LR CFI for the proven ``rt_hw_context_switch`` exit."""
    if (
        metadata is None
        or scheduler_decision is None
        or scheduler_decision.stage != "switch-return"
    ):
        return metadata
    pc = scheduler_decision.seed.pc & ~1
    row = CfiRow(cfa=CfaRule(register=13, offset=0), return_register=14)
    switch_table = CfiTable.from_rows(
        ((pc, pc + 2, row),),
        address_size=metadata.elf.pointer_size,
        endian=metadata.elf.endian,
    )
    return replace(metadata, cfi_tables=(switch_table, *metadata.cfi_tables))


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


def _live_seed_detail(
    arch: str, isa: str, root_pc: int | None
) -> tuple[SeedFrame | None, str]:
    m_profile = isa in ("armv6-m", "armv7-m", "armv7e-m", "armv8-m.mainline")
    seed, reason = _core_live_seed_detail(
        arch,
        isa,
        root_pc=root_pc,
        require_exception_registers=m_profile,
        gdb_module=gdb,
    )
    if seed is None:
        return None, reason
    if isa == "armv7-a":
        try:
            status = int(gdb.newest_frame().read_register("cpsr"))
        except (gdb.error, AttributeError, RuntimeError, TypeError):
            return None, "live-registers-unavailable"
        if status & 0x1F not in (0x13, 0x1F):
            return None, "unsupported-exception-model"
    elif m_profile and not seed.exception_number and seed.sp != seed.process_sp:
        return None, "live-stack-selection-invalid"
    if isa == "armv8-m.mainline":
        try:
            frame = gdb.newest_frame()
            control = int(frame.read_register("control"))
            psplim = int(frame.read_register("psplim"))
        except (gdb.error, AttributeError, RuntimeError, TypeError):
            return None, "live-registers-unavailable"
        tz = read_int(eval_identifier("rt_trustzone_current_context"))
        if tz is None:
            return None, "port-configuration-unavailable"
        if tz != 0 or psplim != 0 or (not seed.exception_number and control != 2):
            return None, "unsupported-extension-context"
    return seed, reason


_SCHEDULER_SYMBOLS = frozenset(
    (
        "rt_hw_context_switch",
        "rt_hw_context_switch_interrupt",
        "PendSV_Handler",
        "rt_hw_context_switch_to",
        "switch_to_thread",
        "pendsv_exit",
    )
)


def _arm_snapshot() -> tuple[ArmSnapshot | None, str]:
    return _core_arm_snapshot(gdb_module=gdb)


def _read_armv8m_regs() -> tuple[int | None, int | None]:
    if gdb is None:
        return None, None
    try:
        frame = gdb.newest_frame()
        return int(frame.read_register("psplim")), int(frame.read_register("control"))
    except (gdb.error, AttributeError, RuntimeError, TypeError):
        return None, None


def _scheduler_decision(
    *,
    architecture: str,
    selection: StackContextSelection,
    elf: ElfImage | None,
    thread,
    current,
    low: int,
    high: int,
) -> SchedulerSeed | tuple[str, str] | None:
    """Resolve active M-profile scheduler transition or fail closed."""
    if architecture not in ("armv6-m", "armv7-m", "armv7e-m", "armv8-m.mainline"):
        return None
    flag = read_int(eval_identifier("rt_thread_switch_interrupt_flag"))
    if flag is None:
        return (
            "scheduler-state-unavailable",
            "rt_thread_switch_interrupt_flag is not readable",
        )
    profile = build_scheduler_profile(elf)
    if profile is None and flag != 0:
        return (
            "scheduler-profile-unverified",
            "compiled M-profile switch handlers do not match a verified profile",
        )
    snapshot, snapshot_reason = _arm_snapshot()
    if snapshot is None:
        if flag != 0:
            return (
                snapshot_reason,
                "live registers are unavailable while a context switch is pending",
            )
        return None
    if profile is None:
        pc = snapshot.pc & ~1
        sym = elf.function_containing(pc) if elf is not None else None
        if sym is not None and sym.name in _SCHEDULER_SYMBOLS:
            return (
                "scheduler-profile-unverified",
                "compiled M-profile switch handlers do not match a verified profile",
            )
        return None
    stage = classify_scheduler_stage(profile, snapshot.pc)
    if flag == 0 and stage is None:
        return None
    if (
        stage is not None
        and stage.function == "PendSV_Handler"
        and stage.name != "ambiguous"
    ):
        if snapshot.sp != snapshot.msp:
            return (
                "scheduler-handler-mode-unproven",
                "the live SP is not the handler MSP inside PendSV_Handler",
            )
        irq_bounds = resolve_arm_irq_stack_bounds(
            symbol_lookup=linker_symbol_address,
            read=read_bytes,
            isa=architecture,
            elf=elf,
            live_msp=snapshot.msp,
            task_bounds=(low, high),
        )
        if irq_bounds is None:
            return (
                "exception-stack-bounds-unavailable",
                "the interrupt stack span could not be resolved at PendSV_Handler",
            )
        if not irq_bounds[0] <= snapshot.msp <= irq_bounds[1]:
            return (
                "exception-stack-outside-bounds",
                "the live MSP is outside the resolved interrupt stack span",
            )
    stkalign = True if architecture == "armv6-m" else arm_ccr_stkalign(read=read_bytes)
    if stkalign is None:
        return (
            "exception-alignment-unavailable",
            "CCR.STKALIGN is not readable at a scheduler transition",
        )
    from_ptr = read_int(eval_identifier("rt_interrupt_from_thread"))
    to_ptr = read_int(eval_identifier("rt_interrupt_to_thread"))
    sp_offset = member_offset("struct rt_thread", ("sp",))
    psplim, control = (
        _read_armv8m_regs() if architecture == "armv8-m.mainline" else (None, None)
    )
    outcome = build_stage_seed(
        profile,
        selection,
        flag_value=flag,
        from_ptr=from_ptr,
        to_ptr=to_ptr,
        sp_offset=sp_offset,
        thread_addr=thread.address,
        thread_sp=thread.sp,
        thread_entry=thread.entry or None,
        current_addr=value_address(current) if current is not None else None,
        low=low,
        high=high,
        endian=arch_or_default()[1],
        stkalign=stkalign,
        read=read_bytes,
        live_pc=snapshot.pc,
        live_sp=snapshot.sp,
        live_psp=snapshot.psp,
        live_exc=snapshot.xpsr & 0x1FF,
        live_regs=snapshot.registers,
        live_psplim=psplim,
        live_control=control,
    )
    if isinstance(outcome, str):
        return outcome, f"the pending switch state refused unwind ({outcome})"
    return outcome


def _stop_report(
    thread, architecture, abi, max_depth, reason, message, *, source="current"
):
    return BacktraceReport(
        task=thread.name,
        address=thread.address,
        architecture=architecture,
        method="none",
        source=source,
        complete=False,
        stop_reason=reason,
        truncated=False,
        max_depth=max_depth,
        bounds="recorded",
        frames=[],
        messages=[message],
        abi=abi,
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
    metadata = _unwind_metadata(adapter)
    scheduler_decision = _scheduler_decision(
        architecture=architecture,
        selection=selection,
        elf=metadata.elf if metadata is not None else None,
        thread=thread,
        current=current,
        low=low,
        high=high,
    )
    if isinstance(scheduler_decision, tuple):
        return _stop_report(
            thread,
            architecture,
            selection.abi,
            max_depth,
            scheduler_decision[0],
            scheduler_decision[1],
            source="none",
        )
    scheduler_note: str | None = None
    is_current = current is not None and value_address(current) == thread.address
    if scheduler_decision is not None:
        seed = scheduler_decision.seed
        scheduler_note = (
            f"scheduler stage {scheduler_decision.stage} "
            f"({scheduler_decision.owner}): {scheduler_decision.evidence}"
        )
    elif is_current:
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
        seed, live_reason = _live_seed_detail(arch, architecture, thread.entry or None)
        if seed is None:
            return BacktraceReport(
                task=thread.name or target,
                address=thread.address,
                architecture=architecture,
                method="none",
                source="current",
                complete=False,
                stop_reason=live_reason,
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
        if selection.alt_context is not None and architecture in (
            "armv7e-m",
            "armv8-m.mainline",
        ):
            decode_pair = (
                decode_armv7em_saved_context
                if architecture == "armv7e-m"
                else decode_armv8m_mainline_saved_context
            )
            seed, reason = decode_pair(
                thread.sp,
                read_bytes,
                basic=context,
                fpu=selection.alt_context,
                endian=arch_or_default()[1],
                entry=thread.entry or None,
                stack_low=low,
                stack_high=high,
            )
        else:
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
    segments = None
    current_segment = None
    detector = hook = guard = None
    stkalign = True
    fault = None
    if seed.exception_number:
        if seed.process_sp is None or not low <= seed.process_sp <= high:
            return _stop_report(
                thread,
                architecture,
                selection.abi,
                max_depth,
                "exception-task-context-conflict",
                "the live PSP does not belong to the requested thread",
            )
        elf = metadata.elf if metadata is not None else None
        setup = prepare_arm_handler_unwind(
            seed,
            architecture,
            scheduler_running=lambda: current is not None,
            irq_stack_bounds=lambda: resolve_arm_irq_stack_bounds_detail(
                symbol_lookup=linker_symbol_address,
                read=read_bytes,
                isa=architecture,
                elf=elf,
                live_msp=seed.sp,
                task_bounds=(low, high),
            ),
            capacity_reader=(
                lambda: resolve_armv6m_vector_limit(
                    symbol_lookup=linker_symbol_address, read=read_bytes, elf=elf
                )
            )
            if architecture == "armv6-m"
            else (lambda: arm_external_exception_limit(read=read_bytes)),
            stkalign_reader=lambda: arm_ccr_stkalign(read=read_bytes),
            vector_entry_reader=(
                lambda number: armv6m_vector_entry(number, read=read_bytes)
            )
            if architecture == "armv6-m"
            else (lambda number: armv7m_vector_entry(number, read=read_bytes)),
            guard_fn=_systick_aware_guard,
            capacity_guard_factory=_systick_capacity_guard,
            read=read_bytes,
        )
        if isinstance(setup, tuple):
            return _stop_report(thread, architecture, selection.abi, max_depth, *setup)
        seed = setup.seed
        fault = setup.fault
        task_segment = StackSegment(
            ident=f"task:{thread.address:#x}",
            owner=thread.name,
            low=low,
            high=high,
            source="task-recorded",
            stack="process",
            kind="task",
        )
        segments = (task_segment, setup.exception_segment)
        current_segment = setup.exception_segment.ident
        detector, hook, guard = (
            setup.exception_detector,
            setup.exception_hook,
            setup.exception_guard,
        )
        metadata, hook = _apply_hardfault_trampoline(
            seed, metadata, setup.exception_segment.ident, hook
        )
        stkalign = setup.stkalign
    metadata = _apply_switch_return_cfi(metadata, scheduler_decision)
    result = unwind_chain(
        seed,
        metadata,
        read=read_bytes,
        max_depth=max_depth,
        stack_low=low,
        stack_high=high,
        allow_frame_pointer=architecture != "armv7-a",
        segments=segments,
        current_segment=current_segment,
        exception_detector=detector,
        exception_hook=hook,
        exception_guard=guard,
        stkalign=stkalign,
    )
    frames = [
        BacktraceFrame(
            index=item.index,
            pc=item.pc,
            method=item.method,
            symbol=lookup_symbol_at(item.address_in_block),
            source=source_location_at(item.address_in_block),
            segment=item.segment,
        )
        for item in result.frames
    ]
    method = next(
        (item.method for item in result.frames if item.method != "seed"), "seed"
    )
    stop_reason = result.stop_reason
    messages = []
    if scheduler_note is not None:
        messages.append(scheduler_note)
    if fault is not None and fault.precise:
        messages.append(f"synchronous fault {fault.fault}: {fault.evidence}")
        if result.complete:
            stop_reason = "synchronous-fault"
    if metadata is None:
        messages.append("no ELF unwind metadata could be read")
    messages.extend(
        f"frame {item.index}: {evidence}"
        for item in result.frames
        for evidence in item.evidence
    )
    messages.extend(
        f"exception transition {transition.from_segment} -> {transition.to_segment} at {transition.pc:#x} ({transition.evidence})"
        for transition in result.transitions
    )
    return BacktraceReport(
        task=thread.name or target,
        address=thread.address,
        architecture=architecture,
        method=method,
        source=seed.source,
        complete=result.complete,
        stop_reason=stop_reason,
        truncated=result.truncated,
        max_depth=max_depth,
        bounds="recorded",
        frames=frames,
        messages=messages,
        abi=selection.abi,
        segments=list(result.segments),
        transitions=list(result.transitions),
    )
