"""Compiled port, DWARF layout, hardware tick, and raw-frame fixture oracles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread",
    reason="requires an RT-Thread fixture lane",
)

_BASIC_ARM_ISA = "armv6-m" if SPEC.target == "microbit" else "armv7-m"
_basic_arm_skip = pytest.mark.skipif(
    SPEC.target not in {"mps2-an385", "microbit"},
    reason="requires an RT-Thread basic Arm M-profile BSP fixture",
)
_an505_skip = pytest.mark.skipif(
    (SPEC.target, SPEC.version) != ("mps2-an505", "4.0.5"),
    reason="requires the RT-Thread Armv8-M Mainline port fixture",
)
_rv32_skip = pytest.mark.skipif(
    (SPEC.target, SPEC.version) != ("rv32", "4.0.5"),
    reason="requires the RT-Thread RV32 port fixture",
)
_microbit_skip = pytest.mark.skipif(
    (SPEC.target, SPEC.version) != ("microbit", "4.0.5"),
    reason="requires the RT-Thread microbit compatibility fixture",
)


def _json(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"missing fixture evidence:\n{output}")


# ---------------------------------------------------------------------------
# Basic Armv6-M / Armv7-M port instructions, DWARF layout, and raw frames
# ---------------------------------------------------------------------------


@_basic_arm_skip
def test_basic_arm_fixture_has_real_port_and_bounded_memory_evidence(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from core.elf import cpu_of
from core.insn_thumb import decode_thumb
from core.backtrace.unwind import UnwindMetadata
from core.elf import ElfImage

elf = ElfImage.from_path(gdb.current_progspace().filename)
assert elf is not None
cpu = cpu_of(elf)
metadata = UnwindMetadata.from_elf(elf)
start = elf.symbol_address('PendSV_Handler')
end = elf.symbol_address('rt_hw_context_switch_to')
assert start is not None and end is not None and 0 < end - start < 256
instructions = []
pc = start
while pc < end:
    item = decode_thumb(elf, pc, end)
    assert not isinstance(item, str)
    instructions.append((item.mnemonic, dict(item.operands)))
    pc += item.size
vectors = elf.section('.isr_vector')
print(json.dumps({
    'isa': cpu.isa,
    'word': elf.pointer_size,
    'vector_address': vectors.address,
    'vector_bytes': len(vectors.data),
    'initial_msp': int.from_bytes(vectors.data[:4], 'little'),
    'stack_low': elf.symbol_address('__StackLimit'),
    'stack_high': elf.symbol_address('__StackTop'),
    'stack_section': elf.section_bounds('.irq_stack'),
    'heap_high': elf.symbol_address('__heap_end'),
    'unwind_tables': metadata.available(),
    'instructions': instructions,
}))
"""
        )
    )
    assert evidence["isa"] == _BASIC_ARM_ISA and evidence["word"] == 4
    assert evidence["vector_address"] == 0 and evidence["vector_bytes"] == (
        192 if SPEC.target == "microbit" else 128
    )
    low, high = evidence["stack_low"], evidence["stack_high"]
    assert high - low == 16 * 1024
    assert evidence["heap_high"] <= low
    assert evidence["stack_section"] == [low, high]
    assert evidence["initial_msp"] == high
    assert evidence["unwind_tables"]
    instructions = evidence["instructions"]
    if SPEC.target == "microbit":
        assert (
            instructions.count(
                [
                    "stmia",
                    {
                        "rn": 1,
                        "wback": True,
                        "load": False,
                        "direction": "ia",
                        "reglist": 0xF0,
                    },
                ]
            )
            == 2
        ), instructions
        assert (
            instructions.count(
                [
                    "ldmia",
                    {
                        "rn": 1,
                        "wback": True,
                        "load": True,
                        "direction": "ia",
                        "reglist": 0xF0,
                    },
                ]
            )
            == 2
        ), instructions
        assert ["mrs", {"rd": 1, "sysm": 9}] in instructions
        assert ["msr", {"rn": 1, "sysm": 9}] in instructions
        return
    for mnemonic, operands in (
        ("mrs", {"rd": 1, "sysm": 9}),
        (
            "stmdb",
            {
                "rn": 1,
                "wback": True,
                "load": False,
                "direction": "db",
                "reglist": 0xFF0,
            },
        ),
        (
            "ldmia",
            {"rn": 1, "wback": True, "load": True, "direction": "ia", "reglist": 0xFF0},
        ),
        ("msr", {"rn": 1, "sysm": 9}),
    ):
        assert [mnemonic, operands] in instructions, instructions


