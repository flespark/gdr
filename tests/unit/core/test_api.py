"""Unit tests for the semantic API verb layer and session lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

import gdr.adapter_api as adapter_api
import gdr.api as api
from gdr.adapter_api import HeapReport, ObjectDetail, ObjectTable, SystemSummary
from gdr.serialize import reassemble_chunks


@dataclass
class _ApiAdapter:
    """Protocol stand-in that records which verbs touched it."""

    table: ObjectTable = field(
        default_factory=lambda: ObjectTable(
            headers=["Name"], rows=[["worker"]], messages=["scan note"]
        )
    )
    counts: dict[str, int] = field(default_factory=lambda: {"queue": 1})
    details: dict[str, ObjectDetail] = field(default_factory=dict)
    summary: SystemSummary = field(
        default_factory=lambda: SystemSummary(kernel_version="4.0.5")
    )
    report: HeapReport | None = field(
        default_factory=lambda: HeapReport(pairs=[("Algorithm", "small")])
    )
    resets: int = 0
    boom: bool = False
    boom_exc: Exception | None = None

    def task_table(self):
        if self.boom:
            raise self.boom_exc or RuntimeError("adapter exploded")
        return self.table

    def object_counts(self):
        return self.counts

    def object_table(self, kind):
        return self.table if kind == "queue" else None

    def object_summary_table(self):
        return None

    def object_detail(self, kind, name):  # noqa: ARG002
        return self.details.get(kind)

    def system_summary(self):
        return self.summary

    def heap_report(self):
        return self.report

    def find_task(self, name):  # noqa: ARG002
        return None

    def find_object(self, kind, name):  # noqa: ARG002
        return None

    def iter_tasks(self):
        return iter(())

    def capabilities(self):
        return None

    def reset(self):
        self.resets += 1


@pytest.fixture
def session():
    """Register one fake adapter and always leave the semantic session clear."""
    adapter = _ApiAdapter()
    adapter_api.reset()
    adapter_api.register(adapter, rtos="rtthread", version="4.0.5")
    yield adapter
    adapter_api.reset()


def _data(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def _error(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error


def test_tasks_envelope_carries_session_identity(session):  # noqa: ARG001
    """A verb result names the initialized RTOS without parsing init logs."""
    envelope = api.api_call("tasks")

    assert envelope["ok"] is True
    assert envelope["rtos"] == "rtthread"
    assert envelope["rtos_version"] == "4.0.5"
    assert _data(envelope)["rows"] == [["worker"]]
    assert envelope["warnings"] == ["scan note"]
    elapsed = envelope["elapsed_ms"]
    assert isinstance(elapsed, float)
    assert elapsed >= 0


def test_objects_overview_falls_back_to_sorted_counts(session):  # noqa: ARG001
    """Without a provenance table the overview is the Kind/Count table."""
    envelope = api.api_call("objects")

    assert _data(envelope)["headers"] == ["Kind", "Count"]
    assert _data(envelope)["rows"] == [["queue", "1"]]


def test_objects_kind_uses_the_table_or_reports_unknown(session):  # noqa: ARG001
    """A known kind returns its table; an unknown kind is a coded error."""
    assert _data(api.api_call("list", "queue"))["rows"] == [["worker"]]
    assert _data(api.api_call("objects", "task"))["headers"] == ["Name"]

    missing = api.api_call("objects", "mailbox")

    assert missing["ok"] is False
    assert _error(missing)["code"] == "kind_not_enumerable"


def test_detail_distinguishes_missing_kind_from_missing_object(session):
    """Not found is data; an unknown kind is an error envelope."""
    session.details["queue"] = ObjectDetail(found=False, message="gone")

    found = api.api_call("detail", "queue", "work")
    unknown = api.api_call("detail", "timer", "work")
    empty = api.api_call("detail", "queue", " ")

    assert found["ok"] is True
    assert _data(found)["found"] is False
    assert _error(unknown)["code"] == "kind_not_enumerable"
    assert _error(empty)["code"] == "empty_name"


def test_system_heap_capabilities_and_snapshot_compose(session):
    """Snapshot is the panel payload: one call, every section."""
    session.report = None

    snapshot = api.api_call("snapshot")

    data = _data(snapshot)
    system = data["system"]
    heap = data["heap"]
    capabilities = data["capabilities"]
    assert isinstance(system, dict)
    assert isinstance(heap, dict)
    assert isinstance(capabilities, dict)
    pairs = system["pairs"]
    kinds = capabilities["kinds"]
    assert isinstance(pairs, list)
    assert isinstance(kinds, dict)
    queue = kinds["queue"]
    assert isinstance(queue, dict)
    assert pairs[0] == ["Kernel version", "4.0.5"]
    assert heap["table"] is None
    assert queue["enumerable"] is True
    assert "tasks" in data


def test_uninitialized_and_unknown_verb_are_structured(session):
    """A plugin gets a code, never a GDB traceback.

    An unknown verb is diagnosed only when a session exists: before init the
    single actionable message is "initialize first", and checking the
    vocabulary would only add noise.
    """
    unknown = api.api_call("probe")

    assert _error(unknown)["code"] == "unknown_verb"
    message = _error(unknown)["message"]
    assert isinstance(message, str)
    assert "tasks" in message

    del session
    adapter_api.reset()

    assert _error(api.api_call("tasks"))["code"] == "not_initialized"


def test_adapter_exception_becomes_an_internal_error(session, monkeypatch):
    """Unexpected adapter failures stay inside the envelope."""
    session.boom = True
    monkeypatch.setattr(api, "propagate_exception", lambda: False)

    envelope = api.api_call("tasks")

    assert envelope["ok"] is False
    assert _error(envelope)["code"] == "internal"
    message = _error(envelope)["message"]
    assert isinstance(message, str)
    assert "adapter exploded" in message


def test_target_access_failure_is_recoverable_not_internal(session):
    """A failed target read is degradation the client can retry after a stop.

    ``gdr_bridge.TARGET_ACCESS_ERRORS`` is the documented set of expected
    target-access failures; conflating it with ``internal`` would tell a
    plugin to report a GDR bug for an ordinary halted-core read.
    """
    session.boom = True
    session.boom_exc = ValueError("cannot access memory at 0x10")

    envelope = api.api_call("tasks")

    assert envelope["ok"] is False
    assert _error(envelope)["code"] == "target_unavailable"
    message = _error(envelope)["message"]
    assert isinstance(message, str)
    assert "cannot access memory" in message


def test_debug_build_prints_a_diagnostic_and_can_reraise(session, monkeypatch):
    """GDR_DEBUG/GDR_PROPAGATE_EXCEPTION keep their documented meaning."""
    session.boom = True
    diagnostics: list[str] = []
    monkeypatch.setattr(api, "is_debug", lambda: True)
    monkeypatch.setattr(api, "propagate_exception", lambda: True)
    monkeypatch.setattr(api, "show_last_exception", lambda: diagnostics.append("full"))

    with pytest.raises(RuntimeError, match="adapter exploded"):
        api.api_call("tasks")

    assert diagnostics == ["full"]


def test_generation_bumps_and_rides_in_the_envelope(session):
    """Stop/reset hooks bump a counter the client can use to drop stale data."""
    del session
    before = api.api_call("system")["generation"]
    assert isinstance(before, int)

    assert api.next_generation() == before + 1
    assert api.api_call("system")["generation"] == before + 1


def test_oversized_envelopes_use_the_chunked_transport(session):
    """An envelope past the inline limit reassembles from chunk frames.

    The real threshold is exercised (no internal patching): a wide task table
    pushes the envelope past ``GDR_API_INLINE_LIMIT``, so ``transport_response``
    must answer with a meta frame instead of an inline payload.
    """
    session.table = ObjectTable(
        headers=["Name"], rows=[[f"task-{index:04d}"] for index in range(400)]
    )

    meta = api.transport_response("tasks")

    assert json.loads(meta)["kind"] == "chunks"
    total = json.loads(meta)["total"]
    assert total > 1
    frames = [api.chunk_response(index, "tasks") for index in range(1, total + 1)]

    assert json.loads(reassemble_chunks(meta, frames))["ok"] is True
    assert "chunk_out_of_range" in api.chunk_response(total + 1, "tasks")


def test_chunk_frames_stay_frozen_until_the_generation_changes(session):
    """Every frame of one transfer describes the same frozen envelope.

    Recomputation per frame would embed a new ``elapsed_ms`` each time (the
    checksum could never match) and a target that moved between two frame
    requests would reassemble into a mix of two snapshots.
    """
    session.table = ObjectTable(
        headers=["Name"], rows=[[f"task-{index:04d}"] for index in range(400)]
    )
    meta = api.transport_response("tasks")
    generation = json.loads(
        reassemble_chunks(
            meta,
            [
                api.chunk_response(index, "tasks")
                for index in range(1, json.loads(meta)["total"] + 1)
            ],
        )
    )["generation"]

    assert api.chunk_response(1, "tasks") == api.chunk_response(1, "tasks")

    api.next_generation()
    fresh_meta = api.transport_response("tasks")
    fresh_total = json.loads(fresh_meta)["total"]
    fresh = reassemble_chunks(
        fresh_meta,
        [api.chunk_response(index, "tasks") for index in range(1, fresh_total + 1)],
    )

    assert json.loads(fresh)["generation"] == generation + 1


def test_small_envelopes_stay_inline(session):  # noqa: ARG001
    """A normal verb result is the envelope itself, not a chunk meta frame."""
    payload = api.transport_response("tasks")

    assert json.loads(payload)["ok"] is True


def test_bad_verb_arguments_are_coded(session):  # noqa: ARG001
    """Argument mistakes are envelope errors, not Python exceptions."""
    assert _error(api.api_call("tasks", "extra"))["code"] == "bad_args"
    assert _error(api.api_call("detail", "queue"))["code"] == "bad_args"
