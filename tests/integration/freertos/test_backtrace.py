"""Closed-loop task backtraces on the FreeRTOS QEMU lanes.

The fixture's ``gdr_ready`` task sleeps inside ``vTaskDelay``; its saved
context therefore has a known call chain (``gdr_ready_task -> vTaskDelay``) and
can be unwound without stopping the target in a special place.
"""

from __future__ import annotations

import json

import pytest

from tests.support.freertos_fixture_profiles import get_freertos_test_profile
from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
PROFILE = (
    get_freertos_test_profile(SPEC.variant, SPEC.version, SPEC.target)
    if SPEC.rtos == "freertos"
    else None
)


def _recorded_bounds():
    return PROFILE is not None and PROFILE.high_water_source == "pxEndOfStack"


pytestmark = pytest.mark.skipif(
    SPEC.rtos != "freertos" or SPEC.variant == "snapshot",
    reason="requires a live FreeRTOS QEMU profile",
)


def _envelope(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"no JSON envelope in output:\n{output}")


def test_saved_task_backtrace_has_a_known_chain(gdb_session):
    """A blocked task unwinds from its saved frame through CFI or EHABI."""
    output = gdb_session.run("gdr api backtrace gdr_ready --json", timeout=30)
    envelope = _envelope(output)
    assert envelope["ok"] is True, envelope
    data = envelope["data"]
    assert data["task"] == "gdr_ready"
    if not _recorded_bounds():
        assert data["stop_reason"] == "stack-bounds-unavailable", data
        assert not data["complete"] and data["frames"] == [], data
        return
    assert (
        data["architecture"]
        == {
            "microbit": "armv6-m",
            "mps2-an385": "armv7-m",
            "mps2-an386": "armv7e-m",
            "mps2-an521": "armv8-m.mainline",
            "qemu-virt-rv32": "riscv32",
            "qemu-virt-rv64": "riscv64",
        }[SPEC.target]
    )
    assert "port" not in data and "isa" not in data
    assert data["frames"], data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("vTaskDelay" in function for function in functions), functions
    assert any("gdr_ready_task" in function for function in functions), functions
    delay = next(i for i, name in enumerate(functions) if "vTaskDelay" in name)
    entry = next(i for i, name in enumerate(functions) if "gdr_ready_task" in name)
    assert delay < entry, functions
    assert data["bounds"] == "recorded"
    assert data["source"] == "saved"
    if SPEC.target.startswith("qemu-virt-rv"):
        oracle = _envelope(
            gdb_session.run_python(
                f"""
import json
symbol = gdb.lookup_static_symbol('gdr_ready_task')
assert symbol is not None
print(json.dumps({{
    'entry': int(symbol.value().address),
    'lookup_symbol': gdb.execute('info symbol {data["frames"][-1]["pc"] - 1:#x}', to_string=True).split(' in section ')[0].strip(),
}}))
"""
            )
        )
        assert not data["complete"] and data["stop_reason"] == "cfi-stack-overflow", (
            data
        )
        assert len(data["frames"]) == entry + 2, data
        assert data["frames"][-1]["pc"] == oracle["entry"], data
        assert functions[-1] == oracle["lookup_symbol"].replace(" + ", "+"), (
            data,
            oracle,
        )
    else:
        assert data["complete"] and data["stop_reason"] == "task-exit", data
    assert not data["truncated"], data
    assert data["method"] == ("ehabi" if SPEC.target == "mps2-an385" else "cfi"), data
    assert [f["index"] for f in data["frames"]] == list(range(len(functions)))
    assert data["method"] == data["frames"][1]["method"], data


@pytest.mark.skipif(
    not SPEC.target.startswith("qemu-virt-rv"),
    reason="requires the RISC-V zero task-return sentinel",
)
def test_riscv_nonfirst_task_reaches_the_recorded_zero_exit(gdb_session):
    data = _envelope(
        gdb_session.run("gdr api backtrace gdr_normal --json", timeout=30)
    )["data"]
    assert data["architecture"] == (
        "riscv32" if SPEC.target.endswith("rv32") else "riscv64"
    ), data
    assert "port" not in data and "isa" not in data
    assert data["bounds"] == "recorded" and data["source"] == "saved", data
    assert data["complete"] and not data["truncated"], data
    assert data["stop_reason"] == "task-exit" and data["method"] == "cfi", data
    names = [frame["symbol"] or "" for frame in data["frames"]]
    assert "vTaskDelay" in names[0] and "gdr_delay_task" in names[-1], names
    assert all(frame["pc"] != 0 for frame in data["frames"]), data


def test_backtrace_command_matches_the_api_and_stays_bounded(gdb_session):
    """The text route returns a frame table and an explicit stop reason."""
    text = gdb_session.run("freertos backtrace gdr_ready --max-depth 1", timeout=30)
    assert "Traceback" not in text

    envelope = _envelope(
        gdb_session.run("gdr api backtrace gdr_ready --max-depth 1 --json", timeout=30)
    )
    assert envelope["ok"] is True
    data = envelope["data"]
    if _recorded_bounds():
        assert len(data["frames"]) == 1
        assert data["truncated"] and not data["complete"]
        assert data["stop_reason"] == "max-depth"
        assert "max-depth" in text
    else:
        assert data["frames"] == []
        assert data["stop_reason"] == "stack-bounds-unavailable"
        assert "stack-bounds-unavailable" in text


def test_initial_task_does_not_invent_entry_evidence(gdb_session):
    """Fixture existence is independent of the API's completeness verdict."""
    output = gdb_session.run("gdr api backtrace gdr_new --json", timeout=30)
    envelope = _envelope(output)
    assert envelope["ok"] is True
    data = envelope["data"]
    if not PROFILE.never_started_task:
        assert data["stop_reason"] == "task-not-found" and data["frames"] == [], data
        return
    assert data["stop_reason"] != "task-not-found", data
    assert data["source"] != "unstarted", data
    assert data["stop_reason"] != "task-entry", data
    if _recorded_bounds():
        assert data["frames"] and "gdr_unstarted" in data["frames"][0]["symbol"], data
    else:
        assert data["stop_reason"] == "stack-bounds-unavailable"


def test_running_task_is_not_classified_as_unstarted(gdb_session):
    data = _envelope(gdb_session.run("gdr api backtrace gdr_spin --json", timeout=30))[
        "data"
    ]
    assert data["stop_reason"] != "task-not-found", data
    assert data["source"] != "unstarted", data
    assert data["stop_reason"] != "task-entry", data


def test_unknown_task_reports_structured_failure(gdb_session):
    """A missing name is a stop reason, not a traceback or an empty success."""
    output = gdb_session.run("gdr api backtrace no_such_task --json", timeout=30)
    envelope = _envelope(output)
    assert envelope["ok"] is True
    assert envelope["data"]["stop_reason"] == "task-not-found"
    assert envelope["data"]["frames"] == []
