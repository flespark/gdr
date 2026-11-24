"""GDB-facing RT-Thread backtrace orchestration (no target writes).

Pure saved-context and metadata parsing lives in :mod:`rtthread.backtrace.unwind`
and :mod:`core.backtrace.unwind`; this module resolves one thread, reads live or saved
registers through GDB and converts the bounded result to the adapter model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core.adapter import (
    BacktraceReport,
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
    ArmSnapshot,
    FaultPrecision,
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
from core.backtrace.unwind_arm import (
    arm_snapshot as _core_arm_snapshot,
)
from core.backtrace.unwind_gdb import (
    SchedulerHandlerContext,
    arm_boot_state,
    build_arm_scheduler_handler,
    cached_unwind_metadata,
    unwind_task_or_scheduler_stage,
)
from core.backtrace.unwind_gdb import (
    live_seed as _core_live_seed,
)
from core.backtrace.unwind_gdb import live_seed_detail as _core_live_seed_detail
from core.backtrace.unwind_riscv import (
    RiscvSnapshot,
    prepare_riscv_handler_unwind,
    resolve_riscv_irq_stack_bounds_detail,
    u_xlen_mask,
)
from core.backtrace.unwind_riscv import (
    riscv_snapshot as _core_riscv_snapshot,
)
from core.backtrace.unwind_segments import (
    ExceptionDetector,
    ExceptionHook,
    ExceptionTransitionContext,
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
    value_address,
)
from core.layout import member_offset
from core.stack_frame_parser import StackContextSelection
from rtthread.adapter import Thread, value_to_thread
from rtthread.backtrace.arm_scheduler_switch import (
    ARM_SCHEDULER_SYMBOLS,
)
from rtthread.backtrace.arm_scheduler_switch import (
    build_profile as build_scheduler_profile,
)
from rtthread.backtrace.arm_scheduler_switch import (
    classify_stage as classify_scheduler_stage,
)
from rtthread.backtrace.riscv_scheduler_switch import (
    build_riscv_profile as build_riscv_scheduler_profile,
)
from rtthread.backtrace.riscv_scheduler_switch import (
    classify_riscv_stage,
    riscv_pc_in_scheduler_symbol,
)
from rtthread.backtrace.scheduler_seed import (
    SchedulerSeed,
    build_riscv_stage_seed,
    build_stage_seed,
)
from rtthread.backtrace.unwind import (
    _armv8m_psplim_valid,
    apply_hardfault_trampoline,
    decode_armv7em_saved_context,
    decode_armv8m_mainline_saved_context,
)
from rtthread.backtrace.unwind import (
    apply_switch_return_cfi as _apply_switch_return_cfi,
)
from rtthread.backtrace.unwind import (
    decode_saved_context as decode_backtrace_context,
)
from rtthread.layout import RT_THREAD_STACK_FILL
from rtthread.navigation import find_thread, get_current_thread, iter_threads
from rtthread.stack_frame_profile import (
    analyze_hardfault_trampoline,
    analyze_riscv_trap_profile,
    select_context,
)
from rtthread.stackpc import SavedPcLayout, selected_saved_pc_layout

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


def _systick_aware_guard(
    exception_number: int, pc: int, isa: str = "armv7-m"
) -> str | None:
    """Admit architecturally valid system exceptions alongside core policy."""
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
    return apply_hardfault_trampoline(
        seed,
        metadata,
        exception_ident,
        base_hook,
        analyzer=analyze_hardfault_trampoline,
    )


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
    arch: str,
    isa: str,
    root_pc: int | None,
    stack_low: int | None = None,
    stack_high: int | None = None,
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
        if (
            tz != 0
            or (psplim & 0x7) != 0
            or (not seed.exception_number and control != 2)
        ):
            return None, "unsupported-extension-context"
        if not seed.exception_number:
            if not _armv8m_psplim_valid(psplim, stack_low=stack_low, sp=seed.sp):
                return None, "unsupported-extension-context"
        elif (
            stack_low is None
            or stack_high is None
            or (
                seed.process_sp is not None
                and stack_low <= seed.process_sp <= stack_high
            )
        ) and not _armv8m_psplim_valid(psplim, stack_low=stack_low, sp=seed.process_sp):
            return None, "unsupported-extension-context"
    return seed, reason


def _arm_snapshot() -> tuple[ArmSnapshot | None, str]:
    return _core_arm_snapshot(gdb_module=gdb)


def _riscv_snapshot(*, xlen: int = 32) -> tuple[RiscvSnapshot | None, str]:
    return _core_riscv_snapshot(xlen=xlen, gdb_module=gdb)


def _riscv_call_args(xlen: int) -> tuple[int | None, int | None]:
    if gdb is None:
        return None, None
    mask = u_xlen_mask(xlen)
    try:
        frame = gdb.newest_frame()
        return (
            int(frame.read_register("a0")) & mask,
            int(frame.read_register("a1")) & mask,
        )
    except (gdb.error, AttributeError, RuntimeError, TypeError):
        return None, None


_ARM_M_ISAS = frozenset(("armv6-m", "armv7-m", "armv7e-m", "armv8-m.mainline"))
_RT_SCHEDULE_ACTIVE_SYMBOLS = frozenset(
    (
        "rt_schedule",
        "rt_schedule_insert_thread",
        "rt_schedule_remove_thread",
        "_rt_scheduler_stack_check",
    )
)


def _riscv_irq_stack_bounds(
    elf: ElfImage | None, *, hart_id: int = 0
) -> tuple[int, int, str] | None:
    return resolve_riscv_irq_stack_bounds_detail(
        symbol_lookup=linker_symbol_address,
        elf=elf,
        hart_id=hart_id,
    )


def _riscv_scheduler_decision(
    *,
    architecture: str,
    selection: StackContextSelection,
    elf: ElfImage | None,
    thread,
    current,
    low: int,
    high: int,
) -> SchedulerSeed | tuple[str, str] | None:
    """Resolve active RISC-V scheduler/trap transitions or fail closed."""
    if elf is None:
        return None
    xlen = 64 if architecture == "riscv64" else 32
    profile, profile_reason = build_riscv_scheduler_profile(elf)
    flag = read_int(eval_identifier("rt_thread_switch_interrupt_flag"))
    snapshot, snapshot_reason = _riscv_snapshot(xlen=xlen)
    if snapshot is None:
        if flag not in (None, 0):
            return (
                snapshot_reason,
                "live RISC-V registers are unavailable while a context switch is pending",
            )
        if profile is not None and flag is None:
            return (
                "scheduler-state-unavailable",
                "rt_thread_switch_interrupt_flag is not readable",
            )
        return None
    if profile is None:
        if (flag not in (None, 0)) or riscv_pc_in_scheduler_symbol(elf, snapshot.pc):
            return (
                profile_reason,
                "compiled RISC-V switch/trap code does not match a verified profile",
            )
        return None
    if flag is None:
        return (
            "scheduler-state-unavailable",
            "rt_thread_switch_interrupt_flag is not readable",
        )
    stage = classify_riscv_stage(profile, snapshot.pc)
    if flag == 0 and stage is None:
        return None
    if isinstance(stage, str):
        return stage, "the stop is inside a transition function but not a proven stage"
    if (
        flag == 0
        and stage is not None
        and stage.name in ("trap-entry", "trap-dispatch")
        and current is not None
        and value_address(current) == thread.address
    ):
        # NOTE: Un-switched trap entry and ISR dispatch unwind through
        # prepare_riscv_handler_unwind so frames keep the ISR call chain.
        return None

    from_ptr = read_int(eval_identifier("rt_interrupt_from_thread"))
    to_ptr = read_int(eval_identifier("rt_interrupt_to_thread"))
    sp_offset = member_offset("struct rt_thread", ("sp",))
    nest_count = read_int(eval_identifier("rt_interrupt_nest"))
    irq_detail = _riscv_irq_stack_bounds(elf, hart_id=max(0, thread.oncpu or 0))
    irq_bounds = (irq_detail[0], irq_detail[1]) if irq_detail is not None else None
    live_a0 = snapshot.registers.get(10)
    live_a1 = snapshot.registers.get(11)
    if live_a0 is None or live_a1 is None:
        arg0, arg1 = _riscv_call_args(xlen)
        if live_a0 is None:
            live_a0 = arg0
        if live_a1 is None:
            live_a1 = arg1
    outcome = build_riscv_stage_seed(
        profile,
        selection,
        stage,
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
        read=read_bytes,
        snapshot=snapshot,
        nest_count=nest_count,
        irq_bounds=irq_bounds,
        live_a0=live_a0,
        live_a1=live_a1,
    )
    if isinstance(outcome, str):
        return outcome, f"the pending RISC-V switch state refused unwind ({outcome})"
    return outcome


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
) -> (
    SchedulerSeed
    | tuple[SchedulerSeed, SchedulerHandlerContext]
    | tuple[str, str]
    | None
):
    """Resolve active M-profile or RISC-V scheduler transitions or fail closed."""
    if architecture in ("riscv32", "riscv64"):
        return _riscv_scheduler_decision(
            architecture=architecture,
            selection=selection,
            elf=elf,
            thread=thread,
            current=current,
            low=low,
            high=high,
        )
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
        if sym is not None and sym.name in ARM_SCHEDULER_SYMBOLS:
            return (
                "scheduler-profile-unverified",
                "compiled M-profile switch handlers do not match a verified profile",
            )
        return None
    stage = classify_scheduler_stage(profile, snapshot.pc)
    if flag == 0 and stage is None:
        return None
    irq_bounds: tuple[int, int] | None = None
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
    if (
        outcome is not None
        and stage is not None
        and stage.function == "PendSV_Handler"
        and irq_bounds is not None
    ):
        active_owner = (
            "old"
            if stage.name
            in ("pendsv-entry", "pendsv-save-pending", "pendsv-save-written")
            and from_ptr
            else "new"
        )
        if outcome.owner == active_owner:
            return (
                outcome,
                build_arm_scheduler_handler(
                    snapshot, stage_name=stage.name, bounds=irq_bounds
                ),
            )
    return outcome


@dataclass(frozen=True)
class _TargetContext:
    """Validated target bounds and metadata shared across thread unwind steps."""

    thread: Thread
    target: str
    selection: StackContextSelection
    low: int
    high: int
    current: Any
    is_current: bool
    metadata: UnwindMetadata | None
    max_depth: int

    @property
    def architecture(self) -> str:
        return self.selection.isa or "unknown"

    def stop(
        self, reason: str, message: str, *, source: str = "current"
    ) -> BacktraceReport:
        return _stop_report(
            self.thread,
            self.architecture,
            self.selection.abi,
            self.max_depth,
            reason,
            message,
            source=source,
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
    scheduler_handler: SchedulerHandlerContext | None = None


def _selected_core() -> int:
    if gdb is not None:
        try:
            thread = gdb.selected_thread()
            if thread is not None and int(thread.num) >= 1:
                return int(thread.num) - 1
        except (gdb.error, AttributeError, RuntimeError, TypeError, ValueError):
            pass
    return 0


def _exit_pc() -> int | None:
    for exit_name in ("rt_thread_exit", "_thread_exit"):
        exit_value = eval_identifier(exit_name)
        if exit_value is not None:
            return value_address(exit_value)
    return None


def _stop_report(
    thread,
    architecture,
    abi,
    max_depth,
    reason,
    message,
    *,
    source="current",
    bounds="recorded",
    target="",
):
    return BacktraceReport(
        task=target if thread is None else (thread.name or target),
        address=0 if thread is None else thread.address,
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


def _thread_from_sp_ptr(ptr: int | None, layout):
    sp_offset = member_offset("struct rt_thread", ("sp",))
    if ptr in (None, 0) or sp_offset is None:
        return None
    assert ptr is not None
    return find_thread(f"{ptr - sp_offset:#x}", layout)


def _thread_owning_sp(layout, sp: int):
    if sp <= 0:
        return None
    owner = None
    for cand in iter_threads(layout):
        info = value_to_thread(cand, layout, collect_stack_metrics=False)
        low, high = info.stack_addr, info.stack_addr + info.stack_size
        if low and info.stack_size and low <= sp <= high:
            if owner is not None:
                return None
            owner = cand
    return owner


def _resolve_live_owner(
    adapter,
    current,
    metadata: UnwindMetadata | None,
    selection: StackContextSelection,
    *,
    flag: int | None,
):
    """Return the thread (or ``None`` for boot MSP) owning the live CPU state."""
    if current is None or flag != 0:
        return current
    live_pc: int | None = None
    live_sp = 0
    if selection.isa in _ARM_M_ISAS:
        if (
            arm_boot_state(
                selection.isa,
                metadata=metadata,
                scheduler_symbols=ARM_SCHEDULER_SYMBOLS,
                snapshot_reader=_arm_snapshot,
            )
            == "pre-first-task"
        ):
            return None
        snapshot, _reason = _arm_snapshot()
        if snapshot is None:
            return current
        live_pc = snapshot.pc & ~1
        live_sp = snapshot.psp if (snapshot.xpsr & 0x1FF) else snapshot.sp
    elif selection.family == "riscv" and metadata is not None:
        xlen = 64 if selection.isa == "riscv64" else 32
        rv_snap, _reason = _riscv_snapshot(xlen=xlen)
        nest = read_int(eval_identifier("rt_interrupt_nest"))
        if rv_snap is None or nest not in (None, 0):
            return current
        live_pc, live_sp = rv_snap.pc, rv_snap.sp
        if (
            not riscv_pc_in_scheduler_symbol(metadata.elf, live_pc)
            and (sym := metadata.elf.function_containing(live_pc)) is not None
            and sym.name in ("rt_system_scheduler_start", "rt_schedule_remove_thread")
            and _thread_owning_sp(adapter.layout, live_sp) is None
        ):
            return None
    else:
        return current

    if metadata is None or live_pc is None or live_sp <= 0:
        return current
    sym = metadata.elf.function_containing(live_pc)
    if sym is None or sym.name not in _RT_SCHEDULE_ACTIVE_SYMBOLS:
        return current
    cur_info = value_to_thread(current, adapter.layout, collect_stack_metrics=False)
    cur_low, cur_high = cur_info.stack_addr, cur_info.stack_addr + cur_info.stack_size
    if cur_low and cur_info.stack_size and cur_low <= live_sp <= cur_high:
        return current
    outgoing = _thread_owning_sp(adapter.layout, live_sp)
    return current if outgoing is None else outgoing


def _current_pc_backtrace(adapter, max_depth: int) -> BacktraceReport:
    current = get_current_thread()
    selection = _backtrace_context()
    architecture = selection.isa or "unknown"
    metadata = _unwind_metadata(adapter)
    flag = read_int(eval_identifier("rt_thread_switch_interrupt_flag"))
    live_owner = _resolve_live_owner(adapter, current, metadata, selection, flag=flag)
    if live_owner is not None:
        value = live_owner
        if selection.isa in _ARM_M_ISAS and metadata is not None:
            profile = build_scheduler_profile(metadata.elf)
            snapshot, _reason = _arm_snapshot()
            if profile is not None and snapshot is not None:
                stage = classify_scheduler_stage(profile, snapshot.pc)
                use_from = (stage is None and flag == 1) or (
                    stage is not None
                    and stage.name
                    in (
                        "switch-return",
                        "pendsv-entry",
                        "pendsv-save-pending",
                        "pendsv-save-written",
                    )
                )
                if use_from:
                    outgoing = _thread_from_sp_ptr(
                        read_int(eval_identifier("rt_interrupt_from_thread")),
                        adapter.layout,
                    )
                    if outgoing is not None:
                        value = outgoing
        elif selection.family == "riscv" and metadata is not None:
            xlen = 64 if selection.isa == "riscv64" else 32
            rv_profile, _reason = build_riscv_scheduler_profile(metadata.elf)
            rv_snapshot, _snap_reason = _riscv_snapshot(xlen=xlen)
            if rv_profile is not None and rv_snapshot is not None:
                rv_stage = classify_riscv_stage(rv_profile, rv_snapshot.pc)
                sp_offset = member_offset("struct rt_thread", ("sp",))
                if (
                    not isinstance(rv_stage, str)
                    and rv_stage is not None
                    and rv_stage.name
                    in ("context-switch-entry", "context-switch-saving")
                    and sp_offset is not None
                ):
                    live_a0 = rv_snapshot.registers.get(10)
                    if live_a0 is None:
                        live_a0, _ = _riscv_call_args(xlen)
                    outgoing = (
                        find_thread(f"{live_a0 - sp_offset:#x}", adapter.layout)
                        if rv_stage.pc <= rv_profile.switch_a0_clobber_pc
                        and live_a0 not in (None, 0)
                        else _thread_owning_sp(adapter.layout, rv_snapshot.sp)
                    )
                    if outgoing is not None:
                        value = outgoing
                elif (rv_stage is None and flag == 1) or (
                    not isinstance(rv_stage, str)
                    and rv_stage is not None
                    and (
                        (rv_stage.name in ("trap-entry", "trap-dispatch") and flag == 1)
                        or (
                            rv_stage.name
                            in (
                                "trap-epilogue-before-switch-store",
                                "trap-epilogue-before-switch-load",
                            )
                            and (
                                flag == 1 or rv_stage.pc > rv_profile.trap_clear_flag_pc
                            )
                        )
                    )
                ):
                    outgoing = _thread_from_sp_ptr(
                        read_int(eval_identifier("rt_interrupt_from_thread")),
                        adapter.layout,
                    )
                    if outgoing is not None:
                        value = outgoing
        target_addr = value_address(value)
        target_label = (
            f"{target_addr:#x}"
            if isinstance(target_addr, int) and target_addr
            else "<current>"
        )
        return _backtrace_thread(adapter, value, target_label, max_depth)

    if selection.context is None or not selection.context.verified:
        return _stop_report(
            None,
            architecture,
            selection.abi,
            max_depth,
            selection.reason,
            "no verified libcpu save profile matches the image evidence",
            bounds="unknown",
            target="<current>",
        )
    if flag:
        return _stop_report(
            None,
            architecture,
            selection.abi,
            max_depth,
            "scheduler-current-tcb-unavailable",
            "rt_current_thread is unavailable while a switch is pending",
            bounds="unknown",
            target="<current>",
        )
    if selection.isa in _ARM_M_ISAS and metadata is not None:
        snapshot, _reason = _arm_snapshot()
        if snapshot is not None:
            sym = metadata.elf.function_containing(snapshot.pc & ~1)
            if sym is not None and sym.name in ARM_SCHEDULER_SYMBOLS:
                return _stop_report(
                    None,
                    architecture,
                    selection.abi,
                    max_depth,
                    "scheduler-current-tcb-unavailable",
                    "rt_current_thread is unavailable inside a scheduler handler",
                    bounds="unknown",
                    target="<current>",
                )
    elif selection.family == "riscv" and metadata is not None:
        xlen = 64 if selection.isa == "riscv64" else 32
        rv_snapshot, _reason = _riscv_snapshot(xlen=xlen)
        if rv_snapshot is not None and riscv_pc_in_scheduler_symbol(
            metadata.elf, rv_snapshot.pc
        ):
            return _stop_report(
                None,
                architecture,
                selection.abi,
                max_depth,
                "scheduler-current-tcb-unavailable",
                "rt_current_thread is unavailable inside a RISC-V scheduler handler",
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
        arm_snapshot_reader=_arm_snapshot,
    )


def backtrace(
    adapter, target: str | None = None, max_depth: int = 32
) -> BacktraceReport:
    """Unwind one RT-Thread thread (or the current halted core context)."""
    if target is None:
        return _current_pc_backtrace(adapter, max_depth)
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
    return _backtrace_thread(adapter, value, target, max_depth)


def _resolve_live_seed(ctx: _TargetContext) -> SeedFrame | BacktraceReport:
    thread = ctx.thread
    selected = _selected_core()
    if thread.oncpu is not None and thread.oncpu != selected:
        return ctx.stop(
            "smp-no-context",
            f"thread runs on core {thread.oncpu}; the selected core "
            f"{selected} has no saved context for it",
            source="current",
        )
    if ctx.architecture == "armv8-m.mainline":
        seed, live_reason = _live_seed_detail(
            ctx.selection.family,
            ctx.architecture,
            thread.entry or None,
            ctx.low,
            ctx.high,
        )
    else:
        seed, live_reason = _live_seed_detail(
            ctx.selection.family, ctx.architecture, thread.entry or None
        )
    if seed is None:
        return ctx.stop(
            live_reason,
            "the selected core's registers are not readable",
            source="current",
        )
    return seed


def _resolve_saved_seed(ctx: _TargetContext) -> SeedFrame | BacktraceReport:
    thread = ctx.thread
    context = ctx.selection.context
    if context is None:
        return ctx.stop(
            ctx.selection.reason, "no verified save profile", source="saved"
        )
    if thread.oncpu is not None:
        return ctx.stop(
            "smp-no-context",
            f"thread is running on core {thread.oncpu} and has no saved context",
            source="saved",
        )
    exit_value = eval_identifier("rt_thread_exit")
    exit_pc = value_address(exit_value) if exit_value is not None else None
    if ctx.selection.alt_context is not None and ctx.architecture in (
        "armv7e-m",
        "armv8-m.mainline",
    ):
        decode_pair = (
            decode_armv7em_saved_context
            if ctx.architecture == "armv7e-m"
            else decode_armv8m_mainline_saved_context
        )
        seed, reason = decode_pair(
            thread.sp,
            read_bytes,
            basic=context,
            fpu=ctx.selection.alt_context,
            endian=arch_or_default()[1],
            entry=thread.entry or None,
            stack_low=ctx.low,
            stack_high=ctx.high,
        )
    else:
        seed, reason = decode_backtrace_context(
            context,
            thread.sp,
            read_bytes,
            endian=arch_or_default()[1],
            entry=thread.entry or None,
            stack_low=ctx.low,
            stack_high=ctx.high,
        )
    if seed is None:
        return ctx.stop(
            reason,
            "the saved context is not readable or not proven"
            + (f" (exit {exit_pc:#x})" if exit_pc else ""),
            source="saved",
        )
    return seed


def _resolve_target_seed(
    ctx: _TargetContext,
) -> (
    tuple[
        SeedFrame,
        SchedulerSeed | None,
        str | None,
        FaultPrecision | None,
        SchedulerHandlerContext | None,
    ]
    | BacktraceReport
):
    raw_decision = _scheduler_decision(
        architecture=ctx.architecture,
        selection=ctx.selection,
        elf=ctx.metadata.elf if ctx.metadata is not None else None,
        thread=ctx.thread,
        current=ctx.current,
        low=ctx.low,
        high=ctx.high,
    )
    scheduler_handler: SchedulerHandlerContext | None = None
    if isinstance(raw_decision, tuple):
        first, second = raw_decision
        if isinstance(first, str) and isinstance(second, str):
            return ctx.stop(first, second, source="none")
        assert isinstance(first, SchedulerSeed) and not isinstance(second, str)
        scheduler_decision, scheduler_handler = first, second
    else:
        scheduler_decision = raw_decision
    if scheduler_decision is not None:
        scheduler_note = (
            f"scheduler stage {scheduler_decision.stage} "
            f"({scheduler_decision.owner}): {scheduler_decision.evidence}"
        )
        return (
            scheduler_decision.seed,
            scheduler_decision,
            scheduler_note,
            scheduler_decision.fault,
            scheduler_handler,
        )
    seed_or_err = (
        _resolve_live_seed(ctx) if ctx.is_current else _resolve_saved_seed(ctx)
    )
    if isinstance(seed_or_err, BacktraceReport):
        return seed_or_err
    return seed_or_err, None, None, None, None


def _task_segment(ctx: _TargetContext) -> StackSegment:
    """Build the process-stack segment for one target thread."""
    return StackSegment(
        ident=f"task:{ctx.thread.address:#x}",
        owner=ctx.thread.name,
        low=ctx.low,
        high=ctx.high,
        source="task-recorded",
        stack="process",
        kind="task",
    )


def _prepare_arm_handler(
    ctx: _TargetContext, seed: SeedFrame
) -> _HandlerSetup | BacktraceReport:
    if seed.process_sp is None or not ctx.low <= seed.process_sp <= ctx.high:
        return ctx.stop(
            "exception-task-context-conflict",
            "the live PSP does not belong to the requested thread",
        )
    elf = ctx.metadata.elf if ctx.metadata is not None else None
    setup = prepare_arm_handler_unwind(
        seed,
        ctx.architecture,
        scheduler_running=lambda: ctx.current is not None,
        irq_stack_bounds=lambda: resolve_arm_irq_stack_bounds_detail(
            symbol_lookup=linker_symbol_address,
            read=read_bytes,
            isa=ctx.architecture,
            elf=elf,
            live_msp=seed.sp,
            task_bounds=(ctx.low, ctx.high),
        ),
        capacity_reader=(
            lambda: resolve_armv6m_vector_limit(
                symbol_lookup=linker_symbol_address, read=read_bytes, elf=elf
            )
        )
        if ctx.architecture == "armv6-m"
        else (
            lambda: arm_external_exception_limit(
                read=read_bytes, elf=elf, symbol_lookup=linker_symbol_address
            )
        ),
        stkalign_reader=lambda: arm_ccr_stkalign(read=read_bytes),
        vector_entry_reader=(
            lambda number: armv6m_vector_entry(
                number, read=read_bytes, elf=elf, symbol_lookup=linker_symbol_address
            )
        )
        if ctx.architecture == "armv6-m"
        else (
            lambda number: armv7m_vector_entry(
                number, read=read_bytes, elf=elf, symbol_lookup=linker_symbol_address
            )
        ),
        guard_fn=_systick_aware_guard,
        capacity_guard_factory=_systick_capacity_guard,
        read=read_bytes,
    )
    if isinstance(setup, tuple):
        return ctx.stop(*setup)
    task_segment = _task_segment(ctx)
    new_meta, hook = _apply_hardfault_trampoline(
        setup.seed, ctx.metadata, setup.exception_segment.ident, setup.exception_hook
    )
    return _HandlerSetup(
        seed=setup.seed,
        fault=setup.fault,
        segments=(task_segment, setup.exception_segment),
        current_segment=setup.exception_segment.ident,
        detector=setup.exception_detector,
        hook=hook,
        guard=setup.exception_guard,
        stkalign=setup.stkalign,
        metadata=new_meta,
    )


def _prepare_riscv_handler(
    ctx: _TargetContext,
    seed: SeedFrame,
    fault: FaultPrecision | None,
    *,
    scheduler_decision,
    exit_pc: int | None,
) -> _HandlerSetup | BacktraceReport:
    thread = ctx.thread
    elf_image = ctx.metadata.elf if ctx.metadata is not None else None
    switch_flag = read_int(eval_identifier("rt_thread_switch_interrupt_flag"))
    pending_trap_owner = (
        scheduler_decision is not None
        and scheduler_decision.owner == "old"
        and scheduler_decision.stage
        in ("trap-entry", "trap-dispatch", "pending-before-trap-epilogue")
    )
    if (ctx.is_current and scheduler_decision is None) or pending_trap_owner:
        span = analyze_riscv_trap_profile(elf_image) if elf_image is not None else None
        hart_id = max(0, thread.oncpu or 0)
        irq_bounds = _riscv_irq_stack_bounds(elf_image, hart_id=hart_id)
        nest_count = read_int(eval_identifier("rt_interrupt_nest"))
        in_isr_stack = (
            irq_bounds is not None and irq_bounds[0] <= seed.sp <= irq_bounds[1]
        )
        in_trap_span = span is not None and (
            seed.pc == span.entry
            or span.entry <= seed.pc < span.exit_end
            or span.refused_reason(seed.pc) is not None
        )
        if in_isr_stack or in_trap_span or bool(nest_count):
            if switch_flag and not pending_trap_owner:
                return ctx.stop(
                    "scheduler-transition-unproven",
                    "rt_thread_switch_interrupt_flag is active during RISC-V trap",
                )
            if span is None:
                return ctx.stop(
                    "exception-model-unverified",
                    "the compiled RISC-V trap_entry could not be verified from ELF",
                )
            xlen = 64 if ctx.architecture == "riscv64" else 32
            snap, snap_reason = _riscv_snapshot(xlen=xlen)
            if snap is None:
                return ctx.stop(
                    snap_reason, "live RISC-V exception CSRs are not readable"
                )
            if span.layout.epc_offset is None and nest_count is None:
                return ctx.stop(
                    "exception-context-unproven",
                    "rt_interrupt_nest is unreadable with deferred mepc storage",
                )
            task_segment = _task_segment(ctx)
            mask = u_xlen_mask(xlen)
            endian = arch_or_default()[1]

            def _resolve_rtt_trap_frame(
                context: ExceptionTransitionContext,
            ) -> int | str:
                s0 = context.registers.get(8)
                return "exception-frame-missing" if s0 is None else (s0 & mask)

            setup = prepare_riscv_handler_unwind(
                snap,
                root_pc=exit_pc if exit_pc is not None else (thread.entry or None),
                root_reason="task-exit" if exit_pc is not None else "task-entry",
                span=span,
                task_segment=task_segment,
                irq_stack_bounds=lambda: irq_bounds,
                nest_count=nest_count,
                endian=endian,
                task_frame_resolver=_resolve_rtt_trap_frame,
                elf=elf_image,
                read=read_bytes,
            )
            if isinstance(setup, tuple):
                return ctx.stop(*setup)
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
    elif switch_flag and scheduler_decision is None:
        sp_offset = member_offset("struct rt_thread", ("sp",))
        from_thread = read_int(eval_identifier("rt_interrupt_from_thread"))
        if sp_offset is not None and from_thread == thread.address + sp_offset:
            return ctx.stop(
                "scheduler-transition-unproven",
                "thread has a pending RISC-V interrupt context switch",
            )
    return _HandlerSetup(
        seed=seed,
        fault=fault,
        segments=None,
        current_segment=None,
        metadata=ctx.metadata,
    )


def _prepare_handler_setup(
    ctx: _TargetContext,
    seed: SeedFrame,
    fault: FaultPrecision | None,
    *,
    scheduler_decision: SchedulerSeed | None,
    exit_pc: int | None,
    scheduler_handler: SchedulerHandlerContext | None = None,
) -> _HandlerSetup | BacktraceReport:
    if scheduler_handler is not None:
        task_segment = _task_segment(ctx)
        return _HandlerSetup(
            seed=seed,
            fault=fault,
            segments=(task_segment,),
            current_segment=task_segment.ident,
            metadata=ctx.metadata,
            scheduler_handler=scheduler_handler,
        )
    if seed.exception_number:
        return _prepare_arm_handler(ctx, seed)
    if ctx.selection.family == "riscv":
        return _prepare_riscv_handler(
            ctx,
            seed,
            fault,
            scheduler_decision=scheduler_decision,
            exit_pc=exit_pc,
        )
    return _HandlerSetup(
        seed=seed,
        fault=fault,
        segments=None,
        current_segment=None,
        metadata=ctx.metadata,
    )


def _backtrace_thread(
    adapter, value, target: str, max_depth: int = 32
) -> BacktraceReport:
    selection = _backtrace_context()
    thread = value_to_thread(value, adapter.layout, collect_stack_metrics=False)
    context = selection.context
    architecture = selection.isa or "unknown"
    if context is None or not context.verified:
        return _stop_report(
            thread,
            architecture,
            selection.abi,
            max_depth,
            selection.reason,
            "no verified libcpu save profile matches the image evidence",
            source="none",
            bounds="unknown",
            target=target,
        )
    low = thread.stack_addr if thread.stack_addr else None
    high = low + thread.stack_size if low is not None and thread.stack_size else None
    if low is None or high is None:
        return _stop_report(
            thread,
            architecture,
            selection.abi,
            max_depth,
            "stack-bounds-unavailable",
            "the thread records no usable stack address and size",
            source="none",
            bounds="unknown",
            target=target,
        )
    grows_up = (
        _stack_growth(low, high)
        if thread.stack_grows_up is None
        else thread.stack_grows_up
    )
    if grows_up is not False:
        reason = (
            "unsupported-stack-direction" if grows_up else "stack-direction-unavailable"
        )
        message = (
            "stack growth direction is upward; only downward stacks are supported"
            if grows_up
            else "stack growth direction cannot be proven from configuration or fill sentinels"
        )
        return _stop_report(
            thread,
            architecture,
            selection.abi,
            max_depth,
            reason,
            message,
            source="none",
            target=target,
        )
    current = get_current_thread()
    metadata = _unwind_metadata(adapter)
    flag = read_int(eval_identifier("rt_thread_switch_interrupt_flag"))
    live_owner = _resolve_live_owner(adapter, current, metadata, selection, flag=flag)
    ctx = _TargetContext(
        thread=thread,
        target=target,
        selection=selection,
        low=low,
        high=high,
        current=current,
        is_current=live_owner is not None
        and value_address(live_owner) == thread.address,
        metadata=metadata,
        max_depth=max_depth,
    )
    resolved = _resolve_target_seed(ctx)
    if isinstance(resolved, BacktraceReport):
        return resolved
    seed, scheduler_decision, scheduler_note, fault, scheduler_handler = resolved
    exit_pc = _exit_pc()
    if seed.source != "unstarted" and exit_pc is not None:
        seed = replace(seed, root_pc=exit_pc, root_reason="task-exit")
    handler = _prepare_handler_setup(
        ctx,
        seed,
        fault,
        scheduler_decision=scheduler_decision,
        exit_pc=exit_pc,
        scheduler_handler=scheduler_handler,
    )
    if isinstance(handler, BacktraceReport):
        return handler
    meta = _apply_switch_return_cfi(handler.metadata, scheduler_decision)
    result = unwind_task_or_scheduler_stage(
        handler.seed,
        meta,
        read=read_bytes,
        max_depth=max_depth,
        stack_low=low,
        stack_high=high,
        scheduler_handler=handler.scheduler_handler,
        allow_frame_pointer=architecture != "armv7-a",
        segments=handler.segments,
        current_segment=handler.current_segment,
        exception_detector=handler.detector,
        exception_hook=handler.hook,
        exception_guard=handler.guard,
        stkalign=handler.stkalign,
        evidence=scheduler_note or "",
        unwinder=unwind_chain,
    )
    initial_messages = [scheduler_note] if scheduler_note is not None else []
    return build_backtrace_report(
        task=thread.name or target,
        address=thread.address,
        architecture=architecture,
        abi=selection.abi,
        source=handler.seed.source,
        bounds="recorded",
        max_depth=max_depth,
        result=result,
        metadata=meta,
        symbol_lookup=lookup_symbol_at,
        source_lookup=source_location_at,
        initial_messages=initial_messages,
        fault=handler.fault,
    )
