"""Arm Cortex-M0, Cortex-M4F, and Cortex-M33 FreeRTOS scheduler-transition tests."""

from __future__ import annotations

import pytest

from core.elf import SHF_EXECINSTR, CodeSymbol, ElfImage, ElfSection
from freertos.backtrace.arm_scheduler_switch import (
    SchedulerStage,
    build_profile,
    classify_stage,
)
from freertos.backtrace.scheduler_seed import build_stage_seed
from freertos.backtrace.unwind import (
    ARMV6M_INTEGER,
    ARMV7M_CM4F_BASIC,
    ARMV7M_CM4F_FPU,
    ARMV8M_MAINLINE_INTEGER,
)
from tests.support.loader import cached_elfs, fixture_cache_root

# Compiled GCC port sequences captured from the FreeRTOS fixtures.
CM0_PENDSV = bytes.fromhex(
    "eff309800e4a1168243808607346f8c044464d4656465f46f0c072b6fef78cfa"
    "62b6074a116808681430f0c8a046a946b246bb4680f309882438f8c81847c046"
)
CM0_SVC = bytes.fromhex(
    "074a1168086804c8022181f31488203080f30988bff36f8f1047c046c046c046"
)
CM0_START = bytes.fromhex("62b6bff34f8fbff36f8f64dfc046c046")
CM0_SWITCH = bytes.fromhex(
    "10b5354b1b68002b03d0344b01221a6010bd324b00221a60fff79afb304b1860"
    "1a68304b1b689a4207d92d4a1268d21a2d4b19680b6e9b180b66294b1a68294b"
    "1a60294b1b681b6b1968284a914202d15968914208d0244b196834311868fff7"
    "4ff8234b1c6807e099689142f3d1da688a42f0d1f5e7013c1e4aa3001b199b00"
    "9b58002b05d1002cf5d11b48fef7d8fff1e71100a3001a1992008a1850684068"
    "50601b199b0008335b1898420cd0114ba200121992009b185b68da680a4b1a60"
    "0b4a14601b68a3e7a200121992008b18da685a60ebe7c046ac000020c0000020"
    "a4000020a8000020c0010020a5a5a5a5cc00002048010020fa130000"
)

CM4F_PENDSV = bytes.fromhex(
    "eff30980bff36f8f134b1a681ef0100f08bf20ed108a20e9f04f106009b44ff0"
    "040080f31188bff34f8fbff36f8ffef769fb4ff0000080f3118809bc19680868"
    "b0e8f04f1ef0100f08bfb0ec108a80f30988bff36f8f7047c0010020"
)
CM4F_SVC = bytes.fromhex(
    "064b19680868b0e8f04f80f30988bff36f8f4ff0000080f311887047c0010020"
)
CM4F_START = bytes.fromhex(
    "08480068006880f308884ff0000080f3148862b661b6bff34f8fbff36f8f00df00bf000008ed00e0"
)
CM4F_SWITCH = bytes.fromhex(
    "394b1b681bb1394b01221a60704710b5364b00221a60fff75dfb354b18601a68"
    "344b1b689a4207d9314a1268d21a324b19680b6e13440b662d4b1a682d4b1a60"
    "2d4b1a68116b1a6812681b681b6b9a4203d90b68b3f1a53f2cd0274b19681868"
    "3431fff71bf8254b1c68b4fa84f4e4b2c4f11f0404eb8402214b53f8223033b3"
    "1f4904eb840301eb83035a6852685a6004eb84039b0008330b449a421cd004eb"
    "8404174b03eb84035b68da68124b1a601b6810bd4b68b3f1a53fced18b68b3f1"
    "a53fcad1cb68b3f1a53fc6d1cbe741f29940fef77dffd3e704eb840201eb8203"
    "da685a60dbe700bfac000020c0000020a4000020a8000020c0010020cc000020"
    "48010020"
)

