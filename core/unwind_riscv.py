"""RISC-V M-mode trap frame recovery, CSR inspection and handler setup (pure + GDB helpers).

This module is RTOS-agnostic. It splits pure trap decoding/guards from GDB CSR
snapshots and ELF/linker interrupt-stack discovery so adapters share one
fail-closed RISC-V cross-stack unwind mechanism.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .cpus import RISCV_CONVENTION
from .unwind_arm import FaultPrecision
from .unwind_meta import ElfImage
from .unwind_segments import (
    ExceptionDetector,
    ExceptionHook,
    ExceptionTransitionContext,
    ExceptionTransitionResult,
    StackSegment,
    TransitionGuard,
)

try:
    import gdb  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised under unit mocks
    gdb = None

MSTATUS_MIE = 1 << 3
MSTATUS_MPIE = 1 << 7
MSTATUS_MPP_MASK = 0x3 << 11
MSTATUS_MPP_MACHINE = 0x3 << 11

MTVEC_MODE_MASK = 0x3
MTVEC_MODE_DIRECT = 0
MTVEC_MODE_VECTORED = 1

MCAUSE_EXC_INST_MISALIGNED = 0
MCAUSE_EXC_ECALL_M = 11

_RISCV_LIVE_REGISTERS: dict[int, str] = dict(RISCV_CONVENTION.live_registers)

# Wait — supervisor/hypervisor-only interrupt numbers are outside standard M-mode ports.
_VALID_MMODE_INTERRUPT_CODES = frozenset((3, 7, 11))

_RISCV_SYNC_FAULT_NAMES: dict[int, str] = {
    0: "InstAddrMisaligned",
    1: "InstAccessFault",
    2: "IllegalInstruction",
    4: "LoadAddrMisaligned",
    5: "LoadAccessFault",
    6: "StoreAddrMisaligned",
    7: "StoreAccessFault",
}


def u_xlen_mask(xlen: int) -> int:
    """Return the bitmask for an XLEN-wide unsigned integer register."""
    return (1 << xlen) - 1


def decode_riscv_mcause(mcause: int, xlen: int) -> tuple[bool, int]:
    """Split raw ``mcause`` into ``(is_interrupt, cause_code)``."""
    value = mcause & u_xlen_mask(xlen)
    interrupt_bit = 1 << (xlen - 1)
    return bool(value & interrupt_bit), value & (interrupt_bit - 1)


def riscv_fault_precision(
    mcause: int,
    xlen: int,
    *,
    mepc: int | None = None,
    mtval: int | None = None,
) -> FaultPrecision | None:
    """Return precise synchronous fault diagnostics for modeled RISC-V fault codes."""
    is_interrupt, code = decode_riscv_mcause(mcause, xlen)
    if is_interrupt or code == MCAUSE_EXC_ECALL_M:
        return None
    name = _RISCV_SYNC_FAULT_NAMES.get(code)
    if name is None:
        return None
    mask = u_xlen_mask(xlen)
    parts = [f"mcause={mcause & mask:#x}"]
    if mepc is not None:
        parts.append(f"mepc={mepc & mask:#x}")
    if mtval is not None:
        parts.append(f"mtval={mtval & mask:#x}")
    return FaultPrecision(
        precise=True,
        fault=f"Exception:{name}",
        evidence=" ".join(parts),
    )


def riscv_exception_guard(
    exception_number: int,
    pc: int,
    *,
    xlen: int = 32,
    allow_sync: bool = True,
    allow_ecall: bool = False,
) -> str | None:
    """Refuse scheduler ecalls (unless proven) and unsupported RISC-V trap causes."""
    del pc
    is_interrupt, code = decode_riscv_mcause(exception_number, xlen)
    if is_interrupt:
        if code in _VALID_MMODE_INTERRUPT_CODES or code >= 16:
            return None
        return "exception-number-unverified"
    if code == MCAUSE_EXC_ECALL_M:
        return None if allow_ecall else "scheduler-transition-unproven"
    if not allow_sync:
        return "exception-frame-type-unproven"
    if code > 63:
        return "exception-number-invalid"
    if code not in _RISCV_SYNC_FAULT_NAMES:
        return "exception-frame-type-unproven"
    return None


@dataclass(frozen=True)
class RiscvSnapshot:
    """Raw live RISC-V GPR and M-mode CSR snapshot."""

    xlen: int
    pc: int
    sp: int
    mstatus: int
    mcause: int
    mepc: int
    mtvec: int
    registers: dict[int, int]
    mtval: int | None = None


def riscv_snapshot(
    *,
    xlen: int = 32,
    gdb_module: Any = None,
) -> tuple[RiscvSnapshot | None, str]:
    """Read RISC-V GPRs and M-mode CSRs cleanly without driving the target."""
    active_gdb = gdb if gdb_module is None else gdb_module
    if active_gdb is None:
        return None, "live-registers-unavailable"
    mask = u_xlen_mask(xlen)
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc()) & mask
        sp = int(frame.read_register("sp")) & mask
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers: dict[int, int] = {}
    for register, name in _RISCV_LIVE_REGISTERS.items():
        try:
            registers[register] = int(frame.read_register(name)) & mask
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
            return None, "live-registers-unavailable"
    registers[2] = sp
    try:
        # NOTE: GDB exposes XLEN CSRs and 32-bit addresses as signed integers;
        # mask to XLEN bits so interrupt MSBs (mcause) and RAM PCs stay unsigned.
        mstatus = int(frame.read_register("mstatus")) & mask
        mcause = int(frame.read_register("mcause")) & mask
        mepc = int(frame.read_register("mepc")) & mask
        mtvec = int(frame.read_register("mtvec")) & mask
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "exception-registers-unavailable"
    mtval: int | None
    try:
        mtval = int(frame.read_register("mtval")) & mask
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        mtval = None
    return (
        RiscvSnapshot(xlen, pc, sp, mstatus, mcause, mepc, mtvec, registers, mtval),
        "ok",
    )


def riscv_live_seed_detail(
    isa: str | None = None,
    *,
    root_pc: int | None = None,
    require_exception_registers: bool = False,
    gdb_module: Any = None,
    snapshot_reader: Callable[[], tuple[RiscvSnapshot | None, str]] | None = None,
) -> tuple[Any, str]:
    """Read live RISC-V registers into a SeedFrame without probing CSRs unless asked."""
    from .unwind import SeedFrame

    xlen = 64 if isa == "riscv64" else 32
    mask = u_xlen_mask(xlen)
    if require_exception_registers or snapshot_reader is not None:
        snap, reason = (
            snapshot_reader()
            if snapshot_reader is not None
            else riscv_snapshot(xlen=xlen, gdb_module=gdb_module)
        )
        if snap is None:
            return None, reason
        return (
            SeedFrame(
                pc=snap.pc,
                sp=snap.sp,
                regs=dict(snap.registers),
                source="current",
                root_pc=root_pc,
                exception_number=snap.mcause,
            ),
            "ok",
        )
    active_gdb = gdb if gdb_module is None else gdb_module
    if active_gdb is None:
        return None, "live-registers-unavailable"
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc()) & mask
        sp = int(frame.read_register("sp")) & mask
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers: dict[int, int] = {}
    for register, name in _RISCV_LIVE_REGISTERS.items():
        try:
            registers[register] = int(frame.read_register(name)) & mask
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
            return None, "live-registers-unavailable"
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


@dataclass(frozen=True)
class RiscvTrapFrameLayout:
    """Verified trap-entry saved-register frame geometry on the interrupted stack."""

    frame_bytes: int
    total_frame_bytes: int
    reg_offsets: dict[int, int]
    epc_offset: int | None
    mstatus_offset: int | None = None
    epc_sync_addend: int = 0
    frame_pointer_register: int | None = None


@dataclass(frozen=True)
class RiscvTrapSpan:
    """Compiled assembly trap handler ranges and refusal intervals."""

    entry: int
    save_complete: int
    call_start: int
    call_end: int
    exit_end: int
    layout: RiscvTrapFrameLayout
    refused_spans: tuple[tuple[int, int, str], ...] = ()
    evidence: tuple[str, ...] = ()

    def detect_pc(self, pc: int) -> bool:
        """Whether ``pc`` lies inside the trap handler dispatch or entry point."""
        return pc == self.entry or self.call_start <= pc < self.call_end

    def refused_reason(self, pc: int) -> str | None:
        """Return an explicit fail-closed reason if ``pc`` is in a transitional span."""
        for low, high, reason in self.refused_spans:
            if low <= pc < high:
                return reason
        return None


@dataclass(frozen=True)
class RiscvTrapSetup:
    """Validated RISC-V trap unwind configuration and transition callbacks."""

    seed: Any
    exception_segment: StackSegment
    exception_detector: ExceptionDetector
    exception_hook: ExceptionHook
    exception_guard: TransitionGuard
    fault: FaultPrecision | None = None


def resolve_riscv_irq_stack_bounds_detail(
    *,
    symbol_lookup: Callable[[str], int | None] | None = None,
    elf: ElfImage | None = None,
    stack_object_bounds: Callable[[], tuple[int, int] | None] | None = None,
    stack_start_symbol: str = "__stack_start__",
    stack_size_symbol: str = "__STACKSIZE__",
    hart_id: int = 0,
    symbol_pairs: tuple[tuple[str, str], ...] = (
        ("__StackLimit", "_sp"),
        ("__stack_limit", "_stack_top"),
        ("__irq_stack_start", "__irq_stack_end"),
    ),
    section_names: tuple[str, ...] = (".irq_stack", ".isr_stack", ".stack"),
) -> tuple[int, int, str] | None:
    """Resolve the active RISC-V interrupt stack span from explicit image evidence."""
    if stack_object_bounds is not None:
        bounds = stack_object_bounds()
        if bounds is not None and bounds[0] < bounds[1]:
            return bounds[0], bounds[1], "symbol:isr_stack"
    for low_name, high_name in symbol_pairs:
        low = symbol_lookup(low_name) if symbol_lookup is not None else None
        if low is None and elf is not None:
            low = elf.symbol_address(low_name)
        high = symbol_lookup(high_name) if symbol_lookup is not None else None
        if high is None and elf is not None:
            high = elf.symbol_address(high_name)
        if low is not None and high is not None and low < high:
            return low, high, f"linker:{low_name}..{high_name}"
    start = symbol_lookup(stack_start_symbol) if symbol_lookup is not None else None
    if start is None and elf is not None:
        start = elf.symbol_address(stack_start_symbol)
    size = symbol_lookup(stack_size_symbol) if symbol_lookup is not None else None
    if start is not None and size is not None and size > 0 and hart_id >= 0:
        low = start + hart_id * size
        return low, low + size, f"linker:{stack_start_symbol}+{stack_size_symbol}"
    if elf is not None:
        for sec_name in section_names:
            sec_bounds = elf.section_bounds(sec_name)
            if sec_bounds is not None and sec_bounds[0] < sec_bounds[1]:
                return sec_bounds[0], sec_bounds[1], f"section:{sec_name}"
    return None


def riscv_trap_transition_factory(
    *,
    xlen: int,
    endian: str,
    mcause: int,
    mepc_csr: int | None,
    nest_count: int | None,
    span: RiscvTrapSpan,
    task_frame_resolver: Callable[[ExceptionTransitionContext], int | str],
    require_machine_mpp: bool = True,
) -> ExceptionHook:
    """Build the ``ExceptionHook`` that crosses from ISR stack back to task stack."""
    word = xlen // 8
    mask = u_xlen_mask(xlen)
    byteorder = "little" if endian == "little" else "big"
    is_interrupt, cause_code = decode_riscv_mcause(mcause, xlen)

    def hook(
        context: ExceptionTransitionContext,
    ) -> ExceptionTransitionResult | str | None:
        if context.guard is not None:
            guard_reason = context.guard(mcause, context.value)
            if guard_reason is not None:
                return guard_reason
        refused = span.refused_reason(context.value)
        if refused is not None:
            return refused
        if context.current_segment.kind != "exception":
            return None
        task_segments = [
            segment
            for segment in context.segments.values()
            if segment.stack == "process" and segment.kind == "task"
        ]
        if len(task_segments) != 1:
            return "exception-target-segment-missing"
        task_segment = task_segments[0]

        # At the very first instruction of trap entry, hardware has not saved
        # GPRs or switched SP; the live registers + mepc are already the task context.
        if context.value == span.entry:
            if mepc_csr is None or mepc_csr <= 0:
                return "exception-context-unproven"
            live_regs = {k: v & mask for k, v in context.registers.items()}
            live_regs[2] = context.frame_sp & mask
            return ExceptionTransitionResult(
                segment=task_segment,
                pc=mepc_csr & ~1,
                sp=context.frame_sp & mask,
                registers=live_regs,
                method="exception",
                source="riscv-trap-entry-live",
                bytes_read=0,
                exact_pc=True,
                value=context.value,
                evidence=(
                    f"mcause={mcause:#x}",
                    f"mepc={mepc_csr:#x}",
                    *span.evidence,
                ),
            )

        frame_addr = task_frame_resolver(context)
        if isinstance(frame_addr, str):
            return frame_addr
        layout = span.layout
        if not task_segment.contains(frame_addr, layout.total_frame_bytes):
            return "exception-frame-outside-bounds"
        if layout.frame_bytes > context.remaining_exception_bytes:
            return "exception-read-budget-exceeded"
        raw = context.read(frame_addr, layout.frame_bytes)
        if raw is None or len(raw) != layout.frame_bytes:
            return "exception-frame-unreadable"

        def _word(offset: int) -> int:
            return int.from_bytes(raw[offset : offset + word], byteorder) & mask

        if require_machine_mpp and layout.mstatus_offset is not None:
            saved_mstatus = _word(layout.mstatus_offset)
            if (saved_mstatus & MSTATUS_MPP_MASK) != MSTATUS_MPP_MACHINE:
                return "unsupported-exception-model"

        if layout.epc_offset is not None:
            stored_epc = _word(layout.epc_offset)
            if not is_interrupt and layout.epc_sync_addend:
                if mepc_csr is not None and stored_epc == (
                    (mepc_csr + layout.epc_sync_addend) & mask
                ):
                    interrupted_pc = mepc_csr
                elif stored_epc > layout.epc_sync_addend:
                    interrupted_pc = stored_epc - layout.epc_sync_addend
                else:
                    return "exception-context-unproven"
            else:
                interrupted_pc = stored_epc
        else:
            # NOTE: Some trap ports defer storing epc onto the task frame until
            # post-handler context switch; only single-level traps keep epc in mepc.
            if nest_count is not None and nest_count > 1:
                return "nested-trap-epc-unproven"
            if mepc_csr is None or mepc_csr <= 0:
                return "exception-context-unproven"
            interrupted_pc = mepc_csr

        if interrupted_pc <= 0 or (interrupted_pc & 1):
            return "exception-frame-pc-invalid"
        interrupted_sp = (frame_addr + layout.total_frame_bytes) & mask
        recovered: dict[int, int] = {
            reg: _word(offset) for reg, offset in layout.reg_offsets.items()
        }
        recovered[2] = interrupted_sp
        epc_src = "stacked-epc" if layout.epc_offset is not None else "mepc-csr"
        return ExceptionTransitionResult(
            segment=task_segment,
            pc=interrupted_pc,
            sp=interrupted_sp,
            registers=recovered,
            method="exception",
            source=f"riscv-trap-frame:{epc_src}",
            bytes_read=layout.frame_bytes,
            exact_pc=True,
            value=context.value,
            evidence=(
                f"mcause={mcause:#x}({'irq' if is_interrupt else 'exc'}:{cause_code})",
                f"frame={frame_addr:#x}",
                f"epc={interrupted_pc:#x}({epc_src})",
                *span.evidence,
            ),
        )

    return hook


def prepare_riscv_handler_unwind(
    snapshot: RiscvSnapshot,
    *,
    root_pc: int | None,
    root_reason: str = "task-entry",
    span: RiscvTrapSpan,
    task_segment: StackSegment,
    irq_stack_bounds: Callable[[], tuple[int, int, str] | None],
    nest_count: int | None,
    endian: str,
    task_frame_resolver: Callable[[ExceptionTransitionContext], int | str],
    allow_sync: bool = True,
    allow_ecall: bool = False,
    require_machine_mpp: bool = True,
) -> RiscvTrapSetup | tuple[str, str]:
    """Validate RISC-V live trap state, CSRs and stack bounds before unwinding."""
    from .unwind import SeedFrame

    pc = snapshot.pc
    if span.refused_reason(pc) is not None:
        reason = span.refused_reason(pc)
        assert reason is not None
        return (
            reason,
            f"stopped inside non-unwindable trap transition region at {pc:#x}",
        )

    mtvec_mode = snapshot.mtvec & MTVEC_MODE_MASK
    mtvec_base = snapshot.mtvec & ~MTVEC_MODE_MASK
    if mtvec_mode != MTVEC_MODE_DIRECT or mtvec_base != span.entry:
        return (
            "exception-vector-unproven",
            f"live mtvec ({snapshot.mtvec:#x}) does not target verified trap entry {span.entry:#x}",
        )

    if (
        require_machine_mpp
        and (snapshot.mstatus & MSTATUS_MPP_MASK) != MSTATUS_MPP_MACHINE
    ):
        return (
            "unsupported-exception-model",
            f"live mstatus.MPP is non-Machine ({snapshot.mstatus:#x})",
        )

    guard_reason = riscv_exception_guard(
        snapshot.mcause,
        pc,
        xlen=snapshot.xlen,
        allow_sync=allow_sync,
        allow_ecall=allow_ecall,
    )
    if guard_reason is not None:
        return (
            guard_reason,
            f"live mcause ({snapshot.mcause:#x}) is refused by trap unwind policy",
        )

    at_raw_entry = pc == span.entry
    at_trap_span = at_raw_entry or span.call_start <= pc < span.call_end
    if at_raw_entry:
        if not task_segment.contains(snapshot.sp, 0):
            return (
                "exception-task-context-conflict",
                "trap-entry live SP is outside the requested task stack",
            )
        exc_low, exc_high, exc_source = (
            task_segment.low or 0,
            task_segment.high or 0,
            "trap-entry:pre-switch",
        )
    else:
        bounds = irq_stack_bounds()
        if bounds is None:
            return (
                "exception-stack-bounds-unavailable",
                "the RISC-V interrupt stack bounds could not be resolved",
            )
        exc_low, exc_high, exc_source = bounds
        if not exc_low <= snapshot.sp <= exc_high:
            return (
                "exception-stack-outside-bounds",
                f"live SP {snapshot.sp:#x} is outside resolved interrupt stack {exc_low:#x}..{exc_high:#x}",
            )

    if nest_count is not None and nest_count > 1 and span.layout.epc_offset is None:
        return (
            "nested-trap-epc-unproven",
            "nested RISC-V trap with deferred epc storage cannot recover outer epc",
        )

    exception_segment = StackSegment(
        ident=f"exception:{snapshot.mcause:#x}",
        owner=f"exception:{snapshot.mcause:#x}",
        low=exc_low,
        high=exc_high,
        source=exc_source,
        stack="main",
        kind="exception",
    )
    seed = SeedFrame(
        pc=pc,
        sp=snapshot.sp,
        regs=dict(snapshot.registers),
        source="current",
        root_pc=root_pc,
        root_reason=root_reason,
        exception_number=snapshot.mcause,
        at_exception_entry=at_trap_span,
    )

    def guard(exc_no: int, trap_pc: int) -> str | None:
        return riscv_exception_guard(
            exc_no,
            trap_pc,
            xlen=snapshot.xlen,
            allow_sync=allow_sync,
            allow_ecall=allow_ecall,
        )

    hook = riscv_trap_transition_factory(
        xlen=snapshot.xlen,
        endian=endian,
        mcause=snapshot.mcause,
        mepc_csr=snapshot.mepc,
        nest_count=nest_count,
        span=span,
        task_frame_resolver=task_frame_resolver,
        require_machine_mpp=require_machine_mpp,
    )
    return RiscvTrapSetup(
        seed=seed,
        exception_segment=exception_segment,
        exception_detector=span.detect_pc,
        exception_hook=hook,
        exception_guard=guard,
        fault=riscv_fault_precision(
            snapshot.mcause,
            snapshot.xlen,
            mepc=snapshot.mepc,
            mtval=snapshot.mtval,
        ),
    )
