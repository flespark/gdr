"""RT-Thread saved-frame profile selection from ELF evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from core.cpus import Cpu, cpu_of
from core.insn_arm import ArmInstruction, decode_arm
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_frame_accesses,
    riscv_slot_map,
    riscv_stack_frame_size,
)
from core.insn_thumb import ThumbInstruction, decode_thumb, resolved_literal
from core.stack_frame_parser import (
    SavedStackContext,
    StackContextSelection,
)
from core.unwind_meta import ElfImage

from .backtrace import (
    ARMV6M_INTEGER,
    ARMV7A_LWP_INTEGER,
    ARMV7EM_FPU,
    ARMV7EM_INTEGER,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_FPU,
    ARMV8M_MAINLINE_INTEGER,
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
    alt_context: SavedStackContext | None = None,
) -> StackContextSelection:
    return StackContextSelection(
        cpu.isa, cpu.family, context, reason, evidence, cpu.abi, alt_context
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
    if elf.pointer_size not in (4, 8):
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
        store, load = ("sd", "ld") if word == 8 else ("sw", "lw")
        old_sp = any(op == (store, 2, 10, 0) for op in switch.values())
        new_sp = {
            op[3]
            for op in switch.values()
            if op[0] == load and op[1] == 2 and op[2] == 11
        }
        # rt_hw_context_switch(from, to): the old SP is stored through a0 and
        # the next SP is loaded through a1; no current-thread symbol is read.
        if not old_sp or 0 not in new_sp:
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
                if op[0] == load and op[2] == 2 and op[3] == 0
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
            and any(op == ("addi", 2, 2, 32 * word) for op in restore.values())
        )
    except (EvidenceError, StopIteration, ValueError, TypeError):
        return False


# --- Arm --------------------------------------------------------------------


def _select_arm_context(elf: ElfImage, cpu: Cpu) -> StackContextSelection:
    """Select an integer Arm profile only with matching compiled port evidence."""
    isa = cpu.isa
    if isa is None:
        return _selection_result(cpu, None, "unverified-port")
    if isa == "armv7e-m" and cpu.abi == "aapcs32-vfp":
        if _armv7em_fpu_verify_context(elf):
            return _selection_result(
                cpu,
                ARMV7EM_INTEGER,
                "ok",
                (".ARM.attributes", "compiled libcpu FPU save/restore"),
                alt_context=ARMV7EM_FPU,
            )
        return _selection_result(
            cpu, None, "unrecognizable-stack-frame-layout", ("aapcs32-vfp ABI",)
        )
    if isa in ("armv7-m", "armv7e-m") and cpu.abi == "aapcs32-vfp":
        return _selection_result(
            cpu, None, "unsupported-extension-context", ("aapcs32-vfp ABI",)
        )
    if isa == "armv8-m.mainline" and cpu.abi == "aapcs32-vfp":
        if _armv8m_mainline_fpu_verify_context(elf):
            return _selection_result(
                cpu,
                ARMV8M_MAINLINE_INTEGER,
                "ok",
                (".ARM.attributes", "compiled libcpu FPU save/restore"),
                alt_context=ARMV8M_MAINLINE_FPU,
            )
        return _selection_result(
            cpu, None, "unrecognizable-stack-frame-layout", ("aapcs32-vfp ABI",)
        )
    context = {
        "armv6-m": ARMV6M_INTEGER,
        "armv7-m": ARMV7M_INTEGER,
        "armv8-m.mainline": ARMV8M_MAINLINE_INTEGER,
        "armv7-a": ARMV7A_LWP_INTEGER,
    }.get(isa)
    if context is None or not context.verified:
        return _selection_result(cpu, None, "unverified-port")
    matcher = {
        "armv6-m": _armv6m_verify_context,
        "armv7-m": _armv7m_verify_context,
        "armv8-m.mainline": _armv8m_mainline_verify_context,
        "armv7-a": _armv7a_verify_context,
    }[isa]
    proven = matcher(elf)
    if not proven:
        return _selection_result(cpu, None, "unrecognizable-stack-frame-layout")
    return _selection_result(
        cpu,
        context,
        "ok",
        (".ARM.attributes", "compiled libcpu save/restore/initial context"),
    )


def _armv8m_mainline_fpu_verify_context(elf: ElfImage) -> bool:
    """Prove the FPU Armv8-M Mainline frame, PSP switch and initial-context stores."""
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
    save = (
        ("mrs", {"rd": 1, "sysm": 9}),
        ("tst_imm", {"rn": 14, "imm": 16}),
        ("it", {"firstcond": 0, "mask": 8}),
        ("vstmdb", {"rn": 1, "first": 8, "count": 8}),
        ("stmdb", {"rn": 1, "wback": True, "reglist": 0xFF0}),
        ("ldr_literal", {"rt": 2}),
        ("ldr_imm", {"rt": 2, "rn": 2, "imm": 0}),
        ("mov", {"rd": 3, "rm": 14}),
        ("mrs", {"rd": 4, "sysm": 11}),
        ("mrs", {"rd": 5, "sysm": 20}),
        ("stmdb", {"rn": 1, "wback": True, "reglist": 0x3C}),
        ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
        ("str_imm", {"rt": 1, "rn": 0, "imm": 0}),
    )
    restore = (
        ("ldmia", {"rn": 1, "wback": True, "reglist": 0xFF0}),
        ("tst_imm", {"rn": 14, "imm": 16}),
        ("it", {"firstcond": 0, "mask": 8}),
        ("vldmia", {"rn": 1, "first": 8, "count": 8}),
        ("msr", {"rn": 1, "sysm": 9}),
    )
    saved = _instruction_sequence(pend, save)
    restored = _instruction_sequence(pend, restore)
    if saved is None or restored is None:
        return False
    tz = elf.symbol_address("rt_trustzone_current_context")
    if tz is None or any(
        not isinstance(instruction, ThumbInstruction)
        or resolved_literal(elf, instruction) != tz
        for instruction in (saved[5],)
    ):
        return False
    offsets = {0, 4, 8, 12, 48, 52, 56, 60, 64, 68, 72, 76}
    init_stores = {
        _operand(instruction, "imm")
        for instruction in init
        if instruction.mnemonic == "str_imm" and _operand(instruction, "rn") == 3
    }
    return (
        offsets.issubset(init_stores)
        and _instruction_sequence(
            init,
            (
                ("orn", {"rd": 2, "rn": 15, "immediate": 2}),
                ("str_imm", {"rt": 2, "rn": 3, "imm": 4}),
            ),
        )
        is not None
        and any(
            instruction.mnemonic == "subs" and instruction.operands.get("imm") == 80
            for instruction in init
        )
        and any(instruction.mnemonic == "str_imm" for instruction in request)
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


def _armv7em_fpu_verify_context(elf: ElfImage) -> bool:
    """Prove the FPU Armv7E-M frame, PSP switch and initial-context stores."""
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
    save = (
        ("mrs", {"rd": 1, "sysm": 9}),
        ("tst_imm", {"rn": 14, "imm": 16}),
        ("it", {"firstcond": 0, "mask": 8}),
        ("vstmdb", {"rn": 1, "first": 8, "count": 8}),
        ("stmdb", {"rn": 1, "wback": True, "reglist": 0xFF0}),
        ("mov_imm", {"rd": 4, "imm": 0}),
        ("tst_imm", {"rn": 14, "imm": 16}),
        ("it", {"firstcond": 0, "mask": 8}),
        ("mov_imm", {"rd": 4, "imm": 1}),
        ("str_pre", {"rn": 1, "rt": 4, "imm": -4}),
        ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
        ("str_imm", {"rt": 1, "rn": 0, "imm": 0}),
    )
    restore = (
        ("ldmia", {"rn": 1, "wback": True, "reglist": 0x8}),
        ("ldmia", {"rn": 1, "wback": True, "reglist": 0xFF0}),
        ("cmp", {"rn": 3, "imm": 0}),
        ("it", {"firstcond": 1, "mask": 8}),
        ("vldmia", {"rn": 1, "first": 8, "count": 8}),
        ("msr", {"rn": 1, "sysm": 9}),
    )
    fixup = (
        ("orr_imm", {"rd": 14, "rn": 14, "imm": 16}),
        ("cmp", {"rn": 3, "imm": 0}),
        ("it", {"firstcond": 1, "mask": 8}),
    )
    if (
        _instruction_sequence(pend, save) is None
        or _instruction_sequence(pend, restore) is None
        or _instruction_sequence(pend, fixup) is None
    ):
        return False
    return (
        any(
            instruction.mnemonic == "subs" and instruction.operands.get("imm") == 68
            for instruction in init
        )
        and any(
            instruction.mnemonic == "mov_imm"
            and instruction.operands.get("imm") == 0x01000000
            for instruction in init
        )
        and any(
            instruction.mnemonic == "str_imm" and instruction.operands.get("imm") == 64
            for instruction in init
        )
        and any(
            instruction.mnemonic == "str_imm" and instruction.operands.get("imm") == 0
            for instruction in init
        )
        and any(instruction.mnemonic == "str_imm" for instruction in request)
    )


def _armv8m_mainline_verify_context(elf: ElfImage) -> bool:
    """Prove the integer Armv8-M Mainline frame, PSP switch and initial-context stores."""
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
    save = (
        ("mrs", {"rd": 1, "sysm": 9}),
        ("stmdb", {"rn": 1, "wback": True, "reglist": 0xFF0}),
        ("ldr_literal", {"rt": 2}),
        ("ldr_imm", {"rt": 2, "rn": 2, "imm": 0}),
        ("mov", {"rd": 3, "rm": 14}),
        ("mrs", {"rd": 4, "sysm": 11}),
        ("mrs", {"rd": 5, "sysm": 20}),
        ("stmdb", {"rn": 1, "wback": True, "reglist": 0x3C}),
        ("ldr_imm", {"rt": 0, "rn": 0, "imm": 0}),
        ("str_imm", {"rt": 1, "rn": 0, "imm": 0}),
    )
    restore = (
        ("ldmia", {"rn": 1, "wback": True, "reglist": 0x3C}),
        ("msr", {"rn": 4, "sysm": 11}),
        ("msr", {"rn": 5, "sysm": 20}),
        ("mov", {"rd": 14, "rm": 3}),
        ("ldr_literal", {"rt": 6}),
        ("str_imm", {"rt": 2, "rn": 6, "imm": 0}),
    )
    tail = (
        ("ldmia", {"rn": 1, "wback": True, "reglist": 0xFF0}),
        ("msr", {"rn": 1, "sysm": 9}),
    )
    saved = _instruction_sequence(pend, save)
    restored = _instruction_sequence(pend, restore)
    if saved is None or restored is None or _instruction_sequence(pend, tail) is None:
        return False
    tz = elf.symbol_address("rt_trustzone_current_context")
    if tz is None or any(
        not isinstance(instruction, ThumbInstruction)
        or resolved_literal(elf, instruction) != tz
        for instruction in (saved[2], restored[4])
    ):
        return False
    offsets = {0, 4, 8, 12, 48, 52, 56, 60, 64, 68, 72, 76}
    init_stores = {
        _operand(instruction, "imm")
        for instruction in init
        if instruction.mnemonic == "str_imm" and _operand(instruction, "rn") == 3
    }
    return (
        offsets.issubset(init_stores)
        and _instruction_sequence(
            init,
            (
                ("orn", {"rd": 2, "rn": 15, "immediate": 2}),
                ("str_imm", {"rt": 2, "rn": 3, "imm": 4}),
            ),
        )
        is not None
        and any(
            instruction.mnemonic == "subs" and instruction.operands.get("imm") == 80
            for instruction in init
        )
        and any(instruction.mnemonic == "str_imm" for instruction in request)
    )


def _instruction_sequence(
    instructions: tuple[_PortInstruction, ...],
    expected: tuple[tuple[str, Mapping[str, int | bool]], ...],
) -> tuple[_PortInstruction, ...] | None:
    """Find a contiguous decoded layout effect, without link-address matching."""
    for offset in range(len(instructions) - len(expected) + 1):
        candidate = instructions[offset : offset + len(expected)]
        if all(
            instruction.mnemonic == mnemonic
            and all(
                instruction.operands.get(key) == value
                for key, value in operands.items()
            )
            for instruction, (mnemonic, operands) in zip(
                candidate, expected, strict=True
            )
        ):
            return candidate
    return None


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
