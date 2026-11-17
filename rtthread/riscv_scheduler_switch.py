"""Compiled RT-Thread RISC-V scheduler and trap stage verification (pure).

The module verifies ``rt_hw_context_switch_to``, ``rt_hw_context_switch``,
``rt_hw_context_switch_exit``, ``trap_entry``, and ``spurious_interrupt`` from
the loaded ELF via :mod:`core.insn_riscv`, distinguishing ``virt64``
(``stacked-epc``) from ``e310`` (``deferred-mepc``) and refusing SMP, FPU, or
signal-stack variants.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from core.cpus import cpu_of
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_frame_accesses,
    riscv_slot_map,
    riscv_stack_frame_size,
    riscv_store_completion_slots,
    riscv_update_const_addr,
    riscv_written_register,
)
from core.unwind_meta import EM_RISCV, ElfImage

RV32_PROFILE_ID = "rtthread/rv32/riscv-mtvec-v1"
RV64_PROFILE_ID = "rtthread/rv64/riscv-mtvec-v1"

_FRAME_WORDS = 32
_CSR_MSTATUS = 0x300
_CSR_MEPC = 0x341
_SMP_OR_SIGNAL_SYMBOLS = (
    "rt_cpus_lock_status_restore",
    "rt_scheduler_do_irq_switch",
    "rt_signal_check",
)


@dataclass(frozen=True)
class RiscvSchedulerStage:
    """One classified PC inside RT-Thread's RISC-V scheduler or trap code."""

    name: str
    owner: str
    seed: str
    pc: int
    function: str
    detail: str


@dataclass(frozen=True)
class RiscvSchedulerProfile:
    """Verified symbol-relative boundaries and stages of RT-Thread RISC-V code."""

    profile_id: str
    fingerprint: str
    xlen: int
    word_size: int
    frame_bytes: int
    epc_mode: str  # "stacked-epc" | "deferred-mepc"
    flag_symbol: int
    from_symbol: int
    to_symbol: int
    current_thread_symbol: int
    switch_to_start: int
    switch_to_end: int
    switch_start: int
    switch_end: int
    switch_store_from_pc: int
    switch_a0_clobber_pc: int
    switch_load_to_pc: int
    exit_start: int
    exit_end: int
    exit_pop_sp_pc: int
    exit_mret_pc: int
    trap_start: int
    trap_end: int
    trap_mv_s0_pc: int
    trap_call_start: int
    trap_mv_sp_pc: int
    trap_clear_flag_pc: int
    trap_epc_store_pc: int | None
    trap_store_from_pc: int
    trap_load_to_pc: int
    spurious_start: int
    spurious_end: int
    spurious_mret_pc: int | None
    switch_saved_slots: tuple[tuple[int, int, int], ...]
    trap_saved_slots: tuple[tuple[int, int, int], ...]
    stages: dict[int, RiscvSchedulerStage]


def _expected_gpr_slots(word: int) -> dict[int, int]:
    return {
        1: word,
        **{reg: reg * word for reg in range(4, 32)},
    }


def _preserved_gpr_slots(word: int) -> dict[int, int]:
    """Return ABI callee-saved + link/thread slots that must not be clobbered before save."""
    return {
        1: word,
        4: 4 * word,
        8: 8 * word,
        9: 9 * word,
        **{reg: reg * word for reg in range(18, 28)},
    }


def _insn_end(elf: ElfImage, last_pc: int) -> int:
    raw = elf.read_at(last_pc, 2)
    size = 4 if raw is not None and (int.from_bytes(raw, "little") & 3) == 3 else 2
    return last_pc + size


