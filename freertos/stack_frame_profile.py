"""FreeRTOS saved-frame profile selection from ELF and kernel configuration."""

from __future__ import annotations

from collections.abc import Mapping

from core.cpus import Cpu, cpu_of
from core.insn_riscv import (
    EvidenceError,
    RiscvCodeParser,
    riscv_all_store_offsets,
    riscv_frame_accesses,
    riscv_next_pc,
    riscv_slot_map,
    riscv_stack_frame_size,
    riscv_target_loads,
)
from core.stack_frame_parser import (
    SavedStackContext,
    StackContextSelection,
)
from core.unwind_meta import ElfImage
from core.unwind_riscv import RiscvTrapFrameLayout, RiscvTrapSpan

from .backtrace import (
    ARMV6M_INTEGER,
    ARMV7M_CM4F_BASIC,
    ARMV7M_CM4F_FPU,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_INTEGER,
    ARMV8M_MAINLINE_MPU_INTEGER,
    ARMV8M_MAINLINE_SMP_INTEGER,
    RISCV32_INTEGER,
    RISCV64_INTEGER,
)

# --- Common -----------------------------------------------------------------


def select_context(
    elf: ElfImage,
    *,
    smp: bool = False,
    mpu: bool = False,
    macros: Mapping[str, str] | None = None,
) -> StackContextSelection:
    """Select a verified profile, retaining explicit missing/conflict reasons."""
    cpu = cpu_of(elf)
    if cpu.reason != "ok":
        return _selection_result(cpu, None, cpu.reason)
    if cpu.family == "riscv":
        return _select_riscv_context(elf, cpu, smp=smp, mpu=mpu)
    return _select_arm_context(elf, cpu, smp=smp, mpu=mpu, macros=macros)


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


def _select_riscv_context(
    elf: ElfImage,
    cpu: Cpu,
    *,
    smp: bool,
    mpu: bool,
) -> StackContextSelection:
    """Select and verify the RISC-V integer profile from compiled code."""
    context = _riscv_profile(cpu.isa)
    if context is None or not context.verified:
        return _selection_result(cpu, None, "unverified-port")
    if smp or mpu:
        return _selection_result(cpu, None, "unsupported-extension-context")
    if not _riscv_verify_context_switch(elf):
        return _selection_result(cpu, None, "unrecognizable-stack-frame-layout")
    return _selection_result(
        cpu,
        context,
        "ok",
        (
            ".riscv.attributes",
            "ELF e_flags",
            "compiled save/restore/initial context",
        ),
    )


def _riscv_profile(isa: str | None) -> SavedStackContext | None:
    """Return the integer profile for one canonical RISC-V ISA."""
    return {
        "riscv32": RISCV32_INTEGER,
        "riscv64": RISCV64_INTEGER,
    }.get(isa or "")


