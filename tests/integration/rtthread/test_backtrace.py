"""Closed-loop thread backtraces on the RT-Thread QEMU lanes."""

from __future__ import annotations

import json

import pytest

from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread" or SPEC.target == "cortex-a9",
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
    assert data["port"] == "riscv64", data
    assert data["bounds"] == "recorded" and data["source"] == "saved", data
    assert data["method"] == "cfi", data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("rt_thread_mdelay" in function for function in functions), functions
    assert any("worker1_entry" in function for function in functions), functions
    delay = next(i for i, name in enumerate(functions) if "rt_thread_mdelay" in name)
    entry = next(i for i, name in enumerate(functions) if "worker1_entry" in name)
    assert delay < entry < len(functions) - 1, functions
    assert "thread_exit" in functions[-1], functions
    assert [frame["index"] for frame in data["frames"]] == list(range(len(functions)))
    assert all(frame["method"] == "cfi" for frame in data["frames"][1:]), data
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
