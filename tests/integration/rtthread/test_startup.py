"""Snapshot at QEMU reset, then after scheduler startup, in one GDB session."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.qemu_harness import GdbSession, QemuSession, check_tools


def _envelope(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"missing API envelope:\n{output}")


def test_snapshot_before_and_after_kernel_startup(qemu_profile, tmp_path):
    """A NULL timer-list head must not yield a wrapped fake timer address.

    Separate paused QEMU keeps this test independent of the live suite's shared
    target. Only the test drives it; production collectors never resume it.
    """
    from tests.support.loader import load_integration_spec

    spec = load_integration_spec()
    if qemu_profile.target != "cortex-a9":
        pytest.skip("reset-entry regression uses the Cortex-A9 fixture")
    check_tools(qemu_profile, spec.gdb)
    profile = replace(
        qemu_profile,
        extra_env={"GDR_RTOS": "", "GDR_VERSION": ""},
    )
    root = Path(__file__).resolve().parents[3]
    target = QemuSession(profile)
    session = GdbSession(spec.gdb, profile, target.gdb_port, root)
    try:
        target.start(spec.boot_wait, paused=True)
        session.start()
        before = _envelope(session.run("gdr api snapshot --json", timeout=25))
        assert before["ok"] is True, before
        assert before["data"]["tasks"]["rows"] == []
        assert before["data"]["tables"]["timer"]["rows"] == []

        # Reason: cortex-debug takes the file transport for returned strings;
        # exercise that path at the same pre-kernel stop, not just text output.
        output = tmp_path / "reset-snapshot.json"
        session.run(f"gdr api snapshot --json --out {output}", timeout=25)
        stored = json.loads(output.read_text())
        assert stored["ok"] is True, stored
        assert stored["data"]["tables"]["timer"]["rows"] == []

        session.run("tbreak main")
        stopped = session.run("continue", timeout=30)
        assert "Temporary breakpoint" in stopped, stopped
        after = _envelope(session.run("gdr api snapshot --json", timeout=25))
        assert after["ok"] is True, after
        assert after["data"]["tasks"]["rows"], "scheduler startup should populate tasks"
    finally:
        session.stop()
        target.stop()