def analyze_riscv_trap_profile(elf: ElfImage) -> RiscvTrapSpan | None:
    """Extract a verified M-mode trap handler span from FreeRTOS port code."""
    if elf.pointer_size not in (4, 8):
        return None
    word = elf.pointer_size
    trap_start = elf.symbol_address("freertos_risc_v_trap_handler")
    sync_pc = elf.symbol_address("synchronous_exception")
    async_pc = elf.symbol_address("asynchronous_interrupt")
    idle_pc = elf.symbol_address("handle_interrupt")
    proc_pc = elf.symbol_address("processed_source")
    if None in (trap_start, sync_pc, async_pc, idle_pc, proc_pc):
        return None
    assert (
        trap_start is not None
        and sync_pc is not None
        and async_pc is not None
        and idle_pc is not None
        and proc_pc is not None
    )
    if not (trap_start < async_pc < sync_pc < idle_pc < proc_pc):
        return None
    try:
        trap = RiscvCodeParser(
            elf, "freertos_risc_v_trap_handler", "asynchronous_interrupt"
        ).instructions()
        frame_size = riscv_stack_frame_size(trap)
        expected_saves = {
            1: word,
            **{register: (register - 3) * word for register in range(5, 32)},
        }
        saved = riscv_slot_map(riscv_frame_accesses(trap, "store", 2))
        if frame_size != 31 * word or not _riscv_slots_include(saved, expected_saves):
            return None
        saved_offsets = riscv_all_store_offsets(trap, 2)
        if not {29 * word, 30 * word}.issubset(saved_offsets):
            return None
        loads = riscv_target_loads(elf, trap)
        tcb_register = next(
            (register for register, name in loads.items() if name == "pxCurrentTCB"),
            None,
        )
        if tcb_register is None or not any(
            op[0] in ("sd", "sw")
            and op[1] == 2
            and op[2] == tcb_register
            and op[3] == 0
            for op in trap.values()
        ):
            return None
        mcause_register = next(
            (op[1] for op in trap.values() if op[0] == "csrr" and op[2] == 0x342),
            None,
        )
        branch = next(
            (
                op
                for op in trap.values()
                if op[0] in ("bge", "blt") and op[1] == mcause_register
            ),
            None,
        )
        if mcause_register is None or branch is None or branch[3] != sync_pc:
            return None
        branch_pc = next(pc for pc, op in trap.items() if op is branch)
        if branch_pc + 4 != async_pc:
            return None
        async_code = RiscvCodeParser(
            elf, "asynchronous_interrupt", "synchronous_exception"
        ).instructions()
        sync_code = RiscvCodeParser(
            elf, "synchronous_exception", "handle_interrupt"
        ).instructions()
        mepc_register = next(
            (op[1] for op in trap.values() if op[0] == "csrr" and op[2] == 0x341),
            None,
        )
        if mepc_register is None or not _riscv_route_pc_store(
            async_code, mepc_register=mepc_register, addend=0
        ):
            return None
        if not _riscv_route_pc_store(sync_code, mepc_register=mepc_register, addend=4):
            return None
        if not any(
            op[0] in ("sd", "sw") and op[2] == 2 and op[3] == 0
            for op in async_code.values()
        ):
            return None
        dispatch = RiscvCodeParser(
            elf, "handle_interrupt", "processed_source"
        ).instructions()
        restore = RiscvCodeParser(elf, "processed_source").instructions(
            terminal=("mret",)
        )
        restored_loads = riscv_frame_accesses(restore, "load", 2)
        restored = riscv_slot_map(restored_loads)
        if not _riscv_slots_include(restored, expected_saves):
            return None
        if not {29 * word, 30 * word}.issubset(set(restored_loads)):
            return None
        pc_restore = next(
            (
                op[1]
                for op in restore.values()
                if op[0] in ("ld", "lw") and op[2] == 2 and op[3] == 0
            ),
            None,
        )
        if (
            pc_restore is None
            or not any(
                op[0] == "csrw" and op[1] == 0x341 and op[2] == pc_restore
                for op in restore.values()
            )
            or not any(op[0] == "mret" for op in restore.values())
        ):
            return None
    except (EvidenceError, StopIteration, ValueError, TypeError):
        return None

    mret_pc = max(restore)
    exit_end = mret_pc + 4
    exc_pc = elf.symbol_address("handle_exception")
    call_end = exc_pc if exc_pc is not None and idle_pc < exc_pc < proc_pc else proc_pc
    refused: list[tuple[int, int, str]] = []
    app_exc_pc = elf.symbol_address("application_exception_handler")
    if app_exc_pc is not None and idle_pc < app_exc_pc < proc_pc:
        refused.append((app_exc_pc, proc_pc, "exception-frame-type-unproven"))
    switch_sym = next(
        (sym for sym in elf.symbols if sym.name == "vTaskSwitchContext"),
        None,
    )
    if switch_sym is not None and switch_sym.size > 0:
        all_dispatch = {**async_code, **sync_code, **dispatch}
        dispatch_pcs = sorted(all_dispatch)
        for index, pc in enumerate(dispatch_pcs):
            op = all_dispatch[pc]
            if op[0] == "jal" and op[2] == switch_sym.start:
                ret_pc = (
                    dispatch_pcs[index + 1] if index + 1 < len(dispatch_pcs) else pc + 4
                )
                refused.append((ret_pc, ret_pc + 4, "scheduler-transition-unproven"))

    layout = RiscvTrapFrameLayout(
        frame_bytes=31 * word,
        total_frame_bytes=31 * word,
        reg_offsets=expected_saves,
        epc_offset=0,
        mstatus_offset=30 * word,
        epc_sync_addend=4,
    )
    return RiscvTrapSpan(
        entry=trap_start,
        save_complete=idle_pc,
        call_start=idle_pc,
        call_end=call_end,
        exit_end=exit_end,
        layout=layout,
        refused_spans=tuple(refused),
        evidence=(
            f"trap=freertos_risc_v_trap_handler@{trap_start:#x}",
            f"dispatch={idle_pc:#x}..{call_end:#x}",
        ),
    )


