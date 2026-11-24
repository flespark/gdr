"""Compiled FreeRTOS Arm M-profile scheduler-transition evidence (pure).

The module proves the PendSV/SVC/start-first-task instruction sequences of the
supported FreeRTOS GCC ports (``ARM_CM0``, ``ARM_CM3``, ``ARM_CM4F``, and
single-core non-MPU ``ARM_CM33_NTZ``) from the loaded ELF and classifies a live
stop inside them.  A symbol-relative decoder keeps this independent of any one
fixture's link address.  Owner-consistent seeds for classified stages live in
:mod:`freertos.backtrace.scheduler_seed`.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from core.elf import EM_ARM, ElfImage
from core.insn_thumb import (
    ThumbInstruction,
    decode_expected_thumb,
    decode_thumb_stream,
)
from core.insn_thumb import (
    decode_thumb as _decode_thumb,
)
from core.insn_thumb import (
    expect_thumb as _expect,
)
from core.insn_thumb import (
    literal_pool_start as _literal_pool_start,
)
from core.insn_thumb import (
    resolved_literal as _resolved_literal,
)
from core.insn_thumb import (
    semantic_token as _semantic_token,
)

PROFILE_ID = "freertos/armv7-m/arm-cm3-pendsv-v1"
CM0_PROFILE_ID = "freertos/armv6-m/arm-cm0-pendsv-v1"
CM4F_PROFILE_ID = "freertos/armv7e-m/arm-cm4f-pendsv-v1"
CM33_PROFILE_ID = "freertos/armv8-m.mainline/arm-cm33-pendsv-v1"
CM33_FPU_PROFILE_ID = "freertos/armv8-m.mainline/arm-cm33-fpu-pendsv-v1"

_CORE_REGLIST = 0xFF0
_CORE_LR_REGLIST = 0x4FF0
_CM33_REGLIST = 0xFFC
_CM0_LOW_REGLIST = 0xF0
_CM0_R3_REGLIST = 0xF8
_CM0_SVC_REGLIST = 0x04
_CM33_SVC_REGLIST = 0x06
_R3_LR_PUSH = 0x108
_R3_LR_LDM = 0x4008
_R0_R3_REGLIST = 0x09
_CONTROL_SYSM = 0x14
_MSP_SYSM = 0x08
_PSP_SYSM = 0x09
_PSPLIM_SYSM = 0x0B
_BASEPRI_SYSM = 0x11
_VTOR_ADDRESS = 0xE000ED08
_MAX_PROFILE_INSTRUCTIONS = 96
_MAX_SWITCH_INSTRUCTIONS = 512
_AAPCS_CALLER_SAVED = frozenset({0, 1, 2, 3, 12, 14})

# Decoder mnemonics grouped by the register-write behaviour they imply.  The
# provenance proof trusts only deterministic, single-register dataflow inside
# one basic block; every other write drops the tracked state.
_LOAD_RT = frozenset({"ldr_imm", "ldr_literal", "ldr_reg"})
_MULTI_LOAD = frozenset({"pop", "ldm", "ldmia", "ldmdb"})
_MULTI_STORE = frozenset({"push", "stm", "stmia", "stmdb"})
_SINGLE_STORE = frozenset({"str_imm", "str_reg"})
_STORES = _SINGLE_STORE | _MULTI_STORE
_CALL_MNEMONICS = frozenset({"bl", "blx", "blx_reg", "bx"})
_BRANCH_MNEMONICS = frozenset({"b", "b_cond", "cbz", "cbnz"})
_MASK_PRIORITY_TOKEN = "mov_basepri_mask:r0"

ARM_SCHEDULER_SYMBOLS = frozenset(
    (
        "PendSV_Handler",
        "SVC_Handler",
        "prvPortStartFirstTask",
        "vStartFirstTask",
        "vRestoreContextOfFirstTask",
        "vTaskSwitchContext",
    )
)


@dataclass(frozen=True)
class SchedulerProfile:
    """Verified symbol-relative addresses of one Arm M-profile switch sequence.

    Stage addresses are instruction addresses: a breakpoint there stops
    *before* that instruction executes.  The profile is invalid unless every
    sequence and the ``pxCurrentTCB`` update inside ``vTaskSwitchContext``
    matched the compiled bytes.
    """

    profile_id: str
    fingerprint: str
    pendsv_start: int
    pendsv_end: int
    svc_start: int
    svc_end: int
    start_first_start: int
    start_first_end: int
    switch_start: int
    switch_end: int
    pendsv_entry: int
    pendsv_save_pending: int
    pendsv_save_written: int | None
    pendsv_writeback: int
    pendsv_switch_call: int
    pendsv_after_switch: int
    pendsv_select: int
    pendsv_saved_sp: int
    pendsv_restored_pending_psp: int
    pendsv_psp_written: int | None
    pendsv_exception_return: int
    svc_entry: int
    svc_saved_sp: int
    svc_restored_pending_psp: int
    svc_psp_written: int
    svc_exception_return: int
    start_first_svc: int
    switch_entry: int
    switch_update: int
    switch_after_update: int
    switch_return: int
    px_current_tcb: int
    port: str = "arm-cm3"
    svc_symbol: str = "SVC_Handler"
    start_first_symbol: str = "prvPortStartFirstTask"
    switch_pre_pcs: frozenset[int] = frozenset()
    switch_post_pcs: frozenset[int] = frozenset()


@dataclass(frozen=True)
class SchedulerStage:
    """One exact scheduler stop and the context that owns it."""

    name: str
    owner: str
    seed: str
    pc: int


def _decode_stream(
    elf: ElfImage, symbol: str, start: int, end: int, limit: int
) -> tuple[tuple[str, ...] | None, str]:
    """Decode every instruction in one pure-code span."""
    return decode_thumb_stream(
        elf, symbol, start, end, limit, prefix="scheduler-profile"
    )


def _basepri_mask_ok(instruction: ThumbInstruction) -> bool:
    """Accept any legal non-zero 8-bit BASEPRI mask loaded into r0."""
    if instruction.mnemonic not in {"mov_imm", "movw"}:
        return False
    if instruction.operands.get("rd") != 0:
        return False
    imm = instruction.operands.get("imm")
    return isinstance(imm, int) and 0 < imm <= 0xFF


def _decode_expected(
    elf: ElfImage,
    symbol: str,
    start: int,
    end: int,
    expected: tuple[tuple[str, Mapping[str, int | bool | str]], ...],
    *,
    mask_index: int | None = None,
) -> tuple[tuple[ThumbInstruction, ...] | None, str]:
    """Decode a fixed naked-assembly sequence; one slot uses a mask predicate."""
    if mask_index is None:
        return decode_expected_thumb(
            elf,
            symbol,
            start,
            end,
            expected,
            max_instructions=_MAX_PROFILE_INSTRUCTIONS,
            prefix="scheduler-profile",
        )
    instructions: list[ThumbInstruction] = []
    address = start
    for index, (mnemonic, operands) in enumerate(expected):
        if address < start or address >= end or index >= _MAX_PROFILE_INSTRUCTIONS:
            return None, f"scheduler-profile-mismatch:{symbol}:{index}:truncated"
        decoded = _decode_thumb(elf, address, end)
        if isinstance(decoded, str):
            return None, f"scheduler-profile-mismatch:{symbol}:{index}:{decoded}"
        if decoded.address + decoded.size > end:
            return None, f"scheduler-profile-mismatch:{symbol}:{index}:truncated"
        if index == mask_index:
            if not _basepri_mask_ok(decoded):
                return None, f"scheduler-profile-mismatch:{symbol}:{index}:basepri-mask"
        elif not _expect(decoded, mnemonic, operands):
            return None, f"scheduler-profile-mismatch:{symbol}:{index}"
        instructions.append(decoded)
        address += decoded.size
    return tuple(instructions), "ok"


def _symbol_span(elf: ElfImage, name: str) -> tuple[int, int] | str:
    """Return one sized, unambiguous code span or the exact refusal reason."""
    candidates = {
        (symbol.start, symbol.end)
        for symbol in elf.symbols
        if symbol.name == name and symbol.size > 0
    }
    if not candidates:
        return f"scheduler-profile-symbol-missing:{name}"
    if len(candidates) != 1:
        return f"scheduler-profile-symbol-ambiguous:{name}"
    return next(iter(candidates))


def _mapping_code_spans(
    elf: ElfImage, start: int, end: int
) -> tuple[tuple[int, int], ...]:
    """Split one symbol span into pure-code intervals at mapping anchors.

    ``$d`` starts a data region and ``$t``/``$a``/``$x`` resumes code, so a
    compiler-emitted inline literal pool does not get decoded as instructions.
    """
    anchors = sorted(
        (address, name)
        for name, address in elf.anchors
        if start < address < end and name.startswith(("$d", "$t", "$a", "$x"))
    )
    spans: list[tuple[int, int]] = []
    cursor = start
    in_code = True
    for address, name in anchors:
        if name.startswith("$d"):
            if in_code:
                if address > cursor:
                    spans.append((cursor, address))
                in_code = False
        elif not in_code:
            cursor = address
            in_code = True
    if in_code and end > cursor:
        spans.append((cursor, end))
    return tuple(spans)


def _code_stream_end(elf: ElfImage, start: int, end: int) -> int:
    """Return the exclusive end of a function's trailing pure-code region."""
    spans = _mapping_code_spans(elf, start, end)
    if not spans:
        return start
    last_start = spans[-1][0]
    code_end = spans[-1][1]
    pool = _literal_pool_start(elf, start, end)
    if last_start <= pool < code_end:
        code_end = pool
    # Only the trailing literal pool shrinks the code bound: an inline pool
    # targeted from an earlier code span belongs to that span's data gap.
    address = last_start
    while address < code_end:
        decoded = _decode_thumb(elf, address, code_end)
        if isinstance(decoded, str):
            break
        if decoded.mnemonic == "ldr_literal":
            target = decoded.operands.get("literal")
            if isinstance(target, int) and last_start < target < code_end:
                code_end = min(code_end, target)
        address += decoded.size
    return code_end


