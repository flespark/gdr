"""Compiled Arm M-Profile scheduler-switch stage verification for RT-Thread.

The module inspects compiled ``rt_hw_context_switch``, ``PendSV_Handler`` and
``rt_hw_context_switch_to`` instructions in the loaded ELF through
:mod:`core.insn_thumb` and refuses unrecognised code sequences instead of
hard-coding offsets.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.cpus import Isa, cpu_of
from core.insn_thumb import ThumbInstruction, decode_thumb
from core.unwind_meta import ElfImage

from .stack_frame_profile import _code_span, _operand, _port_instructions

_ICSR_ADDRESS = 0xE000ED04
_PENDSVSET_VALUE = 0x10000000
_CORE_CALLEE_REGLIST = 0x0FF0  # r4..r11
_LOW_CALLEE_REGLIST = 0x00F0  # r4..r7
_PRIMASK_SYSM = 16
_PSP_SYSM = 9
_PSPLIM_SYSM = 11
_CONTROL_SYSM = 20
_MSP_SYSM = 8


@dataclass(frozen=True)
class SchedulerStage:
    """One verified PC inside RT-Thread's M-Profile scheduler transition."""

    name: str
    function: str
    detail: str


@dataclass(frozen=True)
class SchedulerProfile:
    """Verified addresses and stages of RT-Thread's M-Profile switch code."""

    port: str
    isa: Isa
    fpu: bool
    switch_start: int
    switch_end: int
    switch_return: int
    pendsv_start: int
    pendsv_end: int
    switch_to_start: int
    switch_to_end: int
    pendsv_entry: int
    pendsv_save_pending: int
    pendsv_save_written: int | None
    pendsv_switch_to: int
    pendsv_saved_sp: int
    pendsv_restored_pending_psp: int
    pendsv_psp_written: int
    pendsv_exception_return: int
    flag_symbol: int
    from_symbol: int
    to_symbol: int
    trustzone_symbol: int | None
    stages: dict[int, SchedulerStage]


@dataclass(frozen=True)
class _Symbols:
    flag: int
    from_ptr: int
    to_ptr: int
    trustzone: int | None


def _literal_word(elf: ElfImage, instruction: ThumbInstruction) -> int | None:
    if instruction.mnemonic != "ldr_literal":
        return None
    target = _operand(instruction, "literal")
    if target is None:
        return None
    raw = elf.read_at(target, 4)
    if raw is None or len(raw) != 4:
        return None
    return int.from_bytes(raw, "little")


def _expect_literal(
    elf: ElfImage,
    instruction: ThumbInstruction,
    expected_value: int,
    *,
    rt: int | None = None,
) -> bool:
    if rt is not None and _operand(instruction, "rt") != rt:
        return False
    return _literal_word(elf, instruction) == expected_value


def _expect(
    instruction: ThumbInstruction,
    mnemonic: str,
    **operands: object,
) -> bool:
    if instruction.mnemonic != mnemonic:
        return False
    for key, expected in operands.items():
        if instruction.operands.get(key) != expected:
            return False
    return True


def _resolve_symbols(elf: ElfImage) -> _Symbols | None:
    flag = elf.symbol_address("rt_thread_switch_interrupt_flag")
    from_ptr = elf.symbol_address("rt_interrupt_from_thread")
    to_ptr = elf.symbol_address("rt_interrupt_to_thread")
    if flag is None or from_ptr is None or to_ptr is None:
        return None
    return _Symbols(
        flag=flag,
        from_ptr=from_ptr,
        to_ptr=to_ptr,
        trustzone=elf.symbol_address("rt_trustzone_current_context"),
    )


def _decode_thumb(elf: ElfImage, address: int, end: int) -> ThumbInstruction | str:
    return decode_thumb(elf, address, end)


