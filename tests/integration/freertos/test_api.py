"""Real QEMU checks for the machine-readable API on the FreeRTOS lanes.

Closed-loop value: the envelope shape is unit-tested, but only a live target
shows that the typed cells match the rendered text, that ``ids`` are the real
object addresses, and that the capability claim describes *this* build's
configuration (queue sets, trace facility, registry, timers).

COUPLED: fixture names and the variant matrix come from
``ci/freertos/fixture/main.c`` and ``ci/freertos/fixture/config/<variant>/``.
"""

from __future__ import annotations

import json

import pytest

from tests.support.freertos_fixture_profiles import get_freertos_test_profile
from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()
_PROFILE = (
    get_freertos_test_profile(SPEC.variant, SPEC.version, SPEC.target)
    if SPEC.rtos == "freertos" and SPEC.variant != "snapshot"
    else None
)

pytestmark = pytest.mark.skipif(
    _PROFILE is None,
    reason="requires a live FreeRTOS QEMU profile",
)


def _envelope_line(output: str) -> dict:
    """Return the JSON envelope line of a command's output."""
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            parsed = json.loads(candidate)
            assert isinstance(parsed, dict)
            return parsed
    raise AssertionError(f"no JSON envelope in output:\n{output}")


def test_tasks_envelope_names_the_session_and_carries_the_contract(gdb):
    """Typed cells mirror the rendered task rows and key them by address."""
    envelope = _envelope_line(gdb.run("gdr api tasks --json", timeout=25))

    assert envelope["schema_version"] == 1
    assert envelope["ok"] is True
    assert envelope["rtos"] == "freertos"
    assert envelope["rtos_version"] == SPEC.version

    data = envelope["data"]
    assert data["kind"] == "task"
    columns = {column["key"]: column for column in data["columns"]}
    assert list(columns) == data["headers"]
    assert columns["Addr"]["kind"] == "addr"
    assert columns["Prio"]["kind"] == "int"

    value_rows = data["value_rows"]
    assert len(value_rows) == len(data["rows"]) >= 1
    assert len(data["ids"]) == len(data["rows"])

    address_index = data["headers"].index("Addr")
    priority_index = data["headers"].index("Prio")
    for text_row, value_row, row_id in zip(
        data["rows"], value_rows, data["ids"], strict=True
    ):
        assert row_id == value_row[address_index]
        assert text_row[address_index] == hex(row_id)
        # The typed value is the number the text renders, not a re-parse of it.
        assert str(value_row[priority_index]) == text_row[priority_index]


def test_capabilities_describe_this_build_configuration(gdb):
    """The claim matches the variant: registry, trace, queue sets, timers."""
    data = _envelope_line(gdb.run("gdr api capabilities --json", timeout=25))["data"]

    queue = data["kinds"]["queue"]
    assert queue["enumerable"] is True
    assert ("Set" in queue["columns"]) is bool(_PROFILE.queue_sets)
    assert ("configUSE_TRACE_FACILITY" in (queue["note"] or "")) is (
        not _PROFILE.trace_facility
    )
    assert ("configQUEUE_REGISTRY_SIZE" in (queue["note"] or "")) is (
        _PROFILE.registry_size == 0
    )

    assert data["kinds"]["streambuffer"]["enumerable"] is bool(_PROFILE.stream_buffers)
    assert data["extra"]["trace_facility"] is bool(_PROFILE.trace_facility)
    assert data["extra"]["queue_registry"] is (_PROFILE.registry_size > 0)

    task_columns = data["kinds"]["task"]["columns"]
    assert ("CPU" in task_columns) is (_PROFILE.number_of_cores > 1)


def test_checks_report_the_system_verdicts(gdb):
    """``gdr api checks`` carries the same conclusions ``frt system`` prints."""
    envelope = _envelope_line(gdb.run("gdr api checks --json", timeout=25))

    assert envelope["ok"] is True
    findings = envelope["data"]["findings"]
    assert [finding["name"] for finding in findings][:1] == ["TaskCount"]
    assert all(
        finding["verdict"] in {"ok", "fail", "skipped", "info"} for finding in findings
    )
    expected = {
        verdict: sum(1 for f in findings if f["verdict"] == verdict)
        for verdict in {f["verdict"] for f in findings}
    }
    assert envelope["data"]["counts"] == expected


def test_queue_table_declares_the_registry_dependent_columns(gdb):
    """A kind-scoped table answers with its own kind and column contract."""
    envelope = _envelope_line(gdb.run("gdr api objects queue --json", timeout=25))

    if envelope["ok"] is False:
        # Without a registry, no queue may be discoverable at all; that is a
        # coded answer, not a broken verb.
        assert envelope["error"]["code"] == "kind_not_enumerable"
        return

    data = envelope["data"]
    assert data["kind"] == "queue"
    assert ("Set" in data["headers"]) is bool(_PROFILE.queue_sets)
    assert "Addr" in data["headers"]
