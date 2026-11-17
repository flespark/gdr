"""Compiled FreeRTOS RISC-V scheduler and trap stage verification (pure).

The module verifies ``xPortStartFirstTask``, ``freertos_risc_v_trap_handler``
(including ``asynchronous_interrupt``, ``synchronous_exception``,
``handle_interrupt``, ``handle_exception``, and ``processed_source``), and
``vTaskSwitchContext`` from the loaded ELF via :mod:`core.insn_riscv`, then
classifies live stops across every exact transition stage without hardcoding
link addresses.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass

from core.cpus import cpu_of
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_frame_accesses,
    riscv_next_pc,
    riscv_slot_map,
    riscv_stack_frame_size,
    riscv_store_completion_slots,
    riscv_target_loads,
    riscv_update_const_addr,
    riscv_written_register,
)
from core.unwind_meta import EM_RISCV, ElfImage

RV32_PROFILE_ID = "freertos/rv32/riscv-mtvec-v1"
RV64_PROFILE_ID = "freertos/rv64/riscv-mtvec-v1"

_FRAME_WORDS = 31
_CRITICAL_NESTING_WORD = 29
_MSTATUS_WORD = 30
_CSR_MSTATUS = 0x300
_CSR_MEPC = 0x341
_CSR_MCAUSE = 0x342
_ECALL_M_CAUSE = 11
_RISCV_CALLER_SAVED = frozenset(
    {1, 5, 6, 7, 10, 11, 12, 13, 14, 15, 16, 17, 28, 29, 30, 31}
)
_BRANCH_MNEMONICS = frozenset({"beq", "bne", "blt", "bge", "bltu", "bgeu"})
_MAX_SWITCH_INSTRUCTIONS = 512


@dataclass(frozen=True)
class RiscvSchedulerStage:
    """One exact RISC-V scheduler or trap stop and the task context that owns it."""

    name: str
    owner: str
    seed: str
    pc: int
    function: str
    detail: str


@dataclass(frozen=True)
class RiscvSchedulerProfile:
    """Verified symbol-relative boundaries and stages of FreeRTOS RISC-V switch code."""

    profile_id: str
    fingerprint: str
    xlen: int
    word_size: int
    frame_bytes: int
    px_current_tcb: int
    start_first_start: int
    start_first_end: int
    start_first_sp_load_pc: int
    start_first_sp_pop_pc: int
    start_first_ret_pc: int
    trap_start: int
    trap_end: int
    trap_tcb_store_pc: int
    async_epc_store_pc: int
    sync_entry: int
    sync_epc_store_pc: int
    handle_interrupt_pc: int
    handle_exception_pc: int
    processed_source_pc: int
    restore_load_sp_pc: int
    restore_pop_sp_pc: int
    restore_mret_pc: int
    switch_start: int
    switch_end: int
    switch_update_pc: int
    switch_after_update_pc: int
    trap_saved_slots: tuple[tuple[int, int, int], ...]
    stages: dict[int, RiscvSchedulerStage]


def _expected_gpr_slots(word: int) -> dict[int, int]:
    return {
        1: word,
        **{register: (register - 3) * word for register in range(5, 32)},
    }


def _slots_include(slots: dict[int, set[int]], expected: dict[int, int]) -> bool:
    return all(offset in slots.get(reg, set()) for reg, offset in expected.items())


def _symbol_span(elf: ElfImage, name: str) -> tuple[int, int] | str:
    candidates = {
        (sym.start, sym.end) for sym in elf.symbols if sym.name == name and sym.size > 0
    }
    if not candidates:
        return f"scheduler-profile-symbol-missing:{name}"
    if len(candidates) != 1:
        return f"scheduler-profile-symbol-ambiguous:{name}"
    return next(iter(candidates))


def _route_epc_store(
    code: dict[int, tuple], *, mepc_reg: int, addend: int, store_op: str
) -> int | None:
    """Return the PC of ``store_op r, 0(sp)`` storing ``mepc + addend``, or ``None``."""
    values: dict[int, tuple[str, int]] = {mepc_reg: ("mepc", 0)}
    for pc in sorted(code):
        op = code[pc]
        if op[0] == "addi":
            source = values.get(op[2])
            values[op[1]] = (
                (source[0], source[1] + op[3]) if source is not None else None
            )  # type: ignore[assignment]
            if values[op[1]] is None:
                values.pop(op[1], None)
        elif op[0] == store_op and op[2] == 2 and op[3] == 0:
            if values.get(op[1]) == ("mepc", addend):
                return pc
        else:
            written = riscv_written_register(op)
            if written is not None:
                values.pop(written, None)
    return None


def _verify_start_first(
    elf: ElfImage,
    word: int,
    tcb_ptr: int,
    crit_ptr: int,
) -> tuple[dict[int, tuple], int, int, int] | str:
    try:
        insns = RiscvCodeParser(elf, "xPortStartFirstTask").instructions(
            terminal=("ret",)
        )
    except EvidenceError:
        return "scheduler-profile-mismatch:xPortStartFirstTask:decode"
    pcs = sorted(insns)
    if len(pcs) < 6 or insns[pcs[-1]] != ("ret",):
        return "scheduler-profile-mismatch:xPortStartFirstTask:terminal"
    pop_pc = pcs[-2]
    if insns[pop_pc] != ("addi", 2, 2, _FRAME_WORDS * word):
        return "scheduler-profile-mismatch:xPortStartFirstTask:sp-pop"
    load_op = "ld" if word == 8 else "lw"
    store_op = "sd" if word == 8 else "sw"
    loads = riscv_frame_accesses(insns, "load", 2)
    slot_map = riscv_slot_map(loads)
    expected_rest = {
        reg: offset for reg, offset in _expected_gpr_slots(word).items() if reg != 1
    }
    if not _slots_include(slot_map, expected_rest) or 0 not in slot_map.get(1, set()):
        return "scheduler-profile-mismatch:xPortStartFirstTask:slots"
    if not {_CRITICAL_NESTING_WORD * word, _MSTATUS_WORD * word}.issubset(loads):
        return "scheduler-profile-mismatch:xPortStartFirstTask:csr-slots"

    mask = (1 << (word * 8)) - 1
    const_addrs: dict[int, int] = {}
    tcb_regs: set[int] = set()
    crit_regs: set[int] = set()
    sp_load_pc: int | None = None
    wrote_crit = False
    wrote_mstatus = False
    for pc in pcs:
        op = insns[pc]
        if op[0] == load_op:
            rd, rn, imm = op[1], op[2], op[3]
            if (
                rn == 2
                and rd == 2
                and imm == 0
                and 2 in tcb_regs
                and sp_load_pc is None
            ):
                sp_load_pc = pc
            addr = (const_addrs[rn] + imm) & mask if rn in const_addrs else None
            riscv_update_const_addr(op, pc, mask, const_addrs)
            if addr == tcb_ptr:
                tcb_regs.add(rd)
            else:
                tcb_regs.discard(rd)
            if addr == crit_ptr:
                crit_regs.add(rd)
            else:
                crit_regs.discard(rd)
            continue
        if op[0] == store_op and op[3] == 0 and op[2] in crit_regs:
            wrote_crit = True
        elif op[0] == "csrw" and op[1] == _CSR_MSTATUS:
            wrote_mstatus = True
        written = riscv_written_register(op)
        if written is not None:
            tcb_regs.discard(written)
            crit_regs.discard(written)
        riscv_update_const_addr(op, pc, mask, const_addrs)

    if sp_load_pc is None or not wrote_crit or not wrote_mstatus:
        return "scheduler-profile-mismatch:xPortStartFirstTask:sequence"
    return insns, sp_load_pc, pop_pc, pcs[-1]


def _verify_vtask_switch(
    elf: ElfImage, start: int, end: int, word: int, tcb_ptr: int
) -> tuple[dict[int, tuple], int, set[int], set[int], str] | str:
    """Prove one unique ``pxCurrentTCB`` store and a disjoint CFG partition."""
    try:
        insns = RiscvCodeParser(elf, "vTaskSwitchContext").instructions()
    except EvidenceError:
        return "scheduler-profile-mismatch:vTaskSwitchContext:decode"
    pcs = sorted(insns)
    if (
        not pcs
        or pcs[0] != start
        or len(pcs) > _MAX_SWITCH_INSTRUCTIONS
        or insns[start][0] == "nop"
        or insns[start] == ("addi", 0, 0, 0)
    ):
        return "scheduler-profile-mismatch:vTaskSwitchContext:entry"
    last_pc = pcs[-1]
    last_size = (
        4
        if (int.from_bytes(elf.read_at(last_pc, 2) or b"\x00\x00", "little") & 3) == 3
        else 2
    )
    if last_pc + last_size != end:
        return "scheduler-profile-mismatch:vTaskSwitchContext:truncated"

    load_op = "ld" if word == 8 else "lw"
    store_op = "sd" if word == 8 else "sw"
    mask = (1 << (word * 8)) - 1

    branch_targets: set[int] = set()
    for op in insns.values():
        if op[0] in _BRANCH_MNEMONICS:
            branch_targets.add(op[3])
        elif op[0] == "jal" and (op[1] == 0 or start <= op[2] < end):
            branch_targets.add(op[2])

    const_addrs: dict[int, int] = {}
    tcb_addr_regs: set[int] = set()
    list_item_regs: set[int] = set()
    owner_regs: set[int] = set()

    def _clear(reg: int) -> None:
        const_addrs.pop(reg, None)
        tcb_addr_regs.discard(reg)
        list_item_regs.discard(reg)
        owner_regs.discard(reg)

    def _clear_all() -> None:
        const_addrs.clear()
        tcb_addr_regs.clear()
        list_item_regs.clear()
        owner_regs.clear()

    candidates: list[tuple[int, int, bool]] = []
    for pc in pcs:
        if pc in branch_targets:
            _clear_all()
        op = insns[pc]
        mnemonic = op[0]
        if mnemonic == store_op:
            rt, rn, imm = op[1], op[2], op[3]
            if rn in const_addrs and ((const_addrs[rn] + imm) & mask) == tcb_ptr:
                candidates.append((pc, rt, rt in owner_regs and rt != 0))
            continue
        if mnemonic in ("sw", "sd"):
            continue
        if mnemonic == "jal":
            _clear(op[1])
            if op[1] == 1:
                for reg in _RISCV_CALLER_SAVED:
                    _clear(reg)
            continue
        if mnemonic == "jalr":
            _clear_all()
            continue
        if mnemonic == load_op:
            rd, rn, imm = op[1], op[2], op[3]
            base_is_list = rn in list_item_regs
            base_is_tcb = rn in tcb_addr_regs or (
                rn in const_addrs and ((const_addrs[rn] + imm) & mask) == tcb_ptr
            )
            _clear(rd)
            if rd > 0 and imm > 0 and rn != 2 and not base_is_tcb:
                if base_is_list:
                    owner_regs.add(rd)
                else:
                    list_item_regs.add(rd)
            continue
        if mnemonic == "add" and op[1] > 0 and (op[2] == 0 or op[3] == 0):
            src = op[3] if op[2] == 0 else op[2]
            dst = op[1]
            src_addr = const_addrs.get(src)
            src_is_tcb = src in tcb_addr_regs
            src_is_item = src in list_item_regs
            src_is_owner = src in owner_regs
            _clear(dst)
            if src_addr is not None:
                const_addrs[dst] = src_addr
            if src_is_tcb:
                tcb_addr_regs.add(dst)
            if src_is_item:
                list_item_regs.add(dst)
            if src_is_owner:
                owner_regs.add(dst)
            continue
        if mnemonic == "addi" and op[1] > 0:
            dst, src, imm = op[1], op[2], op[3]
            if imm == 0 and src > 0:
                src_addr = const_addrs.get(src)
                src_is_tcb = src in tcb_addr_regs
                src_is_item = src in list_item_regs
                src_is_owner = src in owner_regs
                _clear(dst)
                if src_addr is not None:
                    const_addrs[dst] = src_addr
                if src_is_tcb:
                    tcb_addr_regs.add(dst)
                if src_is_item:
                    list_item_regs.add(dst)
                if src_is_owner:
                    owner_regs.add(dst)
                continue
            riscv_update_const_addr(op, pc, mask, const_addrs)
            tcb_addr_regs.discard(dst)
            list_item_regs.discard(dst)
            owner_regs.discard(dst)
            if const_addrs.get(dst) == tcb_ptr:
                tcb_addr_regs.add(dst)
            continue
        written = riscv_written_register(op)
        if written is not None:
            list_item_regs.discard(written)
            owner_regs.discard(written)
            tcb_addr_regs.discard(written)
        riscv_update_const_addr(op, pc, mask, const_addrs)

    if len(candidates) != 1:
        return "scheduler-profile-mismatch:vTaskSwitchContext:update-count"
    store_pc, _rt, owner_proven = candidates[0]
    if not owner_proven or store_pc <= start:
        return "scheduler-profile-mismatch:vTaskSwitchContext:update-source"
    after_pc = riscv_next_pc(insns, store_pc)
    if after_pc is None:
        return "scheduler-profile-mismatch:vTaskSwitchContext:update-boundary"

    # Partition instructions by CFG reachability around the unique TCB store so
    # cold branches placed after ``ret`` remain classified as pre-store.
    succs: dict[int, tuple[int, ...]] = {}
    for pc in pcs:
        op = insns[pc]
        nxt = riscv_next_pc(insns, pc)
        if op[0] == "ret":
            succs[pc] = ()
        elif op[0] == "jalr":
            return "scheduler-profile-mismatch:vTaskSwitchContext:indirect-jump"
        elif op[0] == "jal":
            if op[1] == 0:
                if op[2] not in insns:
                    return "scheduler-profile-mismatch:vTaskSwitchContext:external-tail"
                succs[pc] = (op[2],)
            else:
                succs[pc] = (nxt,) if nxt is not None else ()
        elif op[0] in _BRANCH_MNEMONICS:
            if op[3] not in insns:
                return "scheduler-profile-mismatch:vTaskSwitchContext:external-branch"
            succs[pc] = (nxt, op[3]) if nxt is not None else (op[3],)
        else:
            succs[pc] = (nxt,) if nxt is not None else ()

    def _reachable(entry: int, *, stop_at: int | None) -> set[int]:
        visited: set[int] = set()
        queue: deque[int] = deque([entry])
        while queue:
            cur = queue.popleft()
            if cur in visited:
                continue
            visited.add(cur)
            if cur == stop_at:
                continue
            for nxt in succs.get(cur, ()):
                if nxt not in visited:
                    queue.append(nxt)
        return visited

    pre_pcs = _reachable(start, stop_at=store_pc)
    post_pcs = _reachable(after_pc, stop_at=None)
    if (
        store_pc not in pre_pcs
        or not post_pcs
        or not pre_pcs.isdisjoint(post_pcs)
        or (pre_pcs | post_pcs) != set(pcs)
    ):
        return "scheduler-profile-mismatch:vTaskSwitchContext:cfg-partition"
    signature = (
        f"vTaskSwitchContext:count={len(pcs)}:store={insns[store_pc][0]}"
        f":pre={len(pre_pcs)}:post={len(post_pcs)}"
    )
    return insns, store_pc, pre_pcs, post_pcs, signature


def _verify_trap_handler(
    elf: ElfImage,
    word: int,
    tcb_ptr: int,
    switch_start: int,
) -> tuple[dict[str, object], tuple[int, ...]] | str:
    trap_start = elf.symbol_address("freertos_risc_v_trap_handler")
    async_pc = elf.symbol_address("asynchronous_interrupt")
    sync_pc = elf.symbol_address("synchronous_exception")
    idle_pc = elf.symbol_address("handle_interrupt")
    exc_pc = elf.symbol_address("handle_exception")
    proc_pc = elf.symbol_address("processed_source")
    if None in (trap_start, async_pc, sync_pc, idle_pc, exc_pc, proc_pc):
        return "scheduler-profile-symbol-missing:freertos_risc_v_trap_handler"
    assert (
        trap_start is not None
        and async_pc is not None
        and sync_pc is not None
        and idle_pc is not None
        and exc_pc is not None
        and proc_pc is not None
    )
    if not (trap_start < async_pc < sync_pc < idle_pc < exc_pc < proc_pc):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:order"

    store_op = "sd" if word == 8 else "sw"
    load_op = "ld" if word == 8 else "lw"
    expected_gprs = _expected_gpr_slots(word)
    try:
        trap = RiscvCodeParser(
            elf, "freertos_risc_v_trap_handler", "asynchronous_interrupt"
        ).instructions()
        async_code = RiscvCodeParser(
            elf, "asynchronous_interrupt", "synchronous_exception"
        ).instructions()
        sync_code = RiscvCodeParser(
            elf, "synchronous_exception", "handle_interrupt"
        ).instructions()
        intr_code = RiscvCodeParser(
            elf, "handle_interrupt", "handle_exception"
        ).instructions()
        exc_code = RiscvCodeParser(
            elf, "handle_exception", "processed_source"
        ).instructions()
        restore = RiscvCodeParser(elf, "processed_source").instructions(
            terminal=("mret",)
        )
    except EvidenceError:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:decode"

    trap_pcs = sorted(trap)
    if not trap_pcs or riscv_stack_frame_size(trap) != _FRAME_WORDS * word:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:frame-size"
    if trap[trap_pcs[0]] != ("addi", 2, 2, -_FRAME_WORDS * word):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:entry"
    saved_slots = riscv_store_completion_slots(
        trap, store_op, base=2, expected_offsets=expected_gprs
    )
    if saved_slots is None:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:gpr-slots"
    saved_offsets = {op[3] for op in trap.values() if op[0] == store_op and op[2] == 2}
    if not {_CRITICAL_NESTING_WORD * word, _MSTATUS_WORD * word}.issubset(
        saved_offsets
    ):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:csr-slots"

    loads = riscv_target_loads(elf, trap)
    tcb_reg = next((reg for reg, name in loads.items() if name == "pxCurrentTCB"), None)
    tcb_store_pc = (
        next(
            (
                pc
                for pc, op in sorted(trap.items())
                if op[0] == store_op and op[1] == 2 and op[2] == tcb_reg and op[3] == 0
            ),
            None,
        )
        if tcb_reg is not None
        else None
    )
    mcause_reg = next(
        (op[1] for op in trap.values() if op[0] == "csrr" and op[2] == _CSR_MCAUSE),
        None,
    )
    mepc_reg = next(
        (op[1] for op in trap.values() if op[0] == "csrr" and op[2] == _CSR_MEPC),
        None,
    )
    branch_pc = next(
        (
            pc
            for pc, op in trap.items()
            if op[0] in ("bge", "blt") and op[1] == mcause_reg and op[3] == sync_pc
        ),
        None,
    )
    if (
        tcb_store_pc is None
        or mcause_reg is None
        or mepc_reg is None
        or branch_pc is None
        or branch_pc <= tcb_store_pc
        or branch_pc + 4 != async_pc
    ):
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:prologue"

    async_epc_store_pc = _route_epc_store(
        async_code, mepc_reg=mepc_reg, addend=0, store_op=store_op
    )
    sync_epc_store_pc = _route_epc_store(
        sync_code, mepc_reg=mepc_reg, addend=4, store_op=store_op
    )
    if async_epc_store_pc is None or sync_epc_store_pc is None:
        return "scheduler-profile-mismatch:freertos_risc_v_trap_handler:epc-store"

    # handle_interrupt: locate optional post-vTaskSwitchContext return PCs.
    intr_pcs = sorted(intr_code)
    after_switch_async_pcs: set[int] = set()
    for idx, pc in enumerate(intr_pcs):
        op = intr_code[pc]
        if op[0] == "jal" and op[1] == 1 and op[2] == switch_start:
            if idx + 1 >= len(intr_pcs):
                return "scheduler-profile-mismatch:handle_interrupt:switch-ret"
            after_switch_async_pcs.add(intr_pcs[idx + 1])

    # handle_exception: locate ecall check, switch call, post-switch ret, and app handler.
    exc_pcs = sorted(exc_code)
    app_exc_sym = elf.symbol_address("freertos_risc_v_application_exception_handler")
    switch_call_sync_pc: int | None = None
    after_switch_sync_pc: int | None = None
    app_exc_call_pc: int | None = None
    has_ecall_branch = any(
        op[0] in ("bne", "beq") and op[1] == mcause_reg for op in exc_code.values()
    ) or any(
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
        not has_ecall_branch
        or switch_call_sync_pc is None
        or after_switch_sync_pc is None
    ):
        return "scheduler-profile-mismatch:handle_exception:structure"
    if app_exc_call_pc is None or app_exc_call_pc != riscv_next_pc(
        exc_code, after_switch_sync_pc
    ):
        return "scheduler-profile-mismatch:handle_exception:app-handler"

    # processed_source .. mret
    restore_pcs = sorted(restore)
    if len(restore_pcs) < 6 or restore[restore_pcs[-1]] != ("mret",):
        return "scheduler-profile-mismatch:processed_source:terminal"
    restore_pop_pc = restore_pcs[-2]
    if restore[restore_pop_pc] != ("addi", 2, 2, _FRAME_WORDS * word):
        return "scheduler-profile-mismatch:processed_source:sp-pop"
    restore_loads = riscv_frame_accesses(restore, "load", 2)
    restore_slot_map = riscv_slot_map(restore_loads)
    if not _slots_include(restore_slot_map, expected_gprs):
        return "scheduler-profile-mismatch:processed_source:slots"
    if not {_CRITICAL_NESTING_WORD * word, _MSTATUS_WORD * word}.issubset(
        restore_loads
    ):
        return "scheduler-profile-mismatch:processed_source:csr-slots"

    mask = (1 << (word * 8)) - 1
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
            if addr == tcb_ptr:
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

    info: dict[str, object] = {
        "trap_start": trap_start,
        "trap_end": restore_pcs[-1] + 4,
        "trap_tcb_store_pc": tcb_store_pc,
        "async_pc": async_pc,
        "async_epc_store_pc": async_epc_store_pc,
        "sync_pc": sync_pc,
        "sync_epc_store_pc": sync_epc_store_pc,
        "idle_pc": idle_pc,
        "exc_pc": exc_pc,
        "proc_pc": proc_pc,
        "restore_load_sp_pc": restore_load_sp_pc,
        "restore_pop_pc": restore_pop_pc,
        "restore_mret_pc": restore_pcs[-1],
        "trap_pcs": trap_pcs,
        "async_pcs": sorted(async_code),
        "sync_pcs": sorted(sync_code),
        "intr_pcs": intr_pcs,
        "exc_pcs": exc_pcs,
        "restore_pcs": restore_pcs,
        "after_switch_async_pcs": after_switch_async_pcs,
        "switch_call_sync_pc": switch_call_sync_pc,
        "after_switch_sync_pc": after_switch_sync_pc,
        "app_exc_call_pc": app_exc_call_pc,
        "saved_slots": saved_slots,
    }
    all_pcs = tuple(
        trap_pcs
        + sorted(async_code)
        + sorted(sync_code)
        + intr_pcs
        + exc_pcs
        + restore_pcs
    )
    return info, all_pcs


def _build_stage_map(
    *,
    start_insns: dict[int, tuple],
    sp_load_pc: int,
    start_pop_pc: int,
    start_ret_pc: int,
    trap_info: dict[str, object],
    switch_pre_pcs: set[int],
    switch_post_pcs: set[int],
) -> dict[int, RiscvSchedulerStage]:
    stages: dict[int, RiscvSchedulerStage] = {}

    for pc in sorted(start_insns):
        if pc <= sp_load_pc:
            stages[pc] = RiscvSchedulerStage(
                name="start-first-entry",
                owner="pending",
                seed="unstarted",
                pc=pc,
                function="xPortStartFirstTask",
                detail="xPortStartFirstTask before loading first task sp",
            )
        elif pc <= start_pop_pc:
            stages[pc] = RiscvSchedulerStage(
                name="start-first-sp-loaded",
                owner="new",
                seed="unstarted",
                pc=pc,
                function="xPortStartFirstTask",
                detail="xPortStartFirstTask with sp pointing to initial saved frame",
            )
        elif pc == start_ret_pc:
            stages[pc] = RiscvSchedulerStage(
                name="start-first-sp-popped",
                owner="new",
                seed="unstarted",
                pc=pc,
                function="xPortStartFirstTask",
                detail="xPortStartFirstTask at ret with first task registers restored",
            )

    trap_pcs: list[int] = trap_info["trap_pcs"]  # type: ignore[assignment]
    tcb_store_pc: int = trap_info["trap_tcb_store_pc"]  # type: ignore[assignment]
    for pc in trap_pcs:
        if pc == trap_pcs[0]:
            stages[pc] = RiscvSchedulerStage(
                name="trap-entry",
                owner="old",
                seed="trap-entry-live",
                pc=pc,
                function="freertos_risc_v_trap_handler",
                detail="trap entry before saving context or decrementing sp",
            )
        elif pc <= tcb_store_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-prologue-saving",
                owner="old",
                seed="live-prologue",
                pc=pc,
                function="freertos_risc_v_trap_handler",
                detail="trap prologue saving interrupted task GPRs prior to TCB writeback",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="trap-prologue-tcb-written",
                owner="old",
                seed="tcb-live-epc",
                pc=pc,
                function="freertos_risc_v_trap_handler",
                detail="trap prologue with GPR frame on task stack and epc still in mepc CSR",
            )

    async_epc_store_pc: int = trap_info["async_epc_store_pc"]  # type: ignore[assignment]
    for pc in trap_info["async_pcs"]:  # type: ignore[attr-defined]
        if pc <= async_epc_store_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-prologue-tcb-written",
                owner="old",
                seed="tcb-live-epc",
                pc=pc,
                function="asynchronous_interrupt",
                detail="asynchronous trap prologue prior to storing mepc into 0(sp)",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="trap-prologue-epc-saved",
                owner="old",
                seed="saved-tcb",
                pc=pc,
                function="asynchronous_interrupt",
                detail="asynchronous trap frame complete on task stack before ISR dispatch",
            )

    sync_epc_store_pc: int = trap_info["sync_epc_store_pc"]  # type: ignore[assignment]
    for pc in trap_info["sync_pcs"]:  # type: ignore[attr-defined]
        if pc <= sync_epc_store_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-prologue-tcb-written",
                owner="old",
                seed="tcb-live-epc",
                pc=pc,
                function="synchronous_exception",
                detail="synchronous trap prologue prior to storing epc into 0(sp)",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="trap-prologue-epc-saved",
                owner="old",
                seed="saved-tcb",
                pc=pc,
                function="synchronous_exception",
                detail="synchronous trap frame complete on task stack before exception dispatch",
            )

    after_switch_async_pcs: set[int] = trap_info["after_switch_async_pcs"]  # type: ignore[assignment]
    for pc in trap_info["intr_pcs"]:  # type: ignore[attr-defined]
        if pc in after_switch_async_pcs:
            stages[pc] = RiscvSchedulerStage(
                name="switch-after-update",
                owner="new",
                seed="saved-tcb",
                pc=pc,
                function="handle_interrupt",
                detail="returned from vTaskSwitchContext in interrupt dispatch",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="trap-dispatch",
                owner="old",
                seed="exception-isr",
                pc=pc,
                function="handle_interrupt",
                detail="interrupt handler dispatch on ISR stack before context switch",
            )

    after_switch_sync_pc: int = trap_info["after_switch_sync_pc"]  # type: ignore[assignment]
    app_exc_call_pc: int = trap_info["app_exc_call_pc"]  # type: ignore[assignment]
    for pc in trap_info["exc_pcs"]:  # type: ignore[attr-defined]
        if pc < after_switch_sync_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-sync-dispatch",
                owner="old",
                seed="saved-tcb-if-ecall",
                pc=pc,
                function="handle_exception",
                detail="synchronous exception dispatch prior to vTaskSwitchContext return",
            )
        elif pc == after_switch_sync_pc:
            stages[pc] = RiscvSchedulerStage(
                name="switch-after-update",
                owner="new",
                seed="saved-tcb",
                pc=pc,
                function="handle_exception",
                detail="returned from vTaskSwitchContext in ecall yield dispatch",
            )
        elif pc >= app_exc_call_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-sync-unhandled",
                owner="old",
                seed="refused",
                pc=pc,
                function="application_exception_handler",
                detail="non-ecall synchronous exception handler path",
            )

    restore_load_sp_pc: int = trap_info["restore_load_sp_pc"]  # type: ignore[assignment]
    restore_pop_pc: int = trap_info["restore_pop_pc"]  # type: ignore[assignment]
    restore_mret_pc: int = trap_info["restore_mret_pc"]  # type: ignore[assignment]
    for pc in trap_info["restore_pcs"]:  # type: ignore[attr-defined]
        if pc <= restore_load_sp_pc:
            stages[pc] = RiscvSchedulerStage(
                name="switch-after-update",
                owner="new",
                seed="saved-tcb",
                pc=pc,
                function="processed_source",
                detail="processed_source prior to loading incoming task sp",
            )
        elif pc <= restore_pop_pc:
            stages[pc] = RiscvSchedulerStage(
                name="restore-sp-loaded",
                owner="new",
                seed="restore-frame",
                pc=pc,
                function="processed_source",
                detail="processed_source restoring incoming task context from intact stack frame",
            )
        elif pc == restore_mret_pc:
            stages[pc] = RiscvSchedulerStage(
                name="restore-sp-popped",
                owner="new",
                seed="live-restore",
                pc=pc,
                function="processed_source",
                detail="processed_source at mret with incoming GPRs and mepc restored",
            )

    for pc in switch_pre_pcs:
        stages[pc] = RiscvSchedulerStage(
            name="switch-before-update",
            owner="old",
            seed="saved-tcb",
            pc=pc,
            function="vTaskSwitchContext",
            detail="vTaskSwitchContext before storing new pxCurrentTCB",
        )
    for pc in switch_post_pcs:
        stages[pc] = RiscvSchedulerStage(
            name="switch-after-update",
            owner="new",
            seed="saved-tcb",
            pc=pc,
            function="vTaskSwitchContext",
            detail="vTaskSwitchContext after storing new pxCurrentTCB",
        )
    return stages


def build_riscv_profile(
    elf: ElfImage | None,
) -> tuple[RiscvSchedulerProfile | None, str]:
    """Verify FreeRTOS RISC-V scheduler/trap code in *elf* and build its stage map."""
    if elf is None:
        return None, "scheduler-profile-unavailable"
    if elf.machine != EM_RISCV or elf.pointer_size not in (4, 8):
        return None, "scheduler-profile-unsupported-image"
    if elf.endian != "little":
        return None, "scheduler-profile-unsupported-endian"
    cpu = cpu_of(elf)
    if cpu.reason != "ok" or cpu.isa not in ("riscv32", "riscv64"):
        return None, "scheduler-profile-unsupported-image"
    word = elf.pointer_size
    xlen = word * 8
    tcb_ptr = elf.symbol_address("pxCurrentTCB")
    crit_ptr = elf.symbol_address("pxCriticalNesting")
    isr_top_ptr = elf.symbol_address("xISRStackTop")
    if tcb_ptr is None or crit_ptr is None or isr_top_ptr is None:
        return None, "scheduler-profile-symbol-missing:kernel-globals"
    switch_span = _symbol_span(elf, "vTaskSwitchContext")
    if isinstance(switch_span, str):
        return None, switch_span
    switch_start, switch_end = switch_span

    start_res = _verify_start_first(elf, word, tcb_ptr, crit_ptr)
    if isinstance(start_res, str):
        return None, start_res
    start_insns, sp_load_pc, start_pop_pc, start_ret_pc = start_res

    trap_res = _verify_trap_handler(elf, word, tcb_ptr, switch_start)
    if isinstance(trap_res, str):
        return None, trap_res
    trap_info, trap_all_pcs = trap_res

    switch_res = _verify_vtask_switch(elf, switch_start, switch_end, word, tcb_ptr)
    if isinstance(switch_res, str):
        return None, switch_res
    switch_insns, switch_update_pc, switch_pre_pcs, switch_post_pcs, switch_sig = (
        switch_res
    )
    switch_after_pc = riscv_next_pc(switch_insns, switch_update_pc)
    assert switch_after_pc is not None

    start_first_start = min(start_insns)
    start_first_end = start_ret_pc + (
        4
        if (int.from_bytes(elf.read_at(start_ret_pc, 2) or b"\x00\x00", "little") & 3)
        == 3
        else 2
    )
    trap_start: int = trap_info["trap_start"]  # type: ignore[assignment]
    trap_end: int = trap_info["trap_end"]  # type: ignore[assignment]

    ordered = sorted(
        (
            ("xPortStartFirstTask", start_first_start, start_first_end),
            ("freertos_risc_v_trap_handler", trap_start, trap_end),
            ("vTaskSwitchContext", switch_start, switch_end),
        ),
        key=lambda item: item[1],
    )
    for (prev_name, _ps, prev_end), (cur_name, cur_start, _ce) in zip(
        ordered, ordered[1:], strict=False
    ):
        if cur_start < prev_end:
            return None, f"scheduler-profile-symbol-overlap:{prev_name}:{cur_name}"

    stages = _build_stage_map(
        start_insns=start_insns,
        sp_load_pc=sp_load_pc,
        start_pop_pc=start_pop_pc,
        start_ret_pc=start_ret_pc,
        trap_info=trap_info,
        switch_pre_pcs=switch_pre_pcs,
        switch_post_pcs=switch_post_pcs,
    )

    tokens = [
        f"xPortStartFirstTask:{len(start_insns)}",
        f"trap:{len(trap_all_pcs)}",
        switch_sig,
    ]
    fingerprint = hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()[:16]

    return (
        RiscvSchedulerProfile(
            profile_id=RV64_PROFILE_ID if xlen == 64 else RV32_PROFILE_ID,
            fingerprint=fingerprint,
            xlen=xlen,
            word_size=word,
            frame_bytes=_FRAME_WORDS * word,
            px_current_tcb=tcb_ptr,
            start_first_start=start_first_start,
            start_first_end=start_first_end,
            start_first_sp_load_pc=sp_load_pc,
            start_first_sp_pop_pc=start_pop_pc,
            start_first_ret_pc=start_ret_pc,
            trap_start=trap_start,
            trap_end=trap_end,
            trap_tcb_store_pc=trap_info["trap_tcb_store_pc"],  # type: ignore[arg-type]
            async_epc_store_pc=trap_info["async_epc_store_pc"],  # type: ignore[arg-type]
            sync_entry=trap_info["sync_pc"],  # type: ignore[arg-type]
            sync_epc_store_pc=trap_info["sync_epc_store_pc"],  # type: ignore[arg-type]
            handle_interrupt_pc=trap_info["idle_pc"],  # type: ignore[arg-type]
            handle_exception_pc=trap_info["exc_pc"],  # type: ignore[arg-type]
            processed_source_pc=trap_info["proc_pc"],  # type: ignore[arg-type]
            restore_load_sp_pc=trap_info["restore_load_sp_pc"],  # type: ignore[arg-type]
            restore_pop_sp_pc=trap_info["restore_pop_pc"],  # type: ignore[arg-type]
            restore_mret_pc=trap_info["restore_mret_pc"],  # type: ignore[arg-type]
            switch_start=switch_start,
            switch_end=switch_end,
            switch_update_pc=switch_update_pc,
            switch_after_update_pc=switch_after_pc,
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
        (profile.start_first_start, profile.start_first_end, "xPortStartFirstTask"),
        (profile.trap_start, profile.trap_end, "freertos_risc_v_trap_handler"),
        (profile.switch_start, profile.switch_end, "vTaskSwitchContext"),
    ):
        if start <= pc < end:
            return f"scheduler-stage-ambiguous:{name}"
    return None