def _verify_context_switch(
    elf: ElfImage,
    instructions: tuple[ThumbInstruction, ...],
    syms: _Symbols,
    *,
    isa: Isa,
) -> int | None:
    """Verify ``rt_hw_context_switch`` and return the ``bx lr`` return PC."""
    if len(instructions) != 14:
        return None
    i = instructions
    checks = (
        _expect_literal(elf, i[0], syms.flag, rt=2),
        _expect(i[1], "ldr_imm", rt=3, rn=2, imm=0),
        _expect(i[2], "cmp", rn=3, imm=1),
        _expect(i[3], "b_cond", cond="eq", target=i[8].address),
        _expect(i[4], "mov_imm", rd=3, imm=1),
        _expect(i[5], "str_imm", rt=3, rn=2, imm=0),
        _expect_literal(elf, i[6], syms.from_ptr, rt=2),
        _expect(i[7], "str_imm", rt=0, rn=2, imm=0),
        _expect_literal(elf, i[8], syms.to_ptr, rt=2),
        _expect(i[9], "str_imm", rt=1, rn=2, imm=0),
        _expect_literal(elf, i[10], _ICSR_ADDRESS, rt=0),
        (
            _expect_literal(elf, i[11], _PENDSVSET_VALUE, rt=1)
            if isa == "armv6-m"
            else _expect(i[11], "mov_imm", rd=1, imm=_PENDSVSET_VALUE)
        ),
        _expect(i[12], "str_imm", rt=1, rn=0, imm=0),
        _expect(i[13], "bx", rm=14),
    )
    return i[13].address if all(checks) else None


def _verify_context_switch_to(elf: ElfImage, syms: _Symbols) -> tuple[int, int] | None:
    """Decode and verify bounded ``rt_hw_context_switch_to`` span up to ``bx lr``."""
    start = elf.symbol_address("rt_hw_context_switch_to")
    if start is None or elf.endian != "little" or not elf.is_code(start):
        return None
    instructions: list[ThumbInstruction] = []
    address = start
    limit = start + 128
    while address < limit and len(instructions) < 40:
        decoded = decode_thumb(elf, address, limit)
        if isinstance(decoded, str) or decoded.mnemonic == "other":
            return None
        instructions.append(decoded)
        if _expect(decoded, "bx", rm=14):
            break
        address += decoded.size
    if not instructions or not _expect(instructions[-1], "bx", rm=14):
        return None
    literals = {
        word for insn in instructions if (word := _literal_word(elf, insn)) is not None
    }
    if not {syms.to_ptr, syms.from_ptr, syms.flag, _ICSR_ADDRESS}.issubset(literals):
        return None
    saw_msp_reset = any(_expect(insn, "msr", sysm=_MSP_SYSM) for insn in instructions)
    if not saw_msp_reset:
        return None
    last = instructions[-1]
    return start, last.address + last.size


def _make_stages(
    switch_return: int,
    *,
    entry: int,
    save_pending: int,
    save_written: int | None,
    switch_to: int,
    saved_sp: int,
    restored_pending_psp: int,
    psp_written: int,
    exception_return: int,
) -> dict[int, SchedulerStage]:
    stages: dict[int, SchedulerStage] = {
        switch_return: SchedulerStage(
            name="switch-return",
            function="rt_hw_context_switch",
            detail="rt_hw_context_switch completed setting PendSV and returns",
        ),
        entry: SchedulerStage(
            name="pendsv-entry",
            function="PendSV_Handler",
            detail="PendSV entry before disabling interrupts or modifying stack state",
        ),
        save_pending: SchedulerStage(
            name="pendsv-save-pending",
            function="PendSV_Handler",
            detail="PendSV read PSP before saving outgoing task registers",
        ),
        switch_to: SchedulerStage(
            name="pendsv-switch-to",
            function="PendSV_Handler",
            detail="PendSV outgoing state settled; preparing to load incoming task",
        ),
        saved_sp: SchedulerStage(
            name="pendsv-saved-sp",
            function="PendSV_Handler",
            detail="PendSV holds incoming task saved SP before register restore",
        ),
        restored_pending_psp: SchedulerStage(
            name="pendsv-restored-pending-psp",
            function="PendSV_Handler",
            detail="PendSV restored incoming software registers; PSP write is pending",
        ),
        psp_written: SchedulerStage(
            name="pendsv-psp-written",
            function="PendSV_Handler",
            detail="PendSV updated PSP to incoming task hardware frame",
        ),
        exception_return: SchedulerStage(
            name="pendsv-exception-return",
            function="PendSV_Handler",
            detail="PendSV is at exception-return instruction with restored registers",
        ),
    }
    if save_written is not None:
        stages[save_written] = SchedulerStage(
            name="pendsv-save-written",
            function="PendSV_Handler",
            detail="PendSV wrote outgoing software context buffer prior to sp writeback",
        )
    return stages


