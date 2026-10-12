"""Real QEMU checks for the machine-readable API layer.

Closed-loop value: unit tests can prove the envelope shape but not that
``$gdr_json`` survives a real GDB evaluate round trip, that ``gdr api`` text
is parseable JSON at the prompt, or that a rejected re-init leaves the live
session usable. Those are the plugin contract, so they are asserted here.

COUPLED: the fixture object names come from the matching patch under
``ci/rt-thread/patches/<target>/<version>/``; keep both sides synchronized.
"""

from __future__ import annotations

import json

import pytest

from gdr.constants import GDR_API_SCHEMA_VERSION
from tests.support.loader import load_integration_spec

SPEC = load_integration_spec()

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread",
    reason="requires an RT-Thread QEMU profile",
)

# The probe line is the only thing printed for the transport test: a long
# envelope printed through GDB would itself be truncated by print settings.
_PROBE = "GDR_API_PROBE"


def _envelope_line(output: str) -> dict:
    """Return the JSON envelope line of a command's output."""
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            parsed = json.loads(candidate)
            assert isinstance(parsed, dict)
            return parsed
    raise AssertionError(f"no JSON envelope in output:\n{output}")


def test_gdr_api_command_prints_a_versioned_envelope(gdb):
    """``gdr api tasks --json`` is one parseable line naming the session."""
    envelope = _envelope_line(gdb.run("gdr api tasks --json", timeout=20))

    assert envelope["schema_version"] == GDR_API_SCHEMA_VERSION
    assert envelope["ok"] is True
    assert envelope["rtos"] == "rtthread"
    assert envelope["rtos_version"] == SPEC.version
    assert isinstance(envelope["generation"], int)
    data = envelope["data"]
    assert data["headers"][0] == "Name"
    assert len(data["rows"]) >= 1


def test_probe_answers_before_initialization(gdb):
    """The plugin's first question is answerable with no adapter selected.

    ``probe`` is the one pre-init verb, so this deliberately resets the session
    and re-initializes afterwards to leave the shared session as it found it.
    """
    gdb.run("gdr reset", timeout=20)
    try:
        envelope = _envelope_line(gdb.run("gdr api probe --json", timeout=20))

        assert envelope["ok"] is True
        assert [item["rtos"] for item in envelope["data"]["candidates"]] == ["rtthread"]

        # The plugin reads the probe through `$gdr_json`, so the data channel has
        # to exist before any adapter is initialized -- not only after `gdr init`.
        output = gdb.run_python(
            """
import gdb
print("GDR_PREINIT", gdb.parse_and_eval('$gdr_json("probe")').string()[:20])
""",
            timeout=20,
        )
        assert f'GDR_PREINIT {{"schema_version":{GDR_API_SCHEMA_VERSION}' in output
    finally:
        # Reason: a failed assertion must not leave the shared session reset, or
        # every later test in this module runs against no adapter at all.
        gdb.run(f"gdr init rtthread {SPEC.version}", timeout=25)


def test_probe_reports_this_fixture_with_its_evidence(gdb):
    """The probe names the adapter and shows the symbols it matched.

    The fixture is built without ``-g3``, so an exact version is asserted only
    when this build actually exports one; a wrong adapter, missing evidence or
    a fabricated version still fails.
    """
    data = _envelope_line(gdb.run("gdr api probe --json", timeout=20))["data"]

    candidates = data["candidates"]
    assert [item["rtos"] for item in candidates] == ["rtthread"]
    candidate = candidates[0]
    # Every lane fixture is built with -g3, so the version must be readable: a
    # range or "undetected" here means the macro section or its read regressed.
    assert candidate["confidence"] == "exact", candidate
    assert candidate["version"] == SPEC.version
    assert data["resolved"] == {"rtos": "rtthread", "version": SPEC.version}
    assert candidate["evidence"], "a candidate must carry its proof"
    assert any(
        marker in " ".join(candidate["evidence"])
        for marker in ("rt_thread_self", "rt_tick", "object_container")
    )


def test_tasks_envelope_carries_the_column_contract(gdb):
    """The envelope carries typed cells, aligned values and stable row ids.

    The text rows stay what the terminal shows, so the typed view is asserted
    to mirror them exactly rather than to replace them.
    """
    data = _envelope_line(gdb.run("gdr api tasks --json", timeout=20))["data"]

    assert data["kind"] == "task"
    columns = {column["key"]: column for column in data["columns"]}
    assert list(columns) == data["headers"]
    assert columns["TCB"]["kind"] == "addr"
    assert columns["Prio"]["kind"] == "int"
    assert columns["Prio"]["align"] == "right"

    value_rows = data["value_rows"]
    assert len(value_rows) == len(data["rows"])
    assert all(len(row) == len(data["headers"]) for row in value_rows)
    assert len(data["ids"]) == len(data["rows"])

    address_index = data["headers"].index("TCB")
    priority_index = data["headers"].index("Prio")
    for text_row, value_row, row_id in zip(
        data["rows"], value_rows, data["ids"], strict=True
    ):
        assert row_id == value_row[address_index]
        assert text_row[address_index] == hex(row_id)
        # The typed value is the number the text renders, not a re-parse of it.
        assert str(value_row[priority_index]) == text_row[priority_index]