@_basic_arm_skip
def test_basic_arm_profile_slots_match_the_compiled_port_dwarf(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
frame = gdb.lookup_type('struct stack_frame')
hardware = gdb.lookup_type('struct exception_stack_frame')
offsets = {field.name: field.bitpos // 8 for field in frame.fields()}
base = offsets.pop('exception_stack_frame')
offsets.update({field.name: base + field.bitpos // 8 for field in hardware.fields()})
print(json.dumps({'size': frame.sizeof, 'hardware_size': hardware.sizeof, 'offsets': offsets}))
"""
        )
    )
    assert evidence["size"] == 64 and evidence["hardware_size"] == 32
    assert evidence["offsets"] == {
        **{f"r{register}": (register - 4) * 4 for register in range(4, 12)},
        "r0": 32,
        "r1": 36,
        "r2": 40,
        "r3": 44,
        "r12": 48,
        "lr": 52,
        "pc": 56,
        "psr": 60,
    }


@_basic_arm_skip
@pytest.mark.parametrize(
    "symbol,source",
    [("worker1_thread", "saved"), ("gdr_unstarted_thread", "unstarted")],
)
def test_basic_arm_saved_and_initial_contexts_match_raw_port_frames(
    gdb_session, symbol, source
):
    evidence = _json(
        gdb_session.run_python(
            f"""
import json
from rtthread.backtrace.unwind import ARMV6M_INTEGER, ARMV7M_INTEGER, decode_saved_context

thread = gdb.lookup_static_symbol('{symbol}').value()
top = int(thread['sp'])
low = int(thread['stack_addr'])
high = low + int(thread['stack_size'])
context = gdb.Value(top).cast(gdb.lookup_type('struct stack_frame').pointer()).dereference()
hardware = context['exception_stack_frame']
registers = {{register: int(context[f'r{{register}}']) for register in range(4, 12)}}
registers.update({{register: int(hardware[f'r{{register}}']) for register in (0, 1, 2, 3, 12)}})
registers[14] = int(hardware['lr'])
registers[15] = int(hardware['pc'])
status = int(hardware['psr'])
registers[13] = top + 64 + (4 if status & (1 << 9) else 0)
reader = lambda address, size: bytes(gdb.selected_inferior().read_memory(address, size))
profile = ARMV6M_INTEGER if {SPEC.target!r} == 'microbit' else ARMV7M_INTEGER
seed, reason = decode_saved_context(profile, top, reader, endian='little',
                                   entry=int(thread['entry']), stack_low=low, stack_high=high)
assert seed is not None, reason
exit_pc = int(gdb.lookup_static_symbol('_thread_exit').value().address)
print(json.dumps({{'reason': reason, 'status': status, 'top': top, 'low': low, 'high': high,
                  'seed': {{'pc': seed.pc, 'sp': seed.sp, 'registers': dict(seed.regs),
                            'source': seed.source, 'root': seed.root_pc}},
                  'registers': registers, 'exit_pc': exit_pc,
                  'entry': int(thread['entry'])}}))
"""
        )
    )
    assert evidence["reason"] == "ok"
    assert evidence["status"] & (1 << 24)
    assert evidence["status"] & 0x1FF == 0
    assert evidence["low"] <= evidence["top"] < evidence["high"]
    seed = evidence["seed"]
    assert seed["source"] == source
    assert seed["registers"] == evidence["registers"]
    assert seed["pc"] == evidence["registers"]["15"] & ~1
    assert seed["sp"] == evidence["registers"]["13"] <= evidence["high"]
    if source == "unstarted":
        assert seed["pc"] == evidence["entry"] & ~1
        assert seed["root"] == seed["pc"]
        assert evidence["registers"]["14"] & 1
        assert evidence["registers"]["14"] & ~1 == evidence["exit_pc"] & ~1
        assert all(
            evidence["registers"][str(register)] == 0xDEADBEEF
            for register in range(4, 12)
        )


@_basic_arm_skip
def test_basic_arm_fixture_opens_its_instruction_proven_runtime_profile(gdb_session):
    envelope = _json(gdb_session.run("gdr api backtrace worker1 --json"))
    assert envelope["ok"]
    data = envelope["data"]
    assert data["architecture"] == _BASIC_ARM_ISA
    assert data["stop_reason"] == "task-exit"
    assert data["frames"] and data["complete"]


# ---------------------------------------------------------------------------
# Armv8-M Mainline (mps2-an505) compiled layout and raw-seed oracles
# ---------------------------------------------------------------------------


@_an505_skip
def test_armv8m_mainline_fixture_compiled_layout_and_tick_evidence(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from core.elf import cpu_of
from core.backtrace.unwind import UnwindMetadata
from core.elf import ElfImage
from rtthread.stack_frame_profile import (
    _armv8m_mainline_fpu_verify_context,
    _armv8m_mainline_verify_context,
)
elf = ElfImage.from_path(gdb.current_progspace().filename)
assert elf is not None
cpu = cpu_of(elf)
verify = (
    _armv8m_mainline_fpu_verify_context
    if cpu.abi == "aapcs32-vfp"
    else _armv8m_mainline_verify_context
)
frame = gdb.lookup_type('struct stack_frame')
offsets = {field.name: field.bitpos // 8 for field in frame.fields()}
base = offsets.pop('exception_stack_frame')
hardware = gdb.lookup_type('struct exception_stack_frame')
offsets.update({'hw_' + field.name: base + field.bitpos // 8 for field in hardware.fields()})
print(json.dumps({'isa': cpu.isa, 'abi': cpu.abi, 'word': elf.pointer_size,
                  'size': frame.sizeof, 'offsets': offsets, 'matched': verify(elf),
                  'metadata': UnwindMetadata.from_elf(elf).available(),
                  'ticks': int(gdb.lookup_global_symbol('gdr_tick_interrupts').value())}))
"""
        )
    )
    assert evidence["isa"] == "armv8-m.mainline"
    assert evidence["abi"] == ("aapcs32-vfp" if SPEC.variant == "fpu" else "aapcs32")
    assert evidence["word"] == 4 and evidence["matched"]
    assert evidence["metadata"] and evidence["ticks"] >= 20
    assert evidence["size"] == 80
    assert evidence["offsets"] == {
        "tz": 0,
        "lr": 4,
        "psplim": 8,
        "control": 12,
        **{f"r{reg}": 16 + (reg - 4) * 4 for reg in range(4, 12)},
        "hw_r0": 48,
        "hw_r1": 52,
        "hw_r2": 56,
        "hw_r3": 60,
        "hw_r12": 64,
        "hw_lr": 68,
        "hw_pc": 72,
        "hw_psr": 76,
    }


@_an505_skip
@pytest.mark.parametrize(
    "symbol,source", [("worker1", "saved"), ("gdr_unstarted", "unstarted")]
)
def test_armv8m_mainline_saved_frames_match_raw_memory(gdb_session, symbol, source):
    evidence = _json(
        gdb_session.run_python(
            f"""
import json
from rtthread.backtrace.unwind import ARMV8M_MAINLINE_INTEGER, decode_saved_context
thread = gdb.lookup_static_symbol('{symbol}_thread').value()
top = int(thread['sp'])
low = int(thread['stack_addr'])
high = low + int(thread['stack_size'])
read = lambda address, size: bytes(gdb.selected_inferior().read_memory(address, size))
raw = read(top, 80)
words = [int.from_bytes(raw[offset:offset+4], 'little') for offset in range(0, 80, 4)]
seed, reason = decode_saved_context(ARMV8M_MAINLINE_INTEGER, top, read, endian='little',
                                   entry=int(thread['entry']), stack_low=low, stack_high=high)
assert seed is not None, reason
print(json.dumps({{'words': words, 'top': top, 'low': low, 'high': high,
                  'seed_pc': seed.pc, 'seed_sp': seed.sp, 'regs': dict(seed.regs),
                  'source': seed.source, 'root': seed.root_pc, 'entry': int(thread['entry']),
                  'exit': int(gdb.lookup_static_symbol('_thread_exit').value().address),
                  'unchanged': raw == read(top, 80)}}))
"""
        )
    )
    words = evidence["words"]
    top = evidence["top"]
    assert evidence["low"] <= top < evidence["seed_sp"] <= evidence["high"]
    assert evidence["source"] == source and evidence["unchanged"]
    assert words[:3] == [0, 0xFFFFFFFD, 0]
    assert words[3] in (0, 2)
    assert words[19] & (1 << 24) and words[19] & 0x1FF == 0
    expected = {str(reg): words[reg] for reg in range(4, 12)}
    expected.update(
        {str(reg): words[12 + index] for index, reg in enumerate((0, 1, 2, 3, 12))}
    )
    expected.update({"14": words[17], "15": words[18]})
    sp = top + 80 + (4 if words[19] & (1 << 9) else 0)
    expected["13"] = sp
    pc, lr = words[18] & ~1, words[17] & ~1
    assert evidence["regs"] == expected
    assert evidence["seed_pc"] == pc and evidence["seed_sp"] == sp
    if source == "unstarted":
        assert pc == evidence["entry"] & ~1 and evidence["root"] == pc
        assert lr == evidence["exit"] & ~1
        assert all(expected[str(reg)] == 0xDEADBEEF for reg in range(4, 12))


@_an505_skip
def test_armv8m_mainline_raw_seed_unwinds_to_the_real_exit(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from dataclasses import replace
from core.backtrace.unwind import UnwindMetadata, unwind_chain
from core.elf import ElfImage
from rtthread.backtrace.unwind import ARMV8M_MAINLINE_INTEGER, decode_saved_context
thread = gdb.lookup_static_symbol('worker1_thread').value()
low = int(thread['stack_addr'])
high = low + int(thread['stack_size'])
read = lambda address, size: bytes(gdb.selected_inferior().read_memory(address, size))
seed, reason = decode_saved_context(ARMV8M_MAINLINE_INTEGER,
    int(thread['sp']), read, endian='little', entry=int(thread['entry']), stack_low=low, stack_high=high)
assert seed is not None, reason
root = int(gdb.lookup_static_symbol('_thread_exit').value().address) & ~1
seed = replace(seed, root_pc=root, root_reason='task-exit')
elf = ElfImage.from_path(gdb.current_progspace().filename)
result = unwind_chain(seed, UnwindMetadata.from_elf(elf), read=read, stack_low=low, stack_high=high)
print(json.dumps({'complete': result.complete, 'reason': result.stop_reason,
                  'frames': [{'pc': item.pc, 'method': item.method} for item in result.frames], 'root': root}))
"""
        )
    )
    assert evidence["complete"] and evidence["reason"] == "task-exit", evidence
    assert evidence["frames"][-1]["pc"] == evidence["root"]


# ---------------------------------------------------------------------------
# RV32 compiled layout and raw-seed oracles
# ---------------------------------------------------------------------------


@_rv32_skip
def test_rv32_fixture_compiled_layout_and_tick_evidence(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from core.elf import cpu_of
from core.backtrace.unwind import UnwindMetadata
from core.elf import ElfImage
from rtthread.stack_frame_profile import _riscv_verify_context_switch
elf = ElfImage.from_path(gdb.current_progspace().filename)
assert elf is not None
cpu = cpu_of(elf)
frame = gdb.lookup_type('struct rt_hw_stack_frame')
offsets = {field.name: field.bitpos // 8 for field in frame.fields()}
print(json.dumps({'isa': cpu.isa, 'abi': cpu.abi, 'word': elf.pointer_size,
                  'size': frame.sizeof, 'offsets': offsets, 'matched': _riscv_verify_context_switch(elf),
                  'metadata': UnwindMetadata.from_elf(elf).available(),
                  'ticks': int(gdb.lookup_global_symbol('gdr_tick_interrupts').value())}))
"""
        )
    )
    assert evidence["isa"] == "riscv32"
    assert evidence["abi"] == "ilp32"
    assert evidence["word"] == 4 and evidence["matched"]
    assert evidence["metadata"] and evidence["ticks"] >= 20
    fields = [
        "epc",
        "ra",
        "mstatus",
        "gp",
        "tp",
        "t0",
        "t1",
        "t2",
        "s0_fp",
        "s1",
        "a0",
        "a1",
        "a2",
        "a3",
        "a4",
        "a5",
        "a6",
        "a7",
        "s2",
        "s3",
        "s4",
        "s5",
        "s6",
        "s7",
        "s8",
        "s9",
        "s10",
        "s11",
        "t3",
        "t4",
        "t5",
        "t6",
    ]
    assert evidence["size"] == 128
    assert evidence["offsets"] == {name: index * 4 for index, name in enumerate(fields)}


@_rv32_skip
@pytest.mark.parametrize(
    "symbol,source", [("worker1", "saved"), ("gdr_unstarted", "unstarted")]
)
def test_rv32_saved_frames_match_raw_memory(gdb_session, symbol, source):
    evidence = _json(
        gdb_session.run_python(
            f"""
import json
from rtthread.backtrace.unwind import RISCV32_INTEGER, decode_saved_context
thread = gdb.lookup_static_symbol('{symbol}_thread').value()
top = int(thread['sp'])
low = int(thread['stack_addr'])
high = low + int(thread['stack_size'])
read = lambda address, size: bytes(gdb.selected_inferior().read_memory(address, size))
raw = read(top, 128)
words = [int.from_bytes(raw[offset:offset+4], 'little') for offset in range(0, 128, 4)]
seed, reason = decode_saved_context(RISCV32_INTEGER, top, read, endian='little',
                                   entry=int(thread['entry']), stack_low=low, stack_high=high)
assert seed is not None, reason
print(json.dumps({{'words': words, 'top': top, 'low': low, 'high': high,
                  'seed_pc': seed.pc, 'seed_sp': seed.sp, 'regs': dict(seed.regs),
                  'source': seed.source, 'root': seed.root_pc, 'entry': int(thread['entry']),
                  'exit': int(gdb.lookup_static_symbol('_thread_exit').value().address),
                  'unchanged': raw == read(top, 128)}}))
"""
        )
    )
    words = evidence["words"]
    top = evidence["top"]
    assert evidence["low"] <= top < evidence["seed_sp"] <= evidence["high"]
    assert evidence["source"] == source and evidence["unchanged"]
    expected = {str(reg): words[reg] for reg in (1, *range(4, 32))}
    sp = top + 128
    expected["2"] = sp
    pc, lr = words[0], words[1]
    assert evidence["regs"] == expected
    assert evidence["seed_pc"] == pc and evidence["seed_sp"] == sp
    if source == "unstarted":
        assert pc == evidence["entry"] & ~1 and evidence["root"] == pc
        assert lr == evidence["exit"] & ~1
        fill_registers = (5, 6, 7, 8, 9, *range(18, 28))
        assert all(expected[str(reg)] == 0xDEADBEEF for reg in fill_registers)


@_rv32_skip
def test_rv32_raw_seed_unwinds_to_the_real_exit(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from dataclasses import replace
from core.backtrace.unwind import UnwindMetadata, unwind_chain
from core.elf import ElfImage
from rtthread.backtrace.unwind import RISCV32_INTEGER, decode_saved_context
thread = gdb.lookup_static_symbol('worker1_thread').value()
low = int(thread['stack_addr'])
high = low + int(thread['stack_size'])
read = lambda address, size: bytes(gdb.selected_inferior().read_memory(address, size))
seed, reason = decode_saved_context(RISCV32_INTEGER,
    int(thread['sp']), read, endian='little', entry=int(thread['entry']), stack_low=low, stack_high=high)
assert seed is not None, reason
root = int(gdb.lookup_static_symbol('_thread_exit').value().address) & ~1
seed = replace(seed, root_pc=root, root_reason='task-exit')
elf = ElfImage.from_path(gdb.current_progspace().filename)
result = unwind_chain(seed, UnwindMetadata.from_elf(elf), read=read, stack_low=low, stack_high=high)
print(json.dumps({'complete': result.complete, 'reason': result.stop_reason,
                  'frames': [{'pc': item.pc, 'method': item.method} for item in result.frames], 'root': root}))
"""
        )
    )
    assert evidence["complete"] and evidence["reason"] == "task-exit", evidence
    assert evidence["frames"][-1]["pc"] == evidence["root"]


# ---------------------------------------------------------------------------
# microbit nRF51 TIMER2 tick and PendSV entry compatibility oracles
# ---------------------------------------------------------------------------


@_microbit_skip
def test_unstarted_hardware_pc_is_even_but_task_exit_lr_keeps_thumb_state(gdb_session):
    data = _json(
        gdb_session.run_python("""
import json
thread = gdb.lookup_static_symbol('gdr_unstarted_thread').value()
base = int(thread['sp'])
def u32(offset):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(base + offset, 4)), 'little')
print(json.dumps({'pc': u32(56), 'lr': u32(52), 'xpsr': u32(60),
                  'entry': int(thread['entry']),
                  'exit': int(gdb.lookup_static_symbol('_thread_exit').value().address)}))