def _match_armv6m(
    elf: ElfImage,
    p: tuple[ThumbInstruction, ...],
    syms: _Symbols,
    switch_span: tuple[int, int],
    switch_return: int,
    pendsv_span: tuple[int, int],
    switch_to_span: tuple[int, int],
) -> SchedulerProfile | None:
    if len(p) != 38:
        return None
    checks = (
        _expect(p[0], "mrs", rd=2, sysm=_PRIMASK_SYSM),
        _expect(p[1], "cpsid", flag="i"),
        _expect_literal(elf, p[2], syms.flag, rt=0),
        _expect(p[3], "ldr_imm", rt=1, rn=0, imm=0),
        _expect(p[4], "cmp", rn=1, imm=0),
        _expect(p[5], "b_cond", cond="eq", target=p[34].address),
        _expect(p[6], "mov_imm", rd=1, imm=0),
        _expect(p[7], "str_imm", rt=1, rn=0, imm=0),
        _expect_literal(elf, p[8], syms.from_ptr, rt=0),
        _expect(p[9], "ldr_imm", rt=1, rn=0, imm=0),
        _expect(p[10], "cmp", rn=1, imm=0),
        _expect(p[11], "b_cond", cond="eq", target=p[22].address),
        _expect(p[12], "mrs", rd=1, sysm=_PSP_SYSM),
        _expect(p[13], "subs", rd=1, rn=1, imm=32),
        _expect(p[14], "ldr_imm", rt=0, rn=0, imm=0),
        _expect(p[15], "str_imm", rt=1, rn=0, imm=0),
        _expect(p[16], "stmia", rn=1, reglist=_LOW_CALLEE_REGLIST),
        _expect(p[21], "stmia", rn=1, reglist=_LOW_CALLEE_REGLIST),
        _expect_literal(elf, p[22], syms.to_ptr, rt=1),
        _expect(p[23], "ldr_imm", rt=1, rn=1, imm=0),
        _expect(p[24], "ldr_imm", rt=1, rn=1, imm=0),
        _expect(p[25], "ldmia", rn=1, reglist=_LOW_CALLEE_REGLIST),
        _expect(p[26], "push", reglist=_LOW_CALLEE_REGLIST),
        _expect(p[27], "ldmia", rn=1, reglist=_LOW_CALLEE_REGLIST),
        _expect(p[32], "pop", reglist=_LOW_CALLEE_REGLIST),
        _expect(p[33], "msr", rn=1, sysm=_PSP_SYSM),
        _expect(p[34], "msr", rn=2, sysm=_PRIMASK_SYSM),
        _expect(p[35], "mov_imm", rd=0, imm=3),
        _expect(p[36], "rsbs", rd=0, rn=0),
        _expect(p[37], "bx", rm=0),
    )
    if not all(checks):
        return None
    stages = _make_stages(
        switch_return,
        entry=p[0].address,
        save_pending=p[12].address,
        save_written=None,
        switch_to=p[22].address,
        saved_sp=p[25].address,
        restored_pending_psp=p[33].address,
        psp_written=p[34].address,
        exception_return=p[37].address,
    )
    return SchedulerProfile(
        port="armv6-m",
        isa="armv6-m",
        fpu=False,
        switch_start=switch_span[0],
        switch_end=switch_span[1],
        switch_return=switch_return,
        pendsv_start=pendsv_span[0],
        pendsv_end=pendsv_span[1],
        switch_to_start=switch_to_span[0],
        switch_to_end=switch_to_span[1],
        pendsv_entry=p[0].address,
        pendsv_save_pending=p[12].address,
        pendsv_save_written=None,
        pendsv_switch_to=p[22].address,
        pendsv_saved_sp=p[25].address,
        pendsv_restored_pending_psp=p[33].address,
        pendsv_psp_written=p[34].address,
        pendsv_exception_return=p[37].address,
        flag_symbol=syms.flag,
        from_symbol=syms.from_ptr,
        to_symbol=syms.to_ptr,
        trustzone_symbol=None,
        stages=stages,
    )


