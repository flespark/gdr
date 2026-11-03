"""GDB-facing Armv6-M/Armv7-M and RISC-V live register and SCB helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .cpus import RISCV_CONVENTION
from .gdb_bridge import loaded_object_path, read_bytes
from .unwind import ElfImage, Reader, SeedFrame, UnwindMetadata
from .unwind_arm import (
    XPSR_ALIGN_BIT,
    XPSR_IPSR_MAX_MASK,
    XPSR_T_BIT,
    arm_capacity_exception_guard,
    arm_exception_guard,
    armv6m_exception_number_kind,
    armv6m_exception_return_candidate,
    armv6m_exception_transition,
    armv7m_exception_number_kind,
    armv7m_exception_return_candidate,
    armv7m_exception_transition,
    armv7m_max_external_exception,
)
from .unwind_segments import (
    ExceptionDetector,
    ExceptionHook,
    StackSegment,
    TransitionGuard,
)

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

SCB_ICTR_ADDRESS = 0xE000E004
SCB_CPUID_ADDRESS = 0xE000ED00
SCB_VTOR_ADDRESS = 0xE000ED08
SCB_CCR_ADDRESS = 0xE000ED14

RISCV_LIVE_REGISTERS: dict[int, str] = dict(RISCV_CONVENTION.live_registers)


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


@dataclass(frozen=True)
class ArmSnapshot:
    """Raw live Arm registers; scheduler stages read the same snapshot."""

    pc: int
    sp: int
    msp: int
    psp: int
    xpsr: int
    registers: dict[int, int]


def arm_snapshot(*, gdb_module: Any = None) -> tuple[ArmSnapshot | None, str]:
    """Read Arm live registers without rejecting an unset PSP.

    The ordinary live seed still refuses a zero PSP; the scheduler path needs
    the raw value so it can prove the SVC first-task restore itself.
    """
    active_gdb = gdb if gdb_module is None else gdb_module
    if active_gdb is None:
        return None, "live-registers-unavailable"
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc()) & 0xFFFFFFFF
        sp = int(frame.read_register("sp")) & 0xFFFFFFFF
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers: dict[int, int] = {}
    for register in range(13):
        try:
            registers[register] = int(frame.read_register(f"r{register}")) & 0xFFFFFFFF
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
            return None, "live-registers-unavailable"
    registers[13] = sp
    try:
        registers[14] = int(frame.read_register("lr")) & 0xFFFFFFFF
        registers[15] = pc
        xpsr = int(frame.read_register("xpsr")) & 0xFFFFFFFF
        msp = int(frame.read_register("msp")) & 0xFFFFFFFF
        psp = int(frame.read_register("psp")) & 0xFFFFFFFF
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "exception-registers-unavailable"
    return ArmSnapshot(pc, sp, msp, psp, xpsr, registers), "ok"


def live_seed_detail(
    arch: str,
    isa: str | None = None,
    *,
    root_pc: int | None = None,
    require_exception_registers: bool = True,
    gdb_module: Any = None,
    snapshot_reader: Callable[[], tuple[ArmSnapshot | None, str]] | None = None,
) -> tuple[SeedFrame | None, str]:
    """Read the selected core's live registers without driving the target.

    ``isa`` is the selected profile evidence. A live handler seed requires it
    so architecture-specific exception-number rules are applied only to the
    matching profile; an unknown profile is refused rather than assumed.
    """
    active_gdb = gdb if gdb_module is None else gdb_module
    if arch.startswith("arm"):
        if require_exception_registers:
            snapshot, reason = (
                snapshot_reader()
                if snapshot_reader is not None
                else arm_snapshot(gdb_module=active_gdb)
            )
            if snapshot is None:
                return None, reason
            exception_number = snapshot.xpsr & XPSR_IPSR_MAX_MASK
            if snapshot.msp <= 0 or snapshot.psp <= 0:
                return None, "live-registers-unavailable"
            if exception_number:
                if not snapshot.xpsr & XPSR_T_BIT:
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
                    root_pc=root_pc,
                    process_sp=snapshot.psp,
                    exception_number=exception_number,
                ),
                "ok",
            )
        if active_gdb is None:
            return None, "live-registers-unavailable"
        try:
            frame = active_gdb.newest_frame()
            pc = int(frame.pc())
            sp = int(frame.read_register("sp"))
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):
            return None, "live-registers-unavailable"
        registers: dict[int, int] = {}
        for register in range(13):
            try:
                registers[register] = int(frame.read_register(f"r{register}"))
            except (active_gdb.error, AttributeError, RuntimeError, TypeError):  # noqa: PERF203
                return None, "live-registers-unavailable"
        registers[13] = sp
        try:
            registers[14] = int(frame.read_register("lr"))
            registers[15] = pc
        except (active_gdb.error, AttributeError, RuntimeError, TypeError):
            return None, "live-registers-unavailable"
        return (
            SeedFrame(
                pc=pc & ~1,
                sp=sp,
                regs=registers,
                source="current",
                root_pc=root_pc,
            ),
            "ok",
        )
    if active_gdb is None:
        return None, "live-registers-unavailable"
    try:
        frame = active_gdb.newest_frame()
        pc = int(frame.pc())
        sp = int(frame.read_register("sp"))
    except (active_gdb.error, AttributeError, RuntimeError, TypeError):
        return None, "live-registers-unavailable"
    registers = {}
    for register, name in RISCV_LIVE_REGISTERS.items():
        try:
            registers[register] = int(frame.read_register(name))
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


def arm_ccr_stkalign(*, read: Reader = read_bytes) -> bool | None:
    """Read CCR.STKALIGN without writing target state."""
    raw = read(SCB_CCR_ADDRESS, 4)
    if raw is None or len(raw) != 4:
        return None
    return bool(int.from_bytes(raw, "little") & XPSR_ALIGN_BIT)


def arm_external_exception_limit(*, read: Reader = read_bytes) -> int | None:
    """Read ICTR and return the highest implemented external IPSR, or ``None``."""
    raw = read(SCB_ICTR_ADDRESS, 4)
    if raw is None or len(raw) != 4:
        return None
    return armv7m_max_external_exception(int.from_bytes(raw, "little"))


def arm_cpuid(*, read: Reader = read_bytes) -> int | None:
    """Read SCB.CPUID without writing target state."""
    raw = read(SCB_CPUID_ADDRESS, 4)
    if raw is None or len(raw) != 4:
        return None
    return int.from_bytes(raw, "little")


def armv6m_vector_entry(
    exception_number: int, *, read: Reader = read_bytes
) -> int | None:
    """Read a vector from an Armv6-M fixed-zero vector map, never VTOR."""
    if not 0 < exception_number <= 47 or armv6m_exception_number_kind(
        exception_number
    ) in ("thread", "invalid"):
        return None
    raw = read(4 * exception_number, 4)
    if raw is None or len(raw) != 4:
        return None
    value = int.from_bytes(raw, "little")
    return value & ~1 if value & 1 else None


def armv7m_vector_entry(
    exception_number: int, *, read: Reader = read_bytes
) -> int | None:
    """Return the current VTOR-indexed vector target for one exception number."""
    raw_vtor = read(SCB_VTOR_ADDRESS, 4)
    if raw_vtor is None or len(raw_vtor) != 4 or exception_number <= 0:
        return None
    vtor = int.from_bytes(raw_vtor, "little")
    raw_entry = read(vtor + 4 * exception_number, 4)
    if raw_entry is None or len(raw_entry) != 4:
        return None
    entry = int.from_bytes(raw_entry, "little")
    return entry & ~1 if entry else None


@dataclass(frozen=True)
class ArmHandlerSetup:
    """Validated Armv6-M/Armv7-M handler-mode unwind configuration."""

    seed: SeedFrame
    exception_segment: StackSegment
    stkalign: bool
    exception_detector: ExceptionDetector
    exception_hook: ExceptionHook
    exception_guard: TransitionGuard


def prepare_arm_handler_unwind(
    seed: SeedFrame,
    isa: str | None,
    *,
    scheduler_running: Callable[[], bool | None],
    irq_stack_bounds: Callable[[], tuple[int, int] | None],
    irq_stack_source: str,
    capacity_reader: Callable[[], int | None],
    stkalign_reader: Callable[[], bool | None],
    vector_entry_reader: Callable[[int], int | None],
    guard_fn: Callable[[int, int, str], str | None] = arm_exception_guard,
    capacity_guard_factory: Callable[
        [int, str], TransitionGuard
    ] = arm_capacity_exception_guard,
) -> ArmHandlerSetup | tuple[str, str]:
    """Validate an Arm handler-mode seed and build its segment and hooks.

    Returns :class:`ArmHandlerSetup` when all architectural and port invariants
    hold, or ``(stop_reason, message)`` when the stop must be refused.
    """
    if isa not in ("armv6-m", "armv7-m") or seed.exception_number is None:
        return (
            "unsupported-exception-model",
            "handler-mode seeds require a verified Armv6-M/Armv7-M model",
        )
    guard_reason = guard_fn(seed.exception_number, seed.pc, isa)
    if guard_reason == "scheduler-transition-unproven":
        return (
            guard_reason,
            "the stop is inside the scheduler's SVC/PendSV transition path",
        )
    if guard_reason == "exception-frame-type-unproven":
        return (
            guard_reason,
            "a synchronous fault frame does not name a proven resume PC",
        )
    if guard_reason == "exception-number-unverified":
        return (
            guard_reason,
            "the live exception number is architecturally valid but "
            "outside the verified external-IRQ proof scope",
        )
    if guard_reason == "exception-number-invalid":
        return (
            guard_reason,
            "the live IPSR names no architecturally valid exception",
        )
    running = scheduler_running()
    if running is None:
        return (
            "scheduler-state-unavailable",
            "xSchedulerRunning is not readable; task ownership is unproven",
        )
    if not running:
        return (
            "scheduler-not-running",
            "handler mode without a running scheduler has no task owner",
        )
    bounds = irq_stack_bounds()
    if bounds is None:
        return (
            "exception-stack-bounds-unavailable",
            "no explicit linker/port symbols define the interrupt stack span",
        )
    irq_low, irq_high = bounds
    if not irq_low <= seed.sp <= irq_high:
        return (
            "exception-stack-outside-bounds",
            "the live MSP is outside the linker-owned interrupt stack",
        )
    capacity = capacity_reader()
    if capacity is None:
        return (
            "exception-capacity-unavailable",
            "the target's implemented external interrupt capacity is not readable",
        )
    if seed.exception_number >= 16 and seed.exception_number > capacity:
        return (
            "exception-number-unimplemented",
            "the live IPSR names an external exception beyond the target's "
            "implemented interrupt capacity",
        )
    alignment = True if isa == "armv6-m" else stkalign_reader()
    if alignment is None:
        return (
            "exception-alignment-unavailable",
            "CCR.STKALIGN is not readable; the frame layout is unproven",
        )
    exception_segment = StackSegment(
        ident=f"exception:{seed.exception_number}",
        owner=f"exception:{seed.exception_number}",
        low=irq_low,
        high=irq_high,
        source=irq_stack_source,
        stack="main",
        kind="exception",
    )
    entry = vector_entry_reader(seed.exception_number)
    if entry is not None and entry == seed.pc:
        seed = replace(seed, at_exception_entry=True)
    detector = (
        armv6m_exception_return_candidate
        if isa == "armv6-m"
        else armv7m_exception_return_candidate
    )
    hook = (
        armv6m_exception_transition if isa == "armv6-m" else armv7m_exception_transition
    )
    return ArmHandlerSetup(
        seed=seed,
        exception_segment=exception_segment,
        stkalign=alignment,
        exception_detector=detector,
        exception_hook=hook,
        exception_guard=capacity_guard_factory(capacity, isa),
    )