CM33_PENDSV = bytes.fromhex(
    "eff30980eff30b82734620e9fc0f0d4a116808604ff0040080f31188bff34f8f"
    "bff36f8ffef762fa4ff0000080f31188044a11680868b0e8fc0f82f30b8880f3"
    "09881847"
)
CM33_SVC = bytes.fromhex(
    "0a4a1168086806c881f30b88eff3148151f0020181f31488203080f30988bff3"
    "6f8f4ff0000080f311881047"
)
CM33_START = bytes.fromhex("06480068006880f3088862b661b6bff34f8fbff36f8f66df00bf")
CM33_SWITCH = bytes.fromhex(
    "384b1b681bb1384b01221a60704710b5354b00221a60fff77ffb344b18601a68"
    "334b1b689a4207d9304a1268d21a314b19680b6e13440b662c4b1a682c4b1a60"
    "2c4b1a68116b1a6812681b681b6b9a4203d90b68b3f1a53f2bd0264b19681868"
    "3431fff74ff8244b1c68b4fa84f4c4f11f0404eb8402214b53f8223033b31f49"
    "04eb840301eb83035a6852685a6004eb84039b0008330b449a421cd004eb8404"
    "164b03eb84035b68da68124b1a601b6810bd4b68b3f1a53fcfd18b68b3f1a53f"
    "cbd1cb68b3f1a53fc7d1cce741f29940fef7b6ffd3e704eb840201eb8203da68"
    "5a60dbe7a4000028b80000289c000028a0000028b8010028c400002840010028"
)

TASK_TCB = 0x20001000
TASK_LOW = 0x20001000
TASK_HIGH = 0x20003000
EXIT_PC = 0x0500
ENTRY_PC = 0x0800


def _cm0_image(*, pendsv: bytes = CM0_PENDSV, svc: bytes = CM0_SVC) -> ElfImage:
    base = bytearray(0x4000)
    base[0x11E8 : 0x11E8 + len(CM0_SWITCH)] = CM0_SWITCH
    base[0x2C60 : 0x2C60 + len(svc)] = svc
    base[0x2C80:0x2C84] = (0x200001C0).to_bytes(4, "little")
    base[0x2C90 : 0x2C90 + len(CM0_START)] = CM0_START
    base[0x2CB0 : 0x2CB0 + len(pendsv)] = pendsv
    base[0x2CF0:0x2CF4] = (0x200001C0).to_bytes(4, "little")
    return ElfImage(
        pointer_size=4,
        endian="little",
        machine=40,
        sections=(ElfSection(".text", 0, bytes(base), SHF_EXECINSTR, 1),),
        symbols=(
            CodeSymbol("vTaskSwitchContext", 0x11E8, len(CM0_SWITCH)),
            CodeSymbol("vRestoreContextOfFirstTask", 0x2C60, len(CM0_SVC)),
            CodeSymbol("vStartFirstTask", 0x2C90, len(CM0_START)),
            CodeSymbol("PendSV_Handler", 0x2CB0, len(CM0_PENDSV)),
            CodeSymbol("task_entry", ENTRY_PC, 0x20),
        ),
        anchors=(("pxCurrentTCB", 0x200001C0),),
    )


def _cm4f_image(*, pendsv: bytes = CM4F_PENDSV, svc: bytes = CM4F_SVC) -> ElfImage:
    base = bytearray(0x4000)
    base[0x1228 : 0x1228 + len(CM4F_SWITCH)] = CM4F_SWITCH
    base[0x2ADC : 0x2ADC + len(svc)] = svc
    base[0x2AFC : 0x2AFC + len(CM4F_START)] = CM4F_START
    base[0x2B24 : 0x2B24 + len(pendsv)] = pendsv
    return ElfImage(
        pointer_size=4,
        endian="little",
        machine=40,
        sections=(ElfSection(".text", 0, bytes(base), SHF_EXECINSTR, 1),),
        symbols=(
            CodeSymbol("vTaskSwitchContext", 0x1228, len(CM4F_SWITCH)),
            CodeSymbol("SVC_Handler", 0x2ADC, len(CM4F_SVC)),
            CodeSymbol("prvPortStartFirstTask", 0x2AFC, len(CM4F_START)),
            CodeSymbol("PendSV_Handler", 0x2B24, len(CM4F_PENDSV)),
            CodeSymbol("task_entry", ENTRY_PC, 0x20),
        ),
        anchors=(("pxCurrentTCB", 0x200001C0),),
    )