def _match_armv7m(
    elf: ElfImage,
    p: tuple[ThumbInstruction, ...],
    syms: _Symbols,
    switch_span: tuple[int, int],
    switch_return: int,
    pendsv_span: tuple[int, int],
    switch_to_span: tuple[int, int],
    *,
    isa: Isa = "armv7-m",
) -> SchedulerProfile | None:
    if len(p) != 22:
        return None
    checks = (
        _expect(p[0], "mrs", rd=2, sysm=_PRIMASK_SYSM),
        _expect(p[1], "cpsid", flag="i"),
        _expect_literal(elf, p[2], syms.flag, rt=0),
        _expect(p[3], "ldr_imm", rt=1, rn=0, imm=0),
        _expect(p[4], "cbz", rn=1, target=p[19].address),
        _expect(p[5], "mov_imm", rd=1, imm=0),
        _expect(p[6], "str_imm", rt=1, rn=0, imm=0),
        _expect_literal(elf, p[7], syms.from_ptr, rt=0),
        _expect(p[8], "ldr_imm", rt=1, rn=0, imm=0),
        _expect(p[9], "cbz", rn=1, target=p[14].address),
        _expect(p[10], "mrs", rd=1, sysm=_PSP_SYSM),
        _expect(p[11], "stmdb", rn=1, reglist=_CORE_CALLEE_REGLIST),
        _expect(p[12], "ldr_imm", rt=0, rn=0, imm=0),
        _expect(p[13], "str_imm", rt=1, rn=0, imm=0),
        _expect_literal(elf, p[14], syms.to_ptr, rt=1),
        _expect(p[15], "ldr_imm", rt=1, rn=1, imm=0),
        _expect(p[16], "ldr_imm", rt=1, rn=1, imm=0),
        _expect(p[17], "ldmia", rn=1, reglist=_CORE_CALLEE_REGLIST),
        _expect(p[18], "msr", rn=1, sysm=_PSP_SYSM),
        _expect(p[19], "msr", rn=2, sysm=_PRIMASK_SYSM),
        _expect(p[20], "orr_imm", rd=14, rn=14, imm=4),
        _expect(p[21], "bx", rm=14),
    )
    if not all(checks):
        return None
    stages = _make_stages(
        switch_return,
        entry=p[0].address,
        save_pending=p[10].address,
        save_written=p[13].address,
        switch_to=p[14].address,
        saved_sp=p[17].address,
        restored_pending_psp=p[18].address,
        psp_written=p[19].address,
        exception_return=p[21].address,
    )
    return SchedulerProfile(
        port="armv7-m",
        isa=isa,
        fpu=False,
        switch_start=switch_span[0],
        switch_end=switch_span[1],
        switch_return=switch_return,
        pendsv_start=pendsv_span[0],
        pendsv_end=pendsv_span[1],
        switch_to_start=switch_to_span[0],
        switch_to_end=switch_to_span[1],
        pendsv_entry=p[0].address,
        pendsv_save_pending=p[10].address,
        pendsv_save_written=p[13].address,
        pendsv_switch_to=p[14].address,
        pendsv_saved_sp=p[17].address,
        pendsv_restored_pending_psp=p[18].address,
        pendsv_psp_written=p[19].address,
        pendsv_exception_return=p[21].address,
        flag_symbol=syms.flag,
        from_symbol=syms.from_ptr,
        to_symbol=syms.to_ptr,
        trustzone_symbol=None,
        stages=stages,
    )


