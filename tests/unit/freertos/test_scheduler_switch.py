"""Arm Cortex-M3 scheduler-transition profile and stage-seed regression.

The byte sequences are the compiled V10.3.1 ``portable/GCC/ARM_CM3`` bodies
captured from the fixture; the synthetic image only makes them independent of
the cache and lets every malformed variant be constructed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.insn_thumb import (
    decode_thumb32 as _decode_thumb32,
)
from core.insn_thumb import (
    semantic_token as _semantic_token,
)
from core.stack_frame_parser import SavedStackContext
from core.unwind_meta import SHF_EXECINSTR, CodeSymbol, ElfImage, ElfSection
from freertos.backtrace import ARMV7M_INTEGER
from freertos.scheduler_switch import (
    SchedulerStage,
    build_profile,
    build_stage_seed,
    classify_stage,
)

PENDSV = bytes.fromhex(
    "eff30980bff36f8f0d4b1a6820e9f00f106008b54ff0040080f31188fef7ccfb4ff000"
    "0080f31188bde8084019680868b0e8f00f80f30988bff36f8f704700bfb8010020"
)
SVC = bytes.fromhex(
    "074b19680868b0e8f00f80f30988bff36f8f4ff0000080f311884ef00d0e7047b8010020"
)
START_FIRST = bytes.fromhex("06480068006880f3088862b661b6bff34f8fbff36f8f00df00bf")
SWITCH = bytes.fromhex(
    "334b1b681bb1334b01221a60704710b5304b00221a60fff733fc2f4b18602f4b1b6898"
    "4205d92e4a11680a6ec31a1a440a662a4b18602a4b1b681b6b1a68b2f1a53f03d15a68"
    "b2f1a53f2bd0244b186819683431fff718f9224b1c68b4fa84f4e4b2c4f11f0404eb84"
    "039b001e4ad3580bb31d4904eb840301eb83035a6852685a6004eb84039b0008330b44"
    "9a4217d004eb8404144b03eb84035b68da68104b1a6010bd9a68b2f1a53fcfd1db68b3"
    "f1a53fcbd1d0e740f6e130fff77df8d8e7516804eb8402084b03eb82035960dfe7a400"
    "0020b80000209c000020a0000020b8010020c400002040010020"
)

PENDSV_START = 0x2850
SVC_START = 0x27C0
START_START = 0x2744
SWITCH_START = 0x1008
TCB_POINTER = 0x200001B8
EXIT_PC = 0x2000
ENTRY_PC = 0x3000
TASK_TCB = 0x20001000
TASK_TOP = 0x20002000
TASK_LOW = 0x20001000
TASK_HIGH = 0x20003000

_STAGE_NAMES = {
    "pendsv-entry": "pendsv_entry",
    "pendsv-save-pending": "pendsv_save_pending",
    "pendsv-save-written": "pendsv_save_written",
    "pendsv-writeback": "pendsv_writeback",
    "pendsv-switch-call": "pendsv_switch_call",
    "pendsv-after-switch": "pendsv_after_switch",
    "pendsv-select": "pendsv_select",
    "pendsv-saved-sp": "pendsv_saved_sp",
    "pendsv-restored-pending-psp": "pendsv_restored_pending_psp",
    "pendsv-psp-written": "pendsv_psp_written",
    "pendsv-exception-return": "pendsv_exception_return",
    "svc-entry": "svc_entry",
    "svc-saved-sp": "svc_saved_sp",
    "svc-restored-pending-psp": "svc_restored_pending_psp",
    "svc-psp-written": "svc_psp_written",
    "svc-exception-return": "svc_exception_return",
    "switch-entry": "switch_entry",
    "switch-before-update": "switch_update",
    "switch-after-update": "switch_after_update",
}


def _image(
    *,
    pendsv: bytes = PENDSV,
    svc: bytes = SVC,
    start_first: bytes = START_FIRST,
    switch: bytes = SWITCH,
    symbols: tuple | None = None,
    offset: int = 0,
    data_offset: int = 0,
    endian: str = "little",
) -> ElfImage:
    data = bytearray(0x5000)
    for start, body in (
        (PENDSV_START + offset, pendsv),
        (SVC_START + offset, svc),
        (START_START + offset, start_first),
        (SWITCH_START + offset, switch),
    ):
        data[start : start + len(body)] = body
    data[START_START + offset + 0x1C : START_START + offset + 0x20] = (
        0xE000ED08
    ).to_bytes(4, "little")
    if symbols is None:
        symbols = (
            ("PendSV_Handler", PENDSV_START + offset, len(PENDSV)),
            ("SVC_Handler", SVC_START + offset, len(SVC)),
            ("prvPortStartFirstTask", START_START + offset, len(START_FIRST)),
            ("vTaskSwitchContext", SWITCH_START + offset, len(SWITCH)),
        )
    return ElfImage(
        pointer_size=4,
        endian=endian,
        machine=40,
        sections=(ElfSection(".text", 0, bytes(data), SHF_EXECINSTR, 1),),
        symbols=tuple(
            CodeSymbol(name, start, size)
            for name, start, size in (
                *symbols,
                ("gdr_runtime_timer_value", 0x888 + offset, 12),
                ("vApplicationStackOverflowHook", 0x290 + offset, 10),
                ("gdr_fixture_assert_failed", 0x1C0 + offset, 18),
            )
        ),
        anchors=(
            ("pxCurrentTCB", TCB_POINTER + data_offset),
            ("uxSchedulerSuspended", 0x200000A4 + data_offset),
            ("xYieldPending", 0x200000B8 + data_offset),
            ("ulTotalRunTime", 0x2000009C + data_offset),
            ("ulTaskSwitchedInTime", 0x200000A0 + data_offset),
            ("uxTopReadyPriority", 0x200000C4 + data_offset),
            ("pxReadyTasksLists", 0x20000140 + data_offset),
        ),
    )


def _profile():
    profile, reason = build_profile(_image())
    assert reason == "ok", reason
    assert profile is not None
    return profile


class _Memory(dict[int, int]):
    def read(self, address: int, size: int) -> bytes | None:
        values = []
        for offset in range(0, size, 4):
            value = self.get(address + offset)
            if value is None:
                return None
            values.append(value.to_bytes(4, "little"))
        return b"".join(values)


def _write_context(
    memory: _Memory,
    top: int,
    *,
    core: tuple[int, ...],
    pc: int = 0x1000,
    lr: int = EXIT_PC,
    xpsr: int = 0x01000000,
) -> None:
    words = list(core) + [0, 0, 0, 0, 0, lr, pc, xpsr]
    for index, value in enumerate(words):
        memory[top + index * 4] = value


def _registers(overrides: dict[int, int] | None = None) -> dict[int, int]:
    registers = {register: 0x4000 + register for register in range(13)}
    registers.update({13: TASK_HIGH, 14: 0xFFFFFFFD, 15: 0x1000})
    if overrides:
        registers.update(overrides)
    return registers


def _stage(profile, name: str) -> SchedulerStage:
    stage = classify_stage(profile, getattr(profile, _STAGE_NAMES[name]))
    assert isinstance(stage, SchedulerStage), stage
    assert stage.name == name, stage
    return stage


def _build(
    profile,
    stage,
    memory,
    registers,
    *,
    psp,
    xpsr,
    current=TASK_TCB,
    context: SavedStackContext = ARMV7M_INTEGER,
    is_entry=lambda address: address == ENTRY_PC,
    stkalign=True,
    task_top=TASK_TOP,
):
    return build_stage_seed(
        profile,
        stage,
        read=memory.read,
        context=context,
        task_tcb=TASK_TCB,
        task_top=task_top,
        task_low=TASK_LOW,
        task_high=TASK_HIGH,
        psp=psp,
        msp=TASK_HIGH,
        registers=registers,
        xpsr=xpsr,
        stkalign=stkalign,
        current_tcb=current,
        exit_pc=EXIT_PC,
        is_entry=is_entry,
    )


def _saved_memory(core: tuple[int, ...] = tuple(0x40 + i for i in range(8))):
    memory = _Memory()
    _write_context(memory, TASK_TOP, core=core)
    return memory


def _matching_registers():
    return _registers({register: 0x40 + register - 4 for register in range(4, 12)})


def test_compiled_sequences_yield_every_exact_stage():
    profile = _profile()
    for name in _STAGE_NAMES:
        _stage(profile, name)
    pending = classify_stage(profile, profile.start_first_svc)
    assert isinstance(pending, SchedulerStage)
    assert pending.name == "start-first-svc-pending" and pending.seed == "none"


def test_mismatched_truncated_or_unsupported_profiles_are_refused():
    profile, reason = build_profile(replace(_image(), machine=243))
    assert profile is None and reason == "scheduler-profile-unsupported-image"

    profile, reason = build_profile(
        _image(
            symbols=(
                ("SVC_Handler", SVC_START, len(SVC)),
                ("prvPortStartFirstTask", START_START, len(START_FIRST)),
                ("vTaskSwitchContext", SWITCH_START, len(SWITCH)),
            )
        )
    )
    assert profile is None and "symbol-missing" in reason

    profile, reason = build_profile(
        _image(
            symbols=(
                ("PendSV_Handler", PENDSV_START, 30),
                ("SVC_Handler", SVC_START, len(SVC)),
                ("prvPortStartFirstTask", START_START, len(START_FIRST)),
                ("vTaskSwitchContext", SWITCH_START, len(SWITCH)),
            )
        )
    )
    assert profile is None and "PendSV_Handler" in reason

    broken = bytearray(PENDSV)
    broken[2:4] = (0x8008).to_bytes(2, "little")
    profile, reason = build_profile(_image(pendsv=bytes(broken)))
    assert profile is None and "PendSV_Handler" in reason

    broken = bytearray(SWITCH)
    broken[0xE0:0xE2] = (0x0000).to_bytes(2, "little")
    profile, reason = build_profile(_image(switch=bytes(broken)))
    assert profile is None and "vTaskSwitchContext" in reason


def test_ambiguous_instruction_inside_a_verified_range_is_refused():
    profile = _profile()
    assert classify_stage(profile, profile.pendsv_start + 2).startswith(
        "scheduler-stage-ambiguous"
    )
    assert classify_stage(profile, profile.switch_start + 2).startswith(
        "scheduler-stage-ambiguous"
    )
    assert classify_stage(profile, 0xDEAD) is None


def test_new_task_seed_from_saved_tcb_and_restore_window():
    profile = _profile()
    memory = _saved_memory()
    registers = _registers({0: TASK_TOP})
    seed = _build(
        profile,
        _stage(profile, "pendsv-saved-sp"),
        memory,
        registers,
        psp=TASK_LOW + 0x100,
        xpsr=0x0100000E,
    )
    assert not isinstance(seed, str), seed
    assert seed.seed.source == "saved"
    assert seed.seed.sp == TASK_TOP + 64

    memory = _saved_memory()
    registers = _matching_registers()
    registers[0] = TASK_TOP + 0x20
    stage = _stage(profile, "pendsv-restored-pending-psp")
    seed = _build(
        profile, stage, memory, registers, psp=TASK_LOW + 0x100, xpsr=0x0100000E
    )
    assert not isinstance(seed, str), seed
    assert seed.seed.pc == 0x1000
    assert seed.seed.sp == TASK_TOP + 64

    registers[0] = TASK_TOP
    refusal = _build(
        profile, stage, memory, registers, psp=TASK_LOW + 0x100, xpsr=0x0100000E
    )
    assert refusal == "scheduler-restore-window-inconsistent"

    stage = _stage(profile, "pendsv-exception-return")
    registers[0] = 0
    registers[14] = 0xFFFFFFFD
    seed = _build(
        profile, stage, memory, registers, psp=TASK_TOP + 0x20, xpsr=0x0100000E
    )
    assert not isinstance(seed, str), seed
    registers[14] = 0xFFFFFFF9
    refusal = _build(
        profile, stage, memory, registers, psp=TASK_TOP + 0x20, xpsr=0x0100000E
    )
    assert refusal == "scheduler-stack-selection-unproven"


def test_old_task_seed_before_and_after_the_save_boundary():
    profile = _profile()
    memory = _saved_memory()
    psp = TASK_TOP + 0x20
    registers = _registers()
    seed = _build(
        profile,
        _stage(profile, "pendsv-entry"),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000E,
    )
    assert not isinstance(seed, str), seed
    assert seed.seed.source == "current"
    assert seed.seed.sp == TASK_TOP + 64

    stage = _stage(profile, "pendsv-writeback")
    seed = _build(profile, stage, memory, registers, psp=psp, xpsr=0x0100000E)
    assert not isinstance(seed, str), seed
    assert seed.seed.source == "saved"
    refusal = _build(profile, stage, memory, registers, psp=psp + 4, xpsr=0x0100000E)
    assert refusal == "scheduler-saved-sp-inconsistent"


def test_unstarted_context_selected_by_pendsv_is_refused():
    profile = _profile()
    memory = _Memory()
    fill = (0xA5A5A5A5,) * 8
    _write_context(memory, TASK_TOP, core=fill, pc=ENTRY_PC)
    refusal = _build(
        profile,
        _stage(profile, "pendsv-saved-sp"),
        memory,
        _registers({0: TASK_TOP}),
        psp=TASK_LOW + 0x100,
        xpsr=0x0100000E,
    )
    assert refusal == "scheduler-unstarted-selection-unproven"


def test_switch_entry_and_update_build_old_and_new_task_seeds():
    profile = _profile()
    memory = _saved_memory()
    registers = _registers()
    registers[14] = profile.switch_return | 1

    old = _build(
        profile,
        _stage(profile, "switch-entry"),
        memory,
        registers,
        psp=TASK_TOP + 0x20,
        xpsr=0x0100000E,
    )
    assert not isinstance(old, str), old
    assert old.seed.source == "saved" and old.owner == "old"
    assert old.seed.pc == 0x1000

    registers[14] = 0x2000
    refusal = _build(
        profile,
        _stage(profile, "switch-entry"),
        memory,
        registers,
        psp=TASK_TOP + 0x20,
        xpsr=0x0100000E,
    )
    assert refusal == "scheduler-switch-call-unproven"

    registers[14] = profile.switch_return
    new = _build(
        profile,
        _stage(profile, "switch-after-update"),
        memory,
        registers,
        psp=TASK_LOW + 0x100,
        xpsr=0x0100000E,
    )
    assert not isinstance(new, str), new
    assert new.seed.source == "saved" and new.owner == "new"


def test_first_start_requires_the_port_initial_context():
    profile = _profile()
    memory = _Memory()
    fill = (0xA5A5A5A5,) * 8
    _write_context(memory, TASK_TOP, core=fill, pc=ENTRY_PC)
    registers = _registers(
        {**{register: 0xA5A5A5A5 for register in range(4, 12)}, 14: 0xFFFFFFF9}
    )
    stage = _stage(profile, "svc-psp-written")
    seed = _build(
        profile, stage, memory, registers, psp=TASK_TOP + 0x20, xpsr=0x0100000B
    )
    assert not isinstance(seed, str), seed
    assert seed.seed.source == "unstarted"
    assert seed.seed.root_pc == ENTRY_PC and seed.seed.root_reason == "task-entry"

    memory[TASK_TOP + 4 * 4] = 0x11111111
    refusal = _build(
        profile, stage, memory, registers, psp=TASK_TOP + 0x20, xpsr=0x0100000B
    )
    assert refusal == "scheduler-saved-buffer-inconsistent"

    memory = _Memory()
    _write_context(memory, TASK_TOP, core=fill, pc=ENTRY_PC)
    seed = _build(
        profile,
        _stage(profile, "svc-entry"),
        memory,
        registers,
        psp=0,
        xpsr=0x0100000B,
    )
    assert not isinstance(seed, str), seed
    assert seed.seed.source == "unstarted"


def test_handler_mode_mismatch_is_refused():
    profile = _profile()
    memory = _saved_memory()
    registers = _matching_registers()
    stage = _stage(profile, "pendsv-psp-written")
    registers[13] = TASK_LOW
    refusal = _build(
        profile, stage, memory, registers, psp=TASK_TOP + 0x20, xpsr=0x0100000E
    )
    assert refusal == "scheduler-handler-mode-unproven"
    registers[13] = TASK_HIGH
    refusal = _build(
        profile, stage, memory, registers, psp=TASK_TOP + 0x20, xpsr=0x01000000
    )
    assert refusal == "scheduler-handler-mode-unproven"


def test_current_tcb_and_missing_buffer_are_refused():
    profile = _profile()
    memory = _saved_memory()
    registers = _matching_registers()
    registers[0] = TASK_TOP + 0x20
    stage = _stage(profile, "pendsv-restored-pending-psp")
    refusal = _build(
        profile,
        stage,
        memory,
        registers,
        psp=TASK_LOW + 0x100,
        xpsr=0x0100000E,
        current=TASK_TCB + 4,
    )
    assert refusal == "scheduler-current-tcb-mismatch"

    memory.clear()
    refusal = _build(
        profile,
        _stage(profile, "pendsv-saved-sp"),
        memory,
        _registers({0: TASK_TOP}),
        psp=TASK_LOW + 0x100,
        xpsr=0x0100000E,
    )
    assert refusal in {
        "no-saved-context",
        "saved-context-unreadable",
        "scheduler-context-unreadable",
    }


def _first_start_inputs(stage_name):
    memory = _Memory()
    fill = (0xA5A5A5A5,) * 8
    _write_context(memory, TASK_TOP, core=fill, pc=ENTRY_PC)
    registers = _registers(
        {**{register: 0xA5A5A5A5 for register in range(4, 12)}, 14: 0xFFFFFFF9}
    )
    psp = 0
    if stage_name in {"svc-restored-pending-psp", "svc-psp-written"}:
        registers[0] = TASK_TOP + 0x20
    if stage_name in {"svc-psp-written", "svc-exception-return"}:
        psp = TASK_TOP + 0x20
    if stage_name == "svc-exception-return":
        registers[14] = 0xFFFFFFFD
    return memory, registers, psp


@pytest.mark.parametrize(
    "stage_name",
    [
        "svc-entry",
        "svc-saved-sp",
        "svc-restored-pending-psp",
        "svc-psp-written",
        "svc-exception-return",
    ],
)
@pytest.mark.parametrize("pc", [ENTRY_PC + 2, 0x1000])
def test_every_first_start_stage_rejects_a_nonentry_pc(stage_name, pc):
    profile = _profile()
    memory, registers, psp = _first_start_inputs(stage_name)
    _write_context(memory, TASK_TOP, core=(0xA5A5A5A5,) * 8, pc=pc)
    refusal = _build(
        profile,
        _stage(profile, stage_name),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000B,
    )
    assert refusal == "scheduler-first-start-entry-unproven", refusal


@pytest.mark.parametrize(
    "stage_name",
    [
        "svc-entry",
        "svc-saved-sp",
        "svc-restored-pending-psp",
        "svc-psp-written",
        "svc-exception-return",
    ],
)
def test_every_first_start_stage_requires_entry_evidence(stage_name):
    profile = _profile()
    memory, registers, psp = _first_start_inputs(stage_name)
    refusal = _build(
        profile,
        _stage(profile, stage_name),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000B,
        is_entry=None,
    )
    assert refusal == "scheduler-first-start-entry-unproven", refusal


@pytest.mark.parametrize(
    "stage_name",
    [
        "pendsv-restored-pending-psp",
        "pendsv-psp-written",
        "pendsv-exception-return",
    ],
)
def test_pendsv_never_claims_an_unstarted_selection(stage_name):
    profile = _profile()
    memory = _Memory()
    fill = (0xA5A5A5A5,) * 8
    _write_context(memory, TASK_TOP, core=fill, pc=ENTRY_PC)
    registers = _registers({**{register: 0xA5A5A5A5 for register in range(4, 12)}})
    if stage_name == "pendsv-restored-pending-psp":
        registers[0] = TASK_TOP + 0x20
        psp = TASK_LOW + 0x100
    elif stage_name == "pendsv-psp-written":
        psp = TASK_TOP + 0x20
    else:
        psp = TASK_TOP + 0x20
        registers[14] = 0xFFFFFFFD
    refusal = _build(
        profile,
        _stage(profile, stage_name),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000E,
    )
    assert refusal == "scheduler-unstarted-selection-unproven", refusal


@pytest.mark.parametrize(
    "xpsr,lr,stkalign,reason",
    [
        (0x0000000E, 0xFFFFFFFD, True, "scheduler-frame-not-thumb"),
        (0x0100000E, 0xFFFFFFF9, True, "scheduler-stack-selection-unproven"),
        (0x0100000E, 0xFFFFFFED, True, "unsupported-extension-context"),
        (0x0100020E, 0xFFFFFFFD, False, "exception-frame-alignment-invalid"),
    ],
)
def test_pendsv_entry_requires_thumb_and_process_exc_return(xpsr, lr, stkalign, reason):
    profile = _profile()
    memory = _saved_memory()
    registers = _registers({14: lr})
    refusal = _build(
        profile,
        _stage(profile, "pendsv-entry"),
        memory,
        registers,
        psp=TASK_TOP + 0x20,
        xpsr=xpsr,
        stkalign=stkalign,
    )
    assert refusal == reason, refusal


def test_svc_stages_require_the_thread_main_return_before_orr():
    profile = _profile()
    memory, registers, psp = _first_start_inputs("svc-psp-written")
    registers[14] = 0xFFFFFFFD
    refusal = _build(
        profile,
        _stage(profile, "svc-psp-written"),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000B,
    )
    assert refusal == "scheduler-stack-selection-unproven", refusal
    memory, registers, psp = _first_start_inputs("svc-exception-return")
    registers[14] = 0xFFFFFFF9
    refusal = _build(
        profile,
        _stage(profile, "svc-exception-return"),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000B,
    )
    assert refusal == "scheduler-stack-selection-unproven", refusal


def test_profile_rejects_big_endian_and_mid_instruction_end():
    profile, reason = build_profile(_image(endian="big"))
    assert profile is None and reason == "scheduler-profile-unsupported-endian"
    profile, reason = build_profile(
        _image(
            symbols=(
                ("PendSV_Handler", PENDSV_START, 0x3D),
                ("SVC_Handler", SVC_START, len(SVC)),
                ("prvPortStartFirstTask", START_START, len(START_FIRST)),
                ("vTaskSwitchContext", SWITCH_START, len(SWITCH)),
            )
        )
    )
    assert profile is None and "PendSV_Handler" in reason


def test_profile_rejects_store_source_nop_entry_and_missing_literal():
    broken = bytearray(SWITCH)
    broken[0xA0:0xA2] = (0x601B).to_bytes(2, "little")
    profile, reason = build_profile(_image(switch=bytes(broken)))
    assert profile is None and "vTaskSwitchContext" in reason

    broken = bytearray(SWITCH)
    broken[0:2] = (0xBF00).to_bytes(2, "little")
    profile, reason = build_profile(_image(switch=bytes(broken)))
    assert profile is None and "vTaskSwitchContext" in reason

    broken = bytearray(SWITCH)
    broken[0x20:0x24] = (0).to_bytes(4, "little")
    profile, reason = build_profile(_image(switch=bytes(broken)))
    assert profile is None and "vTaskSwitchContext" in reason


def test_profile_rejects_zero_size_duplicate_and_overlapping_symbols():
    profile, reason = build_profile(
        _image(
            symbols=(
                ("PendSV_Handler", PENDSV_START, 0),
                ("SVC_Handler", SVC_START, len(SVC)),
                ("prvPortStartFirstTask", START_START, len(START_FIRST)),
                ("vTaskSwitchContext", SWITCH_START, len(SWITCH)),
            )
        )
    )
    assert profile is None and "PendSV_Handler" in reason

    profile, reason = build_profile(
        _image(
            symbols=(
                ("PendSV_Handler", PENDSV_START, len(PENDSV)),
                ("PendSV_Handler", PENDSV_START + 2, len(PENDSV)),
                ("SVC_Handler", SVC_START, len(SVC)),
                ("prvPortStartFirstTask", START_START, len(START_FIRST)),
                ("vTaskSwitchContext", SWITCH_START, len(SWITCH)),
            )
        )
    )
    assert profile is None and "PendSV_Handler" in reason

    profile, reason = build_profile(
        _image(
            symbols=(
                ("PendSV_Handler", SVC_START, 0x30),
                ("SVC_Handler", SVC_START, len(SVC)),
                ("prvPortStartFirstTask", START_START, len(START_FIRST)),
                ("vTaskSwitchContext", SWITCH_START, len(SWITCH)),
            )
        )
    )
    assert profile is None and "overlap" in reason


_DATA_VALUES = (
    0x200000A4,
    0x200000B8,
    0x2000009C,
    0x200000A0,
    0x200001B8,
    0x200000C4,
    0x20000140,
)


def _relocate_data(body: bytes, delta: int) -> bytes:
    data = bytearray(body)
    for offset in range(0, len(data) - 3, 4):
        value = int.from_bytes(data[offset : offset + 4], "little")
        if value in _DATA_VALUES:
            data[offset : offset + 4] = (value + delta).to_bytes(4, "little")
    return bytes(data)


def test_profile_is_independent_of_linked_code_addresses():
    base = _profile()
    relocated, reason = build_profile(_image(offset=0x1000))
    assert reason == "ok" and relocated is not None
    assert relocated.pendsv_entry == base.pendsv_entry + 0x1000
    assert relocated.switch_update == base.switch_update + 0x1000
    assert relocated.fingerprint == base.fingerprint


def test_profile_is_independent_of_linked_data_addresses():
    base = _profile()
    relocated, reason = build_profile(
        _image(
            pendsv=_relocate_data(PENDSV, 0x1000),
            svc=_relocate_data(SVC, 0x1000),
            switch=_relocate_data(SWITCH, 0x1000),
            data_offset=0x1000,
        )
    )
    assert reason == "ok" and relocated is not None
    assert relocated.fingerprint == base.fingerprint
    assert relocated.switch_update == base.switch_update


@pytest.mark.parametrize(
    "first,mnemonic,load,direction,wback",
    [
        (0xE920, "stmdb", False, "db", True),
        (0xE930, "ldmdb", True, "db", True),
        (0xE900, "stmdb", False, "db", False),
        (0xE910, "ldmdb", True, "db", False),
        (0xE8A0, "stmia", False, "ia", True),
        (0xE8B0, "ldmia", True, "ia", True),
        (0xE880, "stmia", False, "ia", False),
        (0xE890, "ldmia", True, "ia", False),
    ],
)
def test_block_transfer_decoder_exposes_operation_direction_and_writeback(
    first, mnemonic, load, direction, wback
):
    instruction = _decode_thumb32(0x1000, first, 0x0FF0)
    assert instruction.mnemonic == mnemonic
    assert instruction.operands == {
        "rn": 0,
        "wback": wback,
        "load": load,
        "direction": direction,
        "reglist": 0x0FF0,
    }


@pytest.mark.parametrize("first", [0xE8C0, 0xE9C0, 0xE800])
def test_block_transfer_decoder_refuses_non_transfer_encodings(first):
    assert _decode_thumb32(0x1000, first, 0x0FF0).mnemonic == "other"


def test_block_transfer_semantic_tokens_distinguish_direction_and_writeback():
    elf = _image()
    original = _semantic_token(elf, _decode_thumb32(0x1000, 0xE920, 0x0FF0))
    assert original != _semantic_token(elf, _decode_thumb32(0x1000, 0xE930, 0x0FF0))
    assert original != _semantic_token(elf, _decode_thumb32(0x1000, 0xE900, 0x0FF0))
    assert original != _semantic_token(elf, _decode_thumb32(0x1000, 0xE920, 0x0FE0))


def test_msr_mrs_and_bl_decoders_require_exact_encodings():
    assert _decode_thumb32(0x1000, 0xF380, 0x8811).mnemonic == "msr"
    assert _decode_thumb32(0x1000, 0xF380, 0x8011).mnemonic == "other"
    assert _decode_thumb32(0x1000, 0xF380, 0x9811).mnemonic == "other"
    assert _decode_thumb32(0x1000, 0xF3EF, 0x8009).mnemonic == "mrs"
    assert _decode_thumb32(0x1000, 0xF3EF, 0x9009).mnemonic == "other"
    branch = _decode_thumb32(0x1000, 0xF7FE, 0xFBCC)
    other_target = _decode_thumb32(0x1000, 0xF7FE, 0xDBCC)
    assert branch.mnemonic == "bl" and other_target.mnemonic == "bl"
    assert branch.operands["target"] != other_target.operands["target"]
    assert _decode_thumb32(0x1000, 0xF7FE, 0xEBCC).mnemonic == "other"
    assert _decode_thumb32(0x1000, 0xF7FE, 0x1BCC).mnemonic == "other"


def _mutate_halfword(body: bytes, offset: int, xor: int) -> bytes:
    changed = bytearray(body)
    value = int.from_bytes(changed[offset : offset + 2], "little") ^ xor
    changed[offset : offset + 2] = value.to_bytes(2, "little")
    return bytes(changed)


@pytest.mark.parametrize(
    "function,offset",
    [
        ("PendSV_Handler", 0x0C),
        ("PendSV_Handler", 0x28),
        ("PendSV_Handler", 0x30),
        ("SVC_Handler", 0x06),
    ],
)
@pytest.mark.parametrize(
    "xor,second_halfword",
    [(0x0010, False), (0x0020, False), (0x0001, False), (0x0010, True)],
)
def test_profile_refuses_block_transfer_direction_writeback_base_and_list_changes(
    function, offset, xor, second_halfword
):
    body = PENDSV if function == "PendSV_Handler" else SVC
    changed = _mutate_halfword(body, offset + (2 if second_halfword else 0), xor)
    image = (
        _image(pendsv=changed) if function == "PendSV_Handler" else _image(svc=changed)
    )
    profile, reason = build_profile(image)
    assert profile is None and function in reason


def test_profile_refuses_msr_mask_and_branch_target_encoding_changes():
    profile, reason = build_profile(
        _image(pendsv=_mutate_halfword(PENDSV, 0x1A, 0x0800))
    )
    assert profile is None and "PendSV_Handler" in reason
    profile, reason = build_profile(_image(svc=_mutate_halfword(SVC, 0x18, 0x0800)))
    assert profile is None and "SVC_Handler" in reason
    profile, reason = build_profile(
        _image(pendsv=_mutate_halfword(PENDSV, 0x1E, 0x1000))
    )
    assert profile is None and "PendSV_Handler" in reason
    profile, reason = build_profile(
        _image(pendsv=_mutate_halfword(PENDSV, 0x1C, 0x0400))
    )
    assert profile is None and "PendSV_Handler" in reason
    profile, reason = build_profile(
        _image(pendsv=_mutate_halfword(PENDSV, 0x02, 0x1000))
    )
    assert profile is None and "PendSV_Handler" in reason


_HARDWARE_STAGES = (
    "pendsv-entry",
    "pendsv-save-written",
    "pendsv-writeback",
    "pendsv-saved-sp",
    "pendsv-restored-pending-psp",
    "pendsv-psp-written",
)


def _hardware_frame_inputs(stage_name, top, *, xpsr):
    memory = _Memory()
    _write_context(
        memory,
        top,
        core=tuple(0x40 + index for index in range(8)),
        xpsr=xpsr,
    )
    registers = _matching_registers()
    psp = top + 0x20
    if stage_name == "pendsv-save-written":
        registers[0] = top
    elif stage_name == "pendsv-saved-sp":
        registers[0] = top
        psp = top + 0x40
    elif stage_name == "pendsv-restored-pending-psp":
        registers[0] = top + 0x20
        psp = top + 0x40
    return memory, registers, psp


@pytest.mark.parametrize("stage_name", _HARDWARE_STAGES)
@pytest.mark.parametrize(
    "stkalign,padding,top_offset,expected_offset",
    [
        (True, False, 0, 0),
        (True, True, 0, 4),
        (False, False, 0, 0),
        (False, False, 4, 0),
    ],
)
def test_hardware_stages_accept_consistent_alignment_combinations(
    stage_name, stkalign, padding, top_offset, expected_offset
):
    top = TASK_TOP + top_offset
    frame_xpsr = 0x01000200 if padding else 0x01000000
    memory, registers, psp = _hardware_frame_inputs(stage_name, top, xpsr=frame_xpsr)
    outcome = _build(
        _profile(),
        _stage(_profile(), stage_name),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000E,
        stkalign=stkalign,
        task_top=top,
    )
    assert not isinstance(outcome, str), outcome
    assert outcome.seed.sp == top + 64 + expected_offset


@pytest.mark.parametrize("stage_name", _HARDWARE_STAGES)
@pytest.mark.parametrize(
    "stkalign,padding,top_offset",
    [
        (True, False, 4),
        (True, True, 4),
        (False, True, 0),
        (False, True, 4),
    ],
)
def test_hardware_stages_refuse_inconsistent_alignment_combinations(
    stage_name, stkalign, padding, top_offset
):
    top = TASK_TOP + top_offset
    frame_xpsr = 0x01000200 if padding else 0x01000000
    memory, registers, psp = _hardware_frame_inputs(stage_name, top, xpsr=frame_xpsr)
    outcome = _build(
        _profile(),
        _stage(_profile(), stage_name),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000E,
        stkalign=stkalign,
        task_top=top,
    )
    assert outcome == "exception-frame-alignment-invalid", outcome


_FIRST_START_STAGES = (
    "svc-entry",
    "svc-saved-sp",
    "svc-restored-pending-psp",
    "svc-psp-written",
    "svc-exception-return",
)


@pytest.mark.parametrize("stage_name", _FIRST_START_STAGES)
@pytest.mark.parametrize("stkalign", [True, False])
@pytest.mark.parametrize("top_offset", [0, 4])
def test_first_start_software_frame_ignores_ccr_and_has_no_padding(
    stage_name, stkalign, top_offset
):
    top = TASK_TOP + top_offset
    memory = _Memory()
    fill = (0xA5A5A5A5,) * 8
    _write_context(memory, top, core=fill, pc=ENTRY_PC)
    registers = _registers(
        {**{register: 0xA5A5A5A5 for register in range(4, 12)}, 14: 0xFFFFFFF9}
    )
    psp = 0
    if stage_name in {"svc-restored-pending-psp", "svc-psp-written"}:
        registers[0] = top + 0x20
    if stage_name in {"svc-psp-written", "svc-exception-return"}:
        psp = top + 0x20
    if stage_name == "svc-exception-return":
        registers[14] = 0xFFFFFFFD
    outcome = _build(
        _profile(),
        _stage(_profile(), stage_name),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000B,
        stkalign=stkalign,
        task_top=top,
    )
    assert not isinstance(outcome, str), outcome
    assert outcome.seed.source == "unstarted"
    assert outcome.seed.sp == top + 64


@pytest.mark.parametrize("stage_name", _FIRST_START_STAGES)
def test_first_start_software_frame_refuses_a_stacked_padding_flag(stage_name):
    top = TASK_TOP
    memory = _Memory()
    fill = (0xA5A5A5A5,) * 8
    _write_context(memory, top, core=fill, pc=ENTRY_PC, xpsr=0x01000200)
    registers = _registers(
        {**{register: 0xA5A5A5A5 for register in range(4, 12)}, 14: 0xFFFFFFF9}
    )
    psp = 0
    if stage_name in {"svc-restored-pending-psp", "svc-psp-written"}:
        registers[0] = top + 0x20
    if stage_name in {"svc-psp-written", "svc-exception-return"}:
        psp = top + 0x20
    if stage_name == "svc-exception-return":
        registers[14] = 0xFFFFFFFD
    outcome = _build(
        _profile(),
        _stage(_profile(), stage_name),
        memory,
        registers,
        psp=psp,
        xpsr=0x0100000B,
        stkalign=True,
        task_top=top,
    )
    assert outcome == "exception-frame-alignment-invalid", outcome