def _code_spans(elf: ElfImage, start: int, end: int) -> tuple[tuple[int, int], ...]:
    """Pure-code intervals with the trailing literal pool removed."""
    spans = list(_mapping_code_spans(elf, start, end))
    if not spans:
        return ()
    code_end = _code_stream_end(elf, start, end)
    last_start, last_end = spans[-1]
    if code_end <= last_start:
        spans.pop()
    elif code_end < last_end:
        spans[-1] = (last_start, code_end)
    return tuple((low, high) for low, high in spans if high > low)


def _decode_switch_instructions(
    elf: ElfImage, start: int, end: int
) -> tuple[tuple[ThumbInstruction, ...] | None, str]:
    """Decode every instruction in the switch function's pure-code spans."""
    spans = _code_spans(elf, start, end)
    if not spans:
        return None, "scheduler-profile-mismatch:vTaskSwitchContext:no-code"
    instructions: list[ThumbInstruction] = []
    for span_start, span_end in spans:
        address = span_start
        while address < span_end:
            if len(instructions) >= _MAX_SWITCH_INSTRUCTIONS:
                return None, "scheduler-profile-mismatch:vTaskSwitchContext:budget"
            decoded = _decode_thumb(elf, address, span_end)
            if isinstance(decoded, str):
                return None, "scheduler-profile-mismatch:vTaskSwitchContext:unreadable"
            if decoded.address + decoded.size > span_end:
                return None, "scheduler-profile-mismatch:vTaskSwitchContext:unreadable"
            instructions.append(decoded)
            address += decoded.size
        if address != span_end:
            return None, "scheduler-profile-mismatch:vTaskSwitchContext:unreadable"
    return tuple(instructions), "ok"


def _written_registers(instruction: ThumbInstruction) -> set[int] | None:
    """Registers an instruction certainly writes; ``None`` means unknown."""
    operands = instruction.operands
    mnemonic = instruction.mnemonic
    if mnemonic == "other":
        return None
    written: set[int] = set()
    if mnemonic in _MULTI_LOAD:
        reglist = operands.get("reglist")
        if isinstance(reglist, int):
            written.update(
                register for register in range(16) if reglist >> register & 1
            )
        written.add(13)
        if operands.get("wback") and isinstance(operands.get("rn"), int):
            written.add(int(operands["rn"]))
    elif mnemonic == "push":
        written.add(13)
    elif mnemonic in _MULTI_STORE:
        if operands.get("wback") and isinstance(operands.get("rn"), int):
            written.add(int(operands["rn"]))
    elif mnemonic in _LOAD_RT:
        rt = operands.get("rt")
        if isinstance(rt, int):
            written.add(int(rt))
    elif mnemonic not in _STORES:
        rd = operands.get("rd")
        if isinstance(rd, int):
            written.add(int(rd))
    return written


