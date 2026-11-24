"""GDB-facing FreeRTOS backtrace orchestration (no target writes).

The pure context layout and ELF/CFI/EHABI parsers live in
:mod:`freertos.backtrace.unwind` and :mod:`core.backtrace.unwind`; this module only resolves
the task, reads live/saved registers through GDB, supplies the linker-owned
interrupt-stack span and exception profile gates, and turns one bounded
:class:`core.backtrace.unwind.UnwindResult` into the adapter presentation model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core.adapter import (
    BacktraceReport,
    active,
    build_backtrace_report,
    live_boot_backtrace_report,
)
from core.backtrace.unwind import (
    ElfImage,
    SeedFrame,
    UnwindMetadata,
    unwind_chain,
)
from core.backtrace.unwind_arm import (
    ArmSnapshot as _ArmSnapshot,
)
from core.backtrace.unwind_arm import (
    FaultPrecision,
    prepare_arm_handler_unwind,
    resolve_arm_irq_stack_bounds,
    resolve_arm_irq_stack_bounds_detail,
    resolve_armv6m_vector_limit,
)
from core.backtrace.unwind_arm import (
    arm_capacity_exception_guard as _capacity_exception_guard,
)
from core.backtrace.unwind_arm import (
    arm_ccr_stkalign as _core_ccr_stkalign,
)
from core.backtrace.unwind_arm import (
    arm_exception_guard as _exception_guard,
)
from core.backtrace.unwind_arm import (
    arm_external_exception_limit as _core_external_exception_limit,
)
from core.backtrace.unwind_arm import (
    arm_snapshot as _core_arm_snapshot,
)
from core.backtrace.unwind_arm import (
    armv6m_vector_entry as _core_armv6m_vector_entry,
)
from core.backtrace.unwind_arm import (
    armv7m_vector_entry as _core_armv7m_vector_entry,
)
from core.backtrace.unwind_gdb import (
    arm_boot_state,
    cached_unwind_metadata,
)
from core.backtrace.unwind_gdb import (
    live_seed_detail as _core_live_seed_detail,
)
from core.backtrace.unwind_riscv import (
    RiscvSnapshot as _RiscvSnapshot,
)
from core.backtrace.unwind_riscv import (
    decode_riscv_mcause as _decode_riscv_mcause,
)
from core.backtrace.unwind_riscv import (
    prepare_riscv_handler_unwind,
    resolve_riscv_irq_stack_bounds_detail,
    riscv_fault_precision,
)
from core.backtrace.unwind_riscv import (
    riscv_snapshot as _core_riscv_snapshot,
)
from core.backtrace.unwind_segments import (
    ExceptionDetector,
    ExceptionHook,
    StackSegment,
    TransitionGuard,
)
from core.gdb_bridge import (
    arch_or_default,
    eval_identifier,
    linker_symbol_address,
    loaded_object_path,
    lookup_symbol_at,
    read_bytes,
    read_int,
    source_location_at,
    type_size,
    value_address,
)
from core.stack_frame_parser import StackContextSelection
from freertos.adapter import FreeRtosTask, find_task, value_to_task
from freertos.backtrace.arm_scheduler_switch import (
    build_profile as build_scheduler_profile,
)
from freertos.backtrace.arm_scheduler_switch import classify_stage
from freertos.backtrace.riscv_scheduler_switch import (
    build_riscv_profile as build_riscv_scheduler_profile,
)
from freertos.backtrace.riscv_scheduler_switch import (
    classify_riscv_stage,
    riscv_pc_in_scheduler_symbol,
)
from freertos.backtrace.scheduler_seed import (
    build_riscv_stage_seed,
    build_stage_seed,
    riscv_interrupted_frame_matches,
)
from freertos.backtrace.unwind import (
    decode_cm4f_saved_context,
    decode_cm33_saved_context,
    validate_armv8m_live_registers,
)
from freertos.backtrace.unwind import (
    decode_saved_context as decode_backtrace_context,
)
from freertos.navigation import (
    current_tasks,
    iter_tasks,
    saved_context_bounds,
    task_state,
)
from freertos.stack_frame_profile import (
    analyze_riscv_trap_profile,
    riscv_zero_lr_sentinel,
)

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


def _unwind_metadata(adapter) -> UnwindMetadata | None:
    """Parse this image's unwind tables once per loaded ELF path."""
    return cached_unwind_metadata(adapter, path_reader=loaded_object_path)


def _exit_pc() -> int | None:
    """The port's task-return sentinel address, when the image exports it."""
    value = eval_identifier("prvTaskExitError")
    if value is not None:
        return value_address(value) or None
    return read_int(eval_identifier("xTaskReturnAddress"))


def _target_exit_pc(elf: ElfImage | None) -> int | None:
    return 0 if riscv_zero_lr_sentinel(elf) else _exit_pc()


def _arm_snapshot() -> tuple[_ArmSnapshot | None, str]:
    return _core_arm_snapshot(gdb_module=gdb)


def _riscv_snapshot(*, xlen: int = 32) -> tuple[_RiscvSnapshot | None, str]:
    return _core_riscv_snapshot(xlen=xlen, gdb_module=gdb)


def _riscv_isr_stack_bounds(elf: ElfImage | None) -> tuple[int, int, str] | None:
    def _isr_obj() -> tuple[int, int] | None:
        val = eval_identifier("xISRStack")
        if val is None:
            return None
        addr = value_address(val)
        size = type_size(val.type)
        if addr > 0 and size is not None and size > 0:
            return addr, addr + size
        return None

    return resolve_riscv_irq_stack_bounds_detail(
        symbol_lookup=linker_symbol_address,
        elf=elf,
        stack_object_bounds=_isr_obj,
        section_names=(".irq_stack", ".isr_stack"),
    )


