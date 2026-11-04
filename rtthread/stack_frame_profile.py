"""RT-Thread saved-frame profile selection from ELF evidence."""

from __future__ import annotations

import hashlib

from core.cpus import Cpu, cpu_of
from core.insn_arm import (
    decode_arm_prefix,
    decode_arm_stream,
    expect_arm_stream,
    semantic_arm_stream_token,
)
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_all_store_offsets,
    riscv_frame_accesses,
    riscv_slot_map,
    riscv_stack_frame_size,
)
from core.insn_thumb import decode_thumb, decode_thumb_stream, semantic_token
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


def _thumb_signature(
    elf: ElfImage, name: str, end_name: str | None = None
) -> str | None:
    """Fingerprint bounded Thumb operands and resolved literals, not link addresses."""
    span = _code_span(elf, name, end_name)
    if span is None or elf.endian != "little":
        return None
    if name == "rt_hw_stack_init":
        tokens = []
        address = span[0]
        for _ in range(256):
            instruction = decode_thumb(elf, address, span[1])
            if isinstance(instruction, str) or instruction.mnemonic == "other":
                return None
            tokens.append(semantic_token(elf, instruction))
            address += instruction.size
            if instruction.mnemonic == "bx" and instruction.operands.get("rm") == 14:
                break
            if (
                instruction.mnemonic == "pop"
                and int(instruction.operands.get("reglist", 0)) & 0x100
            ):
                break
        else:
            return None
    else:
        tokens, _reason = decode_thumb_stream(
            elf, name, *span, 256, reject_unknown=True
        )
        if tokens is None:
            return None
    return hashlib.sha256("\n".join(tokens).encode()).hexdigest()


def _armv7m_verify_context(elf: ElfImage) -> bool:
    """Match the compiled integer PendSV, switch request and initializer paths."""
    signatures = (
        (
            "rt_hw_context_switch",
            "PendSV_Handler",
            "0fb31e58d24712ec03e0ef2ab25044f14925b2d6f82e03a5b0d0e1c1038999af",
        ),
        (
            "PendSV_Handler",
            "rt_hw_context_switch_to",
            "346c3e28784a01c733fcfd138d50ff1ec2f986f49019ee30993f0b48b1105929",
        ),
        (
            "rt_hw_context_switch_to",
            "rt_hw_interrupt_thread_switch",
            "a96bcbe0d18d1975ddbac7110ded93eff89ec171287baac86f8a427b798701fa",
        ),
        (
            "rt_hw_stack_init",
            None,
            "ed6e5dbbb9f844cacc4493d2fbad3c097da62733dee7e355dcb90183ebfb9425",
        ),
    )
    return all(
        _thumb_signature(elf, name, end) == expected
        for name, end, expected in signatures
    )


def _armv6m_verify_context(elf: ElfImage) -> bool:
    """Match Thumb-1 low/high-register saves and a basic Thread/PSP return."""
    signatures = (
        (
            "rt_hw_context_switch",
            "PendSV_Handler",
            "87db45fbb0e7551a9788ebf6abf2ee923827614e9dce8fdba8835d11a2374360",
        ),
        (
            "PendSV_Handler",
            "rt_hw_context_switch_to",
            "83f8cbb34a7c61a1b39cd0fe9aab2922fe32aa3091fd184fbc1f8eb304d389f5",
        ),
        (
            "rt_hw_context_switch_to",
            "rt_hw_interrupt_thread_switch",
            "419e16e8cda9fdccb4ae3091adf6ec90d1ddbee5fd6a39c21033e35948617736",
        ),
        (
            "rt_hw_stack_init",
            None,
            "d41d8887bba3e752e52a4f8cca03ef8e3d38edf3ced29d6dbf4a670027ee5b44",
        ),
    )
    return all(
        _thumb_signature(elf, name, end) == signature
        for name, end, signature in signatures
    )


def _arm_instructions_match(
    elf: ElfImage,
    name: str,
    expected: tuple[tuple[str, dict[str, int | bool | str]], ...],
    end_name: str | None = None,
) -> bool:
    """Match a bounded A32 function using decoded mnemonics and operands."""
    span = _code_span(elf, name, end_name)
    if span is None:
        return False
    instructions = decode_arm_prefix(elf, *span, len(expected))
    return instructions is not None and expect_arm_stream(
        instructions, expected, elf=elf
    )


