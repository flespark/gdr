"""Independent compiled and raw-frame oracles for RV32."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread" or (SPEC.target, SPEC.version) != ("rv32", "4.0.5"),
    reason="requires the RT-Thread RV32 port fixture",
)


def _json(output: str) -> dict:
    for line in output.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"missing port evidence:\n{output}")


def test_rv32_fixture_compiled_layout_and_tick_evidence(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from core.cpus import cpu_of
from core.unwind import UnwindMetadata
from core.unwind_meta import ElfImage
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


@pytest.mark.parametrize(
    "symbol,source", [("worker1", "saved"), ("gdr_unstarted", "unstarted")]
)
def test_rv32_saved_frames_match_raw_memory(gdb_session, symbol, source):
    evidence = _json(
        gdb_session.run_python(
            f"""
import json
from rtthread.backtrace import RISCV32_INTEGER, decode_saved_context
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


def test_rv32_raw_seed_unwinds_to_the_real_exit(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from dataclasses import replace
from core.unwind import UnwindMetadata, unwind_chain
from core.unwind_meta import ElfImage
from rtthread.backtrace import RISCV32_INTEGER, decode_saved_context
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