def _riscv_verify_context_switch(elf: ElfImage) -> bool:
    """Compare FreeRTOS frame effects, trap routes, startup restore and initializer."""
    span = analyze_riscv_trap_profile(elf)
    if span is None:
        return False
    word = elf.pointer_size
    expected_saves = span.layout.reg_offsets
    try:
        initial = _riscv_initial_stack(elf, word)
        if initial is None:
            return False
        initial_size, initial_stores = initial
        if initial_size != 31 * word:
            return False
        expected_initial = {
            0: ("arg", 1),
            word: ("symbol", "xTaskReturnAddress"),
            7 * word: ("arg", 2),
            29 * word: 0,
            30 * word: ("csr", 0x300),
        }
        if any(
            initial_stores.get(offset) != value
            for offset, value in expected_initial.items()
        ):
            return False
        first = RiscvCodeParser(elf, "xPortStartFirstTask").instructions(
            terminal=("ret",)
        )
        first_loads = riscv_frame_accesses(first, "load", 2)
        first_maps = riscv_slot_map(first_loads)
        expected_first = {
            register: offset
            for register, offset in expected_saves.items()
            if register != 1
        }
        if not _riscv_slots_include(first_maps, expected_first):
            return False
        # The first-task path loads the initial entry into ra from slot 0 and
        # returns to it, so the saved LR slot (word) is not read here.
        if first_maps.get(1) is None or 0 not in first_maps[1]:
            return False
        if not {29 * word, 30 * word}.issubset(set(first_loads)):
            return False
        if not any(
            op[0] == "addi" and op[1:3] == (2, 2) and op[3] == 31 * word
            for op in first.values()
        ):
            return False
        first_symbols = riscv_target_loads(elf, first)
        return "pxCurrentTCB" in first_symbols.values() and any(
            op[0] == "ret" for op in first.values()
        )
    except (EvidenceError, StopIteration, ValueError, TypeError):
        return False