def _live_seed_detail(
    arch: str, isa: str | None = None
) -> tuple[SeedFrame | None, str]:
    return _core_live_seed_detail(
        arch, isa, gdb_module=gdb, snapshot_reader=_arm_snapshot
    )


def _live_seed(arch: str, isa: str | None = None) -> SeedFrame | None:
    seed, _reason = _live_seed_detail(arch, isa)
    return seed


def _task_bounds(task: FreeRtosTask) -> tuple[int | None, int | None, str]:
    """Return ``(low, high, provenance)`` for this task's stack."""
    if task.stack_base <= 0:
        return None, None, "unknown"
    if task.stack_size is not None and task.stack_size > 0:
        return task.stack_base, task.stack_base + task.stack_size, "recorded"
    return task.stack_base, None, "unknown"


def _irq_stack_bounds(
    *,
    isa: str | None = "armv7-m",
    elf: ElfImage | None = None,
    live_msp: int | None = None,
    task_bounds: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    """Return the resolved MSP span, or ``None`` when bounds are unproven."""
    return resolve_arm_irq_stack_bounds(
        symbol_lookup=linker_symbol_address,
        read=read_bytes,
        isa=isa,
        elf=elf,
        live_msp=live_msp,
        task_bounds=task_bounds,
    )


_DEFAULT_IRQ_STACK_BOUNDS = _irq_stack_bounds


def _ccr_stkalign() -> bool | None:
    return _core_ccr_stkalign(read=read_bytes)


def _external_exception_limit(*, elf: ElfImage | None = None) -> int | None:
    return _core_external_exception_limit(
        read=read_bytes, elf=elf, symbol_lookup=linker_symbol_address
    )


_DEFAULT_EXTERNAL_EXCEPTION_LIMIT = _external_exception_limit


def _armv6m_port_limit(*, elf: ElfImage | None = None) -> int | None:
    """Verify the Armv6-M fixed-zero vector map and external IRQ capacity."""
    if elf is None:
        active_adapter = active()
        if active_adapter is not None:
            metadata = _unwind_metadata(active_adapter)
            if metadata is not None:
                elf = metadata.elf
    return resolve_armv6m_vector_limit(
        symbol_lookup=linker_symbol_address,
        read=read_bytes,
        elf=elf,
    )


_DEFAULT_ARMV6M_PORT_LIMIT = _armv6m_port_limit


def _armv6m_vector_entry(
    exception_number: int, *, elf: ElfImage | None = None
) -> int | None:
    return _core_armv6m_vector_entry(
        exception_number, read=read_bytes, elf=elf, symbol_lookup=linker_symbol_address
    )


_DEFAULT_ARMV6M_VECTOR_ENTRY = _armv6m_vector_entry


def _vector_entry(exception_number: int, *, elf: ElfImage | None = None) -> int | None:
    return _core_armv7m_vector_entry(
        exception_number, read=read_bytes, elf=elf, symbol_lookup=linker_symbol_address
    )


_DEFAULT_VECTOR_ENTRY = _vector_entry


def _scheduler_running() -> bool | None:
    """Read ``xSchedulerRunning``; ``None`` when the symbol is unavailable."""
    value = read_int(eval_identifier("xSchedulerRunning"))
    if value is None:
        return None
    return value != 0


def _current_tcb_address() -> int | None:
    """Read ``pxCurrentTCB`` target address without mutating target state."""
    return read_int(eval_identifier("pxCurrentTCB"))


def _task_segment(task: FreeRtosTask, low: int, high: int) -> StackSegment:
    """Build the process-stack segment for one task."""
    name = task.name or f"task@{task.address:#x}"
    return StackSegment(
        ident=f"task:{task.address:#x}",
        owner=name,
        low=low,
        high=high,
        source="task-recorded",
        stack="process",
        kind="task",
    )


@dataclass(frozen=True)
class _SchedulerDecision:
    """A scheduler stop's seed, or the exact reason it cannot be built."""

    seed: SeedFrame | None = None
    reason: str | None = None
    message: str = ""
    fault: FaultPrecision | None = None


_SCHEDULER_SYMBOLS = frozenset(
    (
        "PendSV_Handler",
        "SVC_Handler",
        "prvPortStartFirstTask",
        "vStartFirstTask",
        "vRestoreContextOfFirstTask",
        "vTaskSwitchContext",
    )
)


def _riscv_scheduler_live_decision(
    elf: ElfImage,
    context,
    task: FreeRtosTask,
    low: int,
    high: int,
    *,
    isa: str | None = "riscv32",
) -> _SchedulerDecision | None:
    """Classify a live RISC-V stop across verified scheduler and trap stages."""
    xlen = 64 if isa == "riscv64" else 32
    snapshot, snap_reason = _riscv_snapshot(xlen=xlen)
    if snapshot is None:
        if snap_reason == "exception-registers-unavailable":
            live, _ = _live_seed_detail("riscv", isa)
            if live is not None and riscv_pc_in_scheduler_symbol(elf, live.pc):
                return _SchedulerDecision(
                    reason="exception-registers-unavailable",
                    message="the live RISC-V exception CSRs are not readable",
                )
        return None
    profile, profile_reason = build_riscv_scheduler_profile(elf)
    if profile is None:
        if riscv_pc_in_scheduler_symbol(elf, snapshot.pc):
            return _SchedulerDecision(
                reason=profile_reason,
                message=(
                    "the compiled RISC-V scheduler/trap code does not match a "
                    "verified transition profile"
                ),
            )
        return None
    stage = classify_riscv_stage(profile, snapshot.pc)
    if stage is None:
        return None
    if isinstance(stage, str):
        return _SchedulerDecision(
            reason=stage,
            message="the stop is inside a transition function but not a proven stage",
        )
    is_interrupt, _cause_code = _decode_riscv_mcause(snapshot.mcause, xlen)
    if stage.name in ("trap-entry", "trap-dispatch") and is_interrupt:
        return None
    if (
        (
            stage.name == "trap-entry"
            or (stage.name == "trap-sync-unhandled" and stage.seed == "refused")
        )
        and not is_interrupt
        and riscv_fault_precision(
            snapshot.mcause,
            xlen,
            mepc=snapshot.mepc,
            mtval=snapshot.mtval,
        )
        is not None
    ):
        return None
    running = _scheduler_running()
    if running is None:
        return _SchedulerDecision(
            reason="scheduler-state-unavailable",
            message="xSchedulerRunning is not readable at a scheduler transition",
        )
    if not running:
        return _SchedulerDecision(
            reason="scheduler-not-running",
            message="a scheduler transition is visible but the scheduler is stopped",
        )
    current = _current_tcb_address()
    if current is None:
        return _SchedulerDecision(
            reason="scheduler-current-tcb-unavailable",
            message="pxCurrentTCB is not readable at a scheduler transition",
        )
    isr_bounds = _riscv_isr_stack_bounds(elf)
    irq_span = (isr_bounds[0], isr_bounds[1]) if isr_bounds is not None else None
    endian = arch_or_default()[1]

    def _is_entry(address: int) -> bool:
        symbol = elf.function_containing(address)
        return symbol is not None and symbol.start == address

    outcome = build_riscv_stage_seed(
        profile,
        stage,
        snapshot=snapshot,
        read=read_bytes,
        context=context,
        endian=endian,
        task_tcb=task.address,
        task_top=task.top_of_stack,
        task_low=low,
        task_high=high,
        current_tcb=current,
        exit_pc=_target_exit_pc(elf),
        is_entry=_is_entry,
        irq_bounds=irq_span,
        elf=elf,
    )
    if isinstance(outcome, str):
        return _SchedulerDecision(
            reason=outcome,
            message=(
                "the RISC-V scheduler stage is visible but its registers, stack, "
                "TCB or CSRs do not form one task's consistent seed"
            ),
        )
    return _SchedulerDecision(
        seed=outcome.seed,
        message=outcome.evidence,
        fault=outcome.fault,
    )


def _scheduler_live_decision(
    elf: ElfImage,
    context,
    task: FreeRtosTask,
    low: int,
    high: int,
    fpu_context=None,
    *,
    isa: str | None = "armv7-m",
) -> _SchedulerDecision | None:
    """Classify a live Arm stop without assuming every stop is an IRQ.

    ``None`` means the PC is not a verified scheduler stage and the ordinary
    live/saved path continues.  A refusal is returned for a PC inside one of
    the port's transition functions when the profile or its invariants do not
    match.
    """
    snapshot, _snapshot_reason = _arm_snapshot()
    if snapshot is None:
        return None
    pc = snapshot.pc & ~1
    profile, profile_reason = build_scheduler_profile(elf)
    if profile is None:
        symbol = elf.function_containing(pc)
        if symbol is not None and symbol.name in _SCHEDULER_SYMBOLS:
            return _SchedulerDecision(
                reason=profile_reason,
                message=(
                    "the compiled scheduler transition does not match a "
                    "verified Arm M-profile transition profile"
                ),
            )
        return None
    stage = classify_stage(profile, pc)
    if stage is None:
        return None
    if isinstance(stage, str):
        return _SchedulerDecision(
            reason=stage,
            message="the stop is inside a transition function but not a proven stage",
        )
    if stage.name == "start-first-svc-pending":
        return _SchedulerDecision(
            reason="scheduler-first-start-pending",
            message=(
                "the first task's SVC has not executed; the initial TCB context "
                "is not a running task seed"
            ),
        )
    running = _scheduler_running()
    if running is None:
        return _SchedulerDecision(
            reason="scheduler-state-unavailable",
            message="xSchedulerRunning is not readable at a scheduler transition",
        )
    if not running:
        return _SchedulerDecision(
            reason="scheduler-not-running",
            message="a scheduler transition is visible but the scheduler is stopped",
        )
    if snapshot.sp != snapshot.msp:
        return _SchedulerDecision(
            reason="scheduler-handler-mode-unproven",
            message="the live SP is not the handler MSP at a scheduler transition",
        )
    irq_bounds = (
        _irq_stack_bounds(
            isa=isa or "armv7-m",
            elf=elf,
            live_msp=snapshot.msp,
            task_bounds=(low, high),
        )
        if _irq_stack_bounds is _DEFAULT_IRQ_STACK_BOUNDS
        else _irq_stack_bounds()
    )
    if irq_bounds is None:
        return _SchedulerDecision(
            reason="exception-stack-bounds-unavailable",
            message=(
                "the interrupt stack span could not be resolved from symbols, "
                "ELF sections, or vector-table initial MSP"
            ),
        )
    irq_low, irq_high = irq_bounds
    if not irq_low <= snapshot.msp <= irq_high:
        return _SchedulerDecision(
            reason="exception-stack-outside-bounds",
            message="the live MSP is outside the resolved interrupt stack span",
        )
    stkalign = True if isa == "armv6-m" else _ccr_stkalign()
    if stkalign is None:
        return _SchedulerDecision(
            reason="exception-alignment-unavailable",
            message="CCR.STKALIGN is not readable; the frame layout is unproven",
        )
    current = _current_tcb_address()
    if current is None:
        return _SchedulerDecision(
            reason="scheduler-current-tcb-unavailable",
            message="pxCurrentTCB is not readable at a scheduler transition",
        )

    live_psplim: int | None = None
    if isa == "armv8-m.mainline":
        live_psplim, _control = _read_armv8m_regs()
        if live_psplim is None:
            return _SchedulerDecision(
                reason="live-registers-unavailable",
                message="PSPLIM is not readable on Armv8-M Mainline",
            )

    def _is_entry(address: int) -> bool:
        symbol = elf.function_containing(address)
        return symbol is not None and symbol.start == address

    outcome = build_stage_seed(
        profile,
        stage,
        read=read_bytes,
        context=context,
        task_tcb=task.address,
        task_top=task.top_of_stack,
        task_low=low,
        task_high=high,
        psp=snapshot.psp,
        msp=snapshot.msp,
        registers=snapshot.registers,
        xpsr=snapshot.xpsr,
        stkalign=stkalign,
        current_tcb=current,
        exit_pc=_exit_pc(),
        is_entry=_is_entry,
        fpu_context=fpu_context,
        live_psplim=live_psplim,
    )
    if isinstance(outcome, str):
        return _SchedulerDecision(
            reason=outcome,
            message=(
                "the scheduler stage is visible but its registers, stack, TCB "
                "or saved context do not form one task's consistent seed"
            ),
        )
    return _SchedulerDecision(
        seed=outcome.seed,
        message=outcome.evidence,
        fault=outcome.fault,
    )


def _selected_core() -> int:
    if gdb is not None:
        try:
            thread = gdb.selected_thread()
            if thread is not None and int(thread.num) >= 1:
                return int(thread.num) - 1
        except (gdb.error, AttributeError, RuntimeError, TypeError, ValueError):
            pass
    return 0


def _task_value_by_address(adapter, address: int):
    if getattr(adapter.layout, "structs", None) is not None:
        for value, _state, _core in iter_tasks(adapter.layout):
            if value_address(value) == address:
                return value
        if callable(getattr(adapter, "find_object", None)):
            found = adapter.find_object("task", f"{address:#x}")
            if found is not None:
                return found
    return find_task(f"{address:#x}", adapter.layout)


_RISCV_SWITCH_RESTORE_STAGES = frozenset(
    (
        "switch-before-update",
        "switch-after-update",
        "restore-sp-loaded",
        "restore-sp-popped",
        "start-first-entry",
        "start-first-sp-loaded",
        "start-first-sp-popped",
    )
)


def _resolve_riscv_interrupted_task(
    adapter,
    snap: _RiscvSnapshot,
    context,
    *,
    known_task: FreeRtosTask | None = None,
    mstatus_offset: int | None = None,
    exit_pc: int | None = None,
) -> tuple[int, int] | str:
    """Identify the unique task interrupted onto ``xISRStack`` cleanly.

    After ``portYIELD_FROM_ISR()`` updates ``pxCurrentTCB`` while returning into
    a peripheral ISR, ``pxCurrentTCB`` points to the new task whereas the old
    task still holds ``0(pxTopOfStack) == mepc`` for the active interrupt.
    """
    is_interrupt, _cause = _decode_riscv_mcause(snap.mcause, snap.xlen)
    mask = (1 << snap.xlen) - 1
    expected_epc = (
        (snap.mepc & mask & ~1) if is_interrupt else ((snap.mepc + 4) & mask & ~1)
    )
    if expected_epc <= 0:
        return "exception-task-context-conflict"
    endian = arch_or_default()[1]
    resolved_exit = exit_pc if exit_pc is not None else _exit_pc()
    current_tcb = _current_tcb_address()
    matches: list[tuple[int, int]] = []
    if current_tcb is not None:
        cur_task: FreeRtosTask | None = None
        if known_task is not None and known_task.address == current_tcb:
            cur_task = known_task
        else:
            try:
                current_val = _task_value_by_address(adapter, current_tcb)
            except RuntimeError:
                current_val = None
            if current_val is not None:
                cur_state, cur_core = task_state(current_val, adapter.layout)
                cur_task = value_to_task(
                    current_val,
                    cur_state,
                    cur_core,
                    adapter.layout,
                    collect_stack_metrics=False,
                )
        if cur_task is not None:
            low, high, _ = _task_bounds(cur_task)
            if riscv_interrupted_frame_matches(
                cur_task.top_of_stack,
                low,
                high,
                snap,
                context,
                read=read_bytes,
                endian=endian,
                expected_epc=expected_epc,
                mstatus_offset=mstatus_offset,
                exit_pc=resolved_exit,
            ):
                matches.append((cur_task.address, cur_task.top_of_stack))

    try:
        candidates = list(iter_tasks(adapter.layout))
    except RuntimeError:
        candidates = []
    for task_val, st, core in candidates:
        cand = value_to_task(
            task_val, st, core, adapter.layout, collect_stack_metrics=False
        )
        if cand.address == current_tcb:
            continue
        low, high, _ = _task_bounds(cand)
        if riscv_interrupted_frame_matches(
            cand.top_of_stack,
            low,
            high,
            snap,
            context,
            read=read_bytes,
            endian=endian,
            expected_epc=expected_epc,
            mstatus_offset=mstatus_offset,
            exit_pc=resolved_exit,
        ):
            matches.append((cand.address, cand.top_of_stack))
            if len(matches) > 1:
                return "exception-task-context-conflict"
    return matches[0] if len(matches) == 1 else "exception-task-context-conflict"


def _riscv_active_isr_owner(
    adapter,
    selection,
    metadata: UnwindMetadata | None,
    *,
    known_task: FreeRtosTask | None = None,
) -> tuple[int, int] | str | None:
    if (
        selection.family != "riscv"
        or selection.isa not in ("riscv32", "riscv64")
        or selection.context is None
        or not selection.context.verified
        or metadata is None
    ):
        return None
    isr_bounds = _riscv_isr_stack_bounds(metadata.elf)
    if isr_bounds is None:
        return None
    xlen = 64 if selection.isa == "riscv64" else 32
    snap, _snap_reason = _riscv_snapshot(xlen=xlen)
    if snap is None or not isr_bounds[0] <= snap.sp <= isr_bounds[1]:
        return None
    is_interrupt, _cause = _decode_riscv_mcause(snap.mcause, xlen)
    if not is_interrupt:
        return None
    sym = metadata.elf.function_containing(snap.pc)
    if sym is not None and sym.name in (
        "vTaskSwitchContext",
        "processed_source",
        "xPortStartFirstTask",
    ):
        return None
    profile, _ = build_riscv_scheduler_profile(metadata.elf)
    if profile is not None:
        stage = classify_riscv_stage(profile, snap.pc)
        if (
            not isinstance(stage, str)
            and stage is not None
            and (
                stage.name in _RISCV_SWITCH_RESTORE_STAGES
                or stage.name.startswith(("trap-prologue-", "trap-sync-"))
            )
        ):
            return None
    span = analyze_riscv_trap_profile(metadata.elf)
    return _resolve_riscv_interrupted_task(
        adapter,
        snap,
        selection.context,
        known_task=known_task,
        mstatus_offset=profile.mstatus_offset
        if profile is not None
        else (span.layout.mstatus_offset if span is not None else None),
        exit_pc=_target_exit_pc(metadata.elf),
    )


def _current_pc_backtrace(adapter, max_depth: int) -> BacktraceReport:
    selected = _selected_core()
    running = _scheduler_running()
    current_addr = next(
        (addr for core, addr in current_tasks(adapter.layout) if core == selected),
        None,
    )
    selection = adapter._context_selection()
    metadata = _unwind_metadata(adapter)
    boot_state = (
        arm_boot_state(
            selection.isa,
            metadata=metadata,
            scheduler_symbols=_SCHEDULER_SYMBOLS,
            snapshot_reader=_arm_snapshot,
        )
        if selection.family == "arm"
        else "other"
    )
    active_scheduler = running is not False and boot_state != "pre-first-task"
    if current_addr is not None and active_scheduler:
        owner = _riscv_active_isr_owner(adapter, selection, metadata)
        target_addr = owner[0] if isinstance(owner, tuple) else current_addr
        value = _task_value_by_address(adapter, target_addr)
        if value is not None:
            return _backtrace_task_value(adapter, value, f"{target_addr:#x}", max_depth)
    context = selection.context
    if context is None or not context.verified:
        return _stop_report(
            None,
            selection.isa or "unknown",
            selection.abi,
            max_depth,
            selection.reason,
            "no verified save profile matches the image/configuration evidence",
            bounds="unknown",
            target="<current>",
        )
    architecture = selection.isa or "unknown"
    if active_scheduler and (current_addr is not None or running is True):
        return _stop_report(
            None,
            architecture,
            selection.abi,
            max_depth,
            "scheduler-current-tcb-unavailable",
            "pxCurrentTCB is unavailable or invalid on the selected core",
            bounds="unknown",
            target="<current>",
            address=current_addr or 0,
        )
    halted_early = boot_state == "scheduler-symbol"
    if (
        not halted_early
        and metadata is not None
        and selection.family == "riscv"
        and selection.isa in ("riscv32", "riscv64")
    ):
        rv_snap, _ = _riscv_snapshot(xlen=64 if selection.isa == "riscv64" else 32)
        halted_early = rv_snap is not None and riscv_pc_in_scheduler_symbol(
            metadata.elf, rv_snap.pc
        )
    if halted_early:
        return _stop_report(
            None,
            architecture,
            selection.abi,
            max_depth,
            "scheduler-not-running",
            "a scheduler transition is halted before xSchedulerRunning",
            bounds="unknown",
            target="<current>",
        )
    return live_boot_backtrace_report(
        architecture=architecture,
        abi=selection.abi,
        family=selection.family,
        max_depth=max_depth,
        metadata=metadata,
        symbol_lookup=lookup_symbol_at,
        source_lookup=source_location_at,
        linker_lookup=linker_symbol_address,
        read=read_bytes,
        hart_id=selected,
        arm_snapshot_reader=_arm_snapshot,
    )


def backtrace(
    adapter, target: str | None = None, max_depth: int = 32
) -> BacktraceReport:
    """Unwind one FreeRTOS task (or the current halted core context)."""
    if target is None:
        return _current_pc_backtrace(adapter, max_depth)
    value = find_task(target, adapter.layout)
    if value is None:
        return _stop_report(
            None,
            "unknown",
            None,
            max_depth,
            "task-not-found",
            f"task {target!r} was not found in the scheduler lists",
            source="none",
            bounds="unknown",
            target=target,
        )
    return _backtrace_task_value(adapter, value, target, max_depth)


@dataclass(frozen=True)
class _TargetContext:
    """Validated target bounds and metadata shared across task unwind steps."""

    adapter: Any
    value: Any
    task: FreeRtosTask
    target: str
    selection: StackContextSelection
    low: int
    high: int
    bounds: str
    current_core: int | None
    metadata: UnwindMetadata | None
    max_depth: int
    live_owner: bool = False
    trap_top_override: int | None = None

    @property
    def architecture(self) -> str:
        return self.selection.isa or "unknown"

    def stop(
        self, reason: str, message: str, *, source: str | None = None
    ) -> BacktraceReport:
        resolved_source = (
            (
                "current"
                if (self.current_core is not None or self.live_owner)
                else "saved"
            )
            if source is None
            else source
        )
        return _stop_report(
            self.task,
            self.architecture,
            self.selection.abi,
            self.max_depth,
            reason,
            message,
            source=resolved_source,
            bounds=self.bounds,
            target=self.target,
        )


@dataclass(frozen=True)
class _HandlerSetup:
    """Resolved cross-stack unwind hooks and segments for one stop."""

    seed: SeedFrame
    fault: FaultPrecision | None
    segments: tuple[StackSegment, ...] | None
    current_segment: str | None
    detector: ExceptionDetector | None = None
    hook: ExceptionHook | None = None
    guard: TransitionGuard | None = None
    stkalign: bool = True
    metadata: UnwindMetadata | None = None


def _stop_report(
    task,
    architecture: str,
    abi: str | None,
    max_depth: int,
    reason: str,
    message: str,
    *,
    source: str = "current",
    bounds: str = "recorded",
    target: str = "",
    address: int | None = None,
) -> BacktraceReport:
    return BacktraceReport(
        task=(task.name or target) if task is not None else target,
        address=address if address is not None else (task.address if task else 0),
        architecture=architecture,
        method="none",
        source=source,
        complete=False,
        stop_reason=reason,
        truncated=False,
        max_depth=max_depth,
        bounds=bounds,
        frames=[],
        messages=[message],
        abi=abi,
    )


def _resolve_live_seed(
    ctx: _TargetContext,
) -> tuple[SeedFrame, str | None, FaultPrecision | None] | BacktraceReport:
    selected = None
    if gdb is not None:
        try:
            selected = gdb.selected_thread().num - 1
        except (gdb.error, AttributeError, RuntimeError):
            selected = None
    if (
        selected is not None
        and ctx.current_core is not None
        and selected != ctx.current_core
    ):
        return ctx.stop(
            "smp-no-context",
            f"task runs on core {ctx.current_core}; the selected core "
            f"{selected} has no saved context for it",
            source="current",
        )
    seed = None
    scheduler_note: str | None = None
    fault: FaultPrecision | None = None
    decision = None
    if (
        ctx.selection.family == "arm"
        and ctx.architecture in ("armv6-m", "armv7-m", "armv7e-m", "armv8-m.mainline")
        and ctx.metadata is not None
    ):
        decision = _scheduler_live_decision(
            ctx.metadata.elf,
            ctx.selection.context,
            ctx.task,
            ctx.low,
            ctx.high,
            fpu_context=ctx.selection.alt_context,
            isa=ctx.architecture,
        )
    elif (
        ctx.selection.family == "riscv"
        and ctx.architecture in ("riscv32", "riscv64")
        and ctx.metadata is not None
    ):
        decision = _riscv_scheduler_live_decision(
            ctx.metadata.elf,
            ctx.selection.context,
            ctx.task,
            ctx.low,
            ctx.high,
            isa=ctx.architecture,
        )
    if decision is not None:
        if decision.seed is None:
            return ctx.stop(
                decision.reason or "scheduler-owner-unproven",
                decision.message,
                source="current",
            )
        seed = decision.seed
        scheduler_note = decision.message
        fault = decision.fault
    if seed is None:
        seed, live_reason = _live_seed_detail(ctx.selection.family, ctx.architecture)
        if seed is not None and ctx.architecture == "armv8-m.mainline":
            psplim, control = _read_armv8m_regs()
            if psplim is None or control is None:
                return ctx.stop(
                    "live-registers-unavailable",
                    "PSPLIM/CONTROL are not readable on Armv8-M Mainline",
                    source="current",
                )
            ext_err = validate_armv8m_live_registers(
                psplim,
                control,
                mpu=bool(
                    ctx.selection.context and ctx.selection.context.context_in_tcb
                ),
                exception_number=seed.exception_number or 0,
                sp=seed.sp,
                process_sp=seed.process_sp,
                stack_low=ctx.low,
                stack_high=ctx.high,
            )
            if ext_err is not None:
                return ctx.stop(
                    ext_err,
                    "live Armv8-M Mainline PSPLIM or CONTROL is unsupported",
                    source="current",
                )
    else:
        live_reason = "ok"
    if seed is None:
        return ctx.stop(
            live_reason,
            "the selected core's registers are not readable or not "
            "consistent with the exception mode",
            source="current",
        )
    return seed, scheduler_note, fault


def _read_armv8m_regs() -> tuple[int | None, int | None]:
    """Read Armv8-M Mainline ``psplim`` and ``control`` from the active GDB frame."""
    if gdb is None:
        return None, None
    try:
        frame = gdb.newest_frame()
        return int(frame.read_register("psplim")), int(frame.read_register("control"))
    except (KeyError, gdb.error, AttributeError, RuntimeError, TypeError, ValueError):
        return None, None


def _resolve_saved_seed(ctx: _TargetContext) -> SeedFrame | BacktraceReport:
    context = ctx.selection.context
    if context is None:
        return ctx.stop(
            ctx.selection.reason, "no verified save profile", source="saved"
        )
    context_low, context_high = (
        saved_context_bounds(ctx.value, ctx.adapter.layout)
        if context.context_in_tcb
        else (None, None)
    )
    if ctx.selection.alt_context is not None or (
        ctx.architecture == "armv8-m.mainline" and not context.context_in_tcb
    ):
        decoder = (
            decode_cm33_saved_context
            if ctx.architecture == "armv8-m.mainline"
            else decode_cm4f_saved_context
        )
        seed, reason = decoder(
            ctx.task.top_of_stack,
            read_bytes,
            basic=context,
            fpu=ctx.selection.alt_context,
            endian=arch_or_default()[1],
            stack_low=ctx.low,
            stack_high=ctx.high,
            exit_pc=_exit_pc(),
        )
    else:
        seed, reason = decode_backtrace_context(
            context,
            ctx.task.top_of_stack,
            read_bytes,
            endian=arch_or_default()[1],
            stack_low=ctx.low,
            stack_high=ctx.high,
            context_low=context_low,
            context_high=context_high,
            exit_pc=_target_exit_pc(ctx.metadata.elf if ctx.metadata else None),
        )
    if seed is None:
        return ctx.stop(
            reason,
            "the saved context is not readable or not proven",
            source="saved",
        )
    return seed


def _resolve_target_seed(
    ctx: _TargetContext,
) -> tuple[SeedFrame, str | None, FaultPrecision | None] | BacktraceReport:
    if ctx.live_owner:
        return _resolve_live_seed(ctx)
    saved = _resolve_saved_seed(ctx)
    if isinstance(saved, BacktraceReport):
        return saved
    return saved, None, None


def _prepare_arm_handler(
    ctx: _TargetContext, seed: SeedFrame, fault: FaultPrecision | None
) -> _HandlerSetup | BacktraceReport:
    task_segment = _task_segment(ctx.task, ctx.low, ctx.high)
    if not seed.exception_number:
        return _HandlerSetup(
            seed=seed,
            fault=fault,
            segments=(task_segment,),
            current_segment=task_segment.ident,
            metadata=ctx.metadata,
        )
    elf_image = ctx.metadata.elf if ctx.metadata is not None else None
    bounds_reader = (
        (
            lambda: resolve_arm_irq_stack_bounds_detail(
                symbol_lookup=linker_symbol_address,
                read=read_bytes,
                isa=ctx.architecture,
                elf=elf_image,
                live_msp=seed.sp,
                task_bounds=(ctx.low, ctx.high),
            )
        )
        if _irq_stack_bounds is _DEFAULT_IRQ_STACK_BOUNDS
        else _irq_stack_bounds
    )
    m0_limit = (
        (lambda: _armv6m_port_limit(elf=elf_image))
        if _armv6m_port_limit is _DEFAULT_ARMV6M_PORT_LIMIT
        else _armv6m_port_limit
    )
    ext_limit = (
        (lambda: _external_exception_limit(elf=elf_image))
        if _external_exception_limit is _DEFAULT_EXTERNAL_EXCEPTION_LIMIT
        else _external_exception_limit
    )
    m0_vec = (
        (lambda number: _armv6m_vector_entry(number, elf=elf_image))
        if _armv6m_vector_entry is _DEFAULT_ARMV6M_VECTOR_ENTRY
        else _armv6m_vector_entry
    )
    vec_reader = (
        (lambda number: _vector_entry(number, elf=elf_image))
        if _vector_entry is _DEFAULT_VECTOR_ENTRY
        else _vector_entry
    )
    setup = prepare_arm_handler_unwind(
        seed,
        ctx.architecture,
        scheduler_running=_scheduler_running,
        irq_stack_bounds=bounds_reader,
        irq_stack_source="linker:__gdr_irq_stack_low..__gdr_irq_stack_top",
        capacity_reader=m0_limit if ctx.architecture == "armv6-m" else ext_limit,
        stkalign_reader=_ccr_stkalign,
        vector_entry_reader=m0_vec if ctx.architecture == "armv6-m" else vec_reader,
        guard_fn=_exception_guard,
        capacity_guard_factory=_capacity_exception_guard,
        read=read_bytes,
    )
    if isinstance(setup, tuple):
        return ctx.stop(setup[0], setup[1])
    return _HandlerSetup(
        seed=setup.seed,
        fault=setup.fault,
        segments=(task_segment, setup.exception_segment),
        current_segment=setup.exception_segment.ident,
        detector=setup.exception_detector,
        hook=setup.exception_hook,
        guard=setup.exception_guard,
        stkalign=setup.stkalign,
        metadata=ctx.metadata,
    )


def _prepare_riscv_handler(
    ctx: _TargetContext,
    seed: SeedFrame,
    fault: FaultPrecision | None,
    *,
    scheduler_note: str | None,
) -> _HandlerSetup | BacktraceReport:
    if not ctx.live_owner:
        return _HandlerSetup(
            seed=seed,
            fault=fault,
            segments=None,
            current_segment=None,
            metadata=ctx.metadata,
        )
    task_segment = _task_segment(ctx.task, ctx.low, ctx.high)
    default_setup = _HandlerSetup(
        seed=seed,
        fault=fault,
        segments=(task_segment,),
        current_segment=task_segment.ident,
        metadata=ctx.metadata,
    )
    if scheduler_note is not None:
        return default_setup
    elf_image = ctx.metadata.elf if ctx.metadata is not None else None
    span = analyze_riscv_trap_profile(elf_image) if elf_image is not None else None
    isr_bounds = _riscv_isr_stack_bounds(elf_image)
    in_isr_stack = isr_bounds is not None and isr_bounds[0] <= seed.sp <= isr_bounds[1]
    in_trap_span = span is not None and (
        seed.pc == span.entry
        or span.entry <= seed.pc < span.exit_end
        or span.refused_reason(seed.pc) is not None
    )
    if not (in_isr_stack or in_trap_span):
        return default_setup
    if span is None:
        return ctx.stop(
            "exception-model-unverified",
            "the compiled RISC-V trap handler could not be verified from ELF",
        )
    xlen = 64 if ctx.architecture == "riscv64" else 32
    snap, snap_reason = _riscv_snapshot(xlen=xlen)
    if snap is None:
        return ctx.stop(snap_reason, "the live RISC-V exception CSRs are not readable")
    if _scheduler_running() is False:
        return ctx.stop(
            "scheduler-not-running",
            "a RISC-V trap handler is active before the scheduler is started",
        )
    current_tcb = (
        ctx.task.address
        if ctx.trap_top_override is not None
        else _current_tcb_address()
    )
    if current_tcb is None:
        return ctx.stop(
            "scheduler-current-tcb-unavailable",
            "pxCurrentTCB is not readable while unwinding a RISC-V trap frame",
        )
    word = xlen // 8
    endian = arch_or_default()[1]

    def _resolve_frt_trap_frame(_ctx) -> int | str:
        if ctx.trap_top_override is not None:
            return ctx.trap_top_override
        raw_top = read_bytes(current_tcb, word)
        if raw_top is None or len(raw_top) != word:
            return "exception-frame-unreadable"
        return int.from_bytes(raw_top, "little" if endian == "little" else "big")

    setup = prepare_riscv_handler_unwind(
        snap,
        root_pc=_target_exit_pc(elf_image),
        span=span,
        task_segment=task_segment,
        irq_stack_bounds=lambda: isr_bounds,
        nest_count=None,
        endian=endian,
        task_frame_resolver=_resolve_frt_trap_frame,
        allow_sync=True,
        elf=elf_image,
        read=read_bytes,
    )
    if isinstance(setup, tuple):
        return ctx.stop(setup[0], setup[1])
    if snap.mcause == 11:
        return ctx.stop(
            "scheduler-transition-unproven",
            "ECALL scheduler transition fell outside proven switch stages",
        )
    if current_tcb != ctx.task.address:
        return ctx.stop(
            "exception-task-context-conflict",
            "pxCurrentTCB does not match the requested task",
        )
    return _HandlerSetup(
        seed=setup.seed,
        fault=setup.fault,
        segments=(task_segment, setup.exception_segment),
        current_segment=setup.exception_segment.ident,
        detector=setup.exception_detector,
        hook=setup.exception_hook,
        guard=setup.exception_guard,
        metadata=ctx.metadata,
    )


def _prepare_handler_setup(
    ctx: _TargetContext,
    seed: SeedFrame,
    fault: FaultPrecision | None,
    *,
    scheduler_note: str | None,
) -> _HandlerSetup | BacktraceReport:
    if ctx.selection.family == "arm":
        return _prepare_arm_handler(ctx, seed, fault)
    if ctx.selection.family == "riscv":
        return _prepare_riscv_handler(ctx, seed, fault, scheduler_note=scheduler_note)
    return _HandlerSetup(
        seed=seed,
        fault=fault,
        segments=None,
        current_segment=None,
        metadata=ctx.metadata,
    )


def _backtrace_task_value(
    adapter, value, target: str, max_depth: int = 32
) -> BacktraceReport:
    selection = adapter._context_selection()
    _state, core = task_state(value, adapter.layout)
    task = value_to_task(
        value, _state, core, adapter.layout, collect_stack_metrics=False
    )
    context = selection.context
    architecture = selection.isa or "unknown"
    if context is None or not context.verified:
        return _stop_report(
            task,
            architecture,
            selection.abi,
            max_depth,
            selection.reason,
            "no verified save profile matches the image/configuration evidence",
            source="none",
            bounds="unknown",
            target=target,
        )
    low, high, bounds = _task_bounds(task)
    if low is None or high is None:
        return _stop_report(
            task,
            architecture,
            selection.abi,
            max_depth,
            "stack-bounds-unavailable",
            "the task records no usable stack span; enable configRECORD_STACK_HIGH_ADDRESS",
            source="none",
            bounds=bounds,
            target=target,
        )
    current_core = {
        address: core for core, address in current_tasks(adapter.layout)
    }.get(task.address)
    metadata = _unwind_metadata(adapter)
    active_owner = _riscv_active_isr_owner(
        adapter, selection, metadata, known_task=task
    )
    live_owner = current_core is not None
    trap_top_override: int | None = None
    if isinstance(active_owner, tuple):
        owner_tcb, owner_top = active_owner
        if task.address == owner_tcb:
            live_owner = True
            trap_top_override = owner_top
        else:
            live_owner = False
    elif isinstance(active_owner, str) and current_core is not None:
        return _stop_report(
            task,
            architecture,
            selection.abi,
            max_depth,
            active_owner,
            "the active RISC-V ISR frame does not match a unique interrupted task",
            source="current",
            bounds=bounds,
            target=target,
        )
    ctx = _TargetContext(
        adapter=adapter,
        value=value,
        task=task,
        target=target,
        selection=selection,
        low=low,
        high=high,
        bounds=bounds,
        current_core=current_core,
        metadata=metadata,
        max_depth=max_depth,
        live_owner=live_owner,
        trap_top_override=trap_top_override,
    )
    resolved = _resolve_target_seed(ctx)
    if isinstance(resolved, BacktraceReport):
        return resolved
    seed, scheduler_note, fault = resolved
    handler = _prepare_handler_setup(ctx, seed, fault, scheduler_note=scheduler_note)
    if isinstance(handler, BacktraceReport):
        return handler
    seed = handler.seed
    exit_pc = (
        _target_exit_pc(ctx.metadata.elf if ctx.metadata else None)
        if selection.family == "riscv"
        else _exit_pc()
    )
    if seed.source != "unstarted" and exit_pc is not None:
        seed = replace(seed, root_pc=exit_pc, root_reason="task-exit")
    messages = (
        [f"scheduler seed {scheduler_note}"] if scheduler_note is not None else []
    )
    if handler.metadata is None:
        messages.append("no ELF unwind metadata could be read")
    result = unwind_chain(
        seed,
        handler.metadata,
        read=read_bytes,
        max_depth=max_depth,
        stack_low=low,
        stack_high=high,
        allow_frame_pointer=True,
        segments=handler.segments,
        current_segment=handler.current_segment,
        exception_detector=handler.detector,
        exception_hook=handler.hook,
        exception_guard=handler.guard,
        stkalign=handler.stkalign,
    )
    return build_backtrace_report(
        task=task.name or target,
        address=task.address,
        architecture=architecture,
        abi=selection.abi,
        source=seed.source,
        bounds=bounds,
        max_depth=max_depth,
        result=result,
        metadata=handler.metadata,
        symbol_lookup=lookup_symbol_at,
        source_lookup=source_location_at,
        initial_messages=messages,
        fault=handler.fault,
    )
