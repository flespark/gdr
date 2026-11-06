"""Independent compiled and raw-frame oracles for Armv8-M Mainline."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread" or (SPEC.target, SPEC.version) != ("mps2-an505", "4.0.5"),
    reason="requires the RT-Thread Armv8-M Mainline port fixture",
)


def _json(output: str) -> dict:
    for line in output.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"missing port evidence:\n{output}")


def test_armv8m_mainline_fixture_compiled_layout_and_tick_evidence(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from core.cpus import cpu_of
from core.unwind import UnwindMetadata
from core.unwind_meta import ElfImage
from rtthread.stack_frame_profile import _armv8m_mainline_verify_context
elf = ElfImage.from_path(gdb.current_progspace().filename)
assert elf is not None
cpu = cpu_of(elf)
frame = gdb.lookup_type('struct stack_frame')
offsets = {field.name: field.bitpos // 8 for field in frame.fields()}
base = offsets.pop('exception_stack_frame')
hardware = gdb.lookup_type('struct exception_stack_frame')
offsets.update({'hw_' + field.name: base + field.bitpos // 8 for field in hardware.fields()})
print(json.dumps({'isa': cpu.isa, 'abi': cpu.abi, 'word': elf.pointer_size,
                  'size': frame.sizeof, 'offsets': offsets, 'matched': _armv8m_mainline_verify_context(elf),
                  'metadata': UnwindMetadata.from_elf(elf).available(),
                  'ticks': int(gdb.lookup_global_symbol('gdr_tick_interrupts').value())}))
"""
        )
    )
    assert evidence["isa"] == "armv8-m.mainline"
    assert evidence["abi"] == "aapcs32"
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


@pytest.mark.parametrize(
    "symbol,source", [("worker1", "saved"), ("gdr_unstarted", "unstarted")]
)
def test_armv8m_mainline_saved_frames_match_raw_memory(gdb_session, symbol, source):
    evidence = _json(
        gdb_session.run_python(
            f"""
import json
from rtthread.backtrace import ARMV8M_MAINLINE_INTEGER, decode_saved_context
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


def test_armv8m_mainline_raw_seed_unwinds_to_the_real_exit(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from dataclasses import replace
from core.unwind import UnwindMetadata, unwind_chain
from core.unwind_meta import ElfImage
from rtthread.backtrace import ARMV8M_MAINLINE_INTEGER, decode_saved_context
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
