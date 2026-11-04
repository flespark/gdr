"""nRF51 TIMER2 tick and architecture-valid saved-frame fixture oracles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession

SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread" or (SPEC.target, SPEC.version) != ("microbit", "4.0.5"),
    reason="requires the RT-Thread microbit compatibility fixture",
)


def _json(output):
    for line in output.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"missing fixture evidence:\n{output}")


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
from core.unwind_meta import ElfImage
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