def _find_switch_update(
    elf: ElfImage, start: int, end: int, tcb_pointer: int
) -> tuple[ThumbInstruction, str] | str:
    """Prove one globally unique pxCurrentTCB store fed by a ready-list owner.

    Forward basic-block provenance tracks registers that certainly hold
    ``&pxCurrentTCB``, a ready-list item pointer or that item's owner.  A store
    through a proven ``&pxCurrentTCB`` base is accepted only when it is the
    single ``str rt, [rn, #0]`` whose source is a proven owner.  Control-flow
    joins, calls and opaque instructions invalidate the tracked state.
    """
    instructions, reason = _decode_switch_instructions(elf, start, end)
    if instructions is None:
        return reason
    first = instructions[0]
    if (
        first.address != start
        or first.mnemonic == "nop"
        or first.mnemonic in _SINGLE_STORE
    ):
        return "scheduler-profile-mismatch:vTaskSwitchContext:entry"
    branch_targets = {
        int(instruction.operands["target"])
        for instruction in instructions
        if instruction.mnemonic in _BRANCH_MNEMONICS
        and isinstance(instruction.operands.get("target"), int)
    }
    const_low16: dict[int, int] = {}
    tcb_addr_regs: set[int] = set()
    tcb_addr_origin: dict[int, str] = {}
    list_item_regs: set[int] = set()
    owner_regs: set[int] = set()

    def clear(register: int) -> None:
        const_low16.pop(register, None)
        tcb_addr_regs.discard(register)
        tcb_addr_origin.pop(register, None)
        list_item_regs.discard(register)
        owner_regs.discard(register)

    def clear_all() -> None:
        const_low16.clear()
        tcb_addr_regs.clear()
        tcb_addr_origin.clear()
        list_item_regs.clear()
        owner_regs.clear()

    candidates: list[tuple[ThumbInstruction, int, int, bool, str]] = []
    for instruction in instructions:
        if instruction.address in branch_targets:
            clear_all()
        operands = instruction.operands
        mnemonic = instruction.mnemonic
        rt = operands.get("rt") if isinstance(operands.get("rt"), int) else None
        rn = operands.get("rn") if isinstance(operands.get("rn"), int) else None
        imm = operands.get("imm") if isinstance(operands.get("imm"), int) else None
        base = 13 if mnemonic == "push" else rn
        base_is_list = rn is not None and rn in list_item_regs
        base_is_tcb = rn is not None and rn in tcb_addr_regs
        if mnemonic in _STORES and base in tcb_addr_regs:
            if mnemonic != "str_imm" or imm != 0 or rt is None:
                return "scheduler-profile-mismatch:vTaskSwitchContext:ambiguous-store"
            candidates.append(
                (
                    instruction,
                    int(rt),
                    int(base),
                    int(rt) in owner_regs,
                    tcb_addr_origin.get(int(base), "unknown"),
                )
            )
        written = _written_registers(instruction)
        if written is None:
            clear_all()
        else:
            for register in written:
                clear(register)
        if mnemonic in _CALL_MNEMONICS:
            if mnemonic == "bx" and operands.get("rm") == 14:
                clear_all()
            else:
                for register in _AAPCS_CALLER_SAVED:
                    clear(register)
        if mnemonic == "ldr_literal":
            if rt is not None and _resolved_literal(elf, instruction) == tcb_pointer:
                tcb_addr_regs.add(int(rt))
                tcb_addr_origin[int(rt)] = "literal"
        elif mnemonic in {"movw", "movt"}:
            rd = operands.get("rd")
            if isinstance(rd, int) and imm is not None:
                if mnemonic == "movw":
                    const_low16[int(rd)] = int(imm) & 0xFFFF
                elif (
                    int(rd) in const_low16
                    and ((int(imm) << 16) | const_low16[int(rd)]) == tcb_pointer
                ):
                    tcb_addr_regs.add(int(rd))
                    tcb_addr_origin[int(rd)] = "movw-movt"
        elif mnemonic == "mov" and isinstance(operands.get("rm"), int):
            source = int(operands["rm"])
            target = operands.get("rd")
            if isinstance(target, int):
                if source in tcb_addr_regs:
                    tcb_addr_regs.add(int(target))
                    tcb_addr_origin[int(target)] = tcb_addr_origin.get(
                        source, "unknown"
                    )
                if source in list_item_regs:
                    list_item_regs.add(int(target))
                if source in owner_regs:
                    owner_regs.add(int(target))
        elif (
            mnemonic == "ldr_imm"
            and rt is not None
            and imm is not None
            and int(imm) > 0
        ):
            if base_is_list:
                owner_regs.add(int(rt))
            elif not base_is_tcb and rn != 13:
                list_item_regs.add(int(rt))
    if len(candidates) != 1:
        return "scheduler-profile-mismatch:vTaskSwitchContext:update-count"
    store, source, base, owner_proven, origin = candidates[0]
    if not owner_proven or source == base:
        return "scheduler-profile-mismatch:vTaskSwitchContext:update-source"
    if store.address <= start:
        return "scheduler-profile-mismatch:vTaskSwitchContext:entry-store"
    following = store.address + store.size
    following_mnemonic = next(
        (
            instruction.mnemonic
            for instruction in instructions
            if instruction.address == following
        ),
        None,
    )
    if following_mnemonic is None:
        return "scheduler-profile-mismatch:vTaskSwitchContext:update-boundary"
    signature = (
        f"vTaskSwitchContext:count={len(instructions)}"
        f":store={_semantic_token(elf, store)}"
        f":base={origin}:source=list-item-owner"
        f":after={following_mnemonic}"
    )
    return store, signature


def _switch_cfg_partition(
    instructions: tuple[ThumbInstruction, ...],
    store_address: int,
) -> tuple[frozenset[int], frozenset[int]]:
    """Partition ``vTaskSwitchContext`` PCs relative to the unique TCB store.

    Follows intra-function Thumb control flow so cold blocks placed after the
    normal return (such as ready-list wrap-around or assert paths that branch
    back before ``store_address``) keep their true pre-update ownership, while
    excluding any instruction reachable on both sides of the store.
    """
    by_addr = {instruction.address for instruction in instructions}
    edges: dict[int, list[int]] = {
        instruction.address: [] for instruction in instructions
    }
    it_remaining = 0
    for instruction in instructions:
        in_it = it_remaining > 0
        if instruction.mnemonic == "it":
            it_remaining = int(instruction.operands.get("count", 1))
        elif in_it:
            it_remaining -= 1
        mnemonic = instruction.mnemonic
        next_addr = instruction.address + instruction.size
        fallthrough = next_addr if next_addr in by_addr else None
        reglist = instruction.operands.get("reglist")
        reglist_int = int(reglist) if isinstance(reglist, int) else 0
        pop_pc = (
            mnemonic == "pop" and instruction.size == 2 and bool(reglist_int & (1 << 8))
        ) or (mnemonic in {"pop", "ldmia", "ldmdb"} and bool(reglist_int & (1 << 15)))
        is_ret = (mnemonic == "bx" and instruction.operands.get("rm") == 14) or pop_pc
        target = instruction.operands.get("target")
        target_addr = target if isinstance(target, int) and target in by_addr else None
        if mnemonic == "b":
            if target_addr is not None:
                edges[instruction.address].append(target_addr)
            if in_it and fallthrough is not None:
                edges[instruction.address].append(fallthrough)
        elif mnemonic in {"b_cond", "cbz", "cbnz"}:
            if fallthrough is not None:
                edges[instruction.address].append(fallthrough)
            if target_addr is not None:
                edges[instruction.address].append(target_addr)
        elif is_ret or mnemonic == "bx":
            if in_it and fallthrough is not None:
                edges[instruction.address].append(fallthrough)
        elif fallthrough is not None:
            edges[instruction.address].append(fallthrough)

    pre: set[int] = set()
    queue = deque([instructions[0].address])
    while queue:
        addr = queue.popleft()
        if addr in pre:
            continue
        pre.add(addr)
        if addr == store_address:
            continue
        for nxt in edges[addr]:
            if nxt not in pre:
                queue.append(nxt)

    post: set[int] = set()
    queue = deque(edges.get(store_address, ()))
    while queue:
        addr = queue.popleft()
        if addr in post:
            continue
        post.add(addr)
        for nxt in edges[addr]:
            if nxt not in post:
                queue.append(nxt)

    return frozenset(pre - post), frozenset(post - pre)


