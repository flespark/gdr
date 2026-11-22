"""FreeRTOS saved-frame profile selection from ELF and kernel configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from core.backtrace.unwind_riscv import (
    RiscvTrapFrameLayout,
    RiscvTrapSpan,
    riscv_decode_until_return,
    riscv_find_sp_loads_from_symbol,
    riscv_partition_by_sp_loads,
    riscv_reachable_pcs,
    riscv_route_epc_store,
)
from core.elf import EM_RISCV, Cpu, ElfImage, Isa, cpu_of
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_frame_accesses,
    riscv_next_pc,
    riscv_slot_map,
    riscv_stack_frame_size,
    riscv_target_loads,
    riscv_update_const_addr,
    riscv_written_register,
)
from core.stack_frame_parser import (
    SavedStackContext,
    StackContextSelection,
)

from .backtrace.unwind import (
    ARMV6M_INTEGER,
    ARMV7M_CM4F_BASIC,
    ARMV7M_CM4F_FPU,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_INTEGER,
    ARMV8M_MAINLINE_MPU_INTEGER,
    ARMV8M_MAINLINE_SMP_INTEGER,
    build_riscv_saved_context,
)

# --- Common -----------------------------------------------------------------


def select_context(
    elf: ElfImage,
    *,
    smp: bool = False,
    mpu: bool = False,
    macros: Mapping[str, str] | None = None,
) -> StackContextSelection:
    """Select a verified profile, retaining explicit missing/conflict reasons."""
    cpu = cpu_of(elf)
    if cpu.reason != "ok":
        return _selection_result(cpu, None, cpu.reason)
    if cpu.family == "riscv":
        return _select_riscv_context(elf, cpu, smp=smp, mpu=mpu)
    return _select_arm_context(elf, cpu, smp=smp, mpu=mpu, macros=macros)


def _selection_result(
    cpu: Cpu,
    context: SavedStackContext | None,
    reason: str,
    evidence: tuple[str, ...] = (),
    alt_context: SavedStackContext | None = None,
) -> StackContextSelection:
    return StackContextSelection(
        cpu.isa, cpu.family, context, reason, evidence, cpu.abi, alt_context
    )


# --- RISC-V -----------------------------------------------------------------


_CSR_MSTATUS = 0x300
_CSR_MEPC = 0x341
_CSR_MCAUSE = 0x342
_ECALL_M_CAUSE = 11
_EXPECTED_RISCV_GPRS = frozenset({1, *range(5, 32)})


@dataclass(frozen=True)
class RiscvTrapPrologue:
    """Derived trap prologue frame geometry and register save slots."""

    frame_bytes: int
    gpr_offsets: dict[int, int]
    saved_slots: tuple[tuple[int, int, int], ...]
    mstatus_offset: int
    critical_nesting_offset: int | None
    tcb_store_pc: int
    pcs: tuple[int, ...]


@dataclass(frozen=True)
class RiscvTrapDispatch:
    """Verified async/sync trap dispatch blocks and EPC/ISR transition boundaries."""

    async_pc: int
    async_epc_store_pc: int
    async_pre_sp_pcs: tuple[int, ...]
    async_dispatch_pcs: tuple[int, ...]
    intr_dispatch_start: int
    after_switch_async_pcs: frozenset[int]
    sync_pc: int
    sync_epc_store_pc: int
    sync_pre_sp_pcs: tuple[int, ...]
    sync_dispatch_pcs: tuple[int, ...]
    sync_dispatch_start: int
    switch_call_sync_pc: int
    after_switch_sync_pc: int
    unhandled_sync_pcs: tuple[int, ...]
    unhandled_sync_on_isr_stack: bool
    app_exc_call_pc: int | None


@dataclass(frozen=True)
class RiscvTrapRestore:
    """Verified ``processed_source`` epilogue boundaries and restore PCs."""

    proc_pc: int
    load_sp_pc: int
    pop_pc: int
    mret_pc: int
    pcs: tuple[int, ...]


@dataclass(frozen=True)
class RiscvTrapAnalysis:
    """Verified FreeRTOS RISC-V trap layout and stage breakdown."""

    trap_start: int
    trap_end: int
    prologue: RiscvTrapPrologue
    dispatch: RiscvTrapDispatch
    restore: RiscvTrapRestore
    refused_spans: tuple[tuple[int, int, str], ...]


def _select_riscv_context(
    elf: ElfImage,
    cpu: Cpu,
    *,
    smp: bool,
    mpu: bool,
) -> StackContextSelection:
    """Select and verify the RISC-V integer profile from compiled code."""
    if cpu.isa not in ("riscv32", "riscv64"):
        return _selection_result(cpu, None, "unverified-port")
    if smp or mpu:
        return _selection_result(cpu, None, "unsupported-extension-context")
    analysis = analyze_riscv_trap_blocks(elf)
    if isinstance(analysis, str) or not _riscv_verify_switch_parts(elf, analysis):
        return _selection_result(cpu, None, "unrecognizable-stack-frame-layout")
    context = build_riscv_saved_context(
        cast(Isa, cpu.isa),
        elf.pointer_size,
        gpr_offsets=analysis.prologue.gpr_offsets,
        frame_bytes=analysis.prologue.frame_bytes,
    )
    return _selection_result(
        cpu,
        context,
        "ok",
        (
            ".riscv.attributes",
            "ELF e_flags",
            "compiled save/restore/initial context",
        ),
    )


def _find_prologue_branch(
    code: dict[int, tuple], *, mcause_reg: int, xlen: int
) -> tuple[int, int] | None:
    """Locate the async-vs-sync trap branch and its synchronous target PC."""
    shift_regs: set[int] = set()
    for pc in sorted(code):
        op = code[pc]
        if op[0] in ("bge", "blt") and op[1] == mcause_reg and op[2] == 0:
            return pc, op[3]
        if op[0] == "beq" and (
            (op[1] in shift_regs and op[2] == 0) or (op[2] in shift_regs and op[1] == 0)
        ):
            return pc, op[3]
        written = riscv_written_register(op)
        if written is not None:
            shift_regs.discard(written)
        if op[0] == "srli" and op[2] == mcause_reg and op[3] == xlen - 1:
            shift_regs.add(op[1])
    return None


def _derive_prologue_slots(
    trap: dict[int, tuple],
    *,
    word: int,
    mask: int,
    tcb_ptr: int | None,
    crit_val_ptr: int | None,
    has_crit_globals: bool,
) -> RiscvTrapPrologue | str:
    """Derive exact GPR, mstatus, and optional critical-nesting offsets from prologue."""
    trap_pcs = sorted(trap)
    frame_bytes = riscv_stack_frame_size(trap)
    if frame_bytes not in (30 * word, 31 * word):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:frame-size"
    if trap[trap_pcs[0]] != ("addi", 2, 2, -frame_bytes):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:entry"

    load_op = "ld" if word == 8 else "lw"
    store_op = "sd" if word == 8 else "sw"
    const_addrs: dict[int, int] = {}
    clobbered: set[int] = set()
    mstatus_regs: set[int] = set()
    crit_regs: set[int] = set()
    tcb_regs: set[int] = set()
    gpr_offsets: dict[int, int] = {}
    gpr_done: dict[int, int] = {}
    mstatus_offset: int | None = None
    crit_offset: int | None = None
    tcb_store_pc: int | None = None

    for idx, pc in enumerate(trap_pcs):
        op = trap[pc]
        next_pc = trap_pcs[idx + 1] if idx + 1 < len(trap_pcs) else pc + 4
        if op[0] == store_op and op[2] == 2:
            reg, offset = op[1], op[3]
            if 0 < offset < frame_bytes and offset % word == 0:
                if reg in crit_regs:
                    if crit_offset is not None and crit_offset != offset:
                        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:csr-slots"
                    crit_offset = offset
                elif reg in mstatus_regs:
                    if mstatus_offset is not None and mstatus_offset != offset:
                        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:csr-slots"
                    mstatus_offset = offset
                elif reg in _EXPECTED_RISCV_GPRS:
                    if reg in clobbered and reg not in gpr_offsets:
                        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:gpr-slots"
                    if reg not in gpr_offsets:
                        gpr_offsets[reg] = offset
                        gpr_done[reg] = next_pc
        elif (
            tcb_ptr is not None
            and op[0] == store_op
            and op[1] == 2
            and op[2] in tcb_regs
            and op[3] == 0
        ):
            tcb_store_pc = pc

        written = riscv_written_register(op)
        if written is not None:
            if written in _EXPECTED_RISCV_GPRS:
                clobbered.add(written)
            mstatus_regs.discard(written)
            crit_regs.discard(written)
            tcb_regs.discard(written)
        if op[0] == "csrr" and op[2] == _CSR_MSTATUS:
            mstatus_regs.add(op[1])
        elif op[0] == load_op:
            rd, rn, imm = op[1], op[2], op[3]
            addr = (const_addrs[rn] + imm) & mask if rn in const_addrs else None
            if crit_val_ptr is not None and addr == crit_val_ptr:
                crit_regs.add(rd)
            if tcb_ptr is not None and addr == tcb_ptr:
                tcb_regs.add(rd)
        riscv_update_const_addr(op, pc, mask, const_addrs)

    if len(gpr_offsets) != len(_EXPECTED_RISCV_GPRS) or len(
        set(gpr_offsets.values())
    ) != len(_EXPECTED_RISCV_GPRS):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:gpr-slots"

    frame_words = frame_bytes // word
    expected_slots = {i * word for i in range(frame_words)}
    if mstatus_offset is None:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:csr-slots"
    if frame_words == 31:
        if (
            not has_crit_globals
            or crit_offset is None
            or {0, mstatus_offset, crit_offset, *gpr_offsets.values()} != expected_slots
        ):
            return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:csr-slots"
    elif (
        has_crit_globals
        or crit_offset is not None
        or {0, mstatus_offset, *gpr_offsets.values()} != expected_slots
    ):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:csr-slots"

    if tcb_store_pc is None:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:prologue"

    saved_slots = tuple(
        (reg, gpr_offsets[reg], gpr_done[reg]) for reg in sorted(gpr_offsets)
    )
    return RiscvTrapPrologue(
        frame_bytes=frame_bytes,
        gpr_offsets=gpr_offsets,
        saved_slots=saved_slots,
        mstatus_offset=mstatus_offset,
        critical_nesting_offset=crit_offset,
        tcb_store_pc=tcb_store_pc,
        pcs=tuple(trap_pcs),
    )


def _decode_trap_sections(
    elf: ElfImage,
    switch_start: int | None,
) -> (
    tuple[int, int, tuple[int | None, ...], int, dict[int, tuple], dict[int, tuple]]
    | str
):
    """Resolve trap handler bounds, split symbols, switch target, and decoded instructions."""
    trap_start = elf.symbol_address("freertos_risc_v_trap_handler")
    proc_pc = elf.symbol_address("processed_source")
    if trap_start is None or proc_pc is None or not trap_start < proc_pc:
        return "scheduler-profile-symbol-missing:freertos_risc_v_trap_handler"

    split_syms = (
        elf.symbol_address("asynchronous_interrupt"),
        elf.symbol_address("synchronous_exception"),
        elf.symbol_address("handle_interrupt"),
        elf.symbol_address("handle_exception"),
    )
    has_split_syms = all(sym is not None for sym in split_syms)
    if any(sym is not None for sym in split_syms) and not has_split_syms:
        return "scheduler-profile-symbol-missing:freertos_risc_v_trap_handler"
    if has_split_syms:
        async_sym, sync_sym, idle_sym, exc_sym = split_syms
        assert (
            async_sym is not None
            and sync_sym is not None
            and idle_sym is not None
            and exc_sym is not None
        )
        if not (trap_start < async_sym < sync_sym < idle_sym < exc_sym < proc_pc):
            return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:order"

    if switch_start is None:
        switch_sym = next(
            (sym for sym in elf.symbols if sym.name == "vTaskSwitchContext"), None
        )
        if switch_sym is None:
            return "scheduler-profile-symbol-missing:vTaskSwitchContext"
        switch_start = switch_sym.start

    try:
        pre_proc = RiscvCodeParser(
            elf, "freertos_risc_v_trap_handler", "processed_source"
        ).instructions()
        restore = RiscvCodeParser(elf, "processed_source").instructions(
            terminal=("mret",)
        )
    except EvidenceError:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:decode"
    return trap_start, proc_pc, split_syms, switch_start, pre_proc, restore


def _analyze_trap_prologue(
    pre_proc: dict[int, tuple],
    *,
    trap_start: int,
    proc_pc: int,
    async_sym: int | None,
    sync_sym: int | None,
    word: int,
    mask: int,
    tcb_ptr: int | None,
    crit_val_ptr: int | None,
    has_crit_globals: bool,
) -> tuple[RiscvTrapPrologue, int, int, int, int] | str:
    """Validate prologue branch and CSR/GPR save slots."""
    mcause_reg = next(
        (op[1] for op in pre_proc.values() if op[0] == "csrr" and op[2] == _CSR_MCAUSE),
        None,
    )
    mepc_reg = next(
        (op[1] for op in pre_proc.values() if op[0] == "csrr" and op[2] == _CSR_MEPC),
        None,
    )
    if mcause_reg is None or mepc_reg is None:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:prologue"

    pre_branch_slice = (
        {pc: op for pc, op in pre_proc.items() if pc < async_sym}
        if async_sym is not None
        else pre_proc
    )
    branch_info = _find_prologue_branch(
        pre_branch_slice, mcause_reg=mcause_reg, xlen=word * 8
    )
    if branch_info is None:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:prologue"
    branch_pc, sync_pc = branch_info
    async_pc = riscv_next_pc(pre_proc, branch_pc)
    if async_pc is None or not (trap_start < async_pc < sync_pc < proc_pc):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:prologue"
    if async_sym is not None and (async_pc != async_sym or sync_pc != sync_sym):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:prologue"

    trap = {pc: op for pc, op in pre_proc.items() if pc < async_pc}
    prologue = _derive_prologue_slots(
        trap,
        word=word,
        mask=mask,
        tcb_ptr=tcb_ptr,
        crit_val_ptr=crit_val_ptr,
        has_crit_globals=has_crit_globals,
    )
    if isinstance(prologue, str):
        return prologue
    if branch_pc <= prologue.tcb_store_pc:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:prologue"
    return prologue, mcause_reg, mepc_reg, async_pc, sync_pc


def _analyze_trap_dispatch(
    elf: ElfImage,
    pre_proc: dict[int, tuple],
    *,
    split_syms: tuple[int | None, ...],
    proc_pc: int,
    async_pc: int,
    sync_pc: int,
    mcause_reg: int,
    mepc_reg: int,
    switch_start: int,
    isr_top_ptr: int | None,
    word: int,
    mask: int,
) -> tuple[RiscvTrapDispatch, tuple[tuple[int, int, str], ...]] | str:
    """Partition async/sync dispatch blocks, prove EPC stores, and compute refused spans."""
    store_op = "sd" if word == 8 else "sw"
    _async_sym, _sync_sym, idle_sym, exc_sym = split_syms
    has_split_syms = all(sym is not None for sym in split_syms)

    if has_split_syms:
        assert idle_sym is not None and exc_sym is not None
        async_code = {pc: op for pc, op in pre_proc.items() if async_pc <= pc < sync_pc}
        sync_code = {pc: op for pc, op in pre_proc.items() if sync_pc <= pc < idle_sym}
        intr_code = {pc: op for pc, op in pre_proc.items() if idle_sym <= pc < exc_sym}
        exc_code = {pc: op for pc, op in pre_proc.items() if exc_sym <= pc < proc_pc}
        async_pre_sp_pcs = tuple(sorted(async_code))
        async_dispatch_pcs = tuple(sorted(intr_code))
        intr_dispatch_start = idle_sym
    else:
        if isr_top_ptr is None:
            return "scheduler-profile-symbol-missing:kernel-globals"
        async_slice = {
            pc: op for pc, op in pre_proc.items() if async_pc <= pc < sync_pc
        }
        sync_code = {pc: op for pc, op in pre_proc.items() if sync_pc <= pc < proc_pc}
        async_code = async_slice
        sp_loads_async = riscv_find_sp_loads_from_symbol(
            async_slice, symbol_addr=isr_top_ptr, word=word, mask=mask
        )
        if not sp_loads_async:
            return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:isr-sp"
        pre_async, post_async = riscv_partition_by_sp_loads(
            async_slice, async_pc, sp_loads_async
        )
        if not post_async:
            return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:isr-sp"
        async_pre_sp_pcs = tuple(sorted(pre_async))
        async_dispatch_pcs = tuple(sorted(post_async))
        intr_code = {pc: async_slice[pc] for pc in async_dispatch_pcs}
        intr_dispatch_start = async_dispatch_pcs[0]
        exc_code = sync_code

    async_epc_store_pc = riscv_route_epc_store(
        async_code, mepc_register=mepc_reg, addend=0, store_ops=(store_op,)
    )
    sync_epc_store_pc = riscv_route_epc_store(
        sync_code, mepc_register=mepc_reg, addend=4, store_ops=(store_op,)
    )
    if async_epc_store_pc is None or sync_epc_store_pc is None:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:epc-store"

    intr_pcs = sorted(intr_code)
    after_switch_async_pcs: set[int] = set()
    for idx, pc in enumerate(intr_pcs):
        op = intr_code[pc]
        if op[0] == "jal" and op[1] == 1 and op[2] == switch_start:
            if idx + 1 >= len(intr_pcs):
                return "scheduler-profile-mismatch:handle_interrupt:switch-ret"
            after_switch_async_pcs.add(intr_pcs[idx + 1])

    exc_pcs = sorted(exc_code)
    app_exc_sym = elf.symbol_address("freertos_risc_v_application_exception_handler")
    switch_call_sync_pc: int | None = None
    after_switch_sync_pc: int | None = None
    app_exc_call_pc: int | None = None
    ecall_branch_op = next(
        (
            (pc, op)
            for pc, op in sorted(exc_code.items())
            if op[0] in ("bne", "beq") and op[1] == mcause_reg
        ),
        None,
    )
    has_ecall_imm = any(
        op[0] == "addi" and op[2] == 0 and op[3] == _ECALL_M_CAUSE
        for op in exc_code.values()
    )
    for idx, pc in enumerate(exc_pcs):
        op = exc_code[pc]
        if op[0] == "jal" and op[1] == 1:
            if op[2] == switch_start:
                if switch_call_sync_pc is not None or idx + 1 >= len(exc_pcs):
                    return "scheduler-profile-mismatch:handle_exception:switch-call"
                switch_call_sync_pc = pc
                after_switch_sync_pc = exc_pcs[idx + 1]
            elif app_exc_sym is None or op[2] == app_exc_sym:
                app_exc_call_pc = pc

    if (
        (ecall_branch_op is None and not has_ecall_imm)
        or switch_call_sync_pc is None
        or after_switch_sync_pc is None
    ):
        return "scheduler-profile-mismatch:handle_exception:structure"

    if has_split_syms:
        if app_exc_call_pc is None or app_exc_call_pc != riscv_next_pc(
            exc_code, after_switch_sync_pc
        ):
            return "scheduler-profile-mismatch:handle_exception:app-handler"
        assert exc_sym is not None
        sync_pre_sp_pcs = tuple(sorted(sync_code))
        sync_dispatch_pcs = tuple(pc for pc in exc_pcs if pc < app_exc_call_pc)
        unhandled_sync_pcs = tuple(pc for pc in exc_pcs if pc >= app_exc_call_pc)
        sync_dispatch_start = exc_sym
        unhandled_sync_on_isr_stack = True
    else:
        if isr_top_ptr is None or ecall_branch_op is None:
            return "scheduler-profile-mismatch:handle_exception:structure"
        unhandled_sync_start = ecall_branch_op[1][3]
        if unhandled_sync_start not in exc_code:
            return "scheduler-profile-mismatch:handle_exception:structure"
        unhandled_set = riscv_reachable_pcs(exc_code, unhandled_sync_start)
        ecall_only = {pc: op for pc, op in exc_code.items() if pc not in unhandled_set}
        sp_loads_sync = riscv_find_sp_loads_from_symbol(
            ecall_only, symbol_addr=isr_top_ptr, word=word, mask=mask
        )
        if not sp_loads_sync or unhandled_set & sp_loads_sync:
            return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:isr-sp"
        pre_sync, post_sync = riscv_partition_by_sp_loads(
            ecall_only, sync_pc, sp_loads_sync
        )
        if not post_sync or switch_call_sync_pc not in post_sync:
            return "scheduler-profile-mismatch:handle_exception:structure"
        sync_pre_sp_pcs = tuple(sorted(pre_sync))
        sync_dispatch_pcs = tuple(sorted(post_sync))
        unhandled_sync_pcs = tuple(sorted(unhandled_set))
        sync_dispatch_start = sync_dispatch_pcs[0]
        unhandled_sync_on_isr_stack = False
        app_exc_call_pc = None

    refused: list[tuple[int, int, str]] = []
    dispatch_floor = min(intr_dispatch_start, sync_dispatch_start)
    dispatch_combined = sorted(pc for pc in pre_proc if pc >= dispatch_floor)
    for index, pc in enumerate(dispatch_combined):
        op = pre_proc[pc]
        if op[0] == "jal" and op[2] == switch_start:
            ret_pc = (
                dispatch_combined[index + 1]
                if index + 1 < len(dispatch_combined)
                else pc + 4
            )
            ret_end = (
                min(dispatch_combined[index + 2], proc_pc)
                if index + 2 < len(dispatch_combined)
                else min(ret_pc + 4, proc_pc)
            )
            refused.append((ret_pc, ret_end, "scheduler-transition-unproven"))

    return (
        RiscvTrapDispatch(
            async_pc=async_pc,
            async_epc_store_pc=async_epc_store_pc,
            async_pre_sp_pcs=async_pre_sp_pcs,
            async_dispatch_pcs=async_dispatch_pcs,
            intr_dispatch_start=intr_dispatch_start,
            after_switch_async_pcs=frozenset(after_switch_async_pcs),
            sync_pc=sync_pc,
            sync_epc_store_pc=sync_epc_store_pc,
            sync_pre_sp_pcs=sync_pre_sp_pcs,
            sync_dispatch_pcs=sync_dispatch_pcs,
            sync_dispatch_start=sync_dispatch_start,
            switch_call_sync_pc=switch_call_sync_pc,
            after_switch_sync_pc=after_switch_sync_pc,
            unhandled_sync_pcs=unhandled_sync_pcs,
            unhandled_sync_on_isr_stack=unhandled_sync_on_isr_stack,
            app_exc_call_pc=app_exc_call_pc,
        ),
        tuple(refused),
    )


def _analyze_trap_restore(
    restore: dict[int, tuple],
    *,
    proc_pc: int,
    word: int,
    mask: int,
    prologue: RiscvTrapPrologue,
    tcb_ptr: int | None,
) -> RiscvTrapRestore | str:
    """Validate ``processed_source`` SP reload, CSR/GPR restore, SP pop, and ``mret``."""
    load_op = "ld" if word == 8 else "lw"
    restore_pcs = sorted(restore)
    if len(restore_pcs) < 6 or restore[restore_pcs[-1]] != ("mret",):
        return "scheduler-profile-mismatch:processed_source:terminal"
    restore_pop_pc = restore_pcs[-2]
    if restore[restore_pop_pc] != ("addi", 2, 2, prologue.frame_bytes):
        return "scheduler-profile-mismatch:processed_source:sp-pop"
    restore_loads = riscv_frame_accesses(restore, "load", 2)
    restore_slot_map = riscv_slot_map(restore_loads)
    if not _riscv_slots_include(restore_slot_map, prologue.gpr_offsets):
        return "scheduler-profile-mismatch:processed_source:slots"
    if prologue.mstatus_offset not in restore_loads or (
        prologue.critical_nesting_offset is not None
        and prologue.critical_nesting_offset not in restore_loads
    ):
        return "scheduler-profile-mismatch:processed_source:csr-slots"

    epc_restore_regs = {reg for reg, offs in restore_slot_map.items() if 0 in offs}
    mstatus_restore_regs = {
        reg for reg, offs in restore_slot_map.items() if prologue.mstatus_offset in offs
    }
    if not any(
        op[0] == "csrw" and op[1] == _CSR_MEPC and op[2] in epc_restore_regs
        for op in restore.values()
    ) or not any(
        op[0] == "csrw" and op[1] == _CSR_MSTATUS and op[2] in mstatus_restore_regs
        for op in restore.values()
    ):
        return "scheduler-profile-mismatch:processed_source:csr-slots"

    const_addrs: dict[int, int] = {}
    restore_tcb_regs: set[int] = set()
    restore_load_sp_pc: int | None = None
    for pc in restore_pcs:
        op = restore[pc]
        if op[0] == load_op:
            rd, rn, imm = op[1], op[2], op[3]
            if (
                rd == 2
                and imm == 0
                and rn in restore_tcb_regs
                and restore_load_sp_pc is None
            ):
                restore_load_sp_pc = pc
            addr = (const_addrs[rn] + imm) & mask if rn in const_addrs else None
            riscv_update_const_addr(op, pc, mask, const_addrs)
            if tcb_ptr is not None and addr == tcb_ptr:
                restore_tcb_regs.add(rd)
            else:
                restore_tcb_regs.discard(rd)
            continue
        written = riscv_written_register(op)
        if written is not None:
            restore_tcb_regs.discard(written)
        riscv_update_const_addr(op, pc, mask, const_addrs)
    if restore_load_sp_pc is None or restore_load_sp_pc >= restore_pop_pc:
        return "scheduler-profile-mismatch:processed_source:sp-load"

    return RiscvTrapRestore(
        proc_pc=proc_pc,
        load_sp_pc=restore_load_sp_pc,
        pop_pc=restore_pop_pc,
        mret_pc=restore_pcs[-1],
        pcs=tuple(restore_pcs),
    )


def analyze_riscv_trap_blocks(
    elf: ElfImage,
    *,
    switch_start: int | None = None,
    crit_ptr: int | None = None,
) -> RiscvTrapAnalysis | str:
    """Analyze compiled FreeRTOS RISC-V trap entry, dispatch, and restore code."""
    if elf.machine != EM_RISCV or elf.pointer_size not in (4, 8):
        return "scheduler-profile-unsupported-image"
    word = elf.pointer_size
    mask = (1 << (word * 8)) - 1

    decoded = _decode_trap_sections(elf, switch_start)
    if isinstance(decoded, str):
        return decoded
    trap_start, proc_pc, split_syms, switch_start, pre_proc, restore = decoded

    tcb_ptr = elf.symbol_address("pxCurrentTCB")
    isr_top_ptr = elf.symbol_address("xISRStackTop")
    crit_val_ptr = elf.symbol_address("xCriticalNesting")
    if crit_ptr is None:
        crit_ptr = elf.symbol_address("pxCriticalNesting")
    has_crit_globals = crit_val_ptr is not None and crit_ptr is not None

    pro_res = _analyze_trap_prologue(
        pre_proc,
        trap_start=trap_start,
        proc_pc=proc_pc,
        async_sym=split_syms[0],
        sync_sym=split_syms[1],
        word=word,
        mask=mask,
        tcb_ptr=tcb_ptr,
        crit_val_ptr=crit_val_ptr,
        has_crit_globals=has_crit_globals,
    )
    if isinstance(pro_res, str):
        return pro_res
    prologue, mcause_reg, mepc_reg, async_pc, sync_pc = pro_res

    disp_res = _analyze_trap_dispatch(
        elf,
        pre_proc,
        split_syms=split_syms,
        proc_pc=proc_pc,
        async_pc=async_pc,
        sync_pc=sync_pc,
        mcause_reg=mcause_reg,
        mepc_reg=mepc_reg,
        switch_start=switch_start,
        isr_top_ptr=isr_top_ptr,
        word=word,
        mask=mask,
    )
    if isinstance(disp_res, str):
        return disp_res
    dispatch, refused_spans = disp_res

    restore_res = _analyze_trap_restore(
        restore,
        proc_pc=proc_pc,
        word=word,
        mask=mask,
        prologue=prologue,
        tcb_ptr=tcb_ptr,
    )
    if isinstance(restore_res, str):
        return restore_res

    return RiscvTrapAnalysis(
        trap_start=trap_start,
        trap_end=restore_res.mret_pc + 4,
        prologue=prologue,
        dispatch=dispatch,
        restore=restore_res,
        refused_spans=refused_spans,
    )


def analyze_riscv_trap_profile(elf: ElfImage) -> RiscvTrapSpan | None:
    """Extract a verified M-mode trap handler span from FreeRTOS port code."""
    analysis = analyze_riscv_trap_blocks(elf)
    if isinstance(analysis, str):
        return None
    layout = RiscvTrapFrameLayout(
        frame_bytes=analysis.prologue.frame_bytes,
        total_frame_bytes=analysis.prologue.frame_bytes,
        reg_offsets=analysis.prologue.gpr_offsets,
        epc_offset=0,
        mstatus_offset=analysis.prologue.mstatus_offset,
        epc_sync_addend=4,
    )
    return RiscvTrapSpan(
        entry=analysis.trap_start,
        save_complete=analysis.dispatch.intr_dispatch_start,
        call_start=analysis.dispatch.intr_dispatch_start,
        call_end=analysis.restore.proc_pc,
        exit_end=analysis.trap_end,
        layout=layout,
        refused_spans=analysis.refused_spans,
        evidence=(
            f"trap=freertos_risc_v_trap_handler@{analysis.trap_start:#x}",
            f"dispatch={analysis.dispatch.intr_dispatch_start:#x}..{analysis.restore.proc_pc:#x}",
        ),
    )


def _riscv_verify_switch_parts(elf: ElfImage, analysis: RiscvTrapAnalysis) -> bool:
    """Compare FreeRTOS initial-stack setup and first-task restore against trap layout."""
    word = elf.pointer_size
    prologue = analysis.prologue
    expected_saves = prologue.gpr_offsets
    try:
        initial = _riscv_initial_stack(elf, word)
        if initial is None:
            return False
        initial_size, initial_stores = initial
        if initial_size != prologue.frame_bytes:
            return False
        expected_lr: object = (
            ("symbol", "xTaskReturnAddress")
            if elf.symbol_address("xTaskReturnAddress") is not None
            else 0
        )
        expected_initial: dict[int, object] = {
            0: ("arg", 1),
            expected_saves[1]: expected_lr,
            expected_saves[10]: ("arg", 2),
            prologue.mstatus_offset: ("csr", _CSR_MSTATUS),
        }
        if prologue.critical_nesting_offset is not None:
            expected_initial[prologue.critical_nesting_offset] = 0
        if any(
            initial_stores.get(offset) != value
            for offset, value in expected_initial.items()
        ):
            return False
        first = RiscvCodeParser(elf, "xPortStartFirstTask").instructions(
            terminal=("ret",)
        )
        first_loads = riscv_frame_accesses(first, "load", 2)
        first_maps = riscv_slot_map(first_loads)
        expected_first = {
            register: offset
            for register, offset in expected_saves.items()
            if register != 1
        }
        if not _riscv_slots_include(first_maps, expected_first):
            return False
        if first_maps.get(1) is None or 0 not in first_maps[1]:
            return False
        if prologue.mstatus_offset not in first_loads or (
            prologue.critical_nesting_offset is not None
            and prologue.critical_nesting_offset not in first_loads
        ):
            return False
        if not any(
            op[0] == "addi" and op[1:3] == (2, 2) and op[3] == prologue.frame_bytes
            for op in first.values()
        ):
            return False
        first_symbols = riscv_target_loads(elf, first)
        return "pxCurrentTCB" in first_symbols.values() and any(
            op[0] == "ret" for op in first.values()
        )
    except (EvidenceError, StopIteration, ValueError, TypeError):
        return False


def _riscv_verify_context_switch(elf: ElfImage) -> bool:
    """Compare FreeRTOS frame effects, trap routes, startup restore and initializer."""
    analysis = analyze_riscv_trap_blocks(elf)
    return isinstance(analysis, RiscvTrapAnalysis) and _riscv_verify_switch_parts(
        elf, analysis
    )


def riscv_zero_lr_sentinel(elf: ElfImage | None) -> bool:
    """Return whether compiled RISC-V ``pxPortInitialiseStack`` writes zero to ``ra``."""
    if elf is None or elf.machine != EM_RISCV or elf.pointer_size not in (4, 8):
        return False
    if elf.symbol_address("xTaskReturnAddress") is not None:
        return False
    analysis = analyze_riscv_trap_blocks(elf)
    if isinstance(analysis, str):
        return False
    try:
        initial = _riscv_initial_stack(elf, elf.pointer_size)
    except EvidenceError:
        return False
    return (
        initial is not None
        and initial[0] == analysis.prologue.frame_bytes
        and initial[1].get(analysis.prologue.gpr_offsets[1]) == 0
    )


def _riscv_initial_stack(
    elf: ElfImage, word: int
) -> tuple[int, dict[int, object]] | None:
    """Evaluate the bounded integer setup path and return its relative stack writes."""
    init_addr = elf.symbol_address("pxPortInitialiseStack")
    if init_addr is None:
        return None
    first_addr = elf.symbol_address("xPortStartFirstTask")
    if first_addr is not None and init_addr < first_addr:
        code = RiscvCodeParser(
            elf, "pxPortInitialiseStack", "xPortStartFirstTask"
        ).instructions()
    else:
        code = riscv_decode_until_return(elf, init_addr)
    pcs = sorted(code)
    registers: dict[int, object] = {
        0: 0,
        10: ("stack", 0),
        11: ("arg", 1),
        12: ("arg", 2),
        13: ("arg", 3),
    }
    stores: dict[int, object] = {}
    pc = pcs[0]
    steps = 0
    while pc in code and steps < len(code) * 2:
        steps += 1
        op = code[pc]
        following = riscv_next_pc(code, pc)
        mnemonic = op[0]
        if mnemonic in ("sw", "sd"):
            value = registers.get(op[1])
            base = registers.get(op[2])
            if isinstance(base, tuple) and base[0] == "stack":
                offset = base[1] + op[3]
                if offset in stores and stores[offset] != value:
                    return None
                stores[offset] = value
        elif mnemonic in ("addi", "addiw"):
            source = registers.get(op[2])
            if isinstance(source, tuple) and source[0] == "stack":
                registers[op[1]] = ("stack", source[1] + op[3])
            elif isinstance(source, int):
                registers[op[1]] = source + op[3]
            else:
                registers[op[1]] = None
        elif mnemonic == "csrr":
            registers[op[1]] = ("csr", op[2])
        elif mnemonic == "andi":
            source = registers.get(op[2])
            registers[op[1]] = (
                source
                if isinstance(source, tuple) and source[0] == "csr"
                else (source & op[3] if isinstance(source, int) else None)
            )
        elif mnemonic == "slli":
            source = registers.get(op[2])
            registers[op[1]] = source << op[3] if isinstance(source, int) else None
        elif mnemonic in ("or", "add"):
            left, right = registers.get(op[2]), registers.get(op[3])
            registers[op[1]] = (
                left
                if isinstance(left, tuple) and left[0] == "csr"
                else right
                if isinstance(right, tuple) and right[0] == "csr"
                else (
                    left | right
                    if isinstance(left, int)
                    and isinstance(right, int)
                    and mnemonic == "or"
                    else left + right
                    if isinstance(left, int) and isinstance(right, int)
                    else None
                )
            )
        elif mnemonic in ("auipc", "lui"):
            registers[op[1]] = pc + op[2] if mnemonic == "auipc" else op[2]
        elif mnemonic in ("lw", "ld"):
            base = registers.get(op[2])
            value = None
            if isinstance(base, int):
                address = (base + op[3]) % (1 << (word * 8))
                names = [
                    name for name, candidate in elf.anchors if candidate == address
                ]
                if len(names) == 1:
                    value = ("symbol", names[0])
            registers[op[1]] = value
        elif mnemonic in ("beq", "bne"):
            left, right = registers.get(op[1]), registers.get(op[2])
            if not isinstance(left, int) or not isinstance(right, int):
                return None
            take = left == right if mnemonic == "beq" else left != right
            if take:
                pc = op[3]
                continue
        elif mnemonic == "jal" and op[1] == 0:
            pc = op[2]
            continue
        elif mnemonic == "nop":
            pass
        elif mnemonic in ("ret", "jalr"):
            final_sp = registers.get(10)
            if not isinstance(final_sp, tuple) or final_sp[0] != "stack":
                return None
            size = -final_sp[1]
            return size, {offset + size: value for offset, value in stores.items()}
        elif mnemonic not in ("addi", "addiw"):
            return None
        if following is None:
            break
        pc = following
    return None


def _riscv_slots_include(slots: dict[int, set[int]], expected: dict[int, int]) -> bool:
    """Return whether every expected register slot was stored or loaded."""
    return all(
        offset in slots.get(register, set()) for register, offset in expected.items()
    )


# --- Arm --------------------------------------------------------------------


def _select_arm_context(
    elf: ElfImage,
    cpu: Cpu,
    *,
    smp: bool,
    mpu: bool,
    macros: Mapping[str, str] | None,
) -> StackContextSelection:
    """Select and verify the Arm profile from symbols and kernel configuration."""
    if cpu.isa in ("armv7-m", "armv7e-m", "armv8-m.mainline"):
        if (
            cpu.abi in ("aapcs32", "aapcs32-vfp")
            and not smp
            and not mpu
            and _is_cm4f_port(elf, cpu)
            and ARMV7M_CM4F_BASIC.verified
            and ARMV7M_CM4F_FPU.verified
        ):
            return _selection_result(
                cpu,
                ARMV7M_CM4F_BASIC,
                "ok",
                (f"{cpu.abi} ABI", "port symbols"),
                alt_context=ARMV7M_CM4F_FPU,
            )
        if cpu.abi == "aapcs32-vfp":
            return _selection_result(
                cpu, None, "unsupported-extension-context", ("aapcs32-vfp ABI",)
            )
    context = _arm_profile(cpu.isa, smp=smp, mpu=mpu)
    if context is None or not context.verified:
        return _selection_result(cpu, None, "unverified-port")
    if any(
        elf.symbol_address(name) is None
        for name in ("PendSV_Handler", "pxPortInitialiseStack")
    ):
        return _selection_result(cpu, None, "unrecognizable-stack-frame-layout")
    if cpu.isa == "armv8-m.mainline":
        reason = _arm_configuration_evidence(macros or {}, mpu=mpu)
        if reason is not None:
            return _selection_result(cpu, None, reason)
    return _selection_result(
        cpu,
        context,
        "ok",
        (
            ".ARM.attributes Tag_CPU_arch/profile",
            "port symbols",
            *(("kernel configuration",) if cpu.isa == "armv8-m.mainline" else ()),
        ),
    )


def _is_cm4f_port(elf: ElfImage, cpu: Cpu) -> bool:
    """Whether the image links the CM4F port, from compiled port symbols.

    The VFP ABI alone does not prove the FPU port: a shared config header
    can enable FPU on a CM3 build that ignores it, and ``softfp`` builds
    report ``aapcs32``. ``vPortEnableVFP`` exists only in the M-profile FPU
    port; on Armv8-M ``xPortSysTickHandler`` together with ``vPortEnableVFP``
    distinguishes the CM4F port from a CM33 port with FPU enabled (which uses
    ``SysTick_Handler`` and ``prvSetupFPU``) and from ``ARM_CM3`` (which has
    ``xPortSysTickHandler`` without ``vPortEnableVFP``).
    """
    if cpu.isa in ("armv7-m", "armv7e-m"):
        return elf.symbol_address("vPortEnableVFP") is not None
    return cpu.isa == "armv8-m.mainline" and (
        elf.symbol_address("vPortEnableVFP") is not None
        and elf.symbol_address("xPortSysTickHandler") is not None
    )


def _arm_profile(isa: str | None, *, smp: bool, mpu: bool) -> SavedStackContext | None:
    """Return the Arm integer profile matching the ISA and context mode."""
    if isa == "armv6-m" and not smp and not mpu:
        return ARMV6M_INTEGER
    if isa in ("armv7-m", "armv7e-m") and not smp and not mpu:
        return ARMV7M_INTEGER
    if isa == "armv8-m.mainline" and not smp and not mpu:
        return ARMV8M_MAINLINE_INTEGER
    if isa == "armv8-m.mainline" and smp and not mpu:
        return ARMV8M_MAINLINE_SMP_INTEGER
    if isa == "armv8-m.mainline" and mpu and not smp:
        return ARMV8M_MAINLINE_MPU_INTEGER
    return None


def _arm_configuration_evidence(macros: Mapping[str, str], *, mpu: bool) -> str | None:
    """Return a rejection reason for an unsupported Armv8-M Mainline configuration."""
    required = ("configENABLE_FPU", "configENABLE_TRUSTZONE", "configENABLE_MPU")
    if mpu:
        required += ("configUSE_MPU_WRAPPERS_V1",)
    if any(name not in macros for name in required):
        return "port-configuration-unavailable"
    for name in ("configENABLE_FPU", "configENABLE_MVE"):
        if macros.get(name, "0") != "0":
            return "unsupported-extension-context"
    if macros["configENABLE_TRUSTZONE"] != "0":
        return "port-configuration-unavailable"
    if macros.get("configENABLE_MPU") != ("1" if mpu else "0") or (
        mpu and macros.get("configUSE_MPU_WRAPPERS_V1") != "0"
    ):
        return "port-configuration-conflict"
    return None
