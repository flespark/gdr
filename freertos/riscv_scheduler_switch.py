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
    riscv_update_const_addr,
    riscv_written_register,
)
from core.unwind_meta import EM_RISCV, ElfImage

from .stack_frame_profile import (
    RiscvTrapAnalysis,
    _riscv_slots_include,
    analyze_riscv_trap_blocks,
)

RV32_PROFILE_ID = "freertos/rv32/riscv-mtvec-v1"
RV64_PROFILE_ID = "freertos/rv64/riscv-mtvec-v1"

_CSR_MSTATUS = 0x300
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
    mstatus_offset: int
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


def _symbol_span(elf: ElfImage, name: str) -> tuple[int, int] | str:
    candidates = {
        (sym.start, sym.end) for sym in elf.symbols if sym.name == name and sym.size > 0
    }
    if not candidates:
        return f"scheduler-profile-symbol-missing:{name}"
    if len(candidates) != 1:
        return f"scheduler-profile-symbol-ambiguous:{name}"
    return next(iter(candidates))


def _verify_start_first(
    elf: ElfImage,
    word: int,
    tcb_ptr: int,
    crit_ptr: int | None,
    analysis: RiscvTrapAnalysis,
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
    prologue = analysis.prologue
    if insns[pop_pc] != ("addi", 2, 2, prologue.frame_bytes):
        return "scheduler-profile-mismatch:xPortStartFirstTask:sp-pop"
    load_op = "ld" if word == 8 else "lw"
    store_op = "sd" if word == 8 else "sw"
    loads = riscv_frame_accesses(insns, "load", 2)
    slot_map = riscv_slot_map(loads)
    expected_rest = {
        reg: offset for reg, offset in prologue.gpr_offsets.items() if reg != 1
    }
    if not _riscv_slots_include(slot_map, expected_rest) or 0 not in slot_map.get(
        1, set()
    ):
        return "scheduler-profile-mismatch:xPortStartFirstTask:slots"
    if prologue.mstatus_offset not in loads or (
        prologue.critical_nesting_offset is not None
        and prologue.critical_nesting_offset not in loads
    ):
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
            if crit_ptr is not None and addr == crit_ptr:
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

    need_crit = prologue.critical_nesting_offset is not None
    if sp_load_pc is None or not wrote_mstatus or (need_crit and not wrote_crit):
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


def _build_stage_map(
    *,
    start_insns: dict[int, tuple],
    sp_load_pc: int,
    start_pop_pc: int,
    start_ret_pc: int,
    analysis: RiscvTrapAnalysis,
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

    prologue = analysis.prologue
    dispatch = analysis.dispatch
    restore = analysis.restore

    for pc in prologue.pcs:
        if pc == prologue.pcs[0]:
            stages[pc] = RiscvSchedulerStage(
                name="trap-entry",
                owner="old",
                seed="trap-entry-live",
                pc=pc,
                function="freertos_risc_v_trap_handler",
                detail="trap entry before saving context or decrementing sp",
            )
        elif pc <= prologue.tcb_store_pc:
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

    for pc in dispatch.async_pre_sp_pcs:
        if pc <= dispatch.async_epc_store_pc:
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

    for pc in dispatch.sync_pre_sp_pcs:
        if pc <= dispatch.sync_epc_store_pc:
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

    for pc in dispatch.async_dispatch_pcs:
        if pc in dispatch.after_switch_async_pcs:
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

    for pc in dispatch.sync_dispatch_pcs:
        if pc < dispatch.after_switch_sync_pc:
            stages[pc] = RiscvSchedulerStage(
                name="trap-sync-dispatch",
                owner="old",
                seed="saved-tcb-if-ecall",
                pc=pc,
                function="handle_exception",
                detail="synchronous exception dispatch prior to vTaskSwitchContext return",
            )
        else:
            stages[pc] = RiscvSchedulerStage(
                name="switch-after-update",
                owner="new",
                seed="saved-tcb",
                pc=pc,
                function="handle_exception",
                detail="returned from vTaskSwitchContext in ecall yield dispatch",
            )

    unhandled_seed = "refused" if dispatch.unhandled_sync_on_isr_stack else "saved-tcb"
    unhandled_fn = (
        "application_exception_handler"
        if dispatch.app_exc_call_pc is not None
        else "is_exception"
    )
    for pc in dispatch.unhandled_sync_pcs:
        stages[pc] = RiscvSchedulerStage(
            name="trap-sync-unhandled",
            owner="old",
            seed=unhandled_seed,
            pc=pc,
            function=unhandled_fn,
            detail="non-ecall synchronous exception handler path",
        )

    for pc in restore.pcs:
        if pc <= restore.load_sp_pc:
            stages[pc] = RiscvSchedulerStage(
                name="switch-after-update",
                owner="new",
                seed="saved-tcb",
                pc=pc,
                function="processed_source",
                detail="processed_source prior to loading incoming task sp",
            )
        elif pc <= restore.pop_pc:
            stages[pc] = RiscvSchedulerStage(
                name="restore-sp-loaded",
                owner="new",
                seed="restore-frame",
                pc=pc,
                function="processed_source",
                detail="processed_source restoring incoming task context from intact stack frame",
            )
        elif pc == restore.mret_pc:
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
    if tcb_ptr is None or isr_top_ptr is None:
        return None, "scheduler-profile-symbol-missing:kernel-globals"
    switch_span = _symbol_span(elf, "vTaskSwitchContext")
    if isinstance(switch_span, str):
        return None, switch_span
    switch_start, switch_end = switch_span

    analysis = analyze_riscv_trap_blocks(
        elf, switch_start=switch_start, crit_ptr=crit_ptr
    )
    if isinstance(analysis, str):
        return None, analysis
    prologue = analysis.prologue
    dispatch = analysis.dispatch
    restore = analysis.restore
    if prologue.critical_nesting_offset is not None and crit_ptr is None:
        return None, "scheduler-profile-symbol-missing:kernel-globals"

    start_res = _verify_start_first(elf, word, tcb_ptr, crit_ptr, analysis)
    if isinstance(start_res, str):
        return None, start_res
    start_insns, sp_load_pc, start_pop_pc, start_ret_pc = start_res

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
    trap_start = analysis.trap_start
    trap_end = analysis.trap_end

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
        analysis=analysis,
        switch_pre_pcs=switch_pre_pcs,
        switch_post_pcs=switch_post_pcs,
    )

    trap_count = (
        len(prologue.pcs)
        + len(dispatch.async_pre_sp_pcs)
        + len(dispatch.async_dispatch_pcs)
        + len(dispatch.sync_pre_sp_pcs)
        + len(dispatch.sync_dispatch_pcs)
        + len(dispatch.unhandled_sync_pcs)
        + len(restore.pcs)
    )
    tokens = [
        f"xPortStartFirstTask:{len(start_insns)}",
        f"trap:{trap_count}",
        switch_sig,
    ]
    fingerprint = hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()[:16]

    return (
        RiscvSchedulerProfile(
            profile_id=RV64_PROFILE_ID if xlen == 64 else RV32_PROFILE_ID,
            fingerprint=fingerprint,
            xlen=xlen,
            word_size=word,
            frame_bytes=prologue.frame_bytes,
            mstatus_offset=prologue.mstatus_offset,
            px_current_tcb=tcb_ptr,
            start_first_start=start_first_start,
            start_first_end=start_first_end,
            start_first_sp_load_pc=sp_load_pc,
            start_first_sp_pop_pc=start_pop_pc,
            start_first_ret_pc=start_ret_pc,
            trap_start=trap_start,
            trap_end=trap_end,
            trap_tcb_store_pc=prologue.tcb_store_pc,
            async_epc_store_pc=dispatch.async_epc_store_pc,
            sync_entry=dispatch.sync_pc,
            sync_epc_store_pc=dispatch.sync_epc_store_pc,
            handle_interrupt_pc=dispatch.intr_dispatch_start,
            handle_exception_pc=dispatch.sync_dispatch_start,
            processed_source_pc=restore.proc_pc,
            restore_load_sp_pc=restore.load_sp_pc,
            restore_pop_sp_pc=restore.pop_pc,
            restore_mret_pc=restore.mret_pc,
            switch_start=switch_start,
            switch_end=switch_end,
            switch_update_pc=switch_update_pc,
            switch_after_update_pc=switch_after_pc,
            trap_saved_slots=prologue.saved_slots,
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