def _riscv_initial_stack(
    elf: ElfImage, word: int
) -> tuple[int, dict[int, object]] | None:
    """Evaluate the bounded integer setup path and return its relative stack writes."""
    code = RiscvCodeParser(
        elf, "pxPortInitialiseStack", "xPortStartFirstTask"
    ).instructions()
    pcs = sorted(code)
    registers: dict[int, object] = {
        0: 0,
        10: ("stack", 0),
        11: ("arg", 1),
        12: ("arg", 2),
        13: ("arg", 3),
    }
    stores: dict[int, object] = {}
    pc = pcs[0]
    steps = 0
    while pc in code and steps < len(code) * 2:
        steps += 1
        op = code[pc]
        following = riscv_next_pc(code, pc)
        mnemonic = op[0]
        if mnemonic in ("sw", "sd"):
            value = registers.get(op[1])
            base = registers.get(op[2])
            if isinstance(base, tuple) and base[0] == "stack":
                offset = base[1] + op[3]
                if offset in stores and stores[offset] != value:
                    return None
                stores[offset] = value
        elif mnemonic in ("addi", "addiw"):
            source = registers.get(op[2])
            if isinstance(source, tuple) and source[0] == "stack":
                registers[op[1]] = ("stack", source[1] + op[3])
            elif isinstance(source, int):
                registers[op[1]] = source + op[3]
            else:
                registers[op[1]] = None
        elif mnemonic == "csrr":
            registers[op[1]] = ("csr", op[2])
        elif mnemonic == "andi":
            source = registers.get(op[2])
            registers[op[1]] = (
                source
                if isinstance(source, tuple) and source[0] == "csr"
                else (source & op[3] if isinstance(source, int) else None)
            )
        elif mnemonic == "slli":
            source = registers.get(op[2])
            registers[op[1]] = source << op[3] if isinstance(source, int) else None
        elif mnemonic in ("or", "add"):
            left, right = registers.get(op[2]), registers.get(op[3])
            registers[op[1]] = (
                left
                if isinstance(left, tuple) and left[0] == "csr"
                else right
                if isinstance(right, tuple) and right[0] == "csr"
                else (
                    left | right
                    if isinstance(left, int)
                    and isinstance(right, int)
                    and mnemonic == "or"
                    else left + right
                    if isinstance(left, int) and isinstance(right, int)
                    else None
                )
            )
        elif mnemonic in ("auipc", "lui"):
            registers[op[1]] = pc + op[2] if mnemonic == "auipc" else op[2]
        elif mnemonic in ("lw", "ld"):
            base = registers.get(op[2])
            value = None
            if isinstance(base, int):
                address = (base + op[3]) % (1 << (word * 8))
                names = [
                    name for name, candidate in elf.anchors if candidate == address
                ]
                if len(names) == 1:
                    value = ("symbol", names[0])
            registers[op[1]] = value
        elif mnemonic in ("beq", "bne"):
            left, right = registers.get(op[1]), registers.get(op[2])
            if not isinstance(left, int) or not isinstance(right, int):
                return None
            take = left == right if mnemonic == "beq" else left != right
            if take:
                pc = op[3]
                continue
        elif mnemonic == "jal" and op[1] == 0:
            pc = op[2]
            continue
        elif mnemonic == "nop":
            pass
        elif mnemonic in ("ret", "jalr"):
            final_sp = registers.get(10)
            if not isinstance(final_sp, tuple) or final_sp[0] != "stack":
                return None
            size = -final_sp[1]
            return size, {offset + size: value for offset, value in stores.items()}
        elif mnemonic not in ("addi", "addiw"):
            return None
        if following is None:
            break
        pc = following
    return None


def _riscv_route_pc_store(
    code: dict[int, tuple], *, mepc_register: int, addend: int
) -> bool:
    """Prove a trap route stores mepc (or mepc plus the synchronous advance) at frame offset zero."""
    values: dict[int, tuple[str, int]] = {mepc_register: ("mepc", 0)}
    saved_pc = False
    for op in code.values():
        if op[0] == "addi":
            source = values.get(op[2])
            if source is not None:
                values[op[1]] = (source[0], source[1] + op[3])
        elif op[0] in ("sd", "sw") and op[2] == 2 and op[3] == 0:
            saved_pc |= values.get(op[1]) == ("mepc", addend)
    return saved_pc


def _riscv_slots_include(slots: dict[int, set[int]], expected: dict[int, int]) -> bool:
    """Return whether every expected register slot was stored or loaded."""
    return all(
        offset in slots.get(register, set()) for register, offset in expected.items()
    )


# --- Arm --------------------------------------------------------------------