def _verify_switch_to(
    elf: ElfImage, word: int, exit_start: int
) -> tuple[dict[int, tuple], int] | str:
    try:
        insns = RiscvCodeParser(
            elf, "rt_hw_context_switch_to", "rt_hw_context_switch"
        ).instructions(terminal=("jal",))
    except EvidenceError:
        return "scheduler-profile-mismatch:rt_hw_context_switch_to:decode"
    pcs = sorted(insns)
    load_op = "ld" if word == 8 else "lw"
    if len(pcs) < 3:
        return "scheduler-profile-mismatch:rt_hw_context_switch_to:length"
    if insns[pcs[0]] != (load_op, 2, 10, 0):
        return "scheduler-profile-mismatch:rt_hw_context_switch_to:entry"
    if any(op[0] == "jal" and op[1] == 1 for op in insns.values()):
        return "scheduler-profile-mismatch:rt_hw_context_switch_to:call"
    if insns[pcs[-1]] != ("jal", 0, exit_start):
        return "scheduler-profile-mismatch:rt_hw_context_switch_to:tail"
    if not any(
        op[0] == load_op and op[2] == 2 and op[3] == 2 * word for op in insns.values()
    ):
        return "scheduler-profile-mismatch:rt_hw_context_switch_to:mstatus-load"
    if not any(op[0] == "csrw" and op[1] == _CSR_MSTATUS for op in insns.values()):
        return "scheduler-profile-mismatch:rt_hw_context_switch_to:mstatus-csr"
    return insns, pcs[0]


def _verify_context_switch(
    elf: ElfImage, word: int, exit_start: int
) -> tuple[dict[int, tuple], int, int, int, tuple[tuple[int, int, int], ...]] | str:
    try:
        insns = RiscvCodeParser(
            elf, "rt_hw_context_switch", "rt_hw_context_switch_exit"
        ).instructions(terminal=("jal",))
    except EvidenceError:
        return "scheduler-profile-mismatch:rt_hw_context_switch:decode"
    pcs = sorted(insns)
    if len(pcs) < 10:
        return "scheduler-profile-mismatch:rt_hw_context_switch:length"
    load_op = "ld" if word == 8 else "lw"
    store_op = "sd" if word == 8 else "sw"
    if insns[pcs[0]] != ("addi", 2, 2, -_FRAME_WORDS * word):
        return "scheduler-profile-mismatch:rt_hw_context_switch:entry"
    if riscv_stack_frame_size(insns) != _FRAME_WORDS * word:
        return "scheduler-profile-mismatch:rt_hw_context_switch:frame-size"
    if any(op[0] == "jal" and op[1] == 1 for op in insns.values()):
        return "scheduler-profile-mismatch:rt_hw_context_switch:call"

    store_from_pc = next(
        (pc for pc, op in insns.items() if op == (store_op, 2, 10, 0)), None
    )
    a0_clobber_pc = next(
        (
            pc
            for pc, op in insns.items()
            if op[0] == "csrr" and op[1] == 10 and op[2] == _CSR_MSTATUS
        ),
        None,
    )
    load_to_pc = pcs[-2]
    if (
        store_from_pc is None
        or a0_clobber_pc is None
        or store_from_pc >= a0_clobber_pc
        or insns[load_to_pc] != (load_op, 2, 11, 0)
        or insns[pcs[-1]] != ("jal", 0, exit_start)
    ):
        return "scheduler-profile-mismatch:rt_hw_context_switch:sequence"

    saves = riscv_slot_map(riscv_frame_accesses(insns, "store", 2))
    expected_saves = {
        1: {0, word},
        **{reg: {reg * word} for reg in range(4, 32)},
    }
    if any(
        not offsets.issubset(saves.get(reg, set()))
        for reg, offsets in expected_saves.items()
    ):
        return "scheduler-profile-mismatch:rt_hw_context_switch:slots"
    if not any(
        op[0] == store_op and op[2] == 2 and op[3] == 2 * word for op in insns.values()
    ):
        return "scheduler-profile-mismatch:rt_hw_context_switch:mstatus-slot"

    saved_slots = riscv_store_completion_slots(
        insns, store_op, base=2, expected_offsets=_preserved_gpr_slots(word)
    )
    if saved_slots is None:
        return "scheduler-profile-mismatch:rt_hw_context_switch:preserved-slots"
    return insns, store_from_pc, a0_clobber_pc, load_to_pc, saved_slots


