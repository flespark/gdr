"""Closed-loop FPU thread backtraces on the RT-Thread Armv8-M Mainline FPU lane.

The ``fpu`` variant builds the Armv8-M Mainline libcpu with hardware FP, so a preempted
FP-using thread carries the 0x68-byte extended hardware frame plus the
S16-S31 software block.  These tests pin the live evidence: the exact saved
EXC_RETURN slot, enabled FP/CP control bits, and complete saved chains.
"""

from __future__ import annotations

import json
import struct

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread" or SPEC.variant != "fpu" or SPEC.target != "mps2-an505",
    reason="requires the RT-Thread M33 FPU fixture",
)


def _envelope(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"no JSON envelope in output:\n{output}")


def _words(gdb_session, address: int, count: int) -> tuple[int, ...]:
    raw = gdb_session.run_python(
        f"import gdb\nprint(gdb.selected_inferior().read_memory({address:#x}, {count * 4}).hex())"
    )
    blob = bytes.fromhex(raw.strip().splitlines()[-1])
    return struct.unpack(f"<{count}I", blob)


def _top(gdb_session, name: str) -> int:
    return int(
        gdb_session.run(f'p/x $gdr_task("{name}").sp')
        .strip()
        .splitlines()[-1]
        .split()[-1],
        16,
    )


def test_fpu_context_registers_are_enabled_on_target(gdb_session):
    fpccr = int(
        gdb_session.run("p/x *(unsigned int *)0xE000EF34").split("=")[1].strip(), 16
    )
    assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
    cpacr = int(
        gdb_session.run("p/x *(unsigned int *)0xE000ED88").split("=")[1].strip(), 16
    )
    assert cpacr & 0x00F00000 == 0x00F00000, f"CPACR={cpacr:#x}"


def test_fpu_saved_slot_names_the_extended_frame(gdb_session):
    top = _top(gdb_session, "worker_fpu")
    header = _words(gdb_session, top, 4)
    assert header[0] == 0, [f"{w:#x}" for w in header]
    assert header[1] == 0xFFFFFFED, [f"{w:#x}" for w in header]
    assert header[2] == 0, [f"{w:#x}" for w in header]
    assert header[3] & 1 == 0, f"CONTROL={header[3]:#x}"
    data = _envelope(
        gdb_session.run("gdr api backtrace worker_fpu --json", timeout=30)
    )["data"]
    assert data["source"] == "saved", data


def test_fpu_task_backtrace_is_complete(gdb_session):
    envelope = _envelope(
        gdb_session.run("gdr api backtrace worker_fpu --json", timeout=30)
    )
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["architecture"] == "armv8-m.mainline", data
    assert data["abi"] == "aapcs32-vfp", data
    assert data["source"] == "saved", data
    assert data["bounds"] == "recorded", data
    assert data["method"] == "leaf", data
    assert data["stop_reason"] == "task-exit", data
    assert data["complete"] and not data["truncated"], data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("worker_fpu_entry" in function for function in functions), functions


def test_switch_request_window_refuses_task_ownership_live(gdb_session):
    gdb_session.run("tbreak PendSV_Handler")
    gdb_session.run("continue", timeout=60)
    data = _envelope(gdb_session.run("gdr api backtrace worker_fpu --json", timeout=30))
    assert data["ok"] is True, data
    data = data["data"]
    assert not data["complete"], data
    assert data["stop_reason"] == "scheduler-transition-unproven", data
    assert data["frames"] == [], data