def _match_armv7em_fpu(
    elf: ElfImage,
    p: tuple[ThumbInstruction, ...],
    syms: _Symbols,
    switch_span: tuple[int, int],
    switch_return: int,
    pendsv_span: tuple[int, int],
    switch_to_span: tuple[int, int],
) -> SchedulerProfile | None:
    if len(p) != 38:
        return None
    checks = (
        _expect(p[0], "mrs", rd=2, sysm=_PRIMASK_SYSM),
        _expect(p[1], "cpsid", flag="i"),
        _expect_literal(elf, p[2], syms.flag, rt=0),
        _expect(p[3], "ldr_imm", rt=1, rn=0, imm=0),
        _expect(p[4], "cbz", rn=1, target=p[35].address),
        _expect(p[5], "mov_imm", rd=1, imm=0),
        _expect(p[6], "str_imm", rt=1, rn=0, imm=0),
        _expect_literal(elf, p[7], syms.from_ptr, rt=0),
        _expect(p[8], "ldr_imm", rt=1, rn=0, imm=0),
        _expect(p[9], "cbz", rn=1, target=p[22].address),
        _expect(p[10], "mrs", rd=1, sysm=_PSP_SYSM),
        _expect(p[11], "tst_imm", rn=14, imm=16),
        _expect(p[12], "it"),
        _expect(p[13], "vstmdb", rn=1, first=8, count=8),
        _expect(p[14], "stmdb", rn=1, reglist=_CORE_CALLEE_REGLIST),
        _expect(p[19], "str_pre", rt=4, rn=1, imm=-4),
        _expect(p[20], "ldr_imm", rt=0, rn=0, imm=0),
        _expect(p[21], "str_imm", rt=1, rn=0, imm=0),
        _expect_literal(elf, p[22], syms.to_ptr, rt=1),
        _expect(p[23], "ldr_imm", rt=1, rn=1, imm=0),
        _expect(p[24], "ldr_imm", rt=1, rn=1, imm=0),
        _expect(p[25], "ldmia", rn=1, reglist=8),
        _expect(p[26], "ldmia", rn=1, reglist=_CORE_CALLEE_REGLIST),
        _expect(p[27], "cmp", rn=3, imm=0),
        _expect(p[28], "it"),
        _expect(p[29], "vldmia", rn=1, first=8, count=8),
        _expect(p[30], "msr", rn=1, sysm=_PSP_SYSM),
        _expect(p[31], "orr_imm", rd=14, rn=14, imm=16),
        _expect(p[34], "bic_imm", rd=14, rn=14, imm=16),
        _expect(p[35], "msr", rn=2, sysm=_PRIMASK_SYSM),
        _expect(p[36], "orr_imm", rd=14, rn=14, imm=4),
        _expect(p[37], "bx", rm=14),
    )
    if not all(checks):
        return None
    stages = _make_stages(
        switch_return,
        entry=p[0].address,
        save_pending=p[10].address,
        save_written=p[21].address,
        switch_to=p[22].address,
        saved_sp=p[25].address,
        restored_pending_psp=p[30].address,
        psp_written=p[31].address,
        exception_return=p[37].address,
    )
    return SchedulerProfile(
        port="armv7e-m-fpu",
        isa="armv7e-m",
        fpu=True,
        switch_start=switch_span[0],
        switch_end=switch_span[1],
        switch_return=switch_return,
        pendsv_start=pendsv_span[0],
        pendsv_end=pendsv_span[1],
        switch_to_start=switch_to_span[0],
        switch_to_end=switch_to_span[1],
        pendsv_entry=p[0].address,
        pendsv_save_pending=p[10].address,
        pendsv_save_written=p[21].address,
        pendsv_switch_to=p[22].address,
        pendsv_saved_sp=p[25].address,
        pendsv_restored_pending_psp=p[30].address,
        pendsv_psp_written=p[31].address,
        pendsv_exception_return=p[37].address,
        flag_symbol=syms.flag,
        from_symbol=syms.from_ptr,
        to_symbol=syms.to_ptr,
        trustzone_symbol=None,
        stages=stages,
    )