""")
    )
    assert data["pc"] & 1 == 0 and data["pc"] == data["entry"] & ~1
    assert data["lr"] & 1 and data["lr"] & ~1 == data["exit"] & ~1
    assert data["xpsr"] == 0x01000000


@_microbit_skip
def test_timer2_is_the_real_tick_source_and_vectors_do_not_install_systick(gdb_session):
    code = """
import json
def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), 'little')
base = 0x4000a000
print(json.dumps({'ipsr': int(gdb.newest_frame().read_register('xpsr')) & 0x1ff,
                  'tick': int(gdb.parse_and_eval('rt_tick')),
                  'mode': u32(base + 0x504), 'bitmode': u32(base + 0x508),
                  'prescaler': u32(base + 0x510), 'compare': u32(base + 0x540),
                  'shorts': u32(base + 0x200), 'inten': u32(base + 0x304),
                  'timer_vector': u32(26 * 4), 'systick_vector': u32(15 * 4),
                  'timer_entry': int(gdb.parse_and_eval('&Timer2_Handler'))}))
"""
    gdb_session.run("tbreak *Timer2_Handler")
    assert "Temporary breakpoint" in gdb_session.run("continue", timeout=30)
    first = _json(gdb_session.run_python(code))
    assert first["ipsr"] == 26
    assert first["mode"] == first["bitmode"] == 0 and first["prescaler"] == 4
    assert first["compare"] == 10000 and first["shorts"] == 1
    assert first["inten"] == 1 << 16
    assert first["timer_vector"] & ~1 == first["timer_entry"] & ~1
    assert first["systick_vector"] == 0
    gdb_session.run("tbreak *Timer2_Handler")
    assert "Temporary breakpoint" in gdb_session.run("continue", timeout=30)
    second = _json(gdb_session.run_python(code))
    assert second["tick"] == (first["tick"] + 1) & 0xFFFFFFFF


@_microbit_skip
def test_first_pendsv_returns_fd_and_enters_the_application(qemu_profile):
    target = QemuSession(qemu_profile)
    session = GdbSession(
        SPEC.gdb, qemu_profile, target.gdb_port, Path(__file__).resolve().parents[3]
    )
    try:
        target.start(SPEC.boot_wait, paused=True)
        session.start()
        address = _json(
            session.run_python("""