def _cm33_image(*, pendsv: bytes = CM33_PENDSV, svc: bytes = CM33_SVC) -> ElfImage:
    text_base = 0x10000000
    base = bytearray(0x4000)
    base[0x1174 : 0x1174 + len(CM33_SWITCH)] = CM33_SWITCH
    base[0x2C14 : 0x2C14 + len(svc)] = svc
    base[0x2C40:0x2C44] = (0x280001B8).to_bytes(4, "little")
    base[0x2C44 : 0x2C44 + len(CM33_START)] = CM33_START
    base[0x2C60:0x2C64] = (0xE000ED08).to_bytes(4, "little")
    base[0x2C88 : 0x2C88 + len(pendsv)] = pendsv
    base[0x2CCC:0x2CD0] = (0x280001B8).to_bytes(4, "little")
    return ElfImage(
        pointer_size=4,
        endian="little",
        machine=40,
        sections=(ElfSection(".text", text_base, bytes(base), SHF_EXECINSTR, 1),),
        symbols=(
            CodeSymbol("vTaskSwitchContext", text_base + 0x1174, len(CM33_SWITCH)),
            CodeSymbol("vRestoreContextOfFirstTask", text_base + 0x2C14, len(CM33_SVC)),
            CodeSymbol("vStartFirstTask", text_base + 0x2C44, len(CM33_START)),
            CodeSymbol("PendSV_Handler", text_base + 0x2C88, len(CM33_PENDSV)),
            CodeSymbol("task_entry", text_base + ENTRY_PC, 0x20),
        ),
        anchors=(("pxCurrentTCB", 0x280001B8),),
    )


class _Memory(dict[int, int]):
    def read(self, address: int, size: int) -> bytes | None:
        chunks = []
        for offset in range(0, size, 4):
            word = self.get(address + offset)
            if word is None:
                return None
            chunks.append(word.to_bytes(4, "little"))
        return b"".join(chunks)


def _write_basic_frame(
    mem: _Memory, base: int, *, core: tuple[int, ...], pc: int, lr: int, exc: int | None
) -> None:
    words = []
    if exc is not None:
        words.append(exc)
    words.extend(core)
    if exc == 0xFFFFFFFD and len(words) == 8:
        pass
    hw = [0x01010101, 0, 0, 0, 0, lr, pc, 0x01000000]
    for idx, word in enumerate(words + hw):
        mem[base + idx * 4] = word


@pytest.mark.parametrize(
    ("builder", "expected_port", "expected_svc", "has_psp_written"),
    [
        (_cm0_image, "arm-cm0", "vRestoreContextOfFirstTask", True),
        (_cm4f_image, "arm-cm4f", "SVC_Handler", True),
        (_cm33_image, "arm-cm33", "vRestoreContextOfFirstTask", False),
    ],
)
def test_build_profile_and_classify_ports(
    builder, expected_port, expected_svc, has_psp_written
):
    profile, reason = build_profile(builder())
    assert reason == "ok" and profile is not None, reason
    assert profile.port == expected_port
    assert profile.svc_symbol == expected_svc
    assert (profile.pendsv_psp_written is not None) is has_psp_written

    for addr, expected_name in (
        (profile.pendsv_entry, "pendsv-entry"),
        (profile.pendsv_writeback, "pendsv-writeback"),
        (profile.pendsv_restored_pending_psp, "pendsv-restored-pending-psp"),
        (profile.pendsv_exception_return, "pendsv-exception-return"),
        (profile.svc_entry, "svc-entry"),
        (profile.svc_restored_pending_psp, "svc-restored-pending-psp"),
        (profile.svc_exception_return, "svc-exception-return"),
        (profile.switch_update, "switch-before-update"),
        (profile.switch_after_update, "switch-after-update"),
    ):
        stage = classify_stage(profile, addr)
        assert not isinstance(stage, str) and stage is not None
        assert stage.name == expected_name

    ambiguous = classify_stage(profile, profile.pendsv_entry + 4)
    assert isinstance(ambiguous, str) and ambiguous.startswith(
        "scheduler-stage-ambiguous:"
    )