def _verify_context_switch_exit(
    elf: ElfImage, word: int
) -> tuple[dict[int, tuple], int, int] | str:
    try:
        insns = RiscvCodeParser(elf, "rt_hw_context_switch_exit").instructions(
            terminal=("mret",)
        )
    except EvidenceError:
        return "scheduler-profile-mismatch:rt_hw_context_switch_exit:decode"
    pcs = sorted(insns)
    if len(pcs) < 10 or insns[pcs[-1]] != ("mret",):
        return "scheduler-profile-mismatch:rt_hw_context_switch_exit:terminal"
    pop_pc = pcs[-2]
    if insns[pop_pc] != ("addi", 2, 2, _FRAME_WORDS * word):
        return "scheduler-profile-mismatch:rt_hw_context_switch_exit:sp-pop"
    if any(op[0] == "jal" for op in insns.values()):
        return "scheduler-profile-mismatch:rt_hw_context_switch_exit:call"
    load_op = "ld" if word == 8 else "lw"
    loads = riscv_frame_accesses(insns, "load", 2)
    restored = riscv_slot_map(loads)
    for reg, offset in _expected_gpr_slots(word).items():
        if offset not in restored.get(reg, set()):
            return "scheduler-profile-mismatch:rt_hw_context_switch_exit:slots"
    epc_load_reg = next(
        (
            op[1]
            for op in insns.values()
            if op[0] == load_op and op[2] == 2 and op[3] == 0
        ),
        None,
    )
    if epc_load_reg is None or not any(
        op[0] == "csrw" and op[1] == _CSR_MEPC and op[2] == epc_load_reg
        for op in insns.values()
    ):
        return "scheduler-profile-mismatch:rt_hw_context_switch_exit:mepc"
    if 2 * word not in loads:
        return "scheduler-profile-mismatch:rt_hw_context_switch_exit:mstatus"
    return insns, pop_pc, pcs[-1]


