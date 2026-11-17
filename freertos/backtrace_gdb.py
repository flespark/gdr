"""GDB-facing FreeRTOS backtrace orchestration (no target writes).

The pure context layout and ELF/CFI/EHABI parsers live in
:mod:`freertos.backtrace` and :mod:`core.unwind`; this module only resolves
the task, reads live/saved registers through GDB, supplies the linker-owned
interrupt-stack span and exception profile gates, and turns one bounded
:class:`core.unwind.UnwindResult` into the adapter presentation model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.adapter import (
    BacktraceReport,
    active,
    build_backtrace_report,
    live_boot_backtrace_report,
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
from core.unwind import (
    ElfImage,
    SeedFrame,
    UnwindMetadata,
    cached_unwind_metadata,
    unwind_chain,
)
from core.unwind import (
    live_seed_detail as _core_live_seed_detail,
)
from core.unwind_arm import (
    ArmSnapshot as _ArmSnapshot,
)
from core.unwind_arm import (
    arm_capacity_exception_guard as _capacity_exception_guard,
)
from core.unwind_arm import (
    arm_ccr_stkalign as _core_ccr_stkalign,
)
from core.unwind_arm import (
    arm_exception_guard as _exception_guard,
)
from core.unwind_arm import (
    arm_external_exception_limit as _core_external_exception_limit,
)
from core.unwind_arm import (
    arm_snapshot as _core_arm_snapshot,
)
from core.unwind_arm import (
    armv6m_vector_entry as _core_armv6m_vector_entry,
)
from core.unwind_arm import (
    armv7m_vector_entry as _core_armv7m_vector_entry,
)
from core.unwind_arm import (
    prepare_arm_handler_unwind,
    resolve_arm_irq_stack_bounds,
    resolve_arm_irq_stack_bounds_detail,
    resolve_armv6m_vector_limit,
)
from core.unwind_riscv import (
    RiscvSnapshot as _RiscvSnapshot,
)
from core.unwind_riscv import (
    decode_riscv_mcause as _decode_riscv_mcause,
)
from core.unwind_riscv import (
    prepare_riscv_handler_unwind,
    resolve_riscv_irq_stack_bounds_detail,
)
from core.unwind_riscv import (
    riscv_snapshot as _core_riscv_snapshot,
)
from core.unwind_segments import StackSegment
from freertos.adapter import FreeRtosTask, find_task, value_to_task
from freertos.arm_scheduler_switch import build_profile as build_scheduler_profile
from freertos.arm_scheduler_switch import classify_stage
from freertos.backtrace import decode_cm4f_saved_context
from freertos.backtrace import decode_saved_context as decode_backtrace_context
from freertos.navigation import (
    current_tasks,
    iter_tasks,
    saved_context_bounds,
    task_state,
)
from freertos.riscv_scheduler_switch import (
    build_riscv_profile as build_riscv_scheduler_profile,
)
from freertos.riscv_scheduler_switch import classify_riscv_stage
from freertos.scheduler_seed import build_riscv_stage_seed, build_stage_seed
from freertos.stack_frame_profile import analyze_riscv_trap_profile

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


def _external_exception_limit() -> int | None:
    return _core_external_exception_limit(read=read_bytes)


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


def _armv6m_vector_entry(exception_number: int) -> int | None:
    return _core_armv6m_vector_entry(exception_number, read=read_bytes)


def _vector_entry(exception_number: int) -> int | None:
    return _core_armv7m_vector_entry(exception_number, read=read_bytes)


def _scheduler_running() -> bool | None:
    """Read ``xSchedulerRunning``; ``None`` when the symbol is unavailable."""
    value = read_int(eval_identifier("xSchedulerRunning"))
    if value is None:
        return None
    return value != 0


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

_RISCV_SCHEDULER_SYMBOLS = frozenset(
    (
        "xPortStartFirstTask",
        "freertos_risc_v_trap_handler",
        "asynchronous_interrupt",
        "synchronous_exception",
        "handle_interrupt",
        "handle_exception",
        "processed_source",
        "vTaskSwitchContext",
    )
)


def _riscv_pc_in_scheduler_symbol(elf: ElfImage, pc: int) -> bool:
    symbol = elf.function_containing(pc)
    if symbol is not None and symbol.name in _RISCV_SCHEDULER_SYMBOLS:
        return True
    trap_span = analyze_riscv_trap_profile(elf)
    if trap_span is not None and trap_span.entry <= pc < trap_span.exit_end:
        return True
    start_first = elf.symbol_address("xPortStartFirstTask")
    if start_first is not None and start_first <= pc < start_first + 0x100:
        return True
    trap_entry = elf.symbol_address("freertos_risc_v_trap_handler")
    return trap_entry is not None and trap_entry <= pc < trap_entry + 0x200


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
            if live is not None and _riscv_pc_in_scheduler_symbol(elf, live.pc):
                return _SchedulerDecision(
                    reason="exception-registers-unavailable",
                    message="the live RISC-V exception CSRs are not readable",
                )
        return None
    profile, profile_reason = build_riscv_scheduler_profile(elf)
    if profile is None:
        if _riscv_pc_in_scheduler_symbol(elf, snapshot.pc):
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
    current = read_int(eval_identifier("pxCurrentTCB"))
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
        exit_pc=_exit_pc(),
        is_entry=_is_entry,
        irq_bounds=irq_span,
    )
    if isinstance(outcome, str):
        return _SchedulerDecision(
            reason=outcome,
            message=(
                "the RISC-V scheduler stage is visible but its registers, stack, "
                "TCB or CSRs do not form one task's consistent seed"
            ),
        )
    return _SchedulerDecision(seed=outcome.seed, message=outcome.evidence)


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
    current = read_int(eval_identifier("pxCurrentTCB"))
    if current is None:
        return _SchedulerDecision(
            reason="scheduler-current-tcb-unavailable",
            message="pxCurrentTCB is not readable at a scheduler transition",
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
    )
    if isinstance(outcome, str):
        return _SchedulerDecision(
            reason=outcome,
            message=(
                "the scheduler stage is visible but its registers, stack, TCB "
                "or saved context do not form one task's consistent seed"
            ),
        )
    return _SchedulerDecision(seed=outcome.seed, message=outcome.evidence)


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


def _current_pc_backtrace(adapter, max_depth: int) -> BacktraceReport:
    selected = _selected_core()
    current_addr = next(
        (addr for core, addr in current_tasks(adapter.layout) if core == selected),
        None,
    )
    if current_addr is not None:
        value = _task_value_by_address(adapter, current_addr)
        if value is not None:
            return _backtrace_task_value(
                adapter, value, f"{current_addr:#x}", max_depth
            )
    selection = adapter._context_selection()
    context = selection.context
    if context is None or not context.verified:
        return BacktraceReport(
            task="<current>",
            address=0,
            architecture=selection.isa or "unknown",
            method="none",
            source="current",
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
    architecture = selection.isa or "unknown"
    if current_addr is not None or _scheduler_running() is True:
        return BacktraceReport(
            task="<current>",
            address=current_addr or 0,
            architecture=architecture,
            method="none",
            source="current",
            complete=False,
            stop_reason="scheduler-current-tcb-unavailable",
            truncated=False,
            max_depth=max_depth,
            bounds="unknown",
            frames=[],
            messages=["pxCurrentTCB is unavailable or invalid on the selected core"],
            abi=selection.abi,
        )
    metadata = _unwind_metadata(adapter)
    if metadata is not None:
        if selection.family == "arm" and selection.isa in (
            "armv6-m",
            "armv7-m",
            "armv7e-m",
            "armv8-m.mainline",
        ):
            snapshot, _reason = _arm_snapshot()
            if snapshot is not None:
                symbol = metadata.elf.function_containing(snapshot.pc & ~1)
                if symbol is not None and symbol.name in _SCHEDULER_SYMBOLS:
                    return BacktraceReport(
                        task="<current>",
                        address=0,
                        architecture=architecture,
                        method="none",
                        source="current",
                        complete=False,
                        stop_reason="scheduler-not-running",
                        truncated=False,
                        max_depth=max_depth,
                        bounds="unknown",
                        frames=[],
                        messages=[
                            "a scheduler transition is halted before xSchedulerRunning"
                        ],
                        abi=selection.abi,
                    )
        elif selection.family == "riscv" and selection.isa in ("riscv32", "riscv64"):
            xlen = 64 if selection.isa == "riscv64" else 32
            rv_snapshot, _reason = _riscv_snapshot(xlen=xlen)
            if rv_snapshot is not None and _riscv_pc_in_scheduler_symbol(
                metadata.elf, rv_snapshot.pc
            ):
                return BacktraceReport(
                    task="<current>",
                    address=0,
                    architecture=architecture,
                    method="none",
                    source="current",
                    complete=False,
                    stop_reason="scheduler-not-running",
                    truncated=False,
                    max_depth=max_depth,
                    bounds="unknown",
                    frames=[],
                    messages=[
                        "a scheduler transition is halted before xSchedulerRunning"
                    ],
                    abi=selection.abi,
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
    return _backtrace_task_value(adapter, value, target, max_depth)


def _backtrace_task_value(
    adapter, value, target: str, max_depth: int = 32
) -> BacktraceReport:
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
    metadata = _unwind_metadata(adapter)
    scheduler_note: str | None = None

    def _stop(stop_reason: str, message: str) -> BacktraceReport:
        return BacktraceReport(
            task=task.name or target,
            address=task.address,
            architecture=architecture,
            method="none",
            source="current" if current_core is not None else "saved",
            complete=False,
            stop_reason=stop_reason,
            truncated=False,
            max_depth=max_depth,
            bounds=bounds,
            frames=[],
            messages=[message],
            abi=selection.abi,
        )

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
        seed = None
        live_reason = "live-registers-unavailable"
        if (
            arch == "arm"
            and selection.isa in ("armv6-m", "armv7-m", "armv7e-m", "armv8-m.mainline")
            and metadata is not None
        ):
            decision = _scheduler_live_decision(
                metadata.elf,
                context,
                task,
                low,
                high,
                fpu_context=selection.alt_context,
                isa=selection.isa,
            )
            if decision is not None:
                if decision.seed is None:
                    return _stop(
                        decision.reason or "scheduler-owner-unproven", decision.message
                    )
                seed = decision.seed
                scheduler_note = decision.message
        elif (
            arch == "riscv"
            and selection.isa in ("riscv32", "riscv64")
            and metadata is not None
        ):
            decision = _riscv_scheduler_live_decision(
                metadata.elf,
                context,
                task,
                low,
                high,
                isa=selection.isa,
            )
            if decision is not None:
                if decision.seed is None:
                    return _stop(
                        decision.reason or "scheduler-owner-unproven", decision.message
                    )
                seed = decision.seed
                scheduler_note = decision.message
        if seed is None:
            seed, live_reason = _live_seed_detail(arch, selection.isa)
        if seed is None:
            return BacktraceReport(
                task=task.name or target,
                address=task.address,
                architecture=architecture,
                method="none",
                source="current",
                complete=False,
                stop_reason=live_reason,
                truncated=False,
                max_depth=max_depth,
                bounds=bounds,
                frames=[],
                messages=[
                    "the selected core's registers are not readable or not "
                    "consistent with the exception mode"
                ],
                abi=selection.abi,
            )
        if scheduler_note is not None:
            messages.append(f"scheduler seed {scheduler_note}")
    else:
        context_low, context_high = (
            saved_context_bounds(value, adapter.layout)
            if context.context_in_tcb
            else (None, None)
        )
        if selection.alt_context is not None:
            seed, reason = decode_cm4f_saved_context(
                task.top_of_stack,
                read_bytes,
                basic=context,
                fpu=selection.alt_context,
                endian=arch_or_default()[1],
                stack_low=low,
                stack_high=high,
                exit_pc=_exit_pc(),
            )
        else:
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

    segments: list[StackSegment] | None = None
    current_segment: str | None = None
    exception_detector = None
    exception_hook = None
    exception_guard = None
    stkalign = True
    fault = None
    handler_mode = arch == "arm" and bool(seed.exception_number)
    if arch == "arm":
        task_segment = _task_segment(task, low, high)
        segments = [task_segment]
        current_segment = task_segment.ident
        if handler_mode:
            elf_image = metadata.elf if metadata is not None else None
            bounds_reader = (
                (
                    lambda: resolve_arm_irq_stack_bounds_detail(
                        symbol_lookup=linker_symbol_address,
                        read=read_bytes,
                        isa=selection.isa,
                        elf=elf_image,
                        live_msp=seed.sp,
                        task_bounds=(low, high),
                    )
                )
                if _irq_stack_bounds is _DEFAULT_IRQ_STACK_BOUNDS
                else _irq_stack_bounds
            )
            armv6m_limit_reader = (
                (lambda: _armv6m_port_limit(elf=elf_image))
                if _armv6m_port_limit is _DEFAULT_ARMV6M_PORT_LIMIT
                else _armv6m_port_limit
            )
            setup = prepare_arm_handler_unwind(
                seed,
                selection.isa,
                scheduler_running=_scheduler_running,
                irq_stack_bounds=bounds_reader,
                irq_stack_source="linker:__gdr_irq_stack_low..__gdr_irq_stack_top",
                capacity_reader=(
                    armv6m_limit_reader
                    if selection.isa == "armv6-m"
                    else _external_exception_limit
                ),
                stkalign_reader=_ccr_stkalign,
                vector_entry_reader=(
                    _armv6m_vector_entry
                    if selection.isa == "armv6-m"
                    else _vector_entry
                ),
                guard_fn=_exception_guard,
                capacity_guard_factory=_capacity_exception_guard,
                read=read_bytes,
            )
            if isinstance(setup, tuple):
                return _stop(setup[0], setup[1])
            seed = setup.seed
            fault = setup.fault
            stkalign = setup.stkalign
            segments.append(setup.exception_segment)
            current_segment = setup.exception_segment.ident
            exception_detector = setup.exception_detector
            exception_hook = setup.exception_hook
            exception_guard = setup.exception_guard
    elif arch == "riscv" and current_core is not None:
        task_segment = _task_segment(task, low, high)
        segments = [task_segment]
        current_segment = task_segment.ident
        if scheduler_note is None:
            elf_image = metadata.elf if metadata is not None else None
            span = (
                analyze_riscv_trap_profile(elf_image) if elf_image is not None else None
            )
            isr_bounds = _riscv_isr_stack_bounds(elf_image)
            in_isr_stack = (
                isr_bounds is not None and isr_bounds[0] <= seed.sp <= isr_bounds[1]
            )
            in_trap_span = span is not None and (
                seed.pc == span.entry
                or span.entry <= seed.pc < span.exit_end
                or span.refused_reason(seed.pc) is not None
            )
            if in_isr_stack or in_trap_span:
                if span is None:
                    return _stop(
                        "exception-model-unverified",
                        "the compiled RISC-V trap handler could not be verified from ELF",
                    )
                xlen = 64 if selection.isa == "riscv64" else 32
                snap, snap_reason = _riscv_snapshot(xlen=xlen)
                if snap is None:
                    return _stop(
                        snap_reason,
                        "the live RISC-V exception CSRs are not readable",
                    )
                running_flag = _scheduler_running()
                if running_flag is False:
                    return _stop(
                        "scheduler-not-running",
                        "a RISC-V trap handler is active before the scheduler is started",
                    )
                current_tcb = read_int(eval_identifier("pxCurrentTCB"))
                if current_tcb is None:
                    return _stop(
                        "scheduler-current-tcb-unavailable",
                        "pxCurrentTCB is not readable while unwinding a RISC-V trap frame",
                    )
                word = xlen // 8
                endian = arch_or_default()[1]

                def _resolve_frt_trap_frame(_ctx) -> int | str:
                    raw_top = read_bytes(current_tcb, word)
                    if raw_top is None or len(raw_top) != word:
                        return "exception-frame-unreadable"
                    return int.from_bytes(
                        raw_top, "little" if endian == "little" else "big"
                    )

                setup = prepare_riscv_handler_unwind(
                    snap,
                    root_pc=_exit_pc(),
                    span=span,
                    task_segment=task_segment,
                    irq_stack_bounds=lambda: isr_bounds,
                    nest_count=None,
                    endian=endian,
                    task_frame_resolver=_resolve_frt_trap_frame,
                    allow_sync=False,
                )
                if isinstance(setup, tuple):
                    return _stop(setup[0], setup[1])
                if current_tcb != task.address:
                    return _stop(
                        "exception-task-context-conflict",
                        "pxCurrentTCB does not match the requested task",
                    )
                seed = setup.seed
                segments.append(setup.exception_segment)
                current_segment = setup.exception_segment.ident
                exception_detector = setup.exception_detector
                exception_hook = setup.exception_hook
                exception_guard = setup.exception_guard

    exit_pc = _exit_pc()
    if seed.source != "unstarted" and exit_pc is not None:
        seed = replace(seed, root_pc=exit_pc, root_reason="task-exit")
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
        segments=tuple(segments) if segments is not None else None,
        current_segment=current_segment,
        exception_detector=exception_detector,
        exception_hook=exception_hook,
        exception_guard=exception_guard,
        stkalign=stkalign,
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
        metadata=metadata,
        symbol_lookup=lookup_symbol_at,
        source_lookup=source_location_at,
        initial_messages=messages,
        fault=fault,
    )
