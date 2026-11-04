"""RT-Thread saved-frame profile selection from ELF evidence."""

from __future__ import annotations

import hashlib

from core.cpus import Cpu, cpu_of
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_all_store_offsets,
    riscv_frame_accesses,
    riscv_slot_map,
    riscv_stack_frame_size,
)
from core.insn_thumb import decode_thumb_stream
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
    tokens, _reason = decode_thumb_stream(elf, name, *span, 256)
    if tokens is None:
        return None
    if name == "rt_hw_stack_init":
        terminal = next(
            (
                i
                for i, token in enumerate(tokens)
                if token == "bx:rm=14"
                or (
                    token.startswith("pop:reglist=")
                    and int(token.split("=", 1)[1]) & 0x100
                )
            ),
            None,
        )
        if terminal is None:
            return None
        tokens = tokens[: terminal + 1]
    return hashlib.sha256("\n".join(tokens).encode()).hexdigest()


def _armv7m_verify_context(elf: ElfImage) -> bool:
    """Match the compiled integer PendSV, switch request and initializer paths."""
    signatures = (
        (
            "rt_hw_context_switch",
            "PendSV_Handler",
            "0f5868d9842be4fa04dead19d1ff65be8c8579926d57e82068a4120d9fe9158a",
        ),
        (
            "PendSV_Handler",
            "rt_hw_context_switch_to",
            "bc11661a5500e38b76176842799d58512d334fe0eba33189f4be10de33859221",
        ),
        (
            "rt_hw_context_switch_to",
            "rt_hw_interrupt_thread_switch",
            "646b53a98a601f62c7f533e62c8e0b2b366340fdd1870fb8b048b57f029aaa6e",
        ),
        (
            "rt_hw_stack_init",
            None,
            "f90ee4e9a5cb03bf03e74ced031313e0add3bc824278e0b51ea0ae51fc0f0e01",
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
            "ff798ab02ae631b7559c5c106a24906fd25c40b75f5771d6f99f885ec46c1a4d",
        ),
        (
            "PendSV_Handler",
            "rt_hw_context_switch_to",
            "555c8712588b4f2f9ddfdf8301394f415736b9b9c79c08e69d4cd7450acaef7c",
        ),
        (
            "rt_hw_context_switch_to",
            "rt_hw_interrupt_thread_switch",
            "bc41615ad8dfa795e7ff2496e9374e08a670004945044a4163e02f0fa8a20a62",
        ),
        (
            "rt_hw_stack_init",
            None,
            "3a0784f5d50ebe9b6f8565cae682076d64fb11783572179dac4812919c66cee7",
        ),
    )
    return all(
        _thumb_signature(elf, name, end) == signature
        for name, end, signature in signatures
    )


def _arm_words_match(elf: ElfImage, name: str, expected: tuple[int | str, ...]) -> bool:
    """Match exact ARM words and symbol-relative unconditional branch operands."""
    start = elf.symbol_address(name)
    if start is None or start & 3 or elf.endian != "little":
        return False
    for index, operand in enumerate(expected):
        address = start + index * 4
        if not elf.is_code(address + 3):
            return False
        raw = elf.read_at(address, 4)
        if raw is None:
            return False
        word = int.from_bytes(raw, "little")
        if isinstance(operand, int):
            if word != operand:
                return False
        else:
            operation, symbol = operand.split(":", 1)
            opcode = 0xEB if operation == "call" else 0xEA
            displacement = word & 0xFFFFFF
            if displacement & 0x800000:
                displacement -= 1 << 24
            if (
                word >> 24 != opcode
                or address + 8 + displacement * 4 != elf.symbol_address(symbol)
            ):
                return False
    return True


def _armv7a_verify_context(elf: ElfImage) -> bool:
    """Prove the integer LWP-header/SMP port without accepting FPU or user seeds."""
    switch = (
        0xE92D4000,
        0xE92D5FFF,
        0xE10F4000,
        0xE31E0001,
        0x13844020,
        0xE92D0010,
        0xE94D6000,
        0xE24DD008,
        0xE580D000,
        0xE591D000,
        0xE1A00002,
        "call:rt_cpus_lock_status_restore",
        "jump:rt_hw_context_switch_exit",
    )
    first = (
        0xE590D000,
        0xE1A00001,
        "call:rt_cpus_lock_status_restore",
        "jump:rt_hw_context_switch_exit",
    )
    restore = (
        0xE1A0000D,
        0xF1020012,
        "call:rt_signal_check",
        0xF1020013,
        0xE1A0D000,
        0xE8DD6000,
        0xE28DD008,
        0xE8BD0002,
        0xE16FF001,
        0xE8FDDFFF,
    )
    if not all(
        _arm_words_match(elf, name, words)
        for name, words in (
            ("rt_hw_context_switch", switch),
            ("rt_hw_context_switch_to", first),
            ("rt_hw_context_switch_exit", restore),
        )
    ):
        return False
    span = _code_span(elf, "rt_hw_stack_init")
    blob = elf.read_at(span[0], span[1] - span[0]) if span is not None else None
    return (
        blob is not None
        and hashlib.sha256(blob).hexdigest()
        == "9abac72009f064ebd793c8b325fe7b2e0f7ec91254b103b7700f08118a7a73b4"
    )
