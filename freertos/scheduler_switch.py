"""Compiled FreeRTOS Armv7-M (ARM_CM3) scheduler-transition evidence (pure).

The module proves the PendSV/SVC/start-first-task instruction sequences of the
kernel's ``portable/GCC/ARM_CM3`` port from the loaded ELF and classifies a live
stop inside them.  A symbol-relative decoder keeps this independent of any
one fixture's link address.  Owner-consistent seeds for classified stages live
in :mod:`freertos.scheduler_seed`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

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
from core.unwind_meta import EM_ARM, ElfImage

PROFILE_ID = "freertos/armv7-m/arm-cm3-pendsv-v1"

_CORE_REGLIST = 0xFF0
_R3_LR_PUSH = 0x108
_R3_LR_LDM = 0x4008
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

# The BASEPRI slot is checked by a predicate rather than an exact operand:
# any legal non-zero 8-bit interrupt-priority mask is a compiled port choice.
_BASEPRI_MASK_INDEX = 7


@dataclass(frozen=True)
class SchedulerProfile:
    """Verified symbol-relative addresses of one Armv7-M (ARM_CM3) switch sequence.

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
    pendsv_save_written: int
    pendsv_writeback: int
    pendsv_switch_call: int
    pendsv_after_switch: int
    pendsv_select: int
    pendsv_saved_sp: int
    pendsv_restored_pending_psp: int
    pendsv_psp_written: int
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
    names = (
        "PendSV_Handler",
        "SVC_Handler",
        "prvPortStartFirstTask",
        "vTaskSwitchContext",
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
    pendsv_start, pendsv_end = spans["PendSV_Handler"]
    svc_start, svc_end = spans["SVC_Handler"]
    start_start, start_end = spans["prvPortStartFirstTask"]
    switch_start, switch_end = spans["vTaskSwitchContext"]
    if min(pendsv_end, svc_end, start_end, switch_end) <= 0:
        return None, "scheduler-profile-symbol-unbounded"
    tcb_pointer = elf.symbol_address("pxCurrentTCB")
    if tcb_pointer is None:
        return None, "scheduler-profile-symbol-missing:pxCurrentTCB"
    code_ends = {
        name: _code_stream_end(elf, span[0], span[1]) for name, span in spans.items()
    }

    pendsv_expected = (
        ("mrs", {"rd": 0, "sysm": 0x09}),
        ("isb", {}),
        ("ldr_literal", {"rt": 3}),
        ("ldr_imm", {"rt": 2, "rn": 3, "imm": 0}),
        (
            "stmdb",
            dict(rn=0, wback=True, load=False, direction="db", reglist=_CORE_REGLIST),
        ),
        ("str_imm", {"rt": 0, "rn": 2, "imm": 0}),
        ("push", {"reglist": _R3_LR_PUSH}),
        ("mov_basepri_mask", {"rd": 0}),
        ("msr", {"rn": 0, "sysm": 0x11}),
        ("bl", {"target": switch_start}),
        ("mov_imm", {"rd": 0, "imm": 0}),
        ("msr", {"rn": 0, "sysm": 0x11}),
        (
            "ldmia",
            dict(rn=13, wback=True, load=True, direction="ia", reglist=_R3_LR_LDM),
        ),
        ("ldr_imm", {"rt": 1, "rn": 3, "imm": 0}),
        ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
        (
            "ldmia",
            dict(rn=0, wback=True, load=True, direction="ia", reglist=_CORE_REGLIST),
        ),
        ("msr", {"rn": 0, "sysm": 0x09}),
        ("isb", {}),
        ("bx", {"rm": 14}),
    )
    svc_expected = (
        ("ldr_literal", {"rt": 3}),
        ("ldr_imm", {"rt": 1, "rn": 3, "imm": 0}),
        ("ldr_imm", {"rt": 0, "rn": 1, "imm": 0}),
        (
            "ldmia",
            dict(rn=0, wback=True, load=True, direction="ia", reglist=_CORE_REGLIST),
        ),
        ("msr", {"rn": 0, "sysm": 0x09}),
        ("isb", {}),
        ("mov_imm", {"rd": 0, "imm": 0}),
        ("msr", {"rn": 0, "sysm": 0x11}),
        ("orr_imm", {"rd": 14, "rn": 14, "imm": 13}),
        ("bx", {"rm": 14}),
    )
    start_expected = (
        ("ldr_literal", {"rt": 0}),
        ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
        ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
        ("msr", {"rn": 0, "sysm": 0x08}),
        ("cpsie", {"flag": "i"}),
        ("cpsie", {"flag": "f"}),
        ("dsb", {}),
        ("isb", {}),
        ("svc", {"imm": 0}),
        ("nop", {}),
    )

    pendsv, reason = _decode_expected(
        elf,
        "PendSV_Handler",
        pendsv_start,
        code_ends["PendSV_Handler"],
        pendsv_expected,
        mask_index=_BASEPRI_MASK_INDEX,
    )
    if pendsv is None:
        return None, reason
    if _resolved_literal(elf, pendsv[2]) != tcb_pointer:
        return None, "scheduler-profile-mismatch:PendSV_Handler:tcb-literal"
    svc, reason = _decode_expected(
        elf, "SVC_Handler", svc_start, code_ends["SVC_Handler"], svc_expected
    )
    if svc is None:
        return None, reason
    if _resolved_literal(elf, svc[0]) != tcb_pointer:
        return None, "scheduler-profile-mismatch:SVC_Handler:tcb-literal"
    start_first, reason = _decode_expected(
        elf,
        "prvPortStartFirstTask",
        start_start,
        code_ends["prvPortStartFirstTask"],
        start_expected,
    )
    if start_first is None:
        return None, reason
    if _resolved_literal(elf, start_first[0]) != 0xE000ED08:
        return None, "scheduler-profile-mismatch:prvPortStartFirstTask:vtor"
    streams: dict[str, tuple[str, ...]] = {}
    for symbol, start, end, instructions, mask_index in (
        ("PendSV_Handler", pendsv_start, code_ends["PendSV_Handler"], pendsv, 7),
        ("SVC_Handler", svc_start, code_ends["SVC_Handler"], svc, None),
        (
            "prvPortStartFirstTask",
            start_start,
            code_ends["prvPortStartFirstTask"],
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

    fingerprint = hashlib.sha256(
        "\n".join(
            [
                token
                for symbol in (
                    "PendSV_Handler",
                    "SVC_Handler",
                    "prvPortStartFirstTask",
                )
                for token in streams[symbol]
            ]
            + [switch_signature]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return (
        SchedulerProfile(
            profile_id=PROFILE_ID,
            fingerprint=fingerprint,
            pendsv_start=pendsv_start,
            pendsv_end=pendsv_end,
            svc_start=svc_start,
            svc_end=svc_end,
            start_first_start=start_start,
            start_first_end=start_end,
            switch_start=switch_start,
            switch_end=switch_end,
            pendsv_entry=pendsv[0].address,
            pendsv_save_pending=pendsv[4].address,
            pendsv_save_written=pendsv[5].address,
            pendsv_writeback=pendsv[6].address,
            pendsv_switch_call=pendsv[9].address,
            pendsv_after_switch=pendsv[10].address,
            pendsv_select=pendsv[14].address,
            pendsv_saved_sp=pendsv[15].address,
            pendsv_restored_pending_psp=pendsv[16].address,
            pendsv_psp_written=pendsv[17].address,
            pendsv_exception_return=pendsv[18].address,
            svc_entry=svc[0].address,
            svc_saved_sp=svc[3].address,
            svc_restored_pending_psp=svc[4].address,
            svc_psp_written=svc[5].address,
            svc_exception_return=svc[9].address,
            start_first_svc=start_first[8].address,
            switch_entry=switch_start,
            switch_update=store.address,
            switch_after_update=store.address + store.size,
            switch_return=pendsv[9].address + pendsv[9].size,
            px_current_tcb=tcb_pointer,
        ),
        "ok",
    )


def classify_stage(profile: SchedulerProfile, pc: int) -> SchedulerStage | str | None:
    """Classify one exact PC; unknown PCs inside a verified range are refused."""
    exact = {
        profile.pendsv_entry: SchedulerStage(
            "pendsv-entry", "old", "live-frame", profile.pendsv_entry
        ),
        profile.pendsv_save_pending: SchedulerStage(
            "pendsv-save-pending", "old", "live-frame", profile.pendsv_save_pending
        ),
        profile.pendsv_save_written: SchedulerStage(
            "pendsv-save-written", "old", "saved-buffer", profile.pendsv_save_written
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
        profile.pendsv_psp_written: SchedulerStage(
            "pendsv-psp-written", "new", "live-frame-new", profile.pendsv_psp_written
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
    if pc in exact:
        return exact[pc]
    if pc == profile.start_first_svc:
        return SchedulerStage(
            "start-first-svc-pending", "pending", "none", profile.start_first_svc
        )
    for start, end, name in (
        (profile.pendsv_start, profile.pendsv_end, "PendSV_Handler"),
        (profile.svc_start, profile.svc_end, "SVC_Handler"),
        (profile.start_first_start, profile.start_first_end, "prvPortStartFirstTask"),
        (profile.switch_start, profile.switch_end, "vTaskSwitchContext"),
    ):
        if start <= pc < end:
            return f"scheduler-stage-ambiguous:{name}"
    return None