@dataclass(frozen=True)
class _PortSpec:
    """Expected symbol sequences and exact stage indices for one port."""

    port: str
    profile_id: str
    pendsv_symbol: str
    svc_symbol: str
    start_first_symbol: str
    switch_symbol: str
    pendsv_expected: tuple[tuple[str, Mapping[str, int | bool | str]], ...]
    svc_expected: tuple[tuple[str, Mapping[str, int | bool | str]], ...]
    start_expected: tuple[tuple[str, Mapping[str, int | bool | str]], ...]
    pendsv_tcb_literals: tuple[int, ...]
    svc_tcb_literals: tuple[int, ...]
    start_vtor_literal: int | None
    basepri_mask_index: int | None
    pendsv_entry_idx: int
    pendsv_save_pending_idx: int
    pendsv_save_written_idx: int | None
    pendsv_writeback_idx: int
    pendsv_switch_call_idx: int
    pendsv_after_switch_idx: int
    pendsv_select_idx: int
    pendsv_saved_sp_idx: int
    pendsv_restored_pending_psp_idx: int
    pendsv_psp_written_idx: int | None
    pendsv_exception_return_idx: int
    svc_entry_idx: int
    svc_saved_sp_idx: int
    svc_restored_pending_psp_idx: int
    svc_psp_written_idx: int
    svc_exception_return_idx: int
    start_first_svc_idx: int


def _has_sized_symbol(elf: ElfImage, name: str) -> bool:
    return any(symbol.name == name and symbol.size > 0 for symbol in elf.symbols)


def _stream_has_fpu_stacking(elf: ElfImage, symbol: str) -> bool:
    span = _symbol_span(elf, symbol)
    if not isinstance(span, tuple):
        return False
    code_end = _code_stream_end(elf, span[0], span[1])
    addr = span[0]
    count = 0
    while addr < code_end and count < _MAX_PROFILE_INSTRUCTIONS:
        decoded = _decode_thumb(elf, addr, code_end)
        if isinstance(decoded, str):
            break
        if decoded.mnemonic in {"vstmdb", "vldmia"}:
            return True
        addr += decoded.size
        count += 1
    return False


def _select_port(elf: ElfImage) -> str:
    """Identify which compiled FreeRTOS M-profile port the image carries."""
    if _has_sized_symbol(elf, "vStartFirstTask") or _has_sized_symbol(
        elf, "vRestoreContextOfFirstTask"
    ):
        is_cm33 = False
        start_span = _symbol_span(elf, "vStartFirstTask")
        if isinstance(start_span, tuple):
            first = _decode_thumb(elf, start_span[0], start_span[1])
            is_cm33 = not isinstance(first, str) and first.mnemonic == "ldr_literal"
        if not is_cm33:
            pendsv_span = _symbol_span(elf, "PendSV_Handler")
            if isinstance(pendsv_span, tuple):
                for offset in (4, 14):
                    if pendsv_span[0] + offset >= pendsv_span[1]:
                        continue
                    insn = _decode_thumb(elf, pendsv_span[0] + offset, pendsv_span[1])
                    if (
                        not isinstance(insn, str)
                        and insn.mnemonic == "mrs"
                        and insn.operands.get("sysm") == _PSPLIM_SYSM
                    ):
                        is_cm33 = True
                        break
        if is_cm33:
            return (
                "arm-cm33-fpu"
                if _stream_has_fpu_stacking(elf, "PendSV_Handler")
                else "arm-cm33"
            )
        return "arm-cm0"
    for sym in ("PendSV_Handler", "SVC_Handler"):
        span = _symbol_span(elf, sym)
        if not isinstance(span, tuple):
            continue
        code_end = _code_stream_end(elf, span[0], span[1])
        addr = span[0]
        count = 0
        while addr < code_end and count < _MAX_PROFILE_INSTRUCTIONS:
            decoded = _decode_thumb(elf, addr, code_end)
            if isinstance(decoded, str):
                break
            if decoded.mnemonic in {"vstmdb", "vldmia"} or (
                decoded.mnemonic in {"stmdb", "ldmia"}
                and decoded.operands.get("reglist") == _CORE_LR_REGLIST
            ):
                return "arm-cm4f"
            addr += decoded.size
            count += 1
    return "arm-cm3"


