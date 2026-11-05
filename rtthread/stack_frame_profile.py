"""RT-Thread saved-frame profile selection from ELF evidence."""

from __future__ import annotations

from collections.abc import Callable

from core.cpus import Cpu, cpu_of
from core.insn_arm import ArmInstruction, decode_arm
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_all_store_offsets,
    riscv_frame_accesses,
    riscv_slot_map,
    riscv_stack_frame_size,
)
from core.insn_thumb import ThumbInstruction, decode_thumb
from core.stack_frame_parser import (
    SavedStackContext,
    StackContextSelection,
)
from core.unwind_meta import ElfImage

from .backtrace import (
    ARMV6M_INTEGER,
    ARMV7A_LWP_INTEGER,
    ARMV7M_INTEGER,
    RISCV32_INTEGER,
    RISCV64_INTEGER,
)

# --- Common -----------------------------------------------------------------


def select_context(elf: ElfImage) -> StackContextSelection:
    """Recognize ISA separately from layout proof and closed-loop verification."""
    cpu = cpu_of(elf)
    if cpu.reason != "ok":
        return _selection_result(cpu, None, cpu.reason)
    if cpu.family == "riscv":
        return _select_riscv_context(elf, cpu)
    return _select_arm_context(elf, cpu)


def _selection_result(
    cpu: Cpu,
    context: SavedStackContext | None,
    reason: str,
    evidence: tuple[str, ...] = (),
) -> StackContextSelection:
    return StackContextSelection(
        cpu.isa, cpu.family, context, reason, evidence, cpu.abi
    )


# --- RISC-V -----------------------------------------------------------------


def _select_riscv_context(elf: ElfImage, cpu: Cpu) -> StackContextSelection:
    """Select and verify the RISC-V integer profile from compiled libcpu code."""
    context = _riscv_profile(cpu.isa)
    if context is None or not context.verified:
        return _selection_result(cpu, None, "unverified-port")
    if not _riscv_verify_context_switch(elf):
        return _selection_result(cpu, None, "unrecognizable-stack-frame-layout")
    return _selection_result(
        cpu,
        context,
        "ok",
        (".riscv.attributes", "ELF e_flags", "compiled libcpu integer save/restore"),
    )


def _riscv_profile(isa: str | None) -> SavedStackContext | None:
    """Return the integer profile for one canonical RISC-V ISA."""
    return {
        "riscv32": RISCV32_INTEGER,
        "riscv64": RISCV64_INTEGER,
    }.get(isa or "")


def _riscv_verify_context_switch(elf: ElfImage) -> bool:
    """Compare RT-Thread context-switch frame effects and restore control flow."""
    if elf.pointer_size != 8:
        return False
    word = elf.pointer_size
    try:
        switch = RiscvCodeParser(
            elf, "rt_hw_context_switch", "rt_hw_context_switch_exit"
        ).instructions()
        frame_size = riscv_stack_frame_size(switch)
        if frame_size != 32 * word:
            return False
        saves = riscv_slot_map(riscv_frame_accesses(switch, "store", 2))
        expected_saves = {
            1: {word, 0},
            **{register: {register * word} for register in range(4, 32)},
        }
        if any(
            not offsets.issubset(saves.get(register, set()))
            for register, offsets in expected_saves.items()
        ):
            return False
        old_sp = riscv_all_store_offsets(switch, 10)
        new_sp = {
            op[3]
            for op in switch.values()
            if op[0] in ("ld", "lw") and op[1] == 2 and op[2] == 11
        }
        # rt_hw_context_switch(from, to): the old SP is stored through a0 and
        # the next SP is loaded through a1; no current-thread symbol is read.
        if 0 not in old_sp or 0 not in new_sp:
            return False
        exit_address = elf.symbol_address("rt_hw_context_switch_exit")
        if exit_address is None or not any(
            op[0] == "jal" and op[1] == 0 and op[2] == exit_address
            for op in switch.values()
        ):
            return False
        restore = RiscvCodeParser(elf, "rt_hw_context_switch_exit").instructions(
            terminal=("mret",)
        )
        loads = riscv_frame_accesses(restore, "load", 2)
        restored = riscv_slot_map(loads)
        # The restore path reloads LR from slot word; slot 0 is mepc and is
        # consumed by the csrw below, not written back to x1.
        restored_expected = {
            1: {word},
            **{register: {register * word} for register in range(4, 32)},
        }
        if any(
            not offsets.issubset(restored.get(register, set()))
            for register, offsets in restored_expected.items()
        ):
            return False
        # The restore path reads mepc from slot 0, then restores x1 from slot 8.
        pc_load = next(
            (
                op[1]
                for op in restore.values()
                if op[0] in ("ld", "lw") and op[2] == 2 and op[3] == 0
            ),
            None,
        )
        return (
            pc_load is not None
            and any(
                op[0] == "csrw" and op[1] == 0x341 and op[2] == pc_load
                for op in restore.values()
            )
            and any(op[0] == "mret" for op in restore.values())
        )
    except (EvidenceError, StopIteration, ValueError, TypeError):
        return False