def _verify_trap_and_spurious(
    elf: ElfImage,
    word: int,
    exit_start: int,
    *,
    flag_addr: int,
    from_addr: int,
    to_addr: int,
) -> tuple[dict[str, object], tuple[int, ...]] | str:
    trap_start = elf.symbol_address("trap_entry")
    spurious_pc = elf.symbol_address("spurious_interrupt")
    if trap_start is None or spurious_pc is None or spurious_pc <= trap_start:
        return "scheduler-profile-symbol-missing:trap_entry"
    try:
        trap_insns = RiscvCodeParser(
            elf, "trap_entry", "spurious_interrupt"
        ).instructions()
        spurious_insns = RiscvCodeParser(elf, "spurious_interrupt").instructions(
            terminal=("mret", "jal", "jalr")
        )
    except EvidenceError:
        return "scheduler-profile-mismatch:trap_entry:decode"

    trap_pcs = sorted(trap_insns)
    spurious_pcs = sorted(spurious_insns)
    if len(trap_pcs) < 12 or not spurious_pcs:
        return "scheduler-profile-mismatch:trap_entry:length"
    store_op = "sd" if word == 8 else "sw"
    if trap_insns[trap_pcs[0]] != ("addi", 2, 2, -_FRAME_WORDS * word):
        return "scheduler-profile-mismatch:trap_entry:entry"

    mv_s0_pc = next(
        (
            pc
            for pc in trap_pcs
            if trap_insns[pc] in (("add", 8, 0, 2), ("addi", 8, 2, 0))
        ),
        None,
    )
    if mv_s0_pc is None:
        return "scheduler-profile-mismatch:trap_entry:mv-s0"
    mv_sp_pc = next(
        (
            pc
            for pc in trap_pcs
            if pc > mv_s0_pc and trap_insns[pc] in (("add", 2, 0, 8), ("addi", 2, 8, 0))
        ),
        None,
    )
    if mv_sp_pc is None:
        return "scheduler-profile-mismatch:trap_entry:mv-sp"

    dispatch_jals = [
        pc for pc in trap_pcs if mv_s0_pc < pc < mv_sp_pc and trap_insns[pc][0] == "jal"
    ]
    if not dispatch_jals:
        return "scheduler-profile-mismatch:trap_entry:dispatch"
    call_start = dispatch_jals[0]
    call_end = mv_sp_pc

    pre_s0 = {pc: trap_insns[pc] for pc in trap_pcs if pc <= mv_s0_pc}
    if riscv_stack_frame_size(pre_s0) != _FRAME_WORDS * word:
        return "scheduler-profile-mismatch:trap_entry:frame-size"
    pre_saves = riscv_slot_map(riscv_frame_accesses(pre_s0, "store", 2))
    for reg, offset in _expected_gpr_slots(word).items():
        if offset not in pre_saves.get(reg, set()):
            return "scheduler-profile-mismatch:trap_entry:gpr-slots"
    if not any(
        op[0] == store_op and op[2] == 2 and op[3] == 2 * word for op in pre_s0.values()
    ):
        return "scheduler-profile-mismatch:trap_entry:mstatus-slot"
    saved_slots = riscv_store_completion_slots(
        pre_s0, store_op, base=2, expected_offsets=_preserved_gpr_slots(word)
    )
    if saved_slots is None:
        return "scheduler-profile-mismatch:trap_entry:preserved-slots"

    def _mepc_store_pc(window: dict[int, tuple]) -> int | None:
        mepc_regs: set[int] = set()
        for pc in sorted(window):
            op = window[pc]
            if op[0] == "csrr" and op[2] == _CSR_MEPC:
                mepc_regs.add(op[1])
            elif op[0] == store_op and op[1] in mepc_regs and op[2] == 2 and op[3] == 0:
                return pc
            else:
                written = riscv_written_register(op)
                if written is not None:
                    mepc_regs.discard(written)
        return None

    pre_call = {pc: trap_insns[pc] for pc in trap_pcs if pc < call_start}
    post_call = {pc: trap_insns[pc] for pc in trap_pcs if pc >= call_end}
    pre_epc_store = _mepc_store_pc(pre_call)
    post_epc_store = _mepc_store_pc(post_call)
    if pre_epc_store is not None:
        epc_mode = "stacked-epc"
        epc_store_pc: int | None = None
    elif post_epc_store is not None:
        epc_mode = "deferred-mepc"
        epc_store_pc = post_epc_store
    else:
        return "scheduler-profile-mismatch:trap_entry:epc-policy"

    # Verify post-call epilogue: flag check/clear, from_thread store, to_thread load.
    mask = (1 << (word * 8)) - 1
    const_addrs: dict[int, int] = {}
    ptr_source: dict[int, str] = {}
    flag_regs: set[int] = set()
    flag_branch_ok = False
    clear_flag_pc: int | None = None
    store_from_pc: int | None = None
    load_to_pc: int | None = None
    for pc in sorted(post_call):
        op = post_call[pc]
        mnemonic = op[0]
        if mnemonic == "lw":
            rd, rn, imm = op[1], op[2], op[3]
            addr = (const_addrs[rn] + imm) & mask if rn in const_addrs else None
            riscv_update_const_addr(op, pc, mask, const_addrs)
            ptr_source.pop(rd, None)
            if addr == flag_addr:
                flag_regs.add(rd)
            else:
                flag_regs.discard(rd)
                if word == 4 and addr == from_addr:
                    ptr_source[rd] = "from"
                elif word == 4 and addr == to_addr:
                    ptr_source[rd] = "to"
                elif rd == 2 and imm == 0 and ptr_source.get(rn) == "to":
                    load_to_pc = pc
            continue
        if mnemonic == "ld" and word == 8:
            rd, rn, imm = op[1], op[2], op[3]
            addr = (const_addrs[rn] + imm) & mask if rn in const_addrs else None
            riscv_update_const_addr(op, pc, mask, const_addrs)
            flag_regs.discard(rd)
            ptr_source.pop(rd, None)
            if addr == from_addr:
                ptr_source[rd] = "from"
            elif addr == to_addr:
                ptr_source[rd] = "to"
            elif rd == 2 and imm == 0 and ptr_source.get(rn) == "to":
                load_to_pc = pc
            continue
        if (
            mnemonic == "beq"
            and op[1] in flag_regs
            and op[2] == 0
            and op[3] == spurious_pc
        ):
            flag_branch_ok = True
            continue
        if mnemonic == "sw" and op[1] == 0 and op[2] in const_addrs:
            if ((const_addrs[op[2]] + op[3]) & mask) == flag_addr:
                clear_flag_pc = pc
            continue
        if (
            mnemonic == store_op
            and op[1] == 2
            and op[3] == 0
            and ptr_source.get(op[2]) == "from"
        ):
            store_from_pc = pc
            continue
        written = riscv_written_register(op)
        if written is not None:
            flag_regs.discard(written)
            ptr_source.pop(written, None)
        riscv_update_const_addr(op, pc, mask, const_addrs)

    if (
        not flag_branch_ok
        or clear_flag_pc is None
        or store_from_pc is None
        or load_to_pc is None
        or not (mv_sp_pc < clear_flag_pc < store_from_pc < load_to_pc)
    ):
        return "scheduler-profile-mismatch:trap_entry:epilogue"
    if epc_store_pc is not None and not (clear_flag_pc < epc_store_pc < store_from_pc):
        return "scheduler-profile-mismatch:trap_entry:deferred-epc-order"

    # Verify spurious_interrupt matches stacked-epc tail or deferred-mepc restore.
    spurious_mret_pc: int | None = None
    if epc_mode == "stacked-epc":
        if spurious_insns[spurious_pcs[-1]] != ("jal", 0, exit_start):
            return "scheduler-profile-mismatch:spurious_interrupt:tail"
    else:
        if len(spurious_pcs) < 6 or spurious_insns[spurious_pcs[-1]] != ("mret",):
            return "scheduler-profile-mismatch:spurious_interrupt:terminal"
        if spurious_insns[spurious_pcs[-2]] != ("addi", 2, 2, _FRAME_WORDS * word):
            return "scheduler-profile-mismatch:spurious_interrupt:sp-pop"
        spurious_loads = riscv_frame_accesses(spurious_insns, "load", 2)
        spurious_restored = riscv_slot_map(spurious_loads)
        if any(
            offset not in spurious_restored.get(reg, set())
            for reg, offset in _expected_gpr_slots(word).items()
        ):
            return "scheduler-profile-mismatch:spurious_interrupt:slots"
        if 2 * word not in spurious_loads:
            return "scheduler-profile-mismatch:spurious_interrupt:mstatus"
        spurious_mret_pc = spurious_pcs[-1]

    info: dict[str, object] = {
        "trap_start": trap_start,
        "trap_end": _insn_end(elf, spurious_pcs[-1]),
        "mv_s0_pc": mv_s0_pc,
        "call_start": call_start,
        "mv_sp_pc": mv_sp_pc,
        "clear_flag_pc": clear_flag_pc,
        "epc_mode": epc_mode,
        "epc_store_pc": epc_store_pc,
        "store_from_pc": store_from_pc,
        "load_to_pc": load_to_pc,
        "spurious_pc": spurious_pc,
        "spurious_end": _insn_end(elf, spurious_pcs[-1]),
        "spurious_mret_pc": spurious_mret_pc,
        "trap_pcs": trap_pcs,
        "spurious_pcs": spurious_pcs,
        "saved_slots": saved_slots,
    }
    return info, tuple(trap_pcs + spurious_pcs)