def test_mutated_port_bodies_are_refused():
    # Corrupt one word inside PendSV so verification rejects instead of guessing.
    bad_cm0 = CM0_PENDSV[:4] + b"\x00\xbf\x00\xbf" + CM0_PENDSV[8:]
    profile, reason = build_profile(_cm0_image(pendsv=bad_cm0))
    assert profile is None and reason.startswith("scheduler-profile-mismatch:")

    bad_cm4f = CM4F_PENDSV[:8] + b"\x00\xbf\x00\xbf" + CM4F_PENDSV[12:]
    profile, reason = build_profile(_cm4f_image(pendsv=bad_cm4f))
    assert profile is None and reason.startswith("scheduler-profile-mismatch:")

    bad_cm33 = CM33_SVC[:4] + b"\x00\xbf\x00\xbf" + CM33_SVC[8:]
    profile, reason = build_profile(_cm33_image(svc=bad_cm33))
    assert profile is None and reason.startswith("scheduler-profile-mismatch:")


def test_cm0_and_cm33_first_start_buffer_accepts_thumb_entry_and_cm0_partial_restore():
    cm0_prof, _ = build_profile(_cm0_image())
    assert cm0_prof is not None
    # Simulate an unstarted CM0 task with Thumb entry bit set in hardware frame PC.
    top = 0x20002000
    mem = _Memory()
    init_core = ARMV6M_INTEGER.initial_registers or ()
    _write_basic_frame(
        mem, top, core=init_core, pc=ENTRY_PC | 1, lr=EXIT_PC | 1, exc=0xFFFFFFFD
    )
    svc_stage = classify_stage(cm0_prof, cm0_prof.svc_restored_pending_psp)
    assert not isinstance(svc_stage, str) and svc_stage is not None
    outcome = build_stage_seed(
        cm0_prof,
        svc_stage,
        read=mem.read,
        context=ARMV6M_INTEGER,
        task_tcb=TASK_TCB,
        task_top=top,
        task_low=TASK_LOW,
        task_high=TASK_HIGH,
        psp=0,
        msp=0x20004000,
        registers={
            **{r: 0 for r in range(4, 12)},
            0: top + 36,
            2: 0xFFFFFFFD,
            13: 0x20004000,
            14: 0xFFFFFFF9,
        },
        xpsr=0x0100000B,
        stkalign=True,
        current_tcb=TASK_TCB,
        exit_pc=EXIT_PC,
        is_entry=lambda pc: pc == ENTRY_PC,
    )
    assert not isinstance(outcome, str), outcome
    assert outcome.seed.source == "unstarted"
    assert outcome.seed.pc == ENTRY_PC

    # Partial CM0 restore at pendsv-restored-pending-psp has only r8..r11 live in regs.
    # Software block is 36B so top ending in +4 gives an 8-byte aligned HW frame.
    pendsv_top = 0x20002004
    saved = tuple(0x10 + i for i in range(8))
    _write_basic_frame(
        mem, pendsv_top, core=saved, pc=0x1000, lr=0x1200 | 1, exc=0xFFFFFFFD
    )
    pendsv_stage = classify_stage(cm0_prof, cm0_prof.pendsv_restored_pending_psp)
    assert not isinstance(pendsv_stage, str) and pendsv_stage is not None
    regs = {r: saved[r - 4] for r in range(8, 12)}
    regs.update({4: saved[4], 5: saved[5], 6: saved[6], 7: saved[7]})
    regs.update({0: pendsv_top + 36, 13: 0x20004000, 14: 0xFFFFFFF9})
    outcome = build_stage_seed(
        cm0_prof,
        pendsv_stage,
        read=mem.read,
        context=ARMV6M_INTEGER,
        task_tcb=TASK_TCB,
        task_top=pendsv_top,
        task_low=TASK_LOW,
        task_high=TASK_HIGH,
        psp=0x20001800,
        msp=0x20004000,
        registers=regs,
        xpsr=0x0100000E,
        stkalign=True,
        current_tcb=TASK_TCB,
        exit_pc=EXIT_PC,
        is_entry=lambda pc: pc == ENTRY_PC,
    )
    assert not isinstance(outcome, str), outcome
    assert outcome.seed.pc == 0x1000

    # CM33 first-start buffer uses r2 as EXC_RETURN and 40-byte software header.
    cm33_prof, _ = build_profile(_cm33_image())
    assert cm33_prof is not None
    cm33_top = 0x20002100
    cm33_mem = _Memory()
    cm33_mem[cm33_top] = 0x20001000  # PSPLIM
    cm33_mem[cm33_top + 4] = 0xFFFFFFFD  # EXC_RETURN
    _write_basic_frame(
        cm33_mem,
        cm33_top + 8,
        core=ARMV8M_MAINLINE_INTEGER.initial_registers or (),
        pc=(0x10000000 + ENTRY_PC) | 1,
        lr=EXIT_PC | 1,
        exc=None,
    )
    cm33_stage = classify_stage(cm33_prof, cm33_prof.svc_restored_pending_psp)
    assert not isinstance(cm33_stage, str) and cm33_stage is not None
    outcome = build_stage_seed(
        cm33_prof,
        cm33_stage,
        read=cm33_mem.read,
        context=ARMV8M_MAINLINE_INTEGER,
        task_tcb=TASK_TCB,
        task_top=cm33_top,
        task_low=TASK_LOW,
        task_high=TASK_HIGH,
        psp=0,
        msp=0x20004000,
        registers={
            **{r: 0 for r in range(4, 12)},
            0: cm33_top + 40,
            2: 0xFFFFFFFD,
            13: 0x20004000,
            14: 0xFFFFFFB8,
        },
        xpsr=0x0100000B,
        stkalign=True,
        current_tcb=TASK_TCB,
        exit_pc=EXIT_PC,
        is_entry=lambda pc: pc == 0x10000000 + ENTRY_PC,
    )
    assert not isinstance(outcome, str), outcome
    assert outcome.seed.source == "unstarted"

    # CM4F extended EXC_RETURN mismatch refuses instead of mis-decoding as basic.
    cm4f_prof, _ = build_profile(_cm4f_image())
    assert cm4f_prof is not None
    cm4f_top = 0x20002200
    cm4f_mem = _Memory()
    for off in range(0, 168, 4):
        cm4f_mem[cm4f_top + off] = 0
    cm4f_mem[cm4f_top + 32] = 0xFFFFFFED  # extended EXC_RETURN
    cm4f_stage = classify_stage(cm4f_prof, cm4f_prof.pendsv_writeback)
    assert not isinstance(cm4f_stage, str) and cm4f_stage is not None
    refused = build_stage_seed(
        cm4f_prof,
        cm4f_stage,
        read=cm4f_mem.read,
        context=ARMV7M_CM4F_BASIC,
        task_tcb=TASK_TCB,
        task_top=cm4f_top,
        task_low=TASK_LOW,
        task_high=TASK_HIGH,
        psp=cm4f_top + 36,
        msp=0x20004000,
        registers={
            **{r: 0 for r in range(4, 12)},
            13: 0x20004000,
            14: 0xFFFFFFFD,
        },  # basic live LR conflicts
        xpsr=0x0100000E,
        stkalign=True,
        current_tcb=TASK_TCB,
        exit_pc=EXIT_PC,
        is_entry=None,
        fpu_context=ARMV7M_CM4F_FPU,
    )
    assert isinstance(refused, str)


