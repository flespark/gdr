"""Compiled FreeRTOS Armv7-M (ARM_CM3) scheduler-transition evidence (pure).

The module proves the PendSV/SVC/start-first-task instruction sequences of the
kernel's ``portable/GCC/ARM_CM3`` port from the loaded ELF and classifies a live
stop inside them.  It never reads target memory itself: the adapter supplies a
reader and the live registers, and every returned seed is bounded by the task's
recorded stack span.  A symbol-relative decoder keeps this independent of any
one fixture's link address.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
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
    resolved_literal as _resolved_literal,
)
from core.insn_thumb import (
    semantic_token as _semantic_token,
)
from core.stack_frame_parser import SavedStackContext
from core.unwind import SeedFrame, read_slot
from core.unwind_arm import decode_armv7m_exception_return
from core.unwind_meta import EM_ARM, ElfImage, Reader

from .backtrace import decode_saved_context

PROFILE_ID = "freertos/armv7-m/arm-cm3-pendsv-v1"

EXC_RETURN_THREAD_PROCESS = 0xFFFFFFFD
XPSR_T_BIT = 1 << 24
XPSR_ALIGN_BIT = 1 << 9

_CORE_REGS = tuple(range(4, 12))
_CORE_REGLIST = 0xFF0
_R3_LR_PUSH = 0x108
_R3_LR_LDM = 0x4008
_MAX_PROFILE_INSTRUCTIONS = 96

_EXPECTED_SWITCH_TOKENS = (
    "ldr_literal:r3:sym:uxSchedulerSuspended",
    "ldr_imm:imm=0,rn=3,rt=3",
    "raw16:0xb11b",
    "ldr_literal:r3:sym:xYieldPending",
    "mov_imm:imm=1,rd=2",
    "str_imm:imm=0,rn=3,rt=2",
    "bx:rm=14",
    "push:reglist=272",
    "ldr_literal:r3:sym:xYieldPending",
    "mov_imm:imm=0,rd=2",
    "str_imm:imm=0,rn=3,rt=2",
    "bl:fn:gdr_runtime_timer_value",
    "ldr_literal:r3:sym:ulTotalRunTime",
    "str_imm:imm=0,rn=3,rt=0",
    "ldr_literal:r3:sym:ulTaskSwitchedInTime",
    "ldr_imm:imm=0,rn=3,rt=3",
    "raw16:0x4298",
    "raw16:0xd905",
    "ldr_literal:r2:sym:pxCurrentTCB",
    "ldr_imm:imm=0,rn=2,rt=1",
    "ldr_imm:imm=96,rn=1,rt=2",
    "raw16:0x1ac3",
    "raw16:0x441a",
    "str_imm:imm=96,rn=1,rt=2",
    "ldr_literal:r3:sym:ulTaskSwitchedInTime",
    "str_imm:imm=0,rn=3,rt=0",
    "ldr_literal:r3:sym:pxCurrentTCB",
    "ldr_imm:imm=0,rn=3,rt=3",
    "ldr_imm:imm=48,rn=3,rt=3",
    "ldr_imm:imm=0,rn=3,rt=2",
    "raw32:0xf1b2:0x3fa5",
    "raw16:0xd103",
    "ldr_imm:imm=4,rn=3,rt=2",
    "raw32:0xf1b2:0x3fa5",
    "raw16:0xd02b",
    "ldr_literal:r3:sym:pxCurrentTCB",
    "ldr_imm:imm=0,rn=3,rt=0",
    "ldr_imm:imm=0,rn=3,rt=1",
    "raw16:0x3134",
    "bl:fn:vApplicationStackOverflowHook",
    "ldr_literal:r3:sym:uxTopReadyPriority",
    "ldr_imm:imm=0,rn=3,rt=4",
    "raw32:0xfab4:0xf484",
    "raw16:0xb2e4",
    "raw32:0xf1c4:0x041f",
    "raw32:0xeb04:0x0384",
    "raw16:0x009b",
    "ldr_literal:r2:sym:pxReadyTasksLists",
    "raw16:0x58d3",
    "raw16:0xb30b",
    "ldr_literal:r1:sym:pxReadyTasksLists",
    "raw32:0xeb04:0x0384",
    "raw32:0xeb01:0x0383",
    "ldr_imm:imm=4,rn=3,rt=2",
    "ldr_imm:imm=4,rn=2,rt=2",
    "str_imm:imm=4,rn=3,rt=2",
    "raw32:0xeb04:0x0384",
    "raw16:0x009b",
    "raw16:0x3308",
    "raw16:0x440b",
    "raw16:0x429a",
    "raw16:0xd017",
    "raw32:0xeb04:0x0484",
    "ldr_literal:r3:sym:pxReadyTasksLists",
    "raw32:0xeb03:0x0384",
    "ldr_imm:imm=4,rn=3,rt=3",
    "ldr_imm:imm=12,rn=3,rt=2",
    "ldr_literal:r3:sym:pxCurrentTCB",
    "str_imm:imm=0,rn=3,rt=2",
    "pop:reglist=272",
    "ldr_imm:imm=8,rn=3,rt=2",
    "raw32:0xf1b2:0x3fa5",
    "raw16:0xd1cf",
    "ldr_imm:imm=12,rn=3,rt=3",
    "raw32:0xf1b3:0x3fa5",
    "raw16:0xd1cb",
    "raw16:0xe7d0",
    "raw32:0xf640:0x30e1",
    "bl:fn:gdr_fixture_assert_failed",
    "raw16:0xe7d8",
    "ldr_imm:imm=4,rn=2,rt=1",
    "raw32:0xeb04:0x0284",
    "ldr_literal:r3:sym:pxReadyTasksLists",
    "raw32:0xeb03:0x0382",
    "str_imm:imm=4,rn=3,rt=1",
    "raw16:0xe7df",
    "pool:sym:uxSchedulerSuspended",
    "pool:sym:xYieldPending",
    "pool:sym:ulTotalRunTime",
    "pool:sym:ulTaskSwitchedInTime",
    "pool:sym:pxCurrentTCB",
    "pool:sym:uxTopReadyPriority",
    "pool:sym:pxReadyTasksLists",
)


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


@dataclass(frozen=True)
class SchedulerSeed:
    """An owner-consistent seed plus the evidence that selected it."""

    seed: SeedFrame
    stage: str
    owner: str
    evidence: str


def _decode_stream(
    elf: ElfImage, symbol: str, start: int, end: int, limit: int
) -> tuple[tuple[str, ...] | None, str]:
    """Decode every instruction and literal-pool word in one function span."""
    return decode_thumb_stream(
        elf, symbol, start, end, limit, prefix="scheduler-profile"
    )


def _decode_expected(
    elf: ElfImage,
    symbol: str,
    start: int,
    end: int,
    expected: tuple[tuple[str, Mapping[str, int | bool | str]], ...],
) -> tuple[tuple[ThumbInstruction, ...] | None, str]:
    return decode_expected_thumb(
        elf,
        symbol,
        start,
        end,
        expected,
        max_instructions=_MAX_PROFILE_INSTRUCTIONS,
        prefix="scheduler-profile",
    )


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


def _find_switch_update(
    elf: ElfImage, start: int, end: int, tcb_pointer: int
) -> tuple[ThumbInstruction, ThumbInstruction] | str:
    """Prove the one local list-owner -> pxCurrentTCB store dataflow block."""
    decoded: list[ThumbInstruction] = []
    address = start
    while address < end:
        instruction = _decode_thumb(elf, address, end)
        if isinstance(instruction, str):
            return "scheduler-profile-mismatch:vTaskSwitchContext:unreadable"
        decoded.append(instruction)
        address += instruction.size
    candidates: list[tuple[ThumbInstruction, ThumbInstruction]] = []
    for index, instruction in enumerate(decoded):
        if not _expect(instruction, "str_imm", {"rt": 2, "rn": 3, "imm": 0}):
            continue
        if index < 3 or index + 1 >= len(decoded):
            continue
        owner = decoded[index - 3]
        load_owner = decoded[index - 2]
        literal = decoded[index - 1]
        following = decoded[index + 1]
        if not _expect(owner, "ldr_imm", {"rt": 3, "rn": 3, "imm": 4}):
            continue
        if not _expect(load_owner, "ldr_imm", {"rt": 2, "rn": 3, "imm": 12}):
            continue
        if (
            literal.mnemonic != "ldr_literal"
            or _resolved_literal(elf, literal) != tcb_pointer
            or literal.operands.get("rt") != 3
        ):
            continue
        if following.mnemonic != "pop" or following.operands.get("reglist") != 0x0110:
            continue
        candidates.append((literal, instruction))
    if len(candidates) != 1:
        return "scheduler-profile-mismatch:vTaskSwitchContext:update-count"
    return candidates[0]


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
        ("mov_imm", {"rd": 0, "imm": 4}),
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
        elf, "PendSV_Handler", pendsv_start, pendsv_end, pendsv_expected
    )
    if pendsv is None:
        return None, reason
    if _resolved_literal(elf, pendsv[2]) != tcb_pointer:
        return None, "scheduler-profile-mismatch:PendSV_Handler:tcb-literal"
    svc, reason = _decode_expected(elf, "SVC_Handler", svc_start, svc_end, svc_expected)
    if svc is None:
        return None, reason
    if _resolved_literal(elf, svc[0]) != tcb_pointer:
        return None, "scheduler-profile-mismatch:SVC_Handler:tcb-literal"
    start_first, reason = _decode_expected(
        elf, "prvPortStartFirstTask", start_start, start_end, start_expected
    )
    if start_first is None:
        return None, reason
    if _resolved_literal(elf, start_first[0]) != 0xE000ED08:
        return None, "scheduler-profile-mismatch:prvPortStartFirstTask:vtor"
    streams: dict[str, tuple[str, ...]] = {}
    for symbol, start, end, instructions in (
        ("PendSV_Handler", pendsv_start, pendsv_end, pendsv),
        ("SVC_Handler", svc_start, svc_end, svc),
        ("prvPortStartFirstTask", start_start, start_end, start_first),
        ("vTaskSwitchContext", switch_start, switch_end, None),
    ):
        stream, reason = _decode_stream(
            elf, symbol, start, end, _MAX_PROFILE_INSTRUCTIONS
        )
        if stream is None:
            return None, reason
        streams[symbol] = stream
        if instructions is not None:
            expected_tokens = tuple(
                _semantic_token(elf, instruction) for instruction in instructions
            )
            if stream[: len(expected_tokens)] != expected_tokens:
                return None, f"scheduler-profile-mismatch:{symbol}:stream"
    if streams["vTaskSwitchContext"] != _EXPECTED_SWITCH_TOKENS:
        return None, "scheduler-profile-mismatch:vTaskSwitchContext:stream"
    update = _find_switch_update(elf, switch_start, switch_end, tcb_pointer)
    if isinstance(update, str):
        return None, update
    _literal, store = update

    fingerprint = hashlib.sha256(
        "\n".join(token for symbol in names for token in streams[symbol]).encode(
            "utf-8"
        )
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


def _in_bounds(low: int, high: int, address: int, size: int) -> bool:
    return low <= address and address + size <= high


_HARDWARE_XPSR_OFFSET = 0x1C
_HARDWARE_FRAME_SIZE = 0x20


def _frame_restored_sp(
    *,
    top: int,
    context: SavedStackContext,
    frame_xpsr: int,
    stkalign: bool,
    hardware_frame: bool,
    low: int,
    high: int,
) -> tuple[int | None, str]:
    """Validate one frame's geometry and return the caller SP it restores.

    A hardware-stacked frame is placed by exception entry: with CCR.STKALIGN=1
    its base is 8-byte aligned and the stacked xPSR.ALIGN bit names the padding
    word that follows the basic frame; with CCR.STKALIGN=0 the base is only
    4-byte aligned and xPSR.ALIGN must be clear.  A first-start frame is built
    in software by ``pxPortInitialiseStack`` and is not placed by exception
    entry, so CCR.STKALIGN does not describe it; its builder always clears
    xPSR.ALIGN and there is no padding word.
    """
    if context.status_offset is None:
        return None, "scheduler-frame-layout-unproven"
    if context.sp_offset != context.status_offset + 4:
        return None, "scheduler-frame-layout-unproven"
    frame_base = top + context.status_offset - _HARDWARE_XPSR_OFFSET
    if not _in_bounds(low, high, frame_base, _HARDWARE_FRAME_SIZE):
        return None, "scheduler-frame-outside-task-stack"
    alignment = bool(frame_xpsr & XPSR_ALIGN_BIT)
    restored = top + context.sp_offset
    if not hardware_frame:
        if alignment or frame_base & 3:
            return None, "exception-frame-alignment-invalid"
        if not low <= restored <= high:
            return None, "scheduler-frame-outside-task-stack"
        return restored, "ok"
    if stkalign is None:
        return None, "exception-alignment-unavailable"
    if stkalign:
        if frame_base & 7:
            return None, "exception-frame-alignment-invalid"
    elif frame_base & 3:
        return None, "exception-frame-alignment-invalid"
    if alignment and not stkalign:
        return None, "exception-frame-alignment-invalid"
    sp = restored + (4 if alignment else 0)
    if stkalign:
        if sp & 7 != (4 if alignment else 0):
            return None, "exception-frame-alignment-invalid"
    elif sp & 3:
        return None, "exception-frame-alignment-invalid"
    if not low <= sp <= high:
        return None, "scheduler-frame-outside-task-stack"
    return sp, "ok"


def _decode_frame(
    *,
    top: int,
    core: Mapping[int, int],
    context: SavedStackContext,
    read: Reader,
    low: int,
    high: int,
    source: str,
    stkalign: bool,
    hardware_frame: bool,
) -> tuple[SeedFrame | None, int | None, str]:
    """Decode a saved core block plus the hardware frame above it."""
    span = context.context_span
    if span is None or not _in_bounds(low, high, top, span[1]):
        return None, None, "scheduler-frame-outside-task-stack"
    registers: dict[int, int] = {}
    for register, offset in context.registers:
        if register in core:
            registers[register] = core[register]
            continue
        value = read_slot(
            read,
            top + offset,
            context.pointer_size,
            "little",
            stack_low=low,
            stack_high=high,
        )
        if value is None:
            return None, None, "scheduler-context-unreadable"
        registers[register] = value
    if context.pc_offset is None or context.lr_offset is None:
        return None, None, "scheduler-context-unreadable"
    pc = read_slot(
        read,
        top + context.pc_offset,
        context.pointer_size,
        "little",
        stack_low=low,
        stack_high=high,
    )
    lr = read_slot(
        read,
        top + context.lr_offset,
        context.pointer_size,
        "little",
        stack_low=low,
        stack_high=high,
    )
    if pc is None or lr is None or context.status_offset is None:
        return None, None, "scheduler-context-unreadable"
    xpsr = read_slot(
        read,
        top + context.status_offset,
        4,
        "little",
        stack_low=low,
        stack_high=high,
    )
    if xpsr is None:
        return None, None, "scheduler-context-unreadable"
    if not xpsr & XPSR_T_BIT:
        return None, xpsr, "scheduler-frame-not-thumb"
    if xpsr & 0x1FF:
        return None, xpsr, "scheduler-frame-ipsr-invalid"
    sp, reason = _frame_restored_sp(
        top=top,
        context=context,
        frame_xpsr=xpsr,
        stkalign=stkalign,
        hardware_frame=hardware_frame,
        low=low,
        high=high,
    )
    if sp is None:
        return None, xpsr, reason
    if pc == 0 or pc & 1:
        return None, xpsr, "scheduler-frame-pc-invalid"
    registers.update({13: sp, 14: lr, 15: pc & ~1})
    return (
        SeedFrame(pc=pc & ~1, sp=sp, regs=registers, source=source),
        xpsr,
        "ok",
    )


def _saved_core_matches(
    *,
    read: Reader,
    top: int,
    context: SavedStackContext,
    registers: Mapping[int, int],
    low: int,
    high: int,
) -> bool:
    offsets = dict(context.registers)
    for register in _CORE_REGS:
        saved = read_slot(
            read,
            top + offsets[register],
            context.pointer_size,
            "little",
            stack_low=low,
            stack_high=high,
        )
        if saved is None or saved != registers.get(register):
            return False
    return True


def _saved_seed(
    *,
    context: SavedStackContext,
    top: int,
    read: Reader,
    low: int,
    high: int,
    exit_pc: int | None,
    first_start: bool,
    is_entry: Callable[[int], bool] | None,
    stkalign: bool,
    refuse_initial: bool = False,
) -> tuple[SeedFrame | None, str]:
    if context.status_offset is None:
        return None, "scheduler-frame-layout-unproven"
    frame_xpsr = read_slot(
        read,
        top + context.status_offset,
        4,
        "little",
        stack_low=low,
        stack_high=high,
    )
    if frame_xpsr is None:
        return None, "scheduler-context-unreadable"
    expected_sp, reason = _frame_restored_sp(
        top=top,
        context=context,
        frame_xpsr=frame_xpsr,
        stkalign=stkalign,
        hardware_frame=not first_start,
        low=low,
        high=high,
    )
    if expected_sp is None:
        return None, reason
    entry_pc = None
    if first_start:
        pc = read_slot(read, top + context.pc_offset, context.pointer_size, "little")
        if pc is None or is_entry is None or not is_entry(pc & ~1):
            return None, "scheduler-first-start-entry-unproven"
        entry_pc = pc & ~1
    seed, reason = decode_saved_context(
        context,
        top,
        read,
        endian="little",
        stack_low=low,
        stack_high=high,
        exit_pc=exit_pc,
        entry_pc=entry_pc,
    )
    if seed is None:
        return None, reason
    if seed.sp != expected_sp:
        return None, "scheduler-frame-layout-unproven"
    if first_start:
        if seed.source != "unstarted":
            return None, "scheduler-first-start-context-unproven"
        return (
            SeedFrame(
                pc=seed.pc,
                sp=seed.sp,
                regs=seed.regs,
                source="unstarted",
                root_pc=seed.pc,
                root_reason="task-entry",
            ),
            "ok",
        )
    if refuse_initial and _initial_pattern(
        seed,
        frame_xpsr=frame_xpsr,
        exit_pc=exit_pc,
        fill_word=context.fill_word,
    ):
        return None, "scheduler-unstarted-selection-unproven"
    return seed, "ok"


def _initial_pattern(
    seed: SeedFrame,
    *,
    frame_xpsr: int | None,
    exit_pc: int | None,
    fill_word: int,
) -> bool:
    if frame_xpsr != 0x01000000:
        return False
    if exit_pc is None or (seed.regs.get(14, 0) & ~1) != (exit_pc & ~1):
        return False
    return all(seed.regs.get(register, 0) == fill_word for register in _CORE_REGS)


def _looks_unstarted(
    seed: SeedFrame,
    *,
    frame_xpsr: int | None,
    exit_pc: int | None,
    fill_word: int,
    is_entry: Callable[[int], bool] | None,
) -> bool:
    if not _initial_pattern(
        seed,
        frame_xpsr=frame_xpsr,
        exit_pc=exit_pc,
        fill_word=fill_word,
    ):
        return False
    return is_entry is not None and is_entry(seed.pc)


def _initial_seed_ok(
    *,
    seed: SeedFrame,
    frame_xpsr: int | None,
    exit_pc: int | None,
    fill_word: int,
    is_entry: Callable[[int], bool] | None,
) -> bool:
    return _looks_unstarted(
        seed,
        frame_xpsr=frame_xpsr,
        exit_pc=exit_pc,
        fill_word=fill_word,
        is_entry=is_entry,
    )


def _require_exception_return(value: int, mode: str, stack: str) -> str | None:
    """Prove one basic EXC_RETURN against the stage's expected stack/mode."""
    decoded = decode_armv7m_exception_return(value)
    if isinstance(decoded, str):
        return decoded
    if decoded.mode != mode:
        return "scheduler-handler-mode-unproven"
    if decoded.stack != stack:
        return "scheduler-stack-selection-unproven"
    return None


