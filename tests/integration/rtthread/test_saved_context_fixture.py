"""Real Armv6-M/Armv7-M port instruction, slot and register-frame evidence."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread" or SPEC.target not in {"mps2-an385", "microbit"},
    reason="requires an RT-Thread basic Arm M-profile BSP fixture",
)
ISA = "armv6-m" if SPEC.target == "microbit" else "armv7-m"


def _json(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"missing fixture evidence:\n{output}")


def test_basic_arm_fixture_has_real_port_and_bounded_memory_evidence(gdb_session):
    evidence = _json(
        gdb_session.run_python(
            """
import json
from core.cpus import cpu_of
from core.insn_thumb import decode_thumb
from core.unwind import UnwindMetadata
from core.unwind_meta import ElfImage

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
    assert evidence["isa"] == ISA and evidence["word"] == 4
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
        assert instructions.count(["other", {"first": 0xC1F0}]) == 2, instructions
        assert instructions.count(["other", {"first": 0xC9F0}]) == 2, instructions
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
from rtthread.backtrace import ARMV6M_INTEGER, ARMV7M_INTEGER, decode_saved_context

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


def test_basic_arm_fixture_opens_its_instruction_proven_runtime_profile(gdb_session):
    envelope = _json(gdb_session.run("gdr api backtrace worker1 --json"))
    assert envelope["ok"]
    data = envelope["data"]
    assert data["architecture"] == ISA
    assert data["stop_reason"] == "task-exit"
    assert data["frames"] and data["complete"]