def _armv7a_verify_context(elf: ElfImage) -> bool:
    """Prove the integer LWP-header/SMP port without accepting FPU or user seeds."""
    switch = (
        (
            "stmdb",
            {
                "cond": "al",
                "rn": 13,
                "wback": True,
                "user_bank": False,
                "reglist": 0x4000,
            },
        ),
        (
            "stmdb",
            {
                "cond": "al",
                "rn": 13,
                "wback": True,
                "user_bank": False,
                "reglist": 0x5FFF,
            },
        ),
        ("mrs", {"cond": "al", "rd": 4, "psr": "cpsr"}),
        ("tst", {"cond": "al", "rn": 14, "immediate": 1}),
        ("orr", {"cond": "ne", "rd": 4, "rn": 4, "immediate": 32}),
        ("stmdb", {"cond": "al", "rn": 13, "wback": True, "reglist": 0x10}),
        (
            "stmdb",
            {
                "cond": "al",
                "rn": 13,
                "wback": False,
                "user_bank": True,
                "reglist": 0x6000,
            },
        ),
        ("sub", {"cond": "al", "rd": 13, "rn": 13, "immediate": 8}),
        (
            "str",
            {
                "cond": "al",
                "rd": 13,
                "rn": 0,
                "offset": 0,
                "pre": True,
                "up": True,
                "byte": False,
                "wback": False,
            },
        ),
        (
            "ldr",
            {
                "cond": "al",
                "rd": 13,
                "rn": 1,
                "offset": 0,
                "pre": True,
                "up": True,
                "byte": False,
                "wback": False,
            },
        ),
        ("mov", {"cond": "al", "rd": 0, "rm": 2, "set_flags": False}),
        ("bl", {"cond": "al", "target_symbol": "rt_cpus_lock_status_restore"}),
        ("b", {"cond": "al", "target_symbol": "rt_hw_context_switch_exit"}),
    )
    first = (
        (
            "ldr",
            {
                "cond": "al",
                "rd": 13,
                "rn": 0,
                "offset": 0,
                "pre": True,
                "up": True,
                "byte": False,
                "wback": False,
            },
        ),
        ("mov", {"cond": "al", "rd": 0, "rm": 1, "set_flags": False}),
        ("bl", {"cond": "al", "target_symbol": "rt_cpus_lock_status_restore"}),
        ("b", {"cond": "al", "target_symbol": "rt_hw_context_switch_exit"}),
    )
    restore = (
        ("mov", {"cond": "al", "rd": 0, "rm": 13, "set_flags": False}),
        ("cps", {"mode": 18, "flags": 0, "cond": "unconditional"}),
        ("bl", {"cond": "al", "target_symbol": "rt_signal_check"}),
        ("cps", {"mode": 19, "flags": 0, "cond": "unconditional"}),
        ("mov", {"cond": "al", "rd": 13, "rm": 0, "set_flags": False}),
        ("ldmia", {"rn": 13, "user_bank": True, "reglist": 0x6000}),
        ("add", {"cond": "al", "rd": 13, "rn": 13, "immediate": 8, "set_flags": False}),
        (
            "ldmia",
            {"cond": "al", "rn": 13, "wback": True, "user_bank": False, "reglist": 0x2},
        ),
        ("msr", {"cond": "al", "psr": "spsr", "fields": 0xF, "rm": 1}),
        ("ldmia", {"rn": 13, "wback": True, "user_bank": True, "reglist": 0xDFFF}),
    )
    if not all(
        _arm_instructions_match(elf, name, instructions, end_name)
        for name, end_name, instructions in (
            ("rt_hw_context_switch", "rt_hw_context_switch_interrupt", switch),
            ("rt_hw_context_switch_to", "rt_hw_spin_lock_init", first),
            ("rt_hw_context_switch_exit", "vector_fiq", restore),
        )
    ):
        return False
    span = _code_span(elf, "rt_hw_stack_init")
    instructions = decode_arm_stream(elf, *span, 256) if span is not None else None
    return (
        instructions is not None
        and hashlib.sha256(
            semantic_arm_stream_token(elf, instructions, *span).encode()
        ).hexdigest()
        == "66097d62f8807885e2d069dbfc09ffcd3ea79284a2e01c565f71c29b517f60f5"
    )