# --- Arm --------------------------------------------------------------------


def _select_arm_context(elf: ElfImage, cpu: Cpu) -> StackContextSelection:
    """Select an integer Arm profile only with matching compiled port evidence."""
    context = {
        "armv6-m": ARMV6M_INTEGER,
        "armv7-m": ARMV7M_INTEGER,
        "armv7-a": ARMV7A_LWP_INTEGER,
    }.get(cpu.isa)
    if context is None or not context.verified:
        return _selection_result(cpu, None, "unverified-port")
    matcher = {
        "armv6-m": _armv6m_verify_context,
        "armv7-m": _armv7m_verify_context,
        "armv7-a": _armv7a_verify_context,
    }[cpu.isa]
    proven = matcher(elf)
    if not proven:
        return _selection_result(cpu, None, "unrecognizable-stack-frame-layout")
    return _selection_result(
        cpu,
        context,
        "ok",
        (".ARM.attributes", "compiled libcpu save/restore/initial context"),
    )


def _code_span(
    elf: ElfImage, name: str, end_name: str | None = None
) -> tuple[int, int] | None:
    """Resolve a bounded executable span using unique named ELF symbols."""
    start = elf.symbol_address(name)
    if start is None:
        return None
    if end_name is not None:
        end = elf.symbol_address(end_name)
    else:
        symbols = [symbol for symbol in elf.symbols if symbol.name == name]
        end = (
            symbols[0].end if len(symbols) == 1 and symbols[0].start == start else None
        )
    if end is None or not 0 < end - start <= 1024:
        return None
    if not elf.is_code(start) or not elf.is_code(end - 1):
        return None
    return start, end


_MAX_PORT_INSTRUCTIONS = 256
_M_PROFILE_CORE = 0xFF0
_M_PROFILE_LOW = 0xF0
_PSP_SYSM = 9

_PortInstruction = ThumbInstruction | ArmInstruction
_PortDecoder = Callable[[ElfImage, int, int], _PortInstruction | str]


def _is_return(instruction: _PortInstruction) -> bool:
    if instruction.mnemonic == "bx":
        return instruction.operands.get("rm") == 14
    if instruction.mnemonic == "pop":
        return bool(int(instruction.operands.get("reglist", 0)) & 0x100)
    return False


def _port_instructions(
    elf: ElfImage,
    name: str,
    end_name: str | None,
    decoder: _PortDecoder,
    *,
    stop_at_return: bool = False,
) -> tuple[_PortInstruction, ...] | None:
    """Decode one bounded port function, refusing unknown or unreadable bytes."""
    span = _code_span(elf, name, end_name)
    if span is None or elf.endian != "little":
        return None
    instructions: list[_PortInstruction] = []
    address = span[0]
    while address < span[1]:
        if len(instructions) >= _MAX_PORT_INSTRUCTIONS:
            return None
        decoded = decoder(elf, address, span[1])
        if isinstance(decoded, str) or decoded.mnemonic == "other":
            return None
        instructions.append(decoded)
        if stop_at_return and _is_return(decoded):
            return tuple(instructions)
        address += decoded.size
    if stop_at_return or address != span[1] or not instructions:
        return None
    return tuple(instructions)


def _reglists(
    instructions: tuple[_PortInstruction, ...], mnemonics: tuple[str, ...]
) -> list[int]:
    return [
        int(instruction.operands["reglist"])
        for instruction in instructions
        if instruction.mnemonic in mnemonics
        and isinstance(instruction.operands.get("reglist"), int)
    ]


def _operand(instruction: _PortInstruction, name: str) -> int | None:
    value = instruction.operands.get(name)
    return int(value) if isinstance(value, int) else None