def _build_stage_map(
    *,
    switch_to_insns: dict[int, tuple],
    switch_to_entry_pc: int,
    switch_insns: dict[int, tuple],
    switch_load_to_pc: int,
    exit_insns: dict[int, tuple],
    exit_pop_pc: int,
    exit_mret_pc: int,
    trap_info: dict[str, object],
) -> dict[int, RiscvSchedulerStage]:
    stages: dict[int, RiscvSchedulerStage] = {}

    for pc in sorted(switch_to_insns):
        if pc == switch_to_entry_pc:
            stages[pc] = RiscvSchedulerStage(
                name="switch-to-entry",
                owner="new",
                seed="unstarted",
                pc=pc,
                function="rt_hw_context_switch_to",
                detail="rt_hw_context_switch_to before loading first thread sp from a0",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="switch-to-sp-loaded",
                owner="new",
                seed="unstarted",
                pc=pc,
                function="rt_hw_context_switch_to",
                detail="rt_hw_context_switch_to with sp pointing to initial thread frame",
            )

    switch_pcs = sorted(switch_insns)
    for pc in switch_pcs:
        if pc == switch_pcs[0]:
            stages[pc] = RiscvSchedulerStage(
                name="context-switch-entry",
                owner="old",
                seed="live-current",
                pc=pc,
                function="rt_hw_context_switch",
                detail="rt_hw_context_switch entry on outgoing thread stack (rt_current_thread already updated)",
            )
        elif pc <= switch_load_to_pc:
            stages[pc] = RiscvSchedulerStage(
                name="context-switch-saving",
                owner="old",
                seed="live-prologue",
                pc=pc,
                function="rt_hw_context_switch",
                detail="rt_hw_context_switch saving outgoing GPRs prior to loading incoming sp",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="context-switch-sp-loaded",
                owner="new",
                seed="restore-frame",
                pc=pc,
                function="rt_hw_context_switch",
                detail="rt_hw_context_switch loaded incoming sp prior to rt_hw_context_switch_exit",
            )

    for pc in sorted(exit_insns):
        if pc <= exit_pop_pc:
            stages[pc] = RiscvSchedulerStage(
                name="restore-window",
                owner="new",
                seed="restore-frame",
                pc=pc,
                function="rt_hw_context_switch_exit",
                detail="rt_hw_context_switch_exit restoring context from intact stack frame",
            )
        elif pc == exit_mret_pc:
            stages[pc] = RiscvSchedulerStage(
                name="restore-sp-popped",
                owner="new",
                seed="live-restore",
                pc=pc,
                function="rt_hw_context_switch_exit",
                detail="rt_hw_context_switch_exit at mret with incoming GPRs and mepc restored",
            )

    trap_pcs: list[int] = trap_info["trap_pcs"]  # type: ignore[assignment]
    call_start: int = trap_info["call_start"]  # type: ignore[assignment]
    mv_sp_pc: int = trap_info["mv_sp_pc"]  # type: ignore[assignment]
    store_from_pc: int = trap_info["store_from_pc"]  # type: ignore[assignment]
    load_to_pc: int = trap_info["load_to_pc"]  # type: ignore[assignment]
    for pc in trap_pcs:
        if pc == trap_pcs[0]:
            stages[pc] = RiscvSchedulerStage(
                name="trap-entry",
                owner="old",
                seed="trap-entry-live",
                pc=pc,
                function="trap_entry",
                detail="trap_entry before saving context or decrementing sp",
            )
        elif pc < call_start:
            stages[pc] = RiscvSchedulerStage(
                name="trap-prologue-saving",
                owner="old",
                seed="live-prologue",
                pc=pc,
                function="trap_entry",
                detail="trap_entry prologue saving GPRs and switching to IRQ stack",
            )
        elif pc < mv_sp_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-dispatch",
                owner="old",
                seed="exception-isr",
                pc=pc,
                function="trap_entry",
                detail="trap_entry ISR C dispatch on IRQ stack",
            )
        elif pc <= store_from_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-epilogue-before-switch-store",
                owner="old",
                seed="epilogue-from",
                pc=pc,
                function="trap_entry",
                detail="trap_entry epilogue on outgoing frame before *rt_interrupt_from_thread store",
            )
        elif pc <= load_to_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-epilogue-before-switch-load",
                owner="old",
                seed="epilogue-from",
                pc=pc,
                function="trap_entry",
                detail="trap_entry epilogue after *rt_interrupt_from_thread store and before loading incoming sp",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="trap-epilogue-after-switch-load",
                owner="new",
                seed="restore-frame",
                pc=pc,
                function="trap_entry",
                detail="trap_entry epilogue with incoming thread sp loaded",
            )

    spurious_mret_pc: int | None = trap_info["spurious_mret_pc"]  # type: ignore[assignment]
    epc_mode: str = trap_info["epc_mode"]  # type: ignore[assignment]
    for pc in trap_info["spurious_pcs"]:  # type: ignore[attr-defined]
        if spurious_mret_pc is not None and pc == spurious_mret_pc:
            stages[pc] = RiscvSchedulerStage(
                name="restore-sp-popped",
                owner="new",
                seed="live-restore",
                pc=pc,
                function="spurious_interrupt",
                detail="spurious_interrupt at mret with GPRs and mepc restored",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="restore-window",
                owner="new",
                seed=f"restore-frame:{epc_mode}",
                pc=pc,
                function="spurious_interrupt",
                detail=f"spurious_interrupt restoring frame ({epc_mode})",
            )

    return stages