import json
from core.insn_thumb import decode_thumb
from core.elf import ElfImage
elf = ElfImage.from_path(gdb.current_progspace().filename)
pc = elf.symbol_address('PendSV_Handler')
end = elf.symbol_address('rt_hw_context_switch_to')
returns = []
while pc < end:
    instruction = decode_thumb(elf, pc, end)
    assert not isinstance(instruction, str)
    if instruction.mnemonic == 'bx' and instruction.operands.get('rm') == 0:
        returns.append(pc)
    pc += instruction.size
assert len(returns) == 1
print(json.dumps({'pc': returns[0]}))
""")
        )["pc"]
        session.run(f"tbreak *{address:#x}")
        assert "Temporary breakpoint" in session.run("continue", timeout=30)
        raw = _json(
            session.run_python("""
import json
frame = gdb.newest_frame()
base = int(frame.read_register('psp')) & 0xffffffff
words = [int.from_bytes(bytes(gdb.selected_inferior().read_memory(base + i*4,4)), 'little') for i in range(8)]
print(json.dumps({'return': int(frame.read_register('r0')) & 0xffffffff,
                  'base': base, 'words': words}))
""")
        )
        assert raw["return"] == 0xFFFFFFFD
        assert raw["base"] % 8 == 0 and raw["words"][6] & 1 == 0
        assert raw["words"][7] == 0x01000000
        session.run("tbreak main")
        assert "main" in session.run("continue", timeout=30)
    finally:
        session.stop()
        target.stop()
