"""GDB-facing Armv6-M/Armv7-M and RISC-V live register and SCB helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .cpus import RISCV_CONVENTION
from .gdb_bridge import linker_symbol_address, loaded_object_path, read_bytes
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


MAX_INFERRED_IRQ_STACK_BYTES = 256 * 1024
_STANDARD_STACK_TOP_SYMBOLS = (
    "__StackTop",
    "_estack",
    "__initial_sp",
    "__stack",
    "_stack_top",
)
_STANDARD_STACK_LOW_SYMBOLS = (
    "__StackLimit",
    "_sstack",
    "__stack_limit",
    "_stack_bottom",
)
_STANDARD_STACK_SECTIONS = (
    ".irq_stack",
    ".isr_stack",
    ".stack",
    ".main_stack",
    "STACK",
)
_STANDARD_VECTOR_BASE_SYMBOLS = (
    "g_pfnVectors",
    "__Vectors",
    "_vectors",
    "__vector_table",
    "vector_table",
)
_STANDARD_VECTOR_END_SYMBOLS = (
    "__Vectors_End",
    "_evectors",
    "__vector_table_end",
    "__isr_vector_end",
)
_STANDARD_VECTOR_SECTIONS = (
    ".isr_vector",
    ".vectors",
    ".vector_table",
)


def arm_vector_initial_msp(
    isa: str | None = "armv7-m", *, read: Reader = read_bytes
) -> int | None:
    """Read word 0 (initial MSP) from the active Arm M-profile vector table."""
    if isa == "armv6-m":
        vtor = 0
    elif isa in ("armv7-m", "armv8-m.mainline"):
        raw_vtor = read(SCB_VTOR_ADDRESS, 4)
        if raw_vtor is None or len(raw_vtor) != 4:
            return None
        vtor = int.from_bytes(raw_vtor, "little")
        if vtor & 0x7F:
            return None
    else:
        return None
    raw_msp = read(vtor, 4)
    if raw_msp is None or len(raw_msp) != 4:
        return None
    initial_msp = int.from_bytes(raw_msp, "little")
    if initial_msp <= 0 or initial_msp & 0x3:
        return None
    return initial_msp


def resolve_arm_irq_stack_bounds_detail(
    *,
    symbol_lookup: Callable[[str], int | None] = linker_symbol_address,
    read: Reader = read_bytes,
    isa: str | None = "armv7-m",
    elf: Any = None,
    live_msp: int | None = None,
    task_bounds: tuple[int, int] | None = None,
) -> tuple[int, int, str] | None:
    """Resolve the Arm M-profile interrupt stack ``(low, high, source)``.

    Resolution tiers:
    1. Explicit ``__gdr_irq_stack_low`` and ``__gdr_irq_stack_top`` override
       (fails closed when only one of the pair is present).
    2. Standard CMSIS/toolchain stack-bottom and stack-top linker symbols.
    3. Dedicated ELF stack section (``.irq_stack``, ``.isr_stack``, ``.stack``,
       ``.main_stack``, ``STACK``).
    4. Architectural initial MSP (from a standard top symbol or Vector Table
       Word 0) paired with a standard low symbol or a non-overlapping halted
       ``live_msp`` lower bound.
    """

    def _safe_read(address: int, size: int) -> bytes | None:
        try:
            return read(address, size)
        except RuntimeError:
            return None

    def _lookup(name: str) -> int | None:
        try:
            value = symbol_lookup(name)
        except RuntimeError:
            value = None
        if value is not None:
            return value
        if elf is not None and callable(getattr(elf, "symbol_address", None)):
            return elf.symbol_address(name)
        return None

    gdr_low = _lookup("__gdr_irq_stack_low")
    gdr_high = _lookup("__gdr_irq_stack_top")
    if gdr_low is not None or gdr_high is not None:
        if gdr_low is None or gdr_high is None or gdr_low <= 0 or gdr_high <= gdr_low:
            return None
        return (
            gdr_low,
            gdr_high,
            "linker:__gdr_irq_stack_low..__gdr_irq_stack_top",
        )

    top_name: str | None = None
    top_addr: int | None = None
    for name in _STANDARD_STACK_TOP_SYMBOLS:
        candidate = _lookup(name)
        if candidate is not None and candidate > 0 and candidate & 0x3 == 0:
            top_name, top_addr = name, candidate
            break

    low_name: str | None = None
    low_addr: int | None = None
    for name in _STANDARD_STACK_LOW_SYMBOLS:
        candidate = _lookup(name)
        if candidate is not None and candidate > 0 and candidate & 0x3 == 0:
            low_name, low_addr = name, candidate
            break

    if low_addr is not None and top_addr is not None and low_addr < top_addr:
        return low_addr, top_addr, f"linker:{low_name}..{top_name}"

    if elf is not None and callable(getattr(elf, "section_bounds", None)):
        for sec_name in _STANDARD_STACK_SECTIONS:
            bounds = elf.section_bounds(sec_name)
            if bounds is None:
                continue
            sec_low, sec_high = bounds
            if (
                0 < sec_low < sec_high
                and sec_low & 0x3 == 0
                and sec_high & 0x3 == 0
                and (top_addr is None or sec_high == top_addr)
            ):
                return sec_low, sec_high, f"section:{sec_name}"

    if top_addr is not None and top_name is not None:
        high_addr: int | None = top_addr
        high_label = top_name
    else:
        high_addr = arm_vector_initial_msp(isa or "armv7-m", read=_safe_read)
        high_label = "vector[0]"
    if high_addr is None or high_addr <= 0 or high_addr & 0x3:
        return None

    if low_addr is not None and low_name is not None and 0 < low_addr < high_addr:
        return low_addr, high_addr, f"linker:{low_name}..{high_label}"

    if (
        live_msp is not None
        and 0 < live_msp < high_addr
        and live_msp & 0x3 == 0
        and high_addr - live_msp <= MAX_INFERRED_IRQ_STACK_BYTES
    ):
        if task_bounds is not None:
            task_low, task_high = task_bounds
            if not (task_high <= live_msp or task_low >= high_addr):
                return None
        return live_msp, high_addr, f"msp:live..{high_label}"

    return None


def resolve_arm_irq_stack_bounds(
    *,
    symbol_lookup: Callable[[str], int | None] = linker_symbol_address,
    read: Reader = read_bytes,
    isa: str | None = "armv7-m",
    elf: Any = None,
    live_msp: int | None = None,
    task_bounds: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    """Return the resolved ``(low, high)`` Arm interrupt stack span, or ``None``."""
    detail = resolve_arm_irq_stack_bounds_detail(
        symbol_lookup=symbol_lookup,
        read=read,
        isa=isa,
        elf=elf,
        live_msp=live_msp,
        task_bounds=task_bounds,
    )
    return (detail[0], detail[1]) if detail is not None else None


def resolve_armv6m_vector_limit(
    *,
    symbol_lookup: Callable[[str], int | None] = linker_symbol_address,
    read: Reader = read_bytes,
    elf: Any = None,
) -> int | None:
    """Verify the Armv6-M fixed-zero vector map and return max external IPSR.

    Resolution tiers:
    1. Explicit ``__gdr_vector_table_end`` and ``__gdr_armv6m_irq_count``
       override (fails closed when partial or inconsistent).
    2. Standard vector-end symbol (``__Vectors_End``, ``_evectors``), dedicated
       vector section size at ``0x0`` (``.isr_vector``, ``.vectors``), or the
       first executable function symbol immediately following the vector table.
    """

    def _safe_read(address: int, size: int) -> bytes | None:
        try:
            return read(address, size)
        except RuntimeError:
            return None

    def _lookup(name: str) -> int | None:
        try:
            value = symbol_lookup(name)
        except RuntimeError:
            value = None
        if value is not None:
            return value
        if elf is not None and callable(getattr(elf, "symbol_address", None)):
            return elf.symbol_address(name)
        return None

    cpuid = arm_cpuid(read=_safe_read)
    if cpuid is None or cpuid & 0xFF0FFFF0 != 0x410CC200:
        return None

    gdr_end = _lookup("__gdr_vector_table_end")
    gdr_count = _lookup("__gdr_armv6m_irq_count")
    if gdr_end is not None or gdr_count is not None:
        base = _lookup("g_pfnVectors")
        if (
            base != 0
            or gdr_count is None
            or not 1 <= gdr_count <= 32
            or gdr_end != (16 + gdr_count) * 4
        ):
            return None
        return 16 + gdr_count - 1

    has_zero_base = False
    for name in _STANDARD_VECTOR_BASE_SYMBOLS:
        addr = _lookup(name)
        if addr is not None:
            if addr != 0:
                return None
            has_zero_base = True

    sec_size: int | None = None
    if elf is not None and callable(getattr(elf, "section_bounds", None)):
        for sec_name in _STANDARD_VECTOR_SECTIONS:
            bounds = elf.section_bounds(sec_name)
            if bounds is None:
                continue
            sec_low, sec_high = bounds
            if sec_low != 0:
                return None
            has_zero_base = True
            sec_size = sec_high
            break

    if not has_zero_base:
        return None

    for name in _STANDARD_VECTOR_END_SYMBOLS:
        end_addr = _lookup(name)
        if end_addr is not None:
            if end_addr & 0x3 or not 17 * 4 <= end_addr <= 48 * 4:
                return None
            return (end_addr // 4) - 1

    if sec_size is not None and sec_size & 0x3 == 0 and 17 * 4 <= sec_size <= 48 * 4:
        return (sec_size // 4) - 1

    symbols = getattr(elf, "symbols", ()) if elf is not None else ()
    first_code = min(
        (sym.start for sym in symbols if getattr(sym, "start", 0) > 0),
        default=None,
    )
    if (
        first_code is not None
        and first_code & 0x3 == 0
        and 17 * 4 <= first_code <= 48 * 4
    ):
        return (first_code // 4) - 1

    return None


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
    irq_stack_bounds: Callable[[], tuple[int, int] | tuple[int, int, str] | None],
    irq_stack_source: str = "linker:__gdr_irq_stack_low..__gdr_irq_stack_top",
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
            "the interrupt stack span could not be resolved from symbols, "
            "ELF sections, or vector-table initial MSP",
        )
    if len(bounds) == 3:
        irq_low, irq_high, resolved_source = bounds
    else:
        irq_low, irq_high = bounds
        resolved_source = irq_stack_source
    if not irq_low <= seed.sp <= irq_high:
        return (
            "exception-stack-outside-bounds",
            "the live MSP is outside the resolved interrupt stack span",
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
        source=resolved_source,
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
