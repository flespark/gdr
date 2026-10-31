"""RT-Thread saved-frame profile selection from ELF evidence."""

from __future__ import annotations

from core.cpus import Cpu, cpu_of
from core.stack_frame_parser import (
    EvidenceError,
    RiscvCodeParser,
    SavedStackContext,
    StackContextSelection,
    riscv_all_store_offsets,
    riscv_frame_accesses,
    riscv_slot_map,
    riscv_stack_frame_size,
)
from core.unwind_meta import ElfImage

from .backtrace import RISCV32_INTEGER, RISCV64_INTEGER

# --- Common -----------------------------------------------------------------


def select_context(elf: ElfImage) -> StackContextSelection:
    """Recognize ISA separately from layout proof and closed-loop verification."""
    cpu = cpu_of(elf)
    if cpu.reason != "ok":
        return _selection_result(cpu, None, cpu.reason)
    if cpu.family == "riscv":
        return _select_riscv_context(elf, cpu)
    return _select_arm_context(cpu)


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


def _select_arm_context(cpu: Cpu) -> StackContextSelection:
    """Keep Arm profiles gated until a closed-loop fixture proves them."""
    return _selection_result(cpu, None, "unverified-port")