def _select_arm_context(
    elf: ElfImage,
    cpu: Cpu,
    *,
    smp: bool,
    mpu: bool,
    macros: Mapping[str, str] | None,
) -> StackContextSelection:
    """Select and verify the Arm profile from symbols and kernel configuration."""
    if cpu.abi == "aapcs32-vfp" and cpu.isa in (
        "armv7-m",
        "armv7e-m",
        "armv8-m.mainline",
    ):
        if (
            _is_cm4f_port(elf, cpu)
            and ARMV7M_CM4F_BASIC.verified
            and ARMV7M_CM4F_FPU.verified
        ):
            return _selection_result(
                cpu,
                ARMV7M_CM4F_BASIC,
                "ok",
                ("aapcs32-vfp ABI", "port symbols"),
                alt_context=ARMV7M_CM4F_FPU,
            )
        return _selection_result(
            cpu, None, "unsupported-extension-context", ("aapcs32-vfp ABI",)
        )
    context = _arm_profile(cpu.isa, smp=smp, mpu=mpu)
    if context is None or not context.verified:
        return _selection_result(cpu, None, "unverified-port")
    if any(
        elf.symbol_address(name) is None
        for name in ("PendSV_Handler", "pxPortInitialiseStack")
    ):
        return _selection_result(cpu, None, "unrecognizable-stack-frame-layout")
    if cpu.isa == "armv8-m.mainline":
        reason = _arm_configuration_evidence(macros or {}, mpu=mpu)
        if reason is not None:
            return _selection_result(cpu, None, reason)
    return _selection_result(
        cpu,
        context,
        "ok",
        (
            ".ARM.attributes Tag_CPU_arch/profile",
            "port symbols",
            *(("kernel configuration",) if cpu.isa == "armv8-m.mainline" else ()),
        ),
    )


def _is_cm4f_port(elf: ElfImage, cpu: Cpu) -> bool:
    """Whether the image links the CM4F port, from ABI and port symbols.

    The VFP ABI alone does not prove the FPU port: a shared config header
    can enable FPU on a CM3 build that ignores it. ``vPortEnableVFP`` exists
    only in the M-profile FPU port; on Armv8-M ``xPortSysTickHandler``
    distinguishes the CM4F port from a CM33 port with FPU enabled, whose
    save layout differs.
    """
    if cpu.abi != "aapcs32-vfp":
        return False
    if cpu.isa in ("armv7-m", "armv7e-m"):
        return elf.symbol_address("vPortEnableVFP") is not None
    return cpu.isa == "armv8-m.mainline" and (
        elf.symbol_address("xPortSysTickHandler") is not None
    )


def _arm_profile(isa: str | None, *, smp: bool, mpu: bool) -> SavedStackContext | None:
    """Return the Arm integer profile matching the ISA and context mode."""
    if isa == "armv6-m" and not smp and not mpu:
        return ARMV6M_INTEGER
    if isa == "armv7-m" and not smp and not mpu:
        return ARMV7M_INTEGER
    if isa == "armv8-m.mainline" and not smp and not mpu:
        return ARMV8M_MAINLINE_INTEGER
    if isa == "armv8-m.mainline" and smp and not mpu:
        return ARMV8M_MAINLINE_SMP_INTEGER
    if isa == "armv8-m.mainline" and mpu and not smp:
        return ARMV8M_MAINLINE_MPU_INTEGER
    return None


def _arm_configuration_evidence(macros: Mapping[str, str], *, mpu: bool) -> str | None:
    """Return a rejection reason for an unsupported Armv8-M Mainline configuration."""
    required = ("configENABLE_FPU", "configENABLE_TRUSTZONE", "configENABLE_MPU")
    if mpu:
        required += ("configUSE_MPU_WRAPPERS_V1",)
    if any(name not in macros for name in required):
        return "port-configuration-unavailable"
    for name in ("configENABLE_FPU", "configENABLE_MVE"):
        if macros.get(name, "0") != "0":
            return "unsupported-extension-context"
    if macros["configENABLE_TRUSTZONE"] != "0":
        return "port-configuration-unavailable"
    if macros.get("configENABLE_MPU") != ("1" if mpu else "0") or (
        mpu and macros.get("configUSE_MPU_WRAPPERS_V1") != "0"
    ):
        return "port-configuration-conflict"
    return None