def _port_spec(port: str, switch_start: int) -> _PortSpec:
    """Return the exact compiled-sequence specification for ``port``."""
    if port == "arm-cm4f":
        return _PortSpec(
            port="arm-cm4f",
            profile_id=CM4F_PROFILE_ID,
            pendsv_symbol="PendSV_Handler",
            svc_symbol="SVC_Handler",
            start_first_symbol="prvPortStartFirstTask",
            switch_symbol="vTaskSwitchContext",
            pendsv_expected=(
                ("mrs", {"rd": 0, "sysm": _PSP_SYSM}),
                ("isb", {}),
                ("ldr_literal", {"rt": 3}),
                ("ldr_imm", {"rt": 2, "rn": 3, "imm": 0}),
                ("tst_imm", {"rn": 14, "imm": 16}),
                ("it", {"firstcond": 0, "mask": 8}),
                (
                    "vstmdb",
                    {
                        "rn": 0,
                        "single": True,
                        "first": 16,
                        "count": 16,
                    },
                ),
                (
                    "stmdb",
                    dict(
                        rn=0,
                        wback=True,
                        load=False,
                        direction="db",
                        reglist=_CORE_LR_REGLIST,
                    ),
                ),
                ("str_imm", {"rt": 0, "rn": 2, "imm": 0}),
                ("push", {"reglist": _R0_R3_REGLIST}),
                ("mov_basepri_mask", {"rd": 0}),
                ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
                ("dsb", {}),
                ("isb", {}),
                ("bl", {"target": switch_start}),
                ("mov_imm", {"rd": 0, "imm": 0}),
                ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
                ("pop", {"reglist": _R0_R3_REGLIST}),
                ("ldr_imm", {"rt": 1, "rn": 3, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
                (
                    "ldmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=True,
                        direction="ia",
                        reglist=_CORE_LR_REGLIST,
                    ),
                ),
                ("tst_imm", {"rn": 14, "imm": 16}),
                ("it", {"firstcond": 0, "mask": 8}),
                (
                    "vldmia",
                    {
                        "rn": 0,
                        "single": True,
                        "first": 16,
                        "count": 16,
                    },
                ),
                ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
                ("isb", {}),
                ("bx", {"rm": 14}),
            ),
            svc_expected=(
                ("ldr_literal", {"rt": 3}),
                ("ldr_imm", {"rt": 1, "rn": 3, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
                (
                    "ldmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=True,
                        direction="ia",
                        reglist=_CORE_LR_REGLIST,
                    ),
                ),
                ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
                ("isb", {}),
                ("mov_imm", {"rd": 0, "imm": 0}),
                ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
                ("bx", {"rm": 14}),
            ),
            start_expected=(
                ("ldr_literal", {"rt": 0}),
                ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
                ("msr", {"rn": 0, "sysm": _MSP_SYSM}),
                ("mov_imm", {"rd": 0, "imm": 0}),
                ("msr", {"rn": 0, "sysm": _CONTROL_SYSM}),
                ("cpsie", {"flag": "i"}),
                ("cpsie", {"flag": "f"}),
                ("dsb", {}),
                ("isb", {}),
                ("svc", {"imm": 0}),
                ("nop", {}),
            ),
            pendsv_tcb_literals=(2,),
            svc_tcb_literals=(0,),
            start_vtor_literal=0,
            basepri_mask_index=10,
            pendsv_entry_idx=0,
            pendsv_save_pending_idx=4,
            pendsv_save_written_idx=8,
            pendsv_writeback_idx=9,
            pendsv_switch_call_idx=14,
            pendsv_after_switch_idx=15,
            pendsv_select_idx=19,
            pendsv_saved_sp_idx=20,
            pendsv_restored_pending_psp_idx=24,
            pendsv_psp_written_idx=25,
            pendsv_exception_return_idx=26,
            svc_entry_idx=0,
            svc_saved_sp_idx=3,
            svc_restored_pending_psp_idx=4,
            svc_psp_written_idx=5,
            svc_exception_return_idx=8,
            start_first_svc_idx=10,
        )
    if port == "arm-cm0":
        return _PortSpec(
            port="arm-cm0",
            profile_id=CM0_PROFILE_ID,
            pendsv_symbol="PendSV_Handler",
            svc_symbol="vRestoreContextOfFirstTask",
            start_first_symbol="vStartFirstTask",
            switch_symbol="vTaskSwitchContext",
            pendsv_expected=(
                ("mrs", {"rd": 0, "sysm": _PSP_SYSM}),
                ("ldr_literal", {"rt": 2}),
                ("ldr_imm", {"rt": 1, "rn": 2, "imm": 0}),
                ("subs", {"rd": 0, "rn": 0, "imm": 36, "set_flags": True}),
                ("str_imm", {"rt": 0, "rn": 1, "imm": 0}),
                ("mov", {"rd": 3, "rm": 14, "set_flags": False}),
                (
                    "stmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=False,
                        direction="ia",
                        reglist=_CM0_R3_REGLIST,
                    ),
                ),
                ("mov", {"rd": 4, "rm": 8, "set_flags": False}),
                ("mov", {"rd": 5, "rm": 9, "set_flags": False}),
                ("mov", {"rd": 6, "rm": 10, "set_flags": False}),
                ("mov", {"rd": 7, "rm": 11, "set_flags": False}),
                (
                    "stmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=False,
                        direction="ia",
                        reglist=_CM0_LOW_REGLIST,
                    ),
                ),
                ("cpsid", {"flag": "i"}),
                ("bl", {"target": switch_start}),
                ("cpsie", {"flag": "i"}),
                ("ldr_literal", {"rt": 2}),
                ("ldr_imm", {"rt": 1, "rn": 2, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
                ("adds", {"rd": 0, "rn": 0, "imm": 20, "set_flags": True}),
                (
                    "ldmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=True,
                        direction="ia",
                        reglist=_CM0_LOW_REGLIST,
                    ),
                ),
                ("mov", {"rd": 8, "rm": 4, "set_flags": False}),
                ("mov", {"rd": 9, "rm": 5, "set_flags": False}),
                ("mov", {"rd": 10, "rm": 6, "set_flags": False}),
                ("mov", {"rd": 11, "rm": 7, "set_flags": False}),
                ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
                ("subs", {"rd": 0, "rn": 0, "imm": 36, "set_flags": True}),
                (
                    "ldmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=True,
                        direction="ia",
                        reglist=_CM0_R3_REGLIST,
                    ),
                ),
                ("bx", {"rm": 3}),
            ),
            svc_expected=(
                ("ldr_literal", {"rt": 2}),
                ("ldr_imm", {"rt": 1, "rn": 2, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
                (
                    "ldmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=True,
                        direction="ia",
                        reglist=_CM0_SVC_REGLIST,
                    ),
                ),
                ("mov_imm", {"rd": 1, "imm": 2}),
                ("msr", {"rn": 1, "sysm": _CONTROL_SYSM}),
                ("adds", {"rd": 0, "rn": 0, "imm": 32, "set_flags": True}),
                ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
                ("isb", {}),
                ("bx", {"rm": 2}),
            ),
            start_expected=(
                ("cpsie", {"flag": "i"}),
                ("dsb", {}),
                ("isb", {}),
                ("svc", {}),
            ),
            pendsv_tcb_literals=(1, 15),
            svc_tcb_literals=(0,),
            start_vtor_literal=None,
            basepri_mask_index=None,
            pendsv_entry_idx=0,
            pendsv_save_pending_idx=3,
            pendsv_save_written_idx=None,
            pendsv_writeback_idx=12,
            pendsv_switch_call_idx=13,
            pendsv_after_switch_idx=14,
            pendsv_select_idx=17,
            pendsv_saved_sp_idx=18,
            pendsv_restored_pending_psp_idx=24,
            pendsv_psp_written_idx=25,
            pendsv_exception_return_idx=27,
            svc_entry_idx=0,
            svc_saved_sp_idx=3,
            svc_restored_pending_psp_idx=7,
            svc_psp_written_idx=8,
            svc_exception_return_idx=9,
            start_first_svc_idx=3,
        )
    if port in ("arm-cm33", "arm-cm33-fpu"):
        fpu = port == "arm-cm33-fpu"
        fpu_save = (
            (
                ("tst_imm", {"rn": 14, "imm": 16}),
                ("it", {"firstcond": 0, "mask": 8}),
                (
                    "vstmdb",
                    {"rn": 0, "single": True, "first": 16, "count": 16},
                ),
            )
            if fpu
            else ()
        )
        fpu_restore = (
            (
                ("tst_imm", {"rn": 3, "imm": 16}),
                ("it", {"firstcond": 0, "mask": 8}),
                (
                    "vldmia",
                    {"rn": 0, "single": True, "first": 16, "count": 16},
                ),
            )
            if fpu
            else ()
        )
        s_off = 3 if fpu else 0
        r_off = 3 if fpu else 0
        return _PortSpec(
            port=port,
            profile_id=CM33_FPU_PROFILE_ID if fpu else CM33_PROFILE_ID,
            pendsv_symbol="PendSV_Handler",
            svc_symbol="vRestoreContextOfFirstTask",
            start_first_symbol="vStartFirstTask",
            switch_symbol="vTaskSwitchContext",
            pendsv_expected=(
                ("mrs", {"rd": 0, "sysm": _PSP_SYSM}),
                *fpu_save,
                ("mrs", {"rd": 2, "sysm": _PSPLIM_SYSM}),
                ("mov", {"rd": 3, "rm": 14, "set_flags": False}),
                (
                    "stmdb",
                    dict(
                        rn=0,
                        wback=True,
                        load=False,
                        direction="db",
                        reglist=_CM33_REGLIST,
                    ),
                ),
                ("ldr_literal", {"rt": 2}),
                ("ldr_imm", {"rt": 1, "rn": 2, "imm": 0}),
                ("str_imm", {"rt": 0, "rn": 1, "imm": 0}),
                ("mov_basepri_mask", {"rd": 0}),
                ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
                ("dsb", {}),
                ("isb", {}),
                ("bl", {"target": switch_start}),
                ("mov_imm", {"rd": 0, "imm": 0}),
                ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
                ("ldr_literal", {"rt": 2}),
                ("ldr_imm", {"rt": 1, "rn": 2, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
                (
                    "ldmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=True,
                        direction="ia",
                        reglist=_CM33_REGLIST,
                    ),
                ),
                *fpu_restore,
                ("msr", {"rn": 2, "sysm": _PSPLIM_SYSM}),
                ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
                ("bx", {"rm": 3}),
            ),
            svc_expected=(
                ("ldr_literal", {"rt": 2}),
                ("ldr_imm", {"rt": 1, "rn": 2, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
                (
                    "ldmia",
                    dict(
                        rn=0,
                        wback=True,
                        load=True,
                        direction="ia",
                        reglist=_CM33_SVC_REGLIST,
                    ),
                ),
                ("msr", {"rn": 1, "sysm": _PSPLIM_SYSM}),
                ("mrs", {"rd": 1, "sysm": _CONTROL_SYSM}),
                ("orr_imm", {"rd": 1, "rn": 1, "imm": 2}),
                ("msr", {"rn": 1, "sysm": _CONTROL_SYSM}),
                ("adds", {"rd": 0, "rn": 0, "imm": 32, "set_flags": True}),
                ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
                ("isb", {}),
                ("mov_imm", {"rd": 0, "imm": 0}),
                ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
                ("bx", {"rm": 2}),
            ),
            start_expected=(
                ("ldr_literal", {"rt": 0}),
                ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
                ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
                ("msr", {"rn": 0, "sysm": _MSP_SYSM}),
                ("cpsie", {"flag": "i"}),
                ("cpsie", {"flag": "f"}),
                ("dsb", {}),
                ("isb", {}),
                ("svc", {}),
                ("nop", {}),
            ),
            pendsv_tcb_literals=(4 + s_off, 14 + s_off),
            svc_tcb_literals=(0,),
            start_vtor_literal=0,
            basepri_mask_index=7 + s_off,
            pendsv_entry_idx=0,
            pendsv_save_pending_idx=1 if fpu else 3,
            pendsv_save_written_idx=6 + s_off,
            pendsv_writeback_idx=7 + s_off,
            pendsv_switch_call_idx=11 + s_off,
            pendsv_after_switch_idx=12 + s_off,
            pendsv_select_idx=16 + s_off,
            pendsv_saved_sp_idx=17 + s_off,
            pendsv_restored_pending_psp_idx=19 + s_off + r_off,
            pendsv_psp_written_idx=None,
            pendsv_exception_return_idx=20 + s_off + r_off,
            svc_entry_idx=0,
            svc_saved_sp_idx=3,
            svc_restored_pending_psp_idx=9,
            svc_psp_written_idx=10,
            svc_exception_return_idx=13,
            start_first_svc_idx=8,
        )
    return _PortSpec(
        port="arm-cm3",
        profile_id=PROFILE_ID,
        pendsv_symbol="PendSV_Handler",
        svc_symbol="SVC_Handler",
        start_first_symbol="prvPortStartFirstTask",
        switch_symbol="vTaskSwitchContext",
        pendsv_expected=(
            ("mrs", {"rd": 0, "sysm": _PSP_SYSM}),
            ("isb", {}),
            ("ldr_literal", {"rt": 3}),
            ("ldr_imm", {"rt": 2, "rn": 3, "imm": 0}),
            (
                "stmdb",
                dict(
                    rn=0, wback=True, load=False, direction="db", reglist=_CORE_REGLIST
                ),
            ),
            ("str_imm", {"rt": 0, "rn": 2, "imm": 0}),
            ("push", {"reglist": _R3_LR_PUSH}),
            ("mov_basepri_mask", {"rd": 0}),
            ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
            ("bl", {"target": switch_start}),
            ("mov_imm", {"rd": 0, "imm": 0}),
            ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
            (
                "ldmia",
                dict(rn=13, wback=True, load=True, direction="ia", reglist=_R3_LR_LDM),
            ),
            ("ldr_imm", {"rt": 1, "rn": 3, "imm": 0}),
            ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
            (
                "ldmia",
                dict(
                    rn=0, wback=True, load=True, direction="ia", reglist=_CORE_REGLIST
                ),
            ),
            ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
            ("isb", {}),
            ("bx", {"rm": 14}),
        ),
        svc_expected=(
            ("ldr_literal", {"rt": 3}),
            ("ldr_imm", {"rt": 1, "rn": 3, "imm": 0}),
            ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
            (
                "ldmia",
                dict(
                    rn=0, wback=True, load=True, direction="ia", reglist=_CORE_REGLIST
                ),
            ),
            ("msr", {"rn": 0, "sysm": _PSP_SYSM}),
            ("isb", {}),
            ("mov_imm", {"rd": 0, "imm": 0}),
            ("msr", {"rn": 0, "sysm": _BASEPRI_SYSM}),
            ("orr_imm", {"rd": 14, "rn": 14, "imm": 13}),
            ("bx", {"rm": 14}),
        ),
        start_expected=(
            ("ldr_literal", {"rt": 0}),
            ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
            ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
            ("msr", {"rn": 0, "sysm": _MSP_SYSM}),
            ("cpsie", {"flag": "i"}),
            ("cpsie", {"flag": "f"}),
            ("dsb", {}),
            ("isb", {}),
            ("svc", {"imm": 0}),
            ("nop", {}),
        ),
        pendsv_tcb_literals=(2,),
        svc_tcb_literals=(0,),
        start_vtor_literal=0,
        basepri_mask_index=7,
        pendsv_entry_idx=0,
        pendsv_save_pending_idx=4,
        pendsv_save_written_idx=5,
        pendsv_writeback_idx=6,
        pendsv_switch_call_idx=9,
        pendsv_after_switch_idx=10,
        pendsv_select_idx=14,
        pendsv_saved_sp_idx=15,
        pendsv_restored_pending_psp_idx=16,
        pendsv_psp_written_idx=17,
        pendsv_exception_return_idx=18,
        svc_entry_idx=0,
        svc_saved_sp_idx=3,
        svc_restored_pending_psp_idx=4,
        svc_psp_written_idx=5,
        svc_exception_return_idx=9,
        start_first_svc_idx=8,
    )


def build_profile(elf: ElfImage) -> tuple[SchedulerProfile | None, str]:
    """Verify every compiled transition sequence and return its stage map.

    A missing symbol, non-ARM/non-32-bit image, unrecognized instruction,
    wrong literal target, truncated function or ambiguous ``pxCurrentTCB``
    update returns ``(None, reason)``.  No stage address is guessed.
    """
    if elf.machine != EM_ARM or elf.pointer_size != 4:
        return None, "scheduler-profile-unsupported-image"
    if elf.endian != "little":
        return None, "scheduler-profile-unsupported-endian"
    port = _select_port(elf)
    placeholder = _port_spec(port, 0)
    names = (
        placeholder.pendsv_symbol,
        placeholder.svc_symbol,
        placeholder.start_first_symbol,
        placeholder.switch_symbol,
    )
    spans: dict[str, tuple[int, int]] = {}
    for name in names:
        span = _symbol_span(elf, name)
        if isinstance(span, str):
            return None, span
        spans[name] = span
    ordered = sorted(spans.items(), key=lambda item: item[1][0])
    for (previous, (_pstart, pend)), (current, (cstart, _cend)) in zip(
        ordered, ordered[1:], strict=False
    ):
        if cstart < pend:
            return None, (f"scheduler-profile-symbol-overlap:{previous}:{current}")
    pendsv_start, pendsv_end = spans[placeholder.pendsv_symbol]
    svc_start, svc_end = spans[placeholder.svc_symbol]
    start_start, start_end = spans[placeholder.start_first_symbol]
    switch_start, switch_end = spans[placeholder.switch_symbol]
    if min(pendsv_end, svc_end, start_end, switch_end) <= 0:
        return None, "scheduler-profile-symbol-unbounded"
    tcb_pointer = elf.symbol_address("pxCurrentTCB")
    if tcb_pointer is None:
        return None, "scheduler-profile-symbol-missing:pxCurrentTCB"
    code_ends = {
        name: _code_stream_end(elf, span[0], span[1]) for name, span in spans.items()
    }
    spec = _port_spec(port, switch_start)

    pendsv, reason = _decode_expected(
        elf,
        spec.pendsv_symbol,
        pendsv_start,
        code_ends[spec.pendsv_symbol],
        spec.pendsv_expected,
        mask_index=spec.basepri_mask_index,
    )
    if pendsv is None:
        return None, reason
    for lit_idx in spec.pendsv_tcb_literals:
        if _resolved_literal(elf, pendsv[lit_idx]) != tcb_pointer:
            return None, f"scheduler-profile-mismatch:{spec.pendsv_symbol}:tcb-literal"
    svc, reason = _decode_expected(
        elf, spec.svc_symbol, svc_start, code_ends[spec.svc_symbol], spec.svc_expected
    )
    if svc is None:
        return None, reason
    for lit_idx in spec.svc_tcb_literals:
        if _resolved_literal(elf, svc[lit_idx]) != tcb_pointer:
            return None, f"scheduler-profile-mismatch:{spec.svc_symbol}:tcb-literal"
    start_first, reason = _decode_expected(
        elf,
        spec.start_first_symbol,
        start_start,
        code_ends[spec.start_first_symbol],
        spec.start_expected,
    )
    if start_first is None:
        return None, reason
    if (
        spec.start_vtor_literal is not None
        and _resolved_literal(elf, start_first[spec.start_vtor_literal])
        != _VTOR_ADDRESS
    ):
        return (
            None,
            f"scheduler-profile-mismatch:{spec.start_first_symbol}:vtor",
        )
    streams: dict[str, tuple[str, ...]] = {}
    for symbol, start, end, instructions, mask_index in (
        (
            spec.pendsv_symbol,
            pendsv_start,
            code_ends[spec.pendsv_symbol],
            pendsv,
            spec.basepri_mask_index,
        ),
        (spec.svc_symbol, svc_start, code_ends[spec.svc_symbol], svc, None),
        (
            spec.start_first_symbol,
            start_start,
            code_ends[spec.start_first_symbol],
            start_first,
            None,
        ),
    ):
        stream, reason = _decode_stream(
            elf, symbol, start, end, _MAX_PROFILE_INSTRUCTIONS
        )
        if stream is None:
            return None, reason
        expected_tokens = tuple(
            _semantic_token(elf, instruction) for instruction in instructions
        )
        if len(stream) < len(expected_tokens) or (
            stream[: len(expected_tokens)] != expected_tokens
        ):
            return None, f"scheduler-profile-mismatch:{symbol}:stream"
        tokens = list(stream)
        if mask_index is not None and mask_index < len(tokens):
            tokens[mask_index] = _MASK_PRIORITY_TOKEN
        streams[symbol] = tuple(tokens)
    update = _find_switch_update(elf, switch_start, switch_end, tcb_pointer)
    if isinstance(update, str):
        return None, update
    store, switch_signature = update
    switch_insns, switch_reason = _decode_switch_instructions(
        elf, switch_start, switch_end
    )
    if switch_insns is None:
        return None, switch_reason
    switch_pre_pcs, switch_post_pcs = _switch_cfg_partition(switch_insns, store.address)

    fingerprint = hashlib.sha256(
        "\n".join(
            [
                token
                for symbol in (
                    spec.pendsv_symbol,
                    spec.svc_symbol,
                    spec.start_first_symbol,
                )
                for token in streams[symbol]
            ]
            + [switch_signature]
        ).encode("utf-8")
    ).hexdigest()[:16]
    call_insn = pendsv[spec.pendsv_switch_call_idx]
    return (
        SchedulerProfile(
            profile_id=spec.profile_id,
            fingerprint=fingerprint,
            pendsv_start=pendsv_start,
            pendsv_end=pendsv_end,
            svc_start=svc_start,
            svc_end=svc_end,
            start_first_start=start_start,
            start_first_end=start_end,
            switch_start=switch_start,
            switch_end=switch_end,
            pendsv_entry=pendsv[spec.pendsv_entry_idx].address,
            pendsv_save_pending=pendsv[spec.pendsv_save_pending_idx].address,
            pendsv_save_written=(
                pendsv[spec.pendsv_save_written_idx].address
                if spec.pendsv_save_written_idx is not None
                else None
            ),
            pendsv_writeback=pendsv[spec.pendsv_writeback_idx].address,
            pendsv_switch_call=call_insn.address,
            pendsv_after_switch=pendsv[spec.pendsv_after_switch_idx].address,
            pendsv_select=pendsv[spec.pendsv_select_idx].address,
            pendsv_saved_sp=pendsv[spec.pendsv_saved_sp_idx].address,
            pendsv_restored_pending_psp=pendsv[
                spec.pendsv_restored_pending_psp_idx
            ].address,
            pendsv_psp_written=(
                pendsv[spec.pendsv_psp_written_idx].address
                if spec.pendsv_psp_written_idx is not None
                else None
            ),
            pendsv_exception_return=pendsv[spec.pendsv_exception_return_idx].address,
            svc_entry=svc[spec.svc_entry_idx].address,
            svc_saved_sp=svc[spec.svc_saved_sp_idx].address,
            svc_restored_pending_psp=svc[spec.svc_restored_pending_psp_idx].address,
            svc_psp_written=svc[spec.svc_psp_written_idx].address,
            svc_exception_return=svc[spec.svc_exception_return_idx].address,
            start_first_svc=start_first[spec.start_first_svc_idx].address,
            switch_entry=switch_start,
            switch_update=store.address,
            switch_after_update=store.address + store.size,
            switch_return=call_insn.address + call_insn.size,
            px_current_tcb=tcb_pointer,
            port=spec.port,
            svc_symbol=spec.svc_symbol,
            start_first_symbol=spec.start_first_symbol,
            switch_pre_pcs=switch_pre_pcs,
            switch_post_pcs=switch_post_pcs,
        ),
        "ok",
    )


def classify_stage(profile: SchedulerProfile, pc: int) -> SchedulerStage | str | None:
    """Classify one exact PC; unknown PCs inside a verified range are refused."""
    exact: dict[int, SchedulerStage] = {
        profile.pendsv_entry: SchedulerStage(
            "pendsv-entry", "old", "live-frame", profile.pendsv_entry
        ),
        profile.pendsv_save_pending: SchedulerStage(
            "pendsv-save-pending", "old", "live-frame", profile.pendsv_save_pending
        ),
        profile.pendsv_writeback: SchedulerStage(
            "pendsv-writeback", "old", "saved-tcb", profile.pendsv_writeback
        ),
        profile.pendsv_switch_call: SchedulerStage(
            "pendsv-switch-call", "old", "saved-tcb", profile.pendsv_switch_call
        ),
        profile.pendsv_after_switch: SchedulerStage(
            "pendsv-after-switch", "new", "saved-tcb", profile.pendsv_after_switch
        ),
        profile.pendsv_select: SchedulerStage(
            "pendsv-select", "new", "saved-tcb", profile.pendsv_select
        ),
        profile.pendsv_saved_sp: SchedulerStage(
            "pendsv-saved-sp", "new", "saved-tcb", profile.pendsv_saved_sp
        ),
        profile.pendsv_restored_pending_psp: SchedulerStage(
            "pendsv-restored-pending-psp",
            "new",
            "restore-buffer",
            profile.pendsv_restored_pending_psp,
        ),
        profile.pendsv_exception_return: SchedulerStage(
            "pendsv-exception-return",
            "new",
            "live-frame-new",
            profile.pendsv_exception_return,
        ),
        profile.svc_entry: SchedulerStage(
            "svc-entry", "new", "first-start-saved", profile.svc_entry
        ),
        profile.svc_saved_sp: SchedulerStage(
            "svc-saved-sp", "new", "first-start-saved", profile.svc_saved_sp
        ),
        profile.svc_restored_pending_psp: SchedulerStage(
            "svc-restored-pending-psp",
            "new",
            "first-start-buffer",
            profile.svc_restored_pending_psp,
        ),
        profile.svc_psp_written: SchedulerStage(
            "svc-psp-written", "new", "first-start-live", profile.svc_psp_written
        ),
        profile.svc_exception_return: SchedulerStage(
            "svc-exception-return",
            "new",
            "first-start-live",
            profile.svc_exception_return,
        ),
        profile.switch_entry: SchedulerStage(
            "switch-entry", "old", "saved-tcb", profile.switch_entry
        ),
        profile.switch_update: SchedulerStage(
            "switch-before-update", "old", "saved-tcb", profile.switch_update
        ),
        profile.switch_after_update: SchedulerStage(
            "switch-after-update", "new", "saved-tcb", profile.switch_after_update
        ),
    }
    if profile.pendsv_save_written is not None:
        exact[profile.pendsv_save_written] = SchedulerStage(
            "pendsv-save-written", "old", "saved-buffer", profile.pendsv_save_written
        )
    if profile.pendsv_psp_written is not None:
        exact[profile.pendsv_psp_written] = SchedulerStage(
            "pendsv-psp-written", "new", "live-frame-new", profile.pendsv_psp_written
        )
    if pc in exact:
        return exact[pc]
    if pc in profile.switch_pre_pcs:
        return SchedulerStage("switch-before-update", "old", "saved-tcb", pc)
    if pc in profile.switch_post_pcs:
        return SchedulerStage("switch-after-update", "new", "saved-tcb", pc)
    if pc == profile.start_first_svc:
        return SchedulerStage(
            "start-first-svc-pending", "pending", "none", profile.start_first_svc
        )
    for start, end, name in (
        (profile.pendsv_start, profile.pendsv_end, "PendSV_Handler"),
        (profile.svc_start, profile.svc_end, profile.svc_symbol),
        (
            profile.start_first_start,
            profile.start_first_end,
            profile.start_first_symbol,
        ),
        (profile.switch_start, profile.switch_end, "vTaskSwitchContext"),
    ):
        if start <= pc < end:
            return f"scheduler-stage-ambiguous:{name}"
    return None