def build_riscv_profile(
    elf: ElfImage | None,
) -> tuple[RiscvSchedulerProfile | None, str]:
    """Verify RT-Thread RISC-V scheduler/trap code in *elf* and build its stage map."""
    if elf is None:
        return None, "scheduler-profile-unavailable"
    if elf.machine != EM_RISCV or elf.pointer_size not in (4, 8):
        return None, "scheduler-profile-unsupported-image"
    if elf.endian != "little":
        return None, "scheduler-profile-unsupported-endian"
    cpu = cpu_of(elf)
    if cpu.reason != "ok" or cpu.isa not in ("riscv32", "riscv64"):
        return None, "scheduler-profile-unsupported-image"
    if any(elf.symbol_address(sym) is not None for sym in _SMP_OR_SIGNAL_SYMBOLS):
        return None, "scheduler-profile-unsupported-smp"

    flag_addr = elf.symbol_address("rt_thread_switch_interrupt_flag")
    from_addr = elf.symbol_address("rt_interrupt_from_thread")
    to_addr = elf.symbol_address("rt_interrupt_to_thread")
    cur_addr = elf.symbol_address("rt_current_thread")
    exit_start = elf.symbol_address("rt_hw_context_switch_exit")
    if None in (flag_addr, from_addr, to_addr, cur_addr, exit_start):
        return None, "scheduler-profile-symbol-missing:kernel-globals"
    assert (
        flag_addr is not None
        and from_addr is not None
        and to_addr is not None
        and cur_addr is not None
        and exit_start is not None
    )

    word = elf.pointer_size
    xlen = word * 8

    switch_to_res = _verify_switch_to(elf, word, exit_start)
    if isinstance(switch_to_res, str):
        return None, switch_to_res
    switch_to_insns, switch_to_entry_pc = switch_to_res

    switch_res = _verify_context_switch(elf, word, exit_start)
    if isinstance(switch_res, str):
        return None, switch_res
    switch_insns, store_from_pc, a0_clobber_pc, load_to_pc, switch_saved_slots = (
        switch_res
    )

    exit_res = _verify_context_switch_exit(elf, word)
    if isinstance(exit_res, str):
        return None, exit_res
    exit_insns, exit_pop_pc, exit_mret_pc = exit_res

    trap_res = _verify_trap_and_spurious(
        elf,
        word,
        exit_start,
        flag_addr=flag_addr,
        from_addr=from_addr,
        to_addr=to_addr,
    )
    if isinstance(trap_res, str):
        return None, trap_res
    trap_info, trap_all_pcs = trap_res

    switch_to_start = min(switch_to_insns)
    switch_to_end = _insn_end(elf, max(switch_to_insns))
    switch_start = min(switch_insns)
    switch_end = _insn_end(elf, max(switch_insns))
    exit_end = _insn_end(elf, exit_mret_pc)
    trap_start: int = trap_info["trap_start"]  # type: ignore[assignment]
    trap_end: int = trap_info["trap_end"]  # type: ignore[assignment]

    ordered = sorted(
        (
            ("rt_hw_context_switch_to", switch_to_start, switch_to_end),
            ("rt_hw_context_switch", switch_start, switch_end),
            ("rt_hw_context_switch_exit", exit_start, exit_end),
            ("trap_entry", trap_start, trap_end),
        ),
        key=lambda item: item[1],
    )
    for (prev_name, _ps, prev_end), (cur_name, cur_start, _ce) in zip(
        ordered, ordered[1:], strict=False
    ):
        if cur_start < prev_end:
            return None, f"scheduler-profile-symbol-overlap:{prev_name}:{cur_name}"

    stages = _build_stage_map(
        switch_to_insns=switch_to_insns,
        switch_to_entry_pc=switch_to_entry_pc,
        switch_insns=switch_insns,
        switch_load_to_pc=load_to_pc,
        exit_insns=exit_insns,
        exit_pop_pc=exit_pop_pc,
        exit_mret_pc=exit_mret_pc,
        trap_info=trap_info,
    )

    tokens = [
        f"switch_to:{len(switch_to_insns)}",
        f"switch:{len(switch_insns)}",
        f"exit:{len(exit_insns)}",
        f"trap:{len(trap_all_pcs)}:{trap_info['epc_mode']}",
    ]
    fingerprint = hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()[:16]

    return (
        RiscvSchedulerProfile(
            profile_id=RV64_PROFILE_ID if xlen == 64 else RV32_PROFILE_ID,
            fingerprint=fingerprint,
            xlen=xlen,
            word_size=word,
            frame_bytes=_FRAME_WORDS * word,
            epc_mode=trap_info["epc_mode"],  # type: ignore[arg-type]
            flag_symbol=flag_addr,
            from_symbol=from_addr,
            to_symbol=to_addr,
            current_thread_symbol=cur_addr,
            switch_to_start=switch_to_start,
            switch_to_end=switch_to_end,
            switch_start=switch_start,
            switch_end=switch_end,
            switch_store_from_pc=store_from_pc,
            switch_a0_clobber_pc=a0_clobber_pc,
            switch_load_to_pc=load_to_pc,
            exit_start=exit_start,
            exit_end=exit_end,
            exit_pop_sp_pc=exit_pop_pc,
            exit_mret_pc=exit_mret_pc,
            trap_start=trap_start,
            trap_end=trap_end,
            trap_mv_s0_pc=trap_info["mv_s0_pc"],  # type: ignore[arg-type]
            trap_call_start=trap_info["call_start"],  # type: ignore[arg-type]
            trap_mv_sp_pc=trap_info["mv_sp_pc"],  # type: ignore[arg-type]
            trap_clear_flag_pc=trap_info["clear_flag_pc"],  # type: ignore[arg-type]
            trap_epc_store_pc=trap_info["epc_store_pc"],  # type: ignore[arg-type]
            trap_store_from_pc=trap_info["store_from_pc"],  # type: ignore[arg-type]
            trap_load_to_pc=trap_info["load_to_pc"],  # type: ignore[arg-type]
            spurious_start=trap_info["spurious_pc"],  # type: ignore[arg-type]
            spurious_end=trap_info["spurious_end"],  # type: ignore[arg-type]
            spurious_mret_pc=trap_info["spurious_mret_pc"],  # type: ignore[arg-type]
            switch_saved_slots=switch_saved_slots,
            trap_saved_slots=trap_info["saved_slots"],  # type: ignore[arg-type]
            stages=stages,
        ),
        "ok",
    )


def classify_riscv_stage(
    profile: RiscvSchedulerProfile, pc: int
) -> RiscvSchedulerStage | str | None:
    """Classify one exact PC; unknown PCs inside a verified range are refused."""
    stage = profile.stages.get(pc)
    if stage is not None:
        return stage
    for start, end, name in (
        (profile.switch_to_start, profile.switch_to_end, "rt_hw_context_switch_to"),
        (profile.switch_start, profile.switch_end, "rt_hw_context_switch"),
        (profile.exit_start, profile.exit_end, "rt_hw_context_switch_exit"),
        (profile.trap_start, profile.trap_end, "trap_entry"),
    ):
        if start <= pc < end:
            return f"scheduler-stage-ambiguous:{name}"
    return None
