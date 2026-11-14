"""SysTick cross-stack backtrace on RT-Thread M-profile lanes.

SysTick is admitted as a proven periodic interrupt (same handler proof as
external IRQs: scheduler ownership, IRQ stack bounds, vector entry). The
interrupted task varies per tick, so this probe asserts the cross-stack
core that holds for every victim: one exact exception transition into the
interrupted task's stack.
"""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
_ISA = {
    "mps2-an385": "armv7-m",
    "mps2-an386": "armv7e-m",
    "mps2-an505": "armv8-m.mainline",
    "microbit": "armv6-m",
}.get(SPEC.target, "")
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread"
    or SPEC.target not in {"mps2-an385", "mps2-an386", "mps2-an505"}
    or SPEC.version != "4.0.5"
    or (SPEC.target == "mps2-an386" and SPEC.variant not in ("fpu", "fault")),
    reason="requires an RT-Thread SysTick-driven M-profile lane",
)


def _json(output):
    for line in output.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"missing systick evidence:\n{output}")


def _stop_at_tick(session):
    session.run("tbreak SysTick_Handler")
    output = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in output, output


def _probe(session):
    return _json(
        session.run_python(
            """
import json
frame = gdb.newest_frame()
base = int(frame.read_register('psp'))
words = [int.from_bytes(bytes(gdb.selected_inferior().read_memory(base + 4*i, 4)), 'little') for i in range(8)]
current = int(gdb.parse_and_eval('rt_current_thread'))
print(json.dumps({'base': base, 'pc': words[6], 'status': words[7],
                  'live_ipsr': int(frame.read_register('xpsr')) & 0x1ff,
                  'current': hex(current)}))
"""
        )
    )


def test_systick_tick_crosses_into_the_interrupted_task(gdb_session):
    raw = data = None
    for _ in range(10):
        _stop_at_tick(gdb_session)
        raw = _probe(gdb_session)
        assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
        assert raw["live_ipsr"] == 15, raw
        data = _json(
            gdb_session.run(f"gdr api backtrace {raw['current']} --json", timeout=30)
        )
        assert data["ok"], data
        data = data["data"]
        if data["task"] == "tidle0":
            break
    assert data is not None and data["task"] == "tidle0", data
    assert data["architecture"] == _ISA and data["source"] == "current", data
    assert data["bounds"] == "recorded", data
    transitions = [f for f in data["frames"] if f["method"] == "exception"]
    assert len(transitions) == 1, data
    assert transitions[0]["pc"] == raw["pc"], data
    assert transitions[0]["symbol"], data
    assert data["complete"] and data["stop_reason"] == "task-exit", data


def test_systick_inside_rt_tick_increase_unwinds_handler_call_chain(gdb_session):
    raw = data = None
    for _ in range(10):
        gdb_session.run("tbreak rt_tick_increase")
        output = gdb_session.run("continue", timeout=30)
        assert "Temporary breakpoint" in output, output
        raw = _probe(gdb_session)
        assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
        assert raw["live_ipsr"] == 15, raw
        data = _json(
            gdb_session.run(f"gdr api backtrace {raw['current']} --json", timeout=30)
        )
        assert data["ok"], data
        data = data["data"]
        if data["task"] == "tidle0":
            break
    assert data is not None and data["task"] == "tidle0", data
    symbols = [frame["symbol"] or "" for frame in data["frames"]]
    assert "rt_tick_increase" in symbols[0], symbols
    assert any("SysTick_Handler" in sym for sym in symbols), symbols
    transitions = [f for f in data["frames"] if f["method"] == "exception"]
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
    assert data["complete"] and data["stop_reason"] == "task-exit", data
