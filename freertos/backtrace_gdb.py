"""GDB-facing FreeRTOS backtrace orchestration (no target writes).

The pure context layout and ELF/CFI/EHABI parsers live in
:mod:`freertos.backtrace` and :mod:`core.unwind`; this module only resolves
the task, reads live/saved registers through GDB, supplies the linker-owned
interrupt-stack span and exception profile gates, and turns one bounded
:class:`core.unwind.UnwindResult` into the adapter presentation model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.adapter import BacktraceFrame, BacktraceReport, session_meta
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
from core.unwind import ElfImage, SeedFrame, UnwindMetadata, unwind_chain
from core.unwind_arm import (
    armv6m_exception_number_kind,
    armv6m_exception_return_candidate,
    armv6m_exception_transition,
    armv7m_exception_number_kind,
    armv7m_exception_return_candidate,
    armv7m_exception_transition,
    armv7m_max_external_exception,
)
from core.unwind_segments import StackSegment
from freertos.adapter import FreeRtosTask, find_task, value_to_task
from freertos.backtrace import decode_saved_context as decode_backtrace_context
from freertos.navigation import current_tasks, saved_context_bounds, task_state
from freertos.scheduler_switch import build_profile as build_scheduler_profile
from freertos.scheduler_switch import build_stage_seed, classify_stage

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


@dataclass(frozen=True)
class _ArmSnapshot:
    """Raw live Arm registers; the scheduler stages read the same snapshot."""

    pc: int
    sp: int
    msp: int
    psp: int
    xpsr: int
    registers: dict[int, int]


def _arm_snapshot() -> tuple[_ArmSnapshot | None, str]:
    """Read Arm live registers without rejecting an unset PSP.

    The ordinary live seed still refuses a zero PSP; the scheduler path needs
    the raw value so it can prove the SVC first-task restore itself.
    """
    if gdb is None:
        return None, "live-registers-unavailable"
    try:
        frame = gdb.newest_frame()
        pc = int(frame.pc()) & 0xFFFFFFFF
        sp = int(frame.read_register("sp")) & 0xFFFFFFFF
    except (gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers: dict[int, int] = {}
    for register in range(13):
        try:
            registers[register] = int(frame.read_register(f"r{register}")) & 0xFFFFFFFF
        except (gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
            return None, "live-registers-unavailable"
    registers[13] = sp
    try:
        registers[14] = int(frame.read_register("lr")) & 0xFFFFFFFF
        registers[15] = pc
        xpsr = int(frame.read_register("xpsr")) & 0xFFFFFFFF
        msp = int(frame.read_register("msp")) & 0xFFFFFFFF
        psp = int(frame.read_register("psp")) & 0xFFFFFFFF
    except (gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "exception-registers-unavailable"
    return _ArmSnapshot(pc, sp, msp, psp, xpsr, registers), "ok"


def _live_seed_detail(
    arch: str, isa: str | None = None
) -> tuple[SeedFrame | None, str]:
    """Read the selected core's live registers without driving the target.

    ``isa`` is the selected profile evidence.  A live handler seed requires it
    so architecture-specific exception-number rules are applied only to the
    matching profile; an unknown profile is refused rather than assumed.
    """
    if arch == "arm":
        snapshot, reason = _arm_snapshot()
        if snapshot is None:
            return None, reason
        exception_number = snapshot.xpsr & 0x1FF
        if snapshot.msp <= 0 or snapshot.psp <= 0:
            return None, "live-registers-unavailable"
        if exception_number:
            if not snapshot.xpsr & (1 << 24):
                return None, "exception-frame-not-thumb"
            if isa is None:
                return None, "unsupported-exception-model"
            classifier = (
                armv6m_exception_number_kind
                if isa == "armv6-m"
                else armv7m_exception_number_kind
            )
            if (
                isa in ("armv6-m", "armv7-m")
                and classifier(exception_number) == "invalid"
            ):
                return None, "exception-number-invalid"
            if snapshot.sp != snapshot.msp:
                return None, "live-stack-selection-invalid"
        return (
            SeedFrame(
                pc=snapshot.pc & ~1,
                sp=snapshot.sp,
                regs=dict(snapshot.registers),
                source="current",
                process_sp=snapshot.psp,
                exception_number=exception_number,
            ),
            "ok",
        )
    if gdb is None:
        return None, "live-registers-unavailable"
    try:
        frame = gdb.newest_frame()
        pc = int(frame.pc())
        sp = int(frame.read_register("sp"))
    except (gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers: dict[int, int] = {}
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
            return None, "live-registers-unavailable"
    registers[2] = sp
    return (
        SeedFrame(pc=pc, sp=sp, regs=registers, source="current"),
        "ok",
    )


def _live_seed(arch: str, isa: str | None = None) -> SeedFrame | None:
    """Compatibility wrapper returning only the decoded live seed."""
    seed, _reason = _live_seed_detail(arch, isa)
    return seed


def _task_bounds(task: FreeRtosTask) -> tuple[int | None, int | None, str]:
    """Return ``(low, high, provenance)`` for this task's stack."""
    if task.stack_base <= 0:
        return None, None, "unknown"
    if task.stack_size is not None and task.stack_size > 0:
        return task.stack_base, task.stack_base + task.stack_size, "recorded"
    return task.stack_base, None, "unknown"