def test_cm0_and_cm33_pendsv_admit_unstarted_tasks_with_thumb_bit_pc():
    cm0_prof, _ = build_profile(_cm0_image())
    assert cm0_prof is not None
    cm0_top = 0x20002004
    cm0_mem = _Memory()
    cm0_init = ARMV6M_INTEGER.initial_registers or ()
    _write_basic_frame(
        cm0_mem,
        cm0_top,
        core=cm0_init,
        pc=ENTRY_PC | 1,
        lr=EXIT_PC | 1,
        exc=0xFFFFFFFD,
    )
    cm0_regs = {r: cm0_init[r - 4] for r in range(4, 12)}
    cm0_regs.update({0: cm0_top + 36, 3: 0xFFFFFFFD, 13: 0x20004000, 14: 0xFFFFFFF9})
    for stage_name, psp in (
        ("pendsv-saved-sp", 0x20001800),
        ("pendsv-restored-pending-psp", 0x20001800),
        ("pendsv-exception-return", cm0_top + 36),
    ):
        stage = classify_stage(
            cm0_prof, getattr(cm0_prof, stage_name.replace("-", "_"))
        )
        assert isinstance(stage, SchedulerStage)
        seed = build_stage_seed(
            cm0_prof,
            stage,
            read=cm0_mem.read,
            context=ARMV6M_INTEGER,
            task_tcb=TASK_TCB,
            task_top=cm0_top,
            task_low=TASK_LOW,
            task_high=TASK_HIGH,
            psp=psp,
            msp=0x20004000,
            registers={
                **cm0_regs,
                0: cm0_top if stage_name == "pendsv-saved-sp" else cm0_top + 36,
            },
            xpsr=0x0100000E,
            stkalign=True,
            current_tcb=TASK_TCB,
            exit_pc=EXIT_PC,
            is_entry=lambda pc: pc == ENTRY_PC,
        )
        assert not isinstance(seed, str), (stage_name, seed)
        assert seed.seed.source == "unstarted"
        assert seed.seed.pc == ENTRY_PC
        assert seed.seed.root_pc == ENTRY_PC
        assert seed.seed.root_reason == "task-entry"

    cm33_prof, _ = build_profile(_cm33_image())
    assert cm33_prof is not None
    cm33_top = 0x20002100
    cm33_entry = 0x10000000 + ENTRY_PC
    cm33_mem = _Memory()
    cm33_init = ARMV8M_MAINLINE_INTEGER.initial_registers or ()
    cm33_mem[cm33_top] = 0x20001000
    cm33_mem[cm33_top + 4] = 0xFFFFFFFD
    _write_basic_frame(
        cm33_mem,
        cm33_top + 8,
        core=cm33_init,
        pc=cm33_entry | 1,
        lr=EXIT_PC | 1,
        exc=None,
    )
    cm33_regs = {r: cm33_init[r - 4] for r in range(4, 12)}
    cm33_regs.update({3: 0xFFFFFFFD, 13: 0x20004000, 14: 0xFFFFFFB8})
    for stage_name, psp in (
        ("pendsv-saved-sp", 0x20001800),
        ("pendsv-restored-pending-psp", 0x20001800),
        ("pendsv-exception-return", cm33_top + 40),
    ):
        stage = classify_stage(
            cm33_prof, getattr(cm33_prof, stage_name.replace("-", "_"))
        )
        assert isinstance(stage, SchedulerStage)
        seed = build_stage_seed(
            cm33_prof,
            stage,
            read=cm33_mem.read,
            context=ARMV8M_MAINLINE_INTEGER,
            task_tcb=TASK_TCB,
            task_top=cm33_top,
            task_low=TASK_LOW,
            task_high=TASK_HIGH,
            psp=psp,
            msp=0x20004000,
            registers={
                **cm33_regs,
                0: cm33_top if stage_name == "pendsv-saved-sp" else cm33_top + 40,
            },
            xpsr=0x0100000E,
            stkalign=True,
            current_tcb=TASK_TCB,
            exit_pc=EXIT_PC,
            is_entry=lambda pc: pc == cm33_entry,
        )
        assert not isinstance(seed, str), (stage_name, seed)
        assert seed.seed.source == "unstarted"
        assert seed.seed.pc == cm33_entry
        assert seed.seed.root_pc == cm33_entry
        assert seed.seed.root_reason == "task-entry"


_CACHED_PORT_ELFS = tuple(
    path
    for path in cached_elfs()
    if path.name == "freertos.elf"
    and path.relative_to(fixture_cache_root()).parts[:2]
    in {
        ("freertos", "microbit"),
        ("freertos", "mps2-an386"),
        ("freertos", "mps2-an505"),
    }
    and "mpu" not in str(path)
    and "smp" not in str(path)
)


@pytest.mark.skipif(not _CACHED_PORT_ELFS, reason="no cached CM0/CM4F/CM33 fixtures")
@pytest.mark.parametrize(
    "path", _CACHED_PORT_ELFS, ids=lambda p: "/".join(p.parts[-5:-2])
)
def test_cached_non_cm3_port_fixtures_prove_profiles(path):
    image = ElfImage.from_path(path)
    assert image is not None
    profile, reason = build_profile(image)
    assert reason == "ok" and profile is not None, reason