def build_stage_seed(
    profile: SchedulerProfile,
    stage: SchedulerStage,
    *,
    read: Reader,
    context: SavedStackContext,
    task_tcb: int,
    task_top: int,
    task_low: int,
    task_high: int,
    psp: int,
    msp: int,
    registers: Mapping[int, int],
    xpsr: int,
    stkalign: bool,
    current_tcb: int,
    exit_pc: int | None,
    is_entry: Callable[[int], bool] | None,
) -> SchedulerSeed | str:
    """Build one owner-consistent seed from the stage's proven context.

    Returns the exact refusal reason when a register, buffer, bounds or owner
    invariant does not match the compiled sequence.  The live PSP is never
    used for a stage whose registers still belong to the previous task.
    """
    ipsr = xpsr & 0x1FF
    if stage.pc == profile.start_first_svc:
        return "scheduler-first-start-pending"
    if not xpsr & XPSR_T_BIT:
        return "scheduler-frame-not-thumb"
    if xpsr & XPSR_ALIGN_BIT and not stkalign:
        return "exception-frame-alignment-invalid"
    lr = registers.get(14, 0) & 0xFFFFFFFF
    if stage.pc in range(profile.pendsv_start, profile.pendsv_end):
        if ipsr != 14 or registers.get(13) != msp:
            return "scheduler-handler-mode-unproven"
        if stage.name != "pendsv-after-switch":
            reason = _require_exception_return(lr, "thread", "process")
            if reason is not None:
                return reason
    elif stage.pc in range(profile.switch_start, profile.switch_end):
        if ipsr != 14 or registers.get(13) != msp:
            return "scheduler-handler-mode-unproven"
        if stage.name == "switch-entry" and (lr & ~1) != (profile.switch_return & ~1):
            return "scheduler-switch-call-unproven"
    elif stage.pc in range(profile.svc_start, profile.svc_end):
        if ipsr != 11 or registers.get(13) != msp:
            return "scheduler-handler-mode-unproven"
        if stage.name == "svc-exception-return":
            reason = _require_exception_return(lr, "thread", "process")
        else:
            reason = _require_exception_return(lr, "thread", "main")
        if reason is not None:
            return reason
    else:
        return "scheduler-stage-ambiguous"
    if current_tcb != task_tcb:
        return "scheduler-current-tcb-mismatch"

    evidence = (
        f"stage={stage.name} seed={stage.seed} pc={stage.pc:#x}"
        f" profile={profile.profile_id}@{profile.fingerprint}"
        f" owner={stage.owner} current={current_tcb:#x} tcb={task_tcb:#x}"
        f" psp={psp:#x} msp={msp:#x} xpsr={xpsr:#010x}"
    )
    core = {register: registers[register] for register in _CORE_REGS}

    if stage.seed == "live-frame":
        if not _in_bounds(task_low, task_high, psp, 0x20):
            return "scheduler-psp-outside-task-stack"
        seed, _frame_xpsr, reason = _decode_frame(
            top=psp - 0x20,
            core=core,
            context=context,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "saved-buffer":
        saved_top = registers.get(0)
        if saved_top is None or saved_top != psp - 0x20:
            return "scheduler-saved-sp-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=saved_top,
            context=context,
            registers=registers,
            low=task_low,
            high=task_high,
        ):
            return "scheduler-saved-buffer-inconsistent"
        seed, _frame_xpsr, reason = _decode_frame(
            top=saved_top,
            core=core,
            context=context,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "saved-tcb":
        if task_top <= 0:
            return "scheduler-saved-context-unavailable"
        if stage.owner == "old" and task_top != psp - 0x20:
            return "scheduler-saved-sp-inconsistent"
        if stage.name == "pendsv-select" and registers.get(1) != task_tcb:
            return "scheduler-new-tcb-inconsistent"
        if stage.name == "pendsv-saved-sp" and registers.get(0) != task_top:
            return "scheduler-new-tcb-inconsistent"
        seed, reason = _saved_seed(
            context=context,
            top=task_top,
            read=read,
            low=task_low,
            high=task_high,
            exit_pc=exit_pc,
            first_start=False,
            is_entry=is_entry,
            stkalign=stkalign,
            refuse_initial=stage.owner == "new",
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "restore-buffer":
        if registers.get(0) != task_top + 0x20:
            return "scheduler-restore-window-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=context,
            registers=registers,
            low=task_low,
            high=task_high,
        ):
            return "scheduler-restore-window-inconsistent"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=core,
            context=context,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        if _initial_pattern(
            seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=context.fill_word,
        ):
            return "scheduler-unstarted-selection-unproven"
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "live-frame-new":
        if psp != task_top + 0x20:
            return "scheduler-psp-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=context,
            registers=registers,
            low=task_low,
            high=task_high,
        ):
            return "scheduler-restore-window-inconsistent"
        if (
            stage.name == "pendsv-exception-return"
            and registers.get(14) != EXC_RETURN_THREAD_PROCESS
        ):
            return "scheduler-exception-return-unproven"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=core,
            context=context,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=True,
        )
        if seed is None:
            return reason
        if _initial_pattern(
            seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=context.fill_word,
        ):
            return "scheduler-unstarted-selection-unproven"
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "first-start-saved":
        seed, reason = _saved_seed(
            context=context,
            top=task_top,
            read=read,
            low=task_low,
            high=task_high,
            exit_pc=exit_pc,
            first_start=True,
            is_entry=is_entry,
            stkalign=stkalign,
        )
        if seed is None:
            return reason
        return SchedulerSeed(seed, stage.name, stage.owner, evidence)

    if stage.seed == "first-start-buffer":
        if registers.get(0) != task_top + 0x20:
            return "scheduler-restore-window-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=context,
            registers=registers,
            low=task_low,
            high=task_high,
        ):
            return "scheduler-saved-buffer-inconsistent"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=core,
            context=context,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=False,
        )
        if seed is None:
            return reason
        if not _initial_seed_ok(
            seed=seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=context.fill_word,
            is_entry=is_entry,
        ):
            if is_entry is None or not is_entry(seed.pc):
                return "scheduler-first-start-entry-unproven"
            return "scheduler-first-start-context-unproven"
        return SchedulerSeed(
            SeedFrame(
                pc=seed.pc,
                sp=seed.sp,
                regs=seed.regs,
                source="unstarted",
                root_pc=seed.pc,
                root_reason="task-entry",
            ),
            stage.name,
            stage.owner,
            evidence,
        )

    if stage.seed == "first-start-live":
        if psp != task_top + 0x20:
            return "scheduler-psp-inconsistent"
        if not _saved_core_matches(
            read=read,
            top=task_top,
            context=context,
            registers=registers,
            low=task_low,
            high=task_high,
        ):
            return "scheduler-saved-buffer-inconsistent"
        if (
            stage.name == "svc-exception-return"
            and registers.get(14) != EXC_RETURN_THREAD_PROCESS
        ):
            return "scheduler-exception-return-unproven"
        seed, frame_xpsr, reason = _decode_frame(
            top=task_top,
            core=core,
            context=context,
            read=read,
            low=task_low,
            high=task_high,
            source="current",
            stkalign=stkalign,
            hardware_frame=False,
        )
        if seed is None:
            return reason
        if not _initial_seed_ok(
            seed=seed,
            frame_xpsr=frame_xpsr,
            exit_pc=exit_pc,
            fill_word=context.fill_word,
            is_entry=is_entry,
        ):
            if is_entry is None or not is_entry(seed.pc):
                return "scheduler-first-start-entry-unproven"
            return "scheduler-first-start-context-unproven"
        return SchedulerSeed(
            SeedFrame(
                pc=seed.pc,
                sp=seed.sp,
                regs=seed.regs,
                source="unstarted",
                root_pc=seed.pc,
                root_reason="task-entry",
            ),
            stage.name,
            stage.owner,
            evidence,
        )

    return "scheduler-stage-ambiguous"