def test_capabilities_and_checks_answer_on_a_live_target(gdb):
    """The capability claim and the system verdicts are reachable verbs."""
    capabilities = _envelope_line(gdb.run("gdr api capabilities --json", timeout=20))
    checks = _envelope_line(gdb.run("gdr api checks --json", timeout=20))

    task = capabilities["data"]["kinds"]["task"]
    assert task["enumerable"] is True
    assert task["columns"][0] == "Name"
    assert "HighWater" in task["columns"]
    assert isinstance(capabilities["data"]["extra"], dict)

    assert checks["ok"] is True
    findings = checks["data"]["findings"]
    assert findings, "a live target must report at least one heap finding"
    assert all(
        finding["verdict"] in {"ok", "fail", "skipped", "info"} for finding in findings
    )
    expected = {
        verdict: sum(1 for f in findings if f["verdict"] == verdict)
        for verdict in {f["verdict"] for f in findings}
    }
    assert checks["data"]["counts"] == expected


def test_gdr_api_detail_reports_the_fixture_thread(gdb):
    """A detail verb answers with the object's vertical pairs.

    The API vocabulary is the semantic kind (``task``), not the CLI command
    name (``thread``): adapters route on the kind, and the command tree's
    singular/plural vocabulary is a presentation detail.
    """
    envelope = _envelope_line(gdb.run("gdr api detail task worker1 --json", timeout=20))

    assert envelope["ok"] is True, envelope
    assert envelope["data"]["found"] is True, envelope
    assert dict(envelope["data"]["pairs"])["Name"] == "worker1"


def test_gdr_json_transport_delivers_the_envelope_through_evaluate(gdb):
    """``$gdr_json`` crosses evaluate inline or as reassemblable chunk frames.

    ``Value.string()`` is used instead of printing so GDB's print-element
    limit cannot truncate the payload and mask a transport defect.
    """
    output = gdb.run_python(
        f"""
import gdb
import json

from gdr.serialize import reassemble_chunks

body = gdb.parse_and_eval('$gdr_json("tasks")').string()
if body.startswith('{{"kind": "chunks"'):
    meta = body
    frames = [
        gdb.parse_and_eval(f'$gdr_json_chunk({{index}}, "tasks")').string()
        for index in range(1, json.loads(meta)["total"] + 1)
    ]
    payload = reassemble_chunks(meta, frames)
else:
    payload = body

envelope = json.loads(payload)
print(
    "{_PROBE}",
    envelope["ok"],
    envelope["rtos"],
    envelope["schema_version"],
    len(envelope["data"]["rows"]),
)
""",
        timeout=25,
    )

    assert f"{_PROBE} True rtthread {GDR_API_SCHEMA_VERSION}" in output


def test_rejected_reinit_is_structured_and_non_destructive(gdb):
    """A second init without ``--force`` reports a code and changes nothing.

    This is the plugin's upgrade path: it may call init on every session
    without risking the adapter a previous debug session already selected.
    """
    refusal = _envelope_line(
        gdb.run(f"gdr init rtthread {SPEC.version} --json", timeout=20)
    )

    assert refusal["ok"] is False
    assert refusal["rtos"] == "rtthread"
    assert refusal["error"]["code"] == "already_initialized"
    assert "already initialized" in refusal["error"]["message"]

    still_working = _envelope_line(gdb.run("gdr api tasks --json", timeout=20))
    assert still_working["ok"] is True


def test_force_reinit_keeps_the_live_session_usable(gdb):
    """``--force`` replaces the adapter in place, with commands still answering.

    Runs after the non-destructive checks so a re-init regression cannot hide
    behind a session that other assertions already validated.
    """
    output = gdb.run(f"gdr init rtthread {SPEC.version} --force", timeout=25)
    assert "RT-Thread support ready" in output

    envelope = _envelope_line(gdb.run("gdr api tasks --json", timeout=20))
    assert envelope["ok"] is True
    assert envelope["data"]["rows"]

    folded = gdb.run('p $gdr_task("worker1")', timeout=20)
    assert "Thread(" in folded, folded
    assert "worker1" in folded, folded