def _match_armv8m(
    elf: ElfImage,
    p: tuple[ThumbInstruction, ...],
    syms: _Symbols,
    switch_span: tuple[int, int],
    switch_return: int,
    pendsv_span: tuple[int, int],
    switch_to_span: tuple[int, int],
    *,
    fpu: bool,
) -> SchedulerProfile | None:
    if syms.trustzone is None:
        return None
    if not fpu:
        if len(p) != 64:
            return None
        s_off = 0  # offset after p[32] stmdb r4..r11
        r_off = 0  # offset after p[59] ldmia r4..r11
    else:
        if len(p) != 70:
            return None
        s_off = 3
        r_off = 3
    prefix_ok = all(
        (
            _expect(p[0], "mrs", rd=2, sysm=_PRIMASK_SYSM),
            _expect(p[1], "cpsid", flag="i"),
            _expect_literal(elf, p[2], syms.flag, rt=0),
            _expect(p[3], "ldr_imm", rt=1, rn=0, imm=0),
            _expect(p[4], "cmp", rn=1, imm=0),
            _expect(p[5], "b_cond", cond="ne", target=p[8].address),
            _expect(p[6], "msr", rn=2, sysm=_PRIMASK_SYSM),
            _expect(p[7], "bx", rm=14),
            _expect(p[8], "push", reglist=4),
            _expect(p[9], "mov_imm", rd=1, imm=0),
            _expect(p[10], "str_imm", rt=1, rn=0, imm=0),
            _expect_literal(elf, p[11], syms.from_ptr, rt=0),
            _expect(p[12], "ldr_imm", rt=1, rn=0, imm=0),
            _expect(p[13], "cbz", rn=1, target=p[41 + s_off].address),
            _expect_literal(elf, p[14], syms.trustzone, rt=1),
            _expect(p[15], "ldr_imm", rt=1, rn=1, imm=0),
            _expect(p[16], "cbz", rn=1, target=p[31].address),
            _expect(p[31], "mrs", rd=1, sysm=_PSP_SYSM),
        )
    )
    if not prefix_ok:
        return None
    if fpu:
        save_ok = all(
            (
                _expect(p[32], "tst_imm", rn=14, imm=16),
                _expect(p[33], "it"),
                _expect(p[34], "vstmdb", rn=1, first=8, count=8),
                _expect(p[35], "stmdb", rn=1, reglist=_CORE_CALLEE_REGLIST),
            )
        )
    else:
        save_ok = _expect(p[32], "stmdb", rn=1, reglist=_CORE_CALLEE_REGLIST)
    if not save_ok:
        return None
    body_ok = all(
        (
            _expect_literal(elf, p[33 + s_off], syms.trustzone, rt=2),
            _expect(p[34 + s_off], "ldr_imm", rt=2, rn=2, imm=0),
            _expect(p[35 + s_off], "mov", rd=3, rm=14),
            _expect(p[36 + s_off], "mrs", rd=4, sysm=_PSPLIM_SYSM),
            _expect(p[37 + s_off], "mrs", rd=5, sysm=_CONTROL_SYSM),
            _expect(p[38 + s_off], "stmdb", rn=1, reglist=60),  # r2..r5
            _expect(p[39 + s_off], "ldr_imm", rt=0, rn=0, imm=0),
            _expect(p[40 + s_off], "str_imm", rt=1, rn=0, imm=0),
            _expect_literal(elf, p[41 + s_off], syms.to_ptr, rt=1),
            _expect(p[42 + s_off], "ldr_imm", rt=1, rn=1, imm=0),
            _expect(p[43 + s_off], "ldr_imm", rt=1, rn=1, imm=0),
            _expect(p[44 + s_off], "ldmia", rn=1, reglist=60),  # r2..r5
            _expect(p[45 + s_off], "msr", rn=4, sysm=_PSPLIM_SYSM),
            _expect(p[46 + s_off], "msr", rn=5, sysm=_CONTROL_SYSM),
            _expect(p[47 + s_off], "mov", rd=14, rm=3),
            _expect_literal(elf, p[48 + s_off], syms.trustzone, rt=6),
            _expect(p[49 + s_off], "str_imm", rt=2, rn=6, imm=0),
            _expect(p[50 + s_off], "mov", rd=0, rm=2),
            _expect(p[51 + s_off], "cbz", rn=0, target=p[59 + s_off].address),
            _expect(p[59 + s_off], "ldmia", rn=1, reglist=_CORE_CALLEE_REGLIST),
        )
    )
    if not body_ok:
        return None
    if fpu:
        tail_ok = all(
            (
                _expect(p[63], "tst_imm", rn=14, imm=16),
                _expect(p[64], "it"),
                _expect(p[65], "vldmia", rn=1, first=8, count=8),
            )
        )
        if not tail_ok:
            return None
    tail_common_ok = all(
        (
            _expect(p[60 + s_off + r_off], "msr", rn=1, sysm=_PSP_SYSM),
            _expect(p[61 + s_off + r_off], "pop", reglist=4),
            _expect(p[62 + s_off + r_off], "msr", rn=2, sysm=_PRIMASK_SYSM),
            _expect(p[63 + s_off + r_off], "bx", rm=14),
        )
    )
    if not tail_common_ok:
        return None

    save_written_pc = p[40 + s_off].address
    switch_to_pc = p[41 + s_off].address
    saved_sp_pc = p[44 + s_off].address
    restored_psp_pc = p[60 + s_off + r_off].address
    psp_written_pc = p[61 + s_off + r_off].address
    exception_return_pc = p[63 + s_off + r_off].address
    stages = _make_stages(
        switch_return,
        entry=p[0].address,
        save_pending=p[31].address,
        save_written=save_written_pc,
        switch_to=switch_to_pc,
        saved_sp=saved_sp_pc,
        restored_pending_psp=restored_psp_pc,
        psp_written=psp_written_pc,
        exception_return=exception_return_pc,
    )
    return SchedulerProfile(
        port="armv8m-fpu" if fpu else "armv8m-integer",
        isa="armv8-m.mainline",
        fpu=fpu,
        switch_start=switch_span[0],
        switch_end=switch_span[1],
        switch_return=switch_return,
        pendsv_start=pendsv_span[0],
        pendsv_end=pendsv_span[1],
        switch_to_start=switch_to_span[0],
        switch_to_end=switch_to_span[1],
        pendsv_entry=p[0].address,
        pendsv_save_pending=p[31].address,
        pendsv_save_written=save_written_pc,
        pendsv_switch_to=switch_to_pc,
        pendsv_saved_sp=saved_sp_pc,
        pendsv_restored_pending_psp=restored_psp_pc,
        pendsv_psp_written=psp_written_pc,
        pendsv_exception_return=exception_return_pc,
        flag_symbol=syms.flag,
        from_symbol=syms.from_ptr,
        to_symbol=syms.to_ptr,
        trustzone_symbol=syms.trustzone,
        stages=stages,
    )


