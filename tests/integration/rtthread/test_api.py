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

    assert envelope["schema_version"] == 1
    assert envelope["ok"] is True
    assert envelope["rtos"] == "rtthread"
    assert envelope["rtos_version"] == SPEC.version
    assert isinstance(envelope["generation"], int)
    data = envelope["data"]
    assert data["headers"][0] == "Name"
    assert len(data["rows"]) >= 1


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

    assert f"{_PROBE} True rtthread 1" in output


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
    assert "already initialized" in refusal["error"]

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