def _irq_stack_bounds() -> tuple[int, int] | None:
    """Return the linker-owned MSP span, or ``None`` when it is not exported."""
    low = linker_symbol_address("__gdr_irq_stack_low")
    high = linker_symbol_address("__gdr_irq_stack_top")
    if low is None or high is None or low <= 0 or high <= low:
        return None
    return low, high


def _ccr_stkalign() -> bool | None:
    """Read CCR.STKALIGN without writing target state."""
    raw = read_bytes(0xE000ED14, 4)
    if raw is None or len(raw) != 4:
        return None
    return bool(int.from_bytes(raw, "little") & (1 << 9))


def _external_exception_limit() -> int | None:
    """Read ICTR and return the highest implemented external IPSR, or ``None``."""
    raw = read_bytes(0xE000E004, 4)
    if raw is None or len(raw) != 4:
        return None
    return armv7m_max_external_exception(int.from_bytes(raw, "little"))


def _armv6m_port_limit() -> int | None:
    """Verify the M0 fixture's fixed vector map and explicit IRQ-count fact."""
    if session_meta()[1] != "11.1.0":
        return None
    cpuid = read_bytes(0xE000ED00, 4)
    if cpuid is None or len(cpuid) != 4:
        return None
    if int.from_bytes(cpuid, "little") & 0xFF0FFFF0 != 0x410CC200:
        return None
    base = linker_symbol_address("g_pfnVectors")
    end = linker_symbol_address("__gdr_vector_table_end")
    count = linker_symbol_address("__gdr_armv6m_irq_count")
    if base != 0 or count != 32 or end != (16 + count) * 4:
        return None
    return 16 + count - 1


def _armv6m_vector_entry(exception_number: int) -> int | None:
    """Read a vector from the proven M0 fixed-zero vector map, never VTOR."""
    if not 0 < exception_number <= 47 or armv6m_exception_number_kind(
        exception_number
    ) in ("thread", "invalid"):
        return None
    raw = read_bytes(4 * exception_number, 4)
    if raw is None or len(raw) != 4:
        return None
    value = int.from_bytes(raw, "little")
    return value & ~1 if value & 1 else None


def _vector_entry(exception_number: int) -> int | None:
    """Return the current vector-table target for one exception number."""
    raw_vtor = read_bytes(0xE000ED08, 4)
    if raw_vtor is None or len(raw_vtor) != 4 or exception_number <= 0:
        return None
    vtor = int.from_bytes(raw_vtor, "little")
    raw_entry = read_bytes(vtor + 4 * exception_number, 4)
    if raw_entry is None or len(raw_entry) != 4:
        return None
    entry = int.from_bytes(raw_entry, "little")
    return entry & ~1 if entry else None


def _scheduler_running() -> bool | None:
    """Read ``xSchedulerRunning``; ``None`` when the symbol is unavailable."""
    value = read_int(eval_identifier("xSchedulerRunning"))
    if value is None:
        return None
    return value != 0


def _exception_guard(
    exception_number: int, pc: int, isa: str = "armv7-m"
) -> str | None:
    """Refuse exception frames whose interrupted PC is not a proven resume PC."""
    del pc
    classifier = (
        armv6m_exception_number_kind
        if isa == "armv6-m"
        else armv7m_exception_number_kind
    )
    kind = classifier(exception_number)
    if kind == "invalid":
        return "exception-number-invalid"
    if kind == "scheduler":
        return "scheduler-transition-unproven"
    if kind == "fault":
        return "exception-frame-type-unproven"
    if kind == "system":
        return "exception-number-unverified"
    return None


def _capacity_exception_guard(max_exception: int, isa: str = "armv7-m"):
    """Return a guard that refuses external IDs beyond target ICTR capacity."""

    def guard(exception_number: int, pc: int) -> str | None:
        reason = _exception_guard(exception_number, pc, isa)
        if reason is not None:
            return reason
        if exception_number >= 16 and exception_number > max_exception:
            return "exception-number-unimplemented"
        return None

    return guard


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
    ("PendSV_Handler", "SVC_Handler", "prvPortStartFirstTask", "vTaskSwitchContext")
)