def build_profile(elf: ElfImage | None) -> SchedulerProfile | None:
    """Prove compiled RT-Thread M-Profile switch handlers in ``elf``."""
    if elf is None:
        return None
    cpu = cpu_of(elf)
    if cpu.isa not in ("armv6-m", "armv7-m", "armv7e-m", "armv8-m.mainline"):
        return None
    syms = _resolve_symbols(elf)
    if syms is None:
        return None
    switch_span = _code_span(elf, "rt_hw_context_switch", "PendSV_Handler")
    pendsv_span = _code_span(elf, "PendSV_Handler", "rt_hw_context_switch_to")
    switch_to_span = _verify_context_switch_to(elf, syms)
    if switch_span is None or pendsv_span is None or switch_to_span is None:
        return None
    switch_insns = _port_instructions(
        elf,
        "rt_hw_context_switch",
        "PendSV_Handler",
        _decode_thumb,
        stop_at_return=True,
    )
    pendsv_insns = _port_instructions(
        elf, "PendSV_Handler", "rt_hw_context_switch_to", _decode_thumb
    )
    if switch_insns is None or pendsv_insns is None:
        return None
    switch_return = _verify_context_switch(elf, switch_insns, syms, isa=cpu.isa)
    if switch_return is None:
        return None
    args = (
        elf,
        pendsv_insns,
        syms,
        switch_span,
        switch_return,
        pendsv_span,
        switch_to_span,
    )
    if cpu.isa == "armv6-m":
        return _match_armv6m(*args)
    if cpu.isa == "armv7-m":
        return _match_armv7m(*args)
    if cpu.isa == "armv7e-m":
        if len(pendsv_insns) == 38:
            return _match_armv7em_fpu(*args)
        if len(pendsv_insns) == 22:
            return _match_armv7m(*args, isa="armv7e-m")
        return None
    if len(pendsv_insns) == 70:
        return _match_armv8m(*args, fpu=True)
    if len(pendsv_insns) == 64:
        return _match_armv8m(*args, fpu=False)
    return None


def classify_stage(profile: SchedulerProfile, pc: int) -> SchedulerStage | None:
    """Classify ``pc`` against verified scheduler transition stages."""
    code_pc = pc & ~1
    stage = profile.stages.get(code_pc)
    if stage is not None:
        return stage
    if profile.switch_start <= code_pc < profile.switch_end:
        return SchedulerStage(
            name="ambiguous",
            function="rt_hw_context_switch",
            detail="pc is inside rt_hw_context_switch before completion",
        )
    if profile.pendsv_start <= code_pc < profile.pendsv_end:
        return SchedulerStage(
            name="ambiguous",
            function="PendSV_Handler",
            detail="pc is at an unverified or mid-transition PendSV_Handler instruction",
        )
    if profile.switch_to_start <= code_pc < profile.switch_to_end:
        return SchedulerStage(
            name="ambiguous",
            function="rt_hw_context_switch_to",
            detail="pc is inside rt_hw_context_switch_to before first PendSV",
        )
    return None
