"""Closed-loop FPU thread backtraces on the Armv7E-M FPU lane.

The ``fpu`` variant builds the ARM_CM4F port with hardware FP enabled, so a
task preempted after floating-point work carries the 0x68-byte extended
hardware frame plus the S16-S31 software block.  These tests pin the live
evidence: the exact saved EXC_RETURN slot, readable FPCCR/CPACR enablement,
and complete saved chains through CFI.
"""

from __future__ import annotations

import json
import struct

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos" or SPEC.variant != "fpu",
    reason="requires the live ARM_CM4F FPU fixture",
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


def _task_top(gdb_session, name: str) -> int:
    return int(
        gdb_session.run_python(
            "import gdb\n"
            f"v = gdb.parse_and_eval('$gdr_task(\"{name}\")')\n"
            "print(int(v['pxTopOfStack']))\n"
        )
        .strip()
        .splitlines()[-1]
    )


def test_fpu_context_registers_are_enabled_on_target(gdb_session):
    fpccr = int(gdb_session.run("p/x *(uint32_t *)0xE000EF34").split("=")[1], 16)
    cpacr = int(gdb_session.run("p/x *(uint32_t *)0xE000ED88").split("=")[1], 16)
    assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
    assert cpacr & 0x00F00000 == 0x00F00000, f"CPACR={cpacr:#x}"


def test_fpu_task_saved_chain_is_complete(gdb_session):
    envelope = _envelope(
        gdb_session.run("gdr api backtrace gdr_fpu --json", timeout=30)
    )
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["architecture"] == "armv7e-m", data
    assert data["abi"] == "aapcs32-vfp", data
    assert data["source"] == "saved", data
    assert data["bounds"] == "recorded", data
    assert data["method"] == "cfi", data
    assert data["stop_reason"] == "task-exit", data
    assert data["complete"] and not data["truncated"], data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("gdr_fpu_task" in function for function in functions), functions


def test_fpu_saved_slot_names_the_extended_frame(gdb_session):
    top = _task_top(gdb_session, "gdr_fpu")
    words = _words(gdb_session, top, 36)
    assert words[8] == 0xFFFFFFED, [f"{w:#x}" for w in words[8:9]]
    assert words[32] & 0x01000000 != 0, f"xPSR={words[32]:#x}"
    data = _envelope(gdb_session.run("gdr api backtrace gdr_fpu --json", timeout=30))[
        "data"
    ]
    assert data["source"] == "saved", data
