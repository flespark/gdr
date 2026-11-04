"""Closed-loop thread backtraces on the RT-Thread QEMU lanes."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread"
    or SPEC.target not in {"rv64", "mps2-an385", "cortex-a9", "microbit"}
    or (SPEC.target == "cortex-a9" and SPEC.version != "4.0.5"),
    reason="requires an RT-Thread lane with a verified saved-context layout",
)


def _envelope(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"no JSON envelope in output:\n{output}")


def test_blocked_thread_backtrace_reaches_the_entry(gdb_session):
    """A sleeping worker unwinds through the scheduler to its entry and exit."""
    output = gdb_session.run("gdr api backtrace worker1 --json", timeout=30)
    envelope = _envelope(output)
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert (
        data["architecture"]
        == {
            "rv64": "riscv64",
            "mps2-an385": "armv7-m",
            "cortex-a9": "armv7-a",
            "microbit": "armv6-m",
        }[SPEC.target]
    ), data
    assert "port" not in data and "isa" not in data
    assert data["bounds"] == "recorded" and data["source"] == "saved", data
    assert (
        data["method"]
        == {
            "rv64": "cfi",
            "mps2-an385": "leaf",
            "cortex-a9": "ehabi",
            "microbit": "leaf",
        }[SPEC.target]
    ), data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("rt_thread_mdelay" in function for function in functions), functions
    assert any("worker1_entry" in function for function in functions), functions
    delay = next(i for i, name in enumerate(functions) if "rt_thread_mdelay" in name)
    entry = next(i for i, name in enumerate(functions) if "worker1_entry" in name)
    assert delay < entry < len(functions) - 1, functions
    assert "thread_exit" in functions[-1], functions
    root = _envelope(
        gdb_session.run_python(
            "import json; print(json.dumps({'pc': int(gdb.lookup_static_symbol('_thread_exit').value().address)}))"
        )
    )
    assert data["frames"][-1]["pc"] == root["pc"], data
    assert [frame["index"] for frame in data["frames"]] == list(range(len(functions)))
    assert all(
        frame["method"] in ({"cfi"} if SPEC.target == "rv64" else {"ehabi", "leaf"})
        for frame in data["frames"][1:]
    ), data
    if SPEC.target in {"mps2-an385", "microbit"}:
        assert any("instruction leaf" in message for message in data["messages"]), data
    assert data["stop_reason"] == "task-exit"
    assert data["complete"] is True
    assert not data["truncated"], data


def test_text_backtrace_is_bounded_and_clean(gdb_session):
    """The text route renders at most the requested number of frames."""
    text = gdb_session.run("rtt backtrace worker1 --max-depth 1", timeout=30)
    assert "Traceback" not in text
    assert "worker1:" in text
    assert "max-depth" in text
    envelope = _envelope(
        gdb_session.run("gdr api backtrace worker1 --max-depth 1 --json", timeout=30)
    )
    assert envelope["ok"] is True
    data = envelope["data"]
    assert len(data["frames"]) == 1
    assert data["stop_reason"] == "max-depth"
    assert data["truncated"] and not data["complete"], data


def test_missing_thread_name_is_structured(gdb_session):
    """A missing thread is a stop reason, not an exception or empty success."""
    envelope = _envelope(
        gdb_session.run("gdr api backtrace no_such_thread --json", timeout=30)
    )
    assert envelope["ok"] is True
    assert envelope["data"]["stop_reason"] == "task-not-found"
    assert envelope["data"]["frames"] == []
    assert not envelope["data"]["complete"]


@pytest.mark.skipif(
    SPEC.target == "rv64", reason="requires the Arm initial-context fixture"
)
def test_initial_thread_has_an_independent_entry_root(gdb_session):
    data = _envelope(gdb_session.run("gdr api backtrace gdr_unstarted --json"))["data"]
    assert data["source"] == "unstarted" and data["complete"], data
    assert data["stop_reason"] == "task-entry" and len(data["frames"]) == 1, data
    assert "gdr_unstarted_entry" in data["frames"][0]["symbol"], data


@pytest.mark.skipif(SPEC.target == "rv64", reason="requires the Arm live-chain fixture")
def test_current_thread_backtrace_preserves_the_halted_state(gdb_session):
    gdb_session.run("tbreak gdr_bt_live_stop")
    stopped = gdb_session.run("continue", timeout=30)
    assert "Temporary breakpoint" in stopped, stopped
    state_code = """
import hashlib, json
thread = gdb.lookup_static_symbol('worker3_thread').value()
frame = gdb.newest_frame()
registers = {name: int(frame.read_register(name)) for name in ('sp', 'lr')}
low = int(thread['stack_addr'])
high = low + int(thread['stack_size'])
print(json.dumps({'pc': int(frame.pc()), 'function': frame.name(), 'registers': registers,
                  'bounds': [low, high], 'thread': int(thread.address),
                  'hash': hashlib.sha256(bytes(gdb.selected_inferior().read_memory(low, high-low))).hexdigest()}))
"""
    before = _envelope(gdb_session.run_python(state_code))
    data = _envelope(gdb_session.run("gdr api backtrace worker3 --json"))["data"]
    after = _envelope(gdb_session.run_python(state_code))
    assert before == after
    assert data["source"] == "current" and data["address"] == before["thread"], data
    assert before["bounds"][0] <= before["registers"]["sp"] <= before["bounds"][1]
    assert data["frames"][0]["pc"] == before["pc"] & ~1, data
    assert before["function"] == "gdr_bt_live_leaf", before
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    leaf = next(
        i
        for i, name in enumerate(functions)
        if "gdr_bt_live_leaf" in name or "gdr_bt_live_stop" in name
    )
    middle = next(i for i, name in enumerate(functions) if "gdr_bt_live_middle" in name)
    entry = next(i for i, name in enumerate(functions) if "worker3_entry" in name)
    assert leaf < middle < entry < len(functions) - 1, functions
    assert data["complete"] and data["stop_reason"] == "task-exit", data