def _scheduler_live_decision(
    elf: ElfImage,
    context,
    task: FreeRtosTask,
    low: int,
    high: int,
) -> _SchedulerDecision | None:
    """Classify a live Arm stop without assuming every stop is an IRQ.

    ``None`` means the PC is not a verified scheduler stage and the ordinary
    live/saved path continues.  A refusal is returned for a PC inside one of
    the port's transition functions when the profile or its invariants do not
    match.
    """
    if session_meta()[1] != "10.3.1":
        return None
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
                    "the compiled scheduler transition does not match the "
                    "verified Arm Cortex-M3 profile"
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
    irq_bounds = _irq_stack_bounds()
    if irq_bounds is None:
        return _SchedulerDecision(
            reason="exception-stack-bounds-unavailable",
            message="no explicit linker/port symbols define the interrupt stack span",
        )
    irq_low, irq_high = irq_bounds
    if not irq_low <= snapshot.msp <= irq_high:
        return _SchedulerDecision(
            reason="exception-stack-outside-bounds",
            message="the live MSP is outside the linker-owned interrupt stack",
        )
    stkalign = _ccr_stkalign()
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
        if arch == "arm" and selection.isa == "armv7-m" and metadata is not None:
            decision = _scheduler_live_decision(metadata.elf, context, task, low, high)
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
    handler_mode = arch == "arm" and bool(seed.exception_number)
    if arch == "arm":
        task_segment = _task_segment(task, low, high)
        segments = [task_segment]
        current_segment = task_segment.ident
        if handler_mode:
            if selection.isa not in ("armv6-m", "armv7-m"):
                return _stop(
                    "unsupported-exception-model",
                    "handler-mode seeds require a verified Armv6-M/Armv7-M model",
                )
            guard_reason = _exception_guard(
                seed.exception_number, seed.pc, selection.isa
            )
            if guard_reason == "scheduler-transition-unproven":
                return _stop(
                    guard_reason,
                    "the stop is inside the scheduler's SVC/PendSV transition path",
                )
            if guard_reason == "exception-frame-type-unproven":
                return _stop(
                    guard_reason,
                    "a synchronous fault frame does not name a proven resume PC",
                )
            if guard_reason == "exception-number-unverified":
                return _stop(
                    guard_reason,
                    "the live exception number is architecturally valid but "
                    "outside the verified external-IRQ proof scope",
                )
            if guard_reason == "exception-number-invalid":
                return _stop(
                    guard_reason,
                    "the live IPSR names no architecturally valid exception",
                )
            running_scheduler = _scheduler_running()
            if running_scheduler is None:
                return _stop(
                    "scheduler-state-unavailable",
                    "xSchedulerRunning is not readable; task ownership is unproven",
                )
            if not running_scheduler:
                return _stop(
                    "scheduler-not-running",
                    "handler mode without a running scheduler has no task owner",
                )
            irq_bounds = _irq_stack_bounds()
            if irq_bounds is None:
                return _stop(
                    "exception-stack-bounds-unavailable",
                    "no explicit linker/port symbols define the interrupt stack span",
                )
            irq_low, irq_high = irq_bounds
            if not irq_low <= seed.sp <= irq_high:
                return _stop(
                    "exception-stack-outside-bounds",
                    "the live MSP is outside the linker-owned interrupt stack",
                )
            capacity = (
                _armv6m_port_limit()
                if selection.isa == "armv6-m"
                else _external_exception_limit()
            )
            if capacity is None:
                return _stop(
                    "exception-capacity-unavailable",
                    "the target's implemented external interrupt capacity is not readable",
                )
            if seed.exception_number >= 16 and seed.exception_number > capacity:
                return _stop(
                    "exception-number-unimplemented",
                    "the live IPSR names an external exception beyond the target's "
                    "implemented interrupt capacity",
                )
            alignment = True if selection.isa == "armv6-m" else _ccr_stkalign()
            if alignment is None:
                return _stop(
                    "exception-alignment-unavailable",
                    "CCR.STKALIGN is not readable; the frame layout is unproven",
                )
            stkalign = alignment
            exception_segment = StackSegment(
                ident=f"exception:{seed.exception_number}",
                owner=f"exception:{seed.exception_number}",
                low=irq_low,
                high=irq_high,
                source="linker:__gdr_irq_stack_low..__gdr_irq_stack_top",
                stack="main",
                kind="exception",
            )
            segments.append(exception_segment)
            current_segment = exception_segment.ident
            entry = (
                _armv6m_vector_entry(seed.exception_number)
                if selection.isa == "armv6-m"
                else _vector_entry(seed.exception_number)
            )
            if entry is not None and entry == seed.pc:
                seed = replace(seed, at_exception_entry=True)
            exception_detector = (
                armv6m_exception_return_candidate
                if selection.isa == "armv6-m"
                else armv7m_exception_return_candidate
            )
            exception_hook = (
                armv6m_exception_transition
                if selection.isa == "armv6-m"
                else armv7m_exception_transition
            )
            exception_guard = _capacity_exception_guard(capacity, selection.isa)

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
    frames = []
    for item in result.frames:
        symbol = lookup_symbol_at(item.address_in_block)
        source = source_location_at(item.address_in_block)
        frames.append(
            BacktraceFrame(
                index=item.index,
                pc=item.pc,
                method=item.method,
                symbol=symbol,
                source=source,
                segment=item.segment,
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
    messages.extend(
        f"exception transition {transition.from_segment} -> "
        f"{transition.to_segment} at {transition.pc:#x} "
        f"({transition.evidence})"
        for transition in result.transitions
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
        segments=list(result.segments),
        transitions=list(result.transitions),
    )
