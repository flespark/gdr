"""RT-Thread Arm M-Profile scheduler-switch profile and seed unit tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.elf import SHF_EXECINSTR, CodeSymbol, ElfImage, ElfSection
from core.stack_frame_parser import StackContextSelection
from rtthread.backtrace.arm_scheduler_switch import (
    build_profile,
    classify_stage,
)
from rtthread.backtrace.backtrace import (
    ARMV6M_INTEGER,
    ARMV7EM_FPU,
    ARMV7EM_INTEGER,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_FPU,
    ARMV8M_MAINLINE_INTEGER,
)
from rtthread.backtrace.scheduler_seed import SchedulerSeed, build_stage_seed
from tests.support.loader import cached_elfs, fixture_cache_root
from tests.unit.core.oracle_support import attributes_blob

# Compiled RT-Thread context_gcc.S switch + literal pools per M-profile port.
_CM0_SWITCH = bytes.fromhex(
    "2d4a1368012b03d0012313602b4a10602b4a11602b482b4901607047eff3108272b62448"
    "016800291dd0002101602248016800290ad0eff30981203900680160f0c144464d465646"
    "5f46f0c11c4909680968f0c9f0b4f0c9a046a946b246bb46f0bc81f3098882f310880320"
    "40420047134908601149002008600e4901200860124812490268114301600d480e490160"
    "c0460f4800680068c04680f3088862b67047c046eff3098000b500f05af800bdeff30580"
    "7047045f0020fc5e0020005f002004ed00e00000001020ed00e00000ffff08ed00e0"
)
_CM3_SWITCH = bytes.fromhex(
    "354a1368012b04d04ff001031360324a1060324a116032484ff0805101607047eff31082"
    "72b62b48016891b14ff0000101602948016829b1eff3098121e9f00f0068016026490968"
    "0968b1e8f00f81f3098882f310884ef0040e7047204908601e494ff0000008601b494ff0"
    "010008601d481d49d0f8002041ea0201016018484ff08051016019480068006800bf80f3"
    "088861b662b6704700bfeff308801ef0040f01d0eff3098020e9f00f40f804ed1ef0040f"
    "02d080f3098801e080f3088800b5fff7e4fe5df804eb4ef0040e7047eff305807047045f"
    "0020fc5e0020005f002004ed00e020ed00e00000ffff08ed00e0"
)
_CM4F_SWITCH = bytes.fromhex(
    "444a1368012b04d04ff001031360414a1060414a116041484ff0805101607047eff31082"
    "72b63a48016851b34ff0000101603848016891b1eff309811ef0100f08bf21ed108b21e9"
    "f00f4ff000041ef0100f08bf012441f8044d006801602e490968096808c9b1e8f00f002b"
    "18bfb1ec108b81f309884ef0100e002b18bf2ef0100e82f310884ef0040e704723490860"
    "eff3148222f0040282f314881e494ff0000008601b494ff0010008601d481d49d0f80020"
    "41ea0201016018484ff08051016019480068006800bf80f3088861b662b6704700bfeff3"
    "08801ef0040f01d0eff3098020e9f00f40f804ed40f804ed1ef0040f02d080f3098801e0"
    "80f3088800b500f05af85df804eb4ef0040e70470000a0630020986300209c63002004ed"
    "00e020ed00e00000ffff08ed00e0"
)
_CM33_INT_SWITCH = bytes.fromhex(
    "544a1368012b04d04ff001031360514a1060514a116051484ff0805101607047eff31082"
    "72b64a480168002902d182f31088704704b44ff0000101604648016831b347490968a1b1"
    "03b50846fff757ffbde80340724612f0400f0ad0eff30b83eff31484eff3098525e91e00"
    "006805600ee0eff3098121e9f00f394a12687346eff30b84eff3148521e93c0000680160"
    "3249096809683cc984f30b8885f314889e462f4e3260104640b10ab4fff732ff0abc9e46"
    "13f0400f00d001e0b1e8f00f81f3098804bc82f3108870472349086021494ff000000860"
    "1e494ff00100086021482149d0f8002041ea020101601b484ff0805101601d4800680068"
    "00bf80f3088861b662b6704700bfeff308801ef0040f01d0eff3098020e9f00f114a1268"
    "7346eff30b84eff3148520e93c0040f804ed1ef0040f02d080f3098801e080f3088800b5"
    "fff7edfd5df804eb4ef0040e7047688a0028608a0028648a002804ed00e0708a002820ed"
    "00e00000ffff08ed00e0"
)
_CM33_FPU_SWITCH = bytes.fromhex(
    "5c4a1368012b04d04ff001031360594a1060594a116059484ff0805101607047eff31082"
    "72b652480168002902d182f31088704704b44ff0000101604e48016859b34f490968a1b1"
    "03b50846fff757ffbde80340724612f0400f0ad0eff30b83eff31484eff3098525e91e00"
    "0068056013e0eff309811ef0100f08bf21ed108b21e9f00f3f4a12687346eff30b84eff3"
    "148521e93c00006801603749096809683cc984f30b8885f314889e46354e3260104640b1"
    "0ab4fff72dff0abc9e4613f0400f00d006e0b1e8f00f1ef0100f08bfb1ec108b81f30988"
    "04bc82f31088704726490860eff3148222f0040282f3148821494ff0000008601e494ff0"
    "0100086021482149d0f8002041ea020101601b484ff0805101601d480068006800bf80f3"
    "088861b662b6704700bfeff308801ef0040f01d0eff3098020e9f00f114a12687346eff3"
    "0b84eff3148520e93c0040f804ed1ef0040f02d080f3098801e080f3088800b5fff7ddfd"
    "5df804eb4ef0040e704748930028409300284493002804ed00e05093002820ed00e00000"
    "ffff08ed00e0"
)


@dataclass(frozen=True)
class _PortFixture:
    name: str
    arch: str
    blob: bytes
    base: int
    pendsv_off: int
    switch_to_off: int
    flag: int
    from_ptr: int
    to_ptr: int
    tz: int | None
    selection: StackContextSelection


_FIXTURES = (
    _PortFixture(
        name="armv6-m",
        arch="armv6-m",
        blob=_CM0_SWITCH,
        base=0x5B2A,
        pendsv_off=0x1C,
        switch_to_off=0x70,
        flag=0x20005F04,
        from_ptr=0x20005EFC,
        to_ptr=0x20005F00,
        tz=None,
        selection=StackContextSelection(
            "armv6-m", "arm", ARMV6M_INTEGER, "ok", abi="aapcs32"
        ),
    ),
    _PortFixture(
        name="armv7-m",
        arch="armv7-m",
        blob=_CM3_SWITCH,
        base=0x583E,
        pendsv_off=0x20,
        switch_to_off=0x5C,
        flag=0x20005F04,
        from_ptr=0x20005EFC,
        to_ptr=0x20005F00,
        tz=None,
        selection=StackContextSelection(
            "armv7-m", "arm", ARMV7M_INTEGER, "ok", abi="aapcs32"
        ),
    ),
    _PortFixture(
        name="armv7e-m-fpu",
        arch="armv7e-m",
        blob=_CM4F_SWITCH,
        base=0x57DE,
        pendsv_off=0x20,
        switch_to_off=0x8C,
        flag=0x200063A0,
        from_ptr=0x20006398,
        to_ptr=0x2000639C,
        tz=None,
        selection=StackContextSelection(
            "armv7e-m",
            "arm",
            ARMV7EM_INTEGER,
            "ok",
            abi="aapcs32-fpu",
            alt_context=ARMV7EM_FPU,
        ),
    ),
    _PortFixture(
        name="armv8m-integer",
        arch="armv8-m.mainline",
        blob=_CM33_INT_SWITCH,
        base=0x10005752,
        pendsv_off=0x20,
        switch_to_off=0xCC,
        flag=0x28008A68,
        from_ptr=0x28008A60,
        to_ptr=0x28008A64,
        tz=0x28008A70,
        selection=StackContextSelection(
            "armv8-m.mainline", "arm", ARMV8M_MAINLINE_INTEGER, "ok", abi="aapcs32"
        ),
    ),
    _PortFixture(
        name="armv8m-fpu",
        arch="armv8-m.mainline",
        blob=_CM33_FPU_SWITCH,
        base=0x10005876,
        pendsv_off=0x20,
        switch_to_off=0xE0,
        flag=0x28009348,
        from_ptr=0x28009340,
        to_ptr=0x28009344,
        tz=0x28009350,
        selection=StackContextSelection(
            "armv8-m.mainline",
            "arm",
            ARMV8M_MAINLINE_INTEGER,
            "ok",
            abi="aapcs32-fpu",
            alt_context=ARMV8M_MAINLINE_FPU,
        ),
    ),
)


_CPU_ARCH_TAG = {
    "armv6-m": 12,
    "armv7-m": 10,
    "armv7e-m": 13,
    "armv8-m.mainline": 17,
}


def _elf_for(fix: _PortFixture, *, blob: bytes | None = None) -> ElfImage:
    code = fix.blob if blob is None else blob
    base = fix.base
    attrs = [(6, _CPU_ARCH_TAG[fix.arch]), (7, "M")]
    if fix.name.endswith("-fpu"):
        attrs.append((10, 6))  # FPv4/FPv5 D16
    anchors: list[tuple[str, int]] = [
        ("rt_hw_context_switch", base),
        ("PendSV_Handler", base + fix.pendsv_off),
        ("rt_hw_context_switch_to", base + fix.switch_to_off),
        ("rt_thread_switch_interrupt_flag", fix.flag),
        ("rt_interrupt_from_thread", fix.from_ptr),
        ("rt_interrupt_to_thread", fix.to_ptr),
    ]
    if fix.tz is not None:
        anchors.append(("rt_trustzone_current_context", fix.tz))
    return ElfImage(
        endian="little",
        pointer_size=4,
        machine=40,
        sections=(
            ElfSection(".text", base, code, SHF_EXECINSTR, 1),
            ElfSection(
                ".ARM.attributes",
                0,
                attributes_blob(b"aeabi", tuple(attrs)),
                0,
                0x70000003,
            ),
        ),
        symbols=(
            CodeSymbol("rt_hw_context_switch", base, fix.pendsv_off),
            CodeSymbol(
                "PendSV_Handler",
                base + fix.pendsv_off,
                fix.switch_to_off - fix.pendsv_off,
            ),
            CodeSymbol("rt_hw_context_switch_to", base + fix.switch_to_off, 0x40),
        ),
        anchors=tuple(anchors),
    )


def _memory_reader(
    mem: bytearray, base: int, *, tz_addr: int | None = None, tz: int = 0
):
    def read(addr: int, size: int) -> bytes | None:
        if tz_addr is not None and addr == tz_addr and size == 4:
            return tz.to_bytes(4, "little")
        off = addr - base
        if off < 0 or off + size > len(mem):
            return None
        return bytes(mem[off : off + size])

    return read


def _write_u32(mem: bytearray, base: int, addr: int, val: int) -> None:
    off = addr - base
    mem[off : off + 4] = val.to_bytes(4, "little")


def _build_saved_frame(
    mem: bytearray,
    base: int,
    *,
    top: int,
    fix: _PortFixture,
    fpu: bool,
    pc: int,
    lr: int,
    fill: int = 0x11110000,
) -> tuple[int, dict[int, int]]:
    regs = {r: (fill if fill == 0xDEADBEEF else fill + r) for r in range(4, 12)}
    if fix.arch in ("armv6-m", "armv7-m"):
        sw_size = 32
        for r in range(4, 12):
            _write_u32(mem, base, top + (r - 4) * 4, regs[r])
    elif fix.arch == "armv7e-m":
        flag = 1 if fpu else 0
        sw_size = 100 if fpu else 36
        _write_u32(mem, base, top, flag)
        for r in range(4, 12):
            _write_u32(mem, base, top + 4 + (r - 4) * 4, regs[r])
    else:
        exc_ret = 0xFFFFFFED if fpu else 0xFFFFFFFD
        sw_size = 112 if fpu else 48
        _write_u32(mem, base, top, 0)
        _write_u32(mem, base, top + 4, exc_ret)
        _write_u32(mem, base, top + 8, 0)
        _write_u32(mem, base, top + 12, 2)
        for r in range(4, 12):
            _write_u32(mem, base, top + 16 + (r - 4) * 4, regs[r])
    hw = top + sw_size
    for idx, val in enumerate((0xA0, 0xA1, 0xA2, 0xA3, 0xAC, lr | 1, pc, 0x01000000)):
        _write_u32(mem, base, hw + idx * 4, val)
    return hw, regs


@pytest.mark.parametrize("fix", _FIXTURES, ids=lambda f: f.name)
def test_build_profile_accepts_all_five_ports_and_rejects_mutations(fix: _PortFixture):
    prof = build_profile(_elf_for(fix))
    assert prof is not None
    assert prof.port == fix.name
    assert classify_stage(prof, prof.pendsv_entry).name == "pendsv-entry"
    assert classify_stage(prof, prof.switch_return).name == "switch-return"
    assert (
        classify_stage(prof, prof.pendsv_exception_return).name
        == "pendsv-exception-return"
    )
    assert classify_stage(prof, prof.pendsv_entry + 2).name == "ambiguous"
    assert classify_stage(prof, prof.switch_start + 2).name == "ambiguous"
    assert classify_stage(prof, prof.switch_to_start).name == "ambiguous"
    assert classify_stage(prof, 0x80000) is None

    # Mutate one instruction byte inside PendSV_Handler -> must refuse.
    mutated = bytearray(fix.blob)
    mutated[fix.pendsv_off] ^= 0x01
    mutated[fix.pendsv_off + 1] ^= 0x20
    assert build_profile(_elf_for(fix, blob=bytes(mutated))) is None

    # Mutate literal word pointing to rt_thread_switch_interrupt_flag -> must refuse.
    bad_sym = bytearray(fix.blob)
    flag_bytes = fix.flag.to_bytes(4, "little")
    idx = bad_sym.find(flag_bytes)
    assert idx != -1
    bad_sym[idx] ^= 0x10
    assert build_profile(_elf_for(fix, blob=bytes(bad_sym))) is None


@pytest.mark.parametrize("fix", _FIXTURES, ids=lambda f: f.name)
def test_stage_seeds_old_new_and_first_start_for_each_port(fix: _PortFixture):
    prof = build_profile(_elf_for(fix))
    assert prof is not None
    stack_base = 0x20000000
    mem = bytearray(0x2000)
    sp_offset = 128
    old_tcb = stack_base + 0x100
    new_tcb = stack_base + 0x200
    other_tcb = stack_base + 0x300
    old_low, old_high = stack_base + 0x400, stack_base + 0x800
    new_low, new_high = stack_base + 0x800, stack_base + 0xC00
    other_low, other_high = stack_base + 0xC00, stack_base + 0x1000

    fpu_mode = fix.name.endswith("-fpu")
    old_sp = old_high - 256
    new_sp = new_high - 256
    other_sp = other_high - 256
    old_hw, old_regs = _build_saved_frame(
        mem, stack_base, top=old_sp, fix=fix, fpu=fpu_mode, pc=0x4020, lr=0x4010
    )
    new_hw, new_regs = _build_saved_frame(
        mem, stack_base, top=new_sp, fix=fix, fpu=fpu_mode, pc=0x5020, lr=0x5010
    )
    _build_saved_frame(
        mem, stack_base, top=other_sp, fix=fix, fpu=False, pc=0x6020, lr=0x6010
    )
    read = _memory_reader(mem, stack_base, tz_addr=fix.tz, tz=0)

    def resolve(
        tcb: int,
        t_sp: int,
        entry: int | None,
        low: int,
        high: int,
        *,
        flag: int,
        from_ptr: int,
        pc: int,
        psp: int,
        exc: int,
        regs: dict[int, int],
        tz_override: int | None = None,
        control: int | None = None,
    ):
        r_fn = _memory_reader(
            mem,
            stack_base,
            tz_addr=fix.tz,
            tz=0 if tz_override is None else tz_override,
        )
        return build_stage_seed(
            prof,
            fix.selection,
            flag_value=flag,
            from_ptr=from_ptr,
            to_ptr=new_tcb + sp_offset,
            sp_offset=sp_offset,
            thread_addr=tcb,
            thread_sp=t_sp,
            thread_entry=entry,
            current_addr=new_tcb,
            low=low,
            high=high,
            endian="little",
            stkalign=True,
            read=r_fn,
            live_pc=pc,
            live_sp=0x20001800 if exc else psp,
            live_psp=psp,
            live_exc=exc,
            live_regs=regs,
            live_psplim=0,
            live_control=(6 if fpu_mode else 2) if control is None else control,
        )

    # Idle outside switch -> None.
    assert (
        resolve(
            new_tcb,
            new_sp,
            0x5000,
            new_low,
            new_high,
            flag=0,
            from_ptr=old_tcb + sp_offset,
            pc=0x4020,
            psp=old_hw,
            exc=0,
            regs={**old_regs, 13: old_hw, 14: 0x4011},
        )
        is None
    )

    # Active IRQ calling rt_hw_context_switch -> old keeps exception_number=19, new stays saved.
    old_irq = resolve(
        old_tcb,
        old_high - 120,
        0x4000,
        old_low,
        old_high,
        flag=1,
        from_ptr=old_tcb + sp_offset,
        pc=prof.switch_return,
        psp=old_hw,
        exc=19,
        regs={**old_regs, 14: 0xFFFFFFFD},
    )
    assert isinstance(old_irq, SchedulerSeed)
    assert old_irq.stage == "switch-return" and old_irq.owner == "old"
    assert old_irq.seed.exception_number == 19 and old_irq.seed.process_sp == old_hw

    new_irq = resolve(
        new_tcb,
        new_sp,
        0x5000,
        new_low,
        new_high,
        flag=1,
        from_ptr=old_tcb + sp_offset,
        pc=prof.switch_return,
        psp=old_hw,
        exc=19,
        regs={**old_regs, 14: 0xFFFFFFFD},
    )
    assert isinstance(new_irq, SchedulerSeed)
    assert new_irq.owner == "new" and new_irq.seed.source == "saved"
    assert new_irq.seed.pc == 0x5020

    # PendSV entry -> old decodes live HW frame on PSP; third thread decodes saved.
    entry_lr = 0xFFFFFFED if fpu_mode else 0xFFFFFFFD
    old_entry = resolve(
        old_tcb,
        old_high - 120,
        0x4000,
        old_low,
        old_high,
        flag=1,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_entry,
        psp=old_hw,
        exc=14,
        regs={**old_regs, 14: entry_lr},
    )
    assert isinstance(old_entry, SchedulerSeed)
    assert old_entry.stage == "pendsv-entry" and old_entry.seed.pc == 0x4020
    assert (
        old_entry.seed.source == "current" and old_entry.seed.exception_number is None
    )

    other_entry = resolve(
        other_tcb,
        other_sp,
        0x6000,
        other_low,
        other_high,
        flag=1,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_entry,
        psp=old_hw,
        exc=14,
        regs={**old_regs, 14: entry_lr},
    )
    assert isinstance(other_entry, SchedulerSeed)
    assert other_entry.owner == "other" and other_entry.seed.pc == 0x6020

    # pendsv-save-written where present -> old uses saved-buffer at r1, mismatch fails closed.
    if prof.pendsv_save_written is not None:
        save_written_regs = {**old_regs, 0: old_tcb + sp_offset, 1: old_sp}
        bad_reg = 4
        if fix.arch == "armv7e-m":
            save_written_regs[4] = 1 if fpu_mode else 0
            bad_reg = 5
        elif fix.arch == "armv8-m.mainline":
            save_written_regs[2] = 0
            save_written_regs[3] = 0xFFFFFFED if fpu_mode else 0xFFFFFFFD
            save_written_regs[4] = 0
            save_written_regs[5] = 2
            bad_reg = 6
        buf_res = resolve(
            old_tcb,
            old_high - 120,
            0x4000,
            old_low,
            old_high,
            flag=0,
            from_ptr=old_tcb + sp_offset,
            pc=prof.pendsv_save_written,
            psp=old_hw,
            exc=14,
            regs=save_written_regs,
        )
        assert isinstance(buf_res, SchedulerSeed)
        assert buf_res.stage == "pendsv-save-written"
        assert buf_res.seed.source == "saved-buffer" and buf_res.seed.pc == 0x4020

        bad_buf = resolve(
            old_tcb,
            old_high - 120,
            0x4000,
            old_low,
            old_high,
            flag=0,
            from_ptr=old_tcb + sp_offset,
            pc=prof.pendsv_save_written,
            psp=old_hw,
            exc=14,
            regs={**save_written_regs, bad_reg: 0x12345678},
        )
        assert bad_buf == "scheduler-save-window-inconsistent"
        if fix.arch in ("armv7e-m", "armv8-m.mainline"):
            header_reg = 4 if fix.arch == "armv7e-m" else 3
            bad_hdr = resolve(
                old_tcb,
                old_high - 120,
                0x4000,
                old_low,
                old_high,
                flag=0,
                from_ptr=old_tcb + sp_offset,
                pc=prof.pendsv_save_written,
                psp=old_hw,
                exc=14,
                regs={**save_written_regs, header_reg: 0x12345678},
            )
            assert bad_hdr == "scheduler-save-window-inconsistent"

    # pendsv-restored-pending-psp -> new decodes live from r1; r4 mismatch refuses.
    restored_regs = {**new_regs, 1: new_hw}
    if fix.arch == "armv7e-m":
        restored_regs[3] = 1 if fpu_mode else 0
    if fix.arch == "armv8-m.mainline":
        restored_regs[14] = 0xFFFFFFED if fpu_mode else 0xFFFFFFFD
    restored_ok = resolve(
        new_tcb,
        new_sp,
        0x5000,
        new_low,
        new_high,
        flag=0,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_restored_pending_psp,
        psp=old_hw,
        exc=14,
        regs=restored_regs,
        control=0 if fix.arch == "armv8-m.mainline" else 2,
    )
    assert isinstance(restored_ok, SchedulerSeed)
    assert restored_ok.stage == "pendsv-restored-pending-psp"
    assert restored_ok.seed.source == "current" and restored_ok.seed.pc == 0x5020

    bad_restored = resolve(
        new_tcb,
        new_sp,
        0x5000,
        new_low,
        new_high,
        flag=0,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_restored_pending_psp,
        psp=old_hw,
        exc=14,
        regs={**restored_regs, 4: 0x99999999},
    )
    assert bad_restored == "scheduler-restore-window-inconsistent"

    # pendsv-exception-return + unstarted first-start (from_ptr == 0; rt_hw_stack_init keeps Thumb bit 0).
    unstarted_hw, unstarted_regs = _build_saved_frame(
        mem,
        stack_base,
        top=new_sp,
        fix=fix,
        fpu=fpu_mode,
        pc=0x5001,
        lr=0xFFFFFFFE,
        fill=0xDEADBEEF,
    )
    ret_regs = {**unstarted_regs}
    if fix.arch == "armv6-m":
        ret_regs[0] = 0xFFFFFFFD
    elif fix.arch == "armv7-m":
        ret_regs[14] = 0xFFFFFFFD
    elif fix.arch == "armv7e-m":
        ret_regs[3] = 1 if fpu_mode else 0
        ret_regs[14] = 0xFFFFFFED if fpu_mode else 0xFFFFFFFD
    else:
        ret_regs[14] = 0xFFFFFFED if fpu_mode else 0xFFFFFFFD
    first_ret = resolve(
        new_tcb,
        new_sp,
        0x5000,
        new_low,
        new_high,
        flag=0,
        from_ptr=0,
        pc=prof.pendsv_exception_return,
        psp=unstarted_hw,
        exc=14,
        regs=ret_regs,
    )
    assert isinstance(first_ret, SchedulerSeed)
    assert first_ret.seed.source == "unstarted" and first_ret.seed.pc == 0x5000

    # Thumb bit 0 on a non-unstarted frame (missing fill pattern) refuses.
    bad_pc = resolve(
        new_tcb,
        new_sp,
        0x9999,
        new_low,
        new_high,
        flag=0,
        from_ptr=0,
        pc=prof.pendsv_exception_return,
        psp=unstarted_hw,
        exc=14,
        regs=ret_regs,
    )
    assert bad_pc == "scheduler-frame-pc-invalid"

    # TrustZone non-zero on CM33 fails closed.
    if fix.tz is not None:
        tz_err = resolve(
            new_tcb,
            new_sp,
            0x5000,
            new_low,
            new_high,
            flag=1,
            from_ptr=old_tcb + sp_offset,
            pc=prof.pendsv_entry,
            psp=old_hw,
            exc=14,
            regs={**old_regs, 14: entry_lr},
            tz_override=1,
        )
        assert tz_err == "scheduler-trustzone-unsupported"

    # Ambiguous instruction inside PendSV fails closed.
    ambiguous = resolve(
        new_tcb,
        new_sp,
        0x5000,
        new_low,
        new_high,
        flag=1,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_entry + 2,
        psp=old_hw,
        exc=14,
        regs=old_regs,
    )
    assert ambiguous == "scheduler-stage-ambiguous:PendSV_Handler"
    assert read is not None


_CACHED_RTT_M_ELFS = tuple(
    path
    for path in cached_elfs()
    if path.name == "rtthread.elf"
    and path.relative_to(fixture_cache_root()).parts[:2]
    in {
        ("rtthread", "microbit"),
        ("rtthread", "mps2-an385"),
        ("rtthread", "mps2-an386"),
        ("rtthread", "mps2-an505"),
    }
)


@pytest.mark.skipif(not _CACHED_RTT_M_ELFS, reason="no cached RT-Thread M-profile ELFs")
@pytest.mark.parametrize(
    "path",
    _CACHED_RTT_M_ELFS,
    ids=lambda p: "/".join(p.relative_to(fixture_cache_root()).parts[:4]),
)
def test_cached_rtthread_m_profile_elfs_build_scheduler_profile(path):
    elf = ElfImage.from_path(path)
    assert elf is not None
    profile = build_profile(elf)
    assert profile is not None
    assert classify_stage(profile, profile.switch_return).name == "switch-return"
    assert classify_stage(profile, profile.pendsv_entry).name == "pendsv-entry"
    assert (
        classify_stage(profile, profile.pendsv_exception_return).name
        == "pendsv-exception-return"
    )


def test_armv7em_soft_float_uses_22_insn_pendsv_and_integer_seeds():
    soft_fix = _PortFixture(
        name="armv7e-m-soft",
        arch="armv7e-m",
        blob=_CM3_SWITCH,
        base=0x583E,
        pendsv_off=0x20,
        switch_to_off=0x5C,
        flag=0x20005F04,
        from_ptr=0x20005EFC,
        to_ptr=0x20005F00,
        tz=None,
        selection=StackContextSelection(
            "armv7e-m", "arm", ARMV7M_INTEGER, "ok", abi="aapcs32"
        ),
    )
    prof = build_profile(_elf_for(soft_fix))
    assert prof is not None
    assert prof.port == "armv7-m"
    assert prof.isa == "armv7e-m"
    assert prof.fpu is False

    stack_base = 0x20004000
    mem = bytearray(b"\x00" * 0x1000)
    sp_offset = 16
    old_tcb = 0x20000100
    new_tcb = 0x20000200
    old_low, old_high = stack_base, stack_base + 0x400
    new_low, new_high = stack_base + 0x400, stack_base + 0x800
    cm3_fix = _FIXTURES[1]
    old_hw, old_regs = _build_saved_frame(
        mem,
        stack_base,
        top=old_high - 120,
        fix=cm3_fix,
        fpu=False,
        pc=0x4020,
        lr=0x4004,
    )
    new_sp = new_high - 120
    new_hw, new_regs = _build_saved_frame(
        mem,
        stack_base,
        top=new_sp,
        fix=cm3_fix,
        fpu=False,
        pc=0x5020,
        lr=0x5004,
    )
    read = _memory_reader(mem, stack_base)

    def resolve(
        thread_addr, thread_sp, entry, low, high, *, flag, from_ptr, pc, psp, exc, regs
    ):
        return build_stage_seed(
            prof,
            soft_fix.selection,
            flag_value=flag,
            from_ptr=from_ptr,
            to_ptr=new_tcb + sp_offset,
            sp_offset=sp_offset,
            thread_addr=thread_addr,
            thread_sp=thread_sp,
            thread_entry=entry,
            current_addr=new_tcb,
            low=low,
            high=high,
            endian="little",
            stkalign=True,
            read=read,
            live_pc=pc,
            live_sp=0x20001000,
            live_psp=psp,
            live_exc=exc,
            live_regs=regs,
        )

    save_res = resolve(
        old_tcb,
        old_high - 120,
        0x4000,
        old_low,
        old_high,
        flag=0,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_save_written,
        psp=old_hw,
        exc=14,
        regs={**old_regs, 0: old_tcb + sp_offset, 1: old_high - 120},
    )
    assert isinstance(save_res, SchedulerSeed)
    assert save_res.seed.source == "saved-buffer" and save_res.seed.pc == 0x4020

    restored_res = resolve(
        new_tcb,
        new_sp,
        0x5000,
        new_low,
        new_high,
        flag=0,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_restored_pending_psp,
        psp=old_hw,
        exc=14,
        regs={**new_regs, 1: new_hw},
    )
    assert isinstance(restored_res, SchedulerSeed)
    assert restored_res.seed.source == "current" and restored_res.seed.pc == 0x5020

    ret_res = resolve(
        new_tcb,
        new_sp,
        0x5000,
        new_low,
        new_high,
        flag=0,
        from_ptr=old_tcb + sp_offset,
        pc=prof.pendsv_exception_return,
        psp=new_hw,
        exc=14,
        regs={**new_regs, 14: 0xFFFFFFFD},
    )
    assert isinstance(ret_res, SchedulerSeed)
    assert ret_res.seed.pc == 0x5020


@pytest.mark.parametrize(
    "fix",
    [f for f in _FIXTURES if f.arch == "armv8-m.mainline"],
    ids=lambda f: f.name,
)
def test_armv8m_scheduler_stages_admit_valid_nonzero_psplim_and_reject_invalid(
    fix: _PortFixture,
):
    prof = build_profile(_elf_for(fix))
    assert prof is not None
    fpu_mode = fix.name.endswith("-fpu")
    stack_base = 0x20004000
    mem = bytearray(b"\x00" * 0x1000)
    sp_offset = 16
    old_tcb, new_tcb = 0x20000100, 0x20000200
    old_low, old_high = stack_base, stack_base + 0x400
    new_low, new_high = stack_base + 0x400, stack_base + 0x800
    old_sp = old_high - (240 if fpu_mode else 120)
    new_sp = new_high - (240 if fpu_mode else 120)
    old_hw, old_regs = _build_saved_frame(
        mem, stack_base, top=old_sp, fix=fix, fpu=fpu_mode, pc=0x4020, lr=0x4004
    )
    new_hw, new_regs = _build_saved_frame(
        mem, stack_base, top=new_sp, fix=fix, fpu=fpu_mode, pc=0x5020, lr=0x5004
    )
    _write_u32(mem, stack_base, old_sp + 8, old_low)
    _write_u32(mem, stack_base, new_sp + 8, new_low)
    read = _memory_reader(mem, stack_base, tz_addr=fix.tz, tz=0)

    def resolve(tcb, t_sp, low, high, *, flag, pc, psp, regs, psplim):
        return build_stage_seed(
            prof,
            fix.selection,
            flag_value=flag,
            from_ptr=old_tcb + sp_offset,
            to_ptr=new_tcb + sp_offset,
            sp_offset=sp_offset,
            thread_addr=tcb,
            thread_sp=t_sp,
            thread_entry=0x5000,
            current_addr=new_tcb,
            low=low,
            high=high,
            endian="little",
            stkalign=True,
            read=read,
            live_pc=pc,
            live_sp=0x20001800,
            live_psp=psp,
            live_exc=14,
            live_regs=regs,
            live_psplim=psplim,
            live_control=6 if fpu_mode else 2,
        )

    exc_ret = 0xFFFFFFED if fpu_mode else 0xFFFFFFFD
    save_regs = {
        **old_regs,
        0: old_tcb + sp_offset,
        1: old_sp,
        2: 0,
        3: exc_ret,
        4: old_low,
        5: 2,
    }
    save_ok = resolve(
        old_tcb,
        old_sp,
        old_low,
        old_high,
        flag=0,
        pc=prof.pendsv_save_written,
        psp=old_hw,
        regs=save_regs,
        psplim=0,
    )
    assert isinstance(save_ok, SchedulerSeed) and save_ok.seed.pc == 0x4020

    save_bad = resolve(
        old_tcb,
        old_sp,
        old_low,
        old_high,
        flag=0,
        pc=prof.pendsv_save_written,
        psp=old_hw,
        regs={**save_regs, 4: old_low + 8},
        psplim=0,
    )
    assert save_bad == "scheduler-save-window-inconsistent"

    restored_ok = resolve(
        new_tcb,
        new_sp,
        new_low,
        new_high,
        flag=0,
        pc=prof.pendsv_restored_pending_psp,
        psp=old_hw,
        regs={**new_regs, 1: new_hw, 14: exc_ret},
        psplim=new_low,
    )
    assert isinstance(restored_ok, SchedulerSeed) and restored_ok.seed.pc == 0x5020

    for bad_psplim in (0, new_low - 8, new_low + 4, new_sp + 0x100):
        bad_restored = resolve(
            new_tcb,
            new_sp,
            new_low,
            new_high,
            flag=0,
            pc=prof.pendsv_restored_pending_psp,
            psp=old_hw,
            regs={**new_regs, 1: new_hw, 14: exc_ret},
            psplim=bad_psplim,
        )
        assert bad_restored == "unsupported-extension-context"

    _write_u32(mem, stack_base, new_sp + 8, new_low - 8)
    bad_saved_hdr = resolve(
        new_tcb,
        new_sp,
        new_low,
        new_high,
        flag=0,
        pc=prof.pendsv_switch_to,
        psp=old_hw,
        regs={**new_regs, 14: exc_ret},
        psplim=new_low - 8,
    )
    assert bad_saved_hdr == "unsupported-extension-context"