def _m_profile_verify_context(elf: ElfImage, isa: str) -> bool:
    """Prove the compiled PendSV save/restore, PSP switch and initial frame."""
    request = _port_instructions(
        elf, "rt_hw_context_switch", "PendSV_Handler", decode_thumb
    )
    pend = _port_instructions(
        elf, "PendSV_Handler", "rt_hw_context_switch_to", decode_thumb
    )
    init = _port_instructions(
        elf, "rt_hw_stack_init", None, decode_thumb, stop_at_return=True
    )
    if request is None or pend is None or init is None:
        return False
    psp_registers = {
        _operand(instruction, "rd")
        for instruction in pend
        if instruction.mnemonic == "mrs"
        and instruction.operands.get("sysm") == _PSP_SYSM
    }
    psp_registers.discard(None)
    if not psp_registers or not any(
        instruction.mnemonic == "msr" and instruction.operands.get("sysm") == _PSP_SYSM
        for instruction in pend
    ):
        return False
    saves = _reglists(pend, ("stmdb", "stmia", "stm"))
    restores = _reglists(pend, ("ldmia", "ldmdb", "ldm"))
    if isa == "armv7-m":
        if _M_PROFILE_CORE not in saves or _M_PROFILE_CORE not in restores:
            return False
    else:
        if saves.count(_M_PROFILE_LOW) < 2 or restores.count(_M_PROFILE_LOW) < 2:
            return False
        high_moves = sum(
            1
            for instruction in pend
            if instruction.mnemonic == "mov"
            and _operand(instruction, "rd") in (4, 5, 6, 7)
            and _operand(instruction, "rm") in (8, 9, 10, 11)
        )
        if high_moves < 4:
            return False
    # rt_thread::sp is written once through a PSP-derived register.
    if not any(
        instruction.mnemonic == "str_imm"
        and instruction.operands.get("rt") in psp_registers
        and instruction.operands.get("imm") == 0
        for instruction in pend
    ):
        return False
    return any(instruction.mnemonic == "str_imm" for instruction in request) and any(
        instruction.mnemonic == "str_imm" for instruction in init
    )


def _armv7m_verify_context(elf: ElfImage) -> bool:
    """Prove the Armv7-M integer PendSV, switch request and initializer paths."""
    return _m_profile_verify_context(elf, "armv7-m")


def _armv6m_verify_context(elf: ElfImage) -> bool:
    """Prove Thumb-1 low/high-register saves and a basic Thread/PSP return."""
    return _m_profile_verify_context(elf, "armv6-m")


_A32_LWP_HEADER = 8
_A32_LR_BITMAP = 0x4000
_A32_CORE_BITMAP = 0x5FFF


def _armv7a_verify_context(elf: ElfImage) -> bool:
    """Prove the integer LWP-header port from decoded save/restore effects."""
    switch = _port_instructions(
        elf, "rt_hw_context_switch", "rt_hw_context_switch_interrupt", decode_arm
    )
    init = _port_instructions(elf, "rt_hw_stack_init", None, decode_arm)
    if switch is None or init is None:
        return False
    # An 8-byte LWP header (CPSR/SPSR) separates the saved integer frame from
    # the exception frame; without it the LWP slot table does not apply.
    if not any(
        instruction.mnemonic == "sub"
        and instruction.operands.get("rd") == 13
        and instruction.operands.get("immediate") == _A32_LWP_HEADER
        for instruction in switch
    ):
        return False
    # rt_hw_context_switch(from=r0, to=r1) stores the old SP through r0 and
    # loads the new SP through r1 at offset zero of the thread control block.
    if not any(
        instruction.mnemonic == "str"
        and instruction.operands.get("rd") == 13
        and instruction.operands.get("rn") == 0
        and instruction.operands.get("offset") == 0
        for instruction in switch
    ) or not any(
        instruction.mnemonic == "ldr"
        and instruction.operands.get("rd") == 13
        and instruction.operands.get("rn") == 1
        and instruction.operands.get("offset") == 0
        for instruction in switch
    ):
        return False
    saves = _reglists(switch, ("stmdb", "stm"))
    if _A32_LR_BITMAP not in saves or _A32_CORE_BITMAP not in saves:
        return False
    if not any(
        instruction.mnemonic == "mrs" and instruction.operands.get("psr") == "cpsr"
        for instruction in switch
    ):
        return False
    return any(
        instruction.mnemonic in ("str", "stmdb", "stmia") for instruction in init
    )
