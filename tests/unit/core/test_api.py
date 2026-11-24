"""Unit tests for the semantic API verb layer and session lifecycle."""

from __future__ import annotations

import json
from dataclasses import (
    dataclass,
    field,
)
from typing import Any, cast

import pytest

import core.adapter as adapter_api
import core.api as api
from core.adapter import (
    BacktraceFrame,
    BacktraceReport,
    Capabilities,
    CheckFinding,
    ColumnSpec,
    HeapMap,
    HeapReport,
    KindCapability,
    ObjectDetail,
    ObjectTable,
    SystemSummary,
)
from core.api import (
    backtrace_to_dict,
    capabilities_to_dict,
    chunk_payload,
    heap_report_to_dict,
    object_detail_to_dict,
    object_table_to_dict,
    pack_envelope,
    reassemble_chunks,
    system_summary_pairs,
    system_summary_to_dict,
    transport_plan,
)
from core.constants import (
    GDR_API_CHUNK_SIZE,
    GDR_API_INLINE_LIMIT,
    GDR_API_SCHEMA_VERSION,
)
from core.version import ProbeCandidate


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
        default_factory=lambda: SystemSummary(current_task="main")
    )
    tables: dict[str, ObjectTable] = field(default_factory=dict)
    heap_map_value: HeapMap | None = None
    report: HeapReport | None = field(
        default_factory=lambda: HeapReport(pairs=[("Algorithm", "small")])
    )
    findings: list[CheckFinding] = field(
        default_factory=lambda: [CheckFinding("TaskCount", "ok", scope="system")]
    )
    backtrace_value: BacktraceReport | None = None
    resets: int = 0
    boom: bool = False
    boom_exc: Exception | None = None
    heap_boom: bool = False

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

    def object_detail(self, kind, target):  # noqa: ARG002
        return self.details.get(kind)

    def detail_table(self, kind):
        return self.tables.get(kind)

    def heap_map(self):
        return self.heap_map_value

    def system_summary(self):
        return self.summary

    def heap_report(self):
        if self.heap_boom:
            raise AssertionError("heap must not be collected")
        return self.report

    def find_task(self, target):  # noqa: ARG002
        return None

    def find_object(self, kind, target):  # noqa: ARG002
        return None

    def iter_tasks(self):
        return iter(())

    def capabilities(self):
        return None

    def diagnostics_report(self):
        return self.findings

    def backtrace(self, target, max_depth=32):  # noqa: ARG002
        return self.backtrace_value

    def reset(self):
        self.resets += 1


@pytest.fixture
def session():
    """Register one fake adapter and always leave the semantic session clear."""
    adapter = _ApiAdapter()
    adapter_api.reset()
    adapter_api.register(cast(Any, adapter), rtos="rtthread", version="4.0.5")
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


def _dict_list(value: object) -> list[dict[str, object]]:
    """Narrow a serialized list of objects for typed assertions."""
    assert isinstance(value, list)
    result: list[dict[str, object]] = []
    for item in value:
        assert isinstance(item, dict)
        result.append(item)
    return result


def _strings(value: object) -> list[str]:
    """Narrow a serialized list of strings for typed assertions."""
    assert isinstance(value, list)
    result: list[str] = []
    for item in value:
        assert isinstance(item, str)
        result.append(item)
    return result


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
    assert pairs[0] == ["Current task", "main"]
    assert "Kernel version" not in [key for key, _value in pairs]
    assert heap["table"] is None
    assert queue["enumerable"] is True
    assert "tasks" in data
    # Reason: the snapshot carries no separate checks section; the verdicts ride
    # inside the heap section and the per-kind tables, and ``checks`` stays a verb.
    assert "checks" not in data
    assert data["tables"] == {}
    envelope = api.api_call("check")
    assert _data(envelope)["counts"] == {"ok": 1}


def test_snapshot_separates_build_advice_from_general_messages(session):
    from core.adapter import Capabilities

    session.table.build_hints = ["fill stacks", "keep debug symbols"]
    session.capabilities = lambda: Capabilities(
        build_hints=["keep debug symbols", "enable tracing"]
    )
    result = api.api_call("snapshot")
    assert _data(result)["build_hints"] == [
        "fill stacks",
        "keep debug symbols",
        "enable tracing",
    ]
    assert result["warnings"] == ["scan note"]
    assert "scan note" not in _strings(_data(result)["build_hints"])


def test_snapshot_no_heap_skips_the_heap_walk(session):
    """The cost switch must not even touch the heap collector."""
    session.heap_boom = True

    snapshot = api.api_call("snapshot", "--no-heap")

    assert snapshot["ok"] is True
    data = _data(snapshot)
    assert data["heap"] is None
    assert "tasks" in data and "tables" in data


def test_snapshot_no_details_drops_the_tables(session):
    """``--no-details`` skips the per-kind tables and keeps everything else."""
    session.counts["semaphore"] = 1
    session.tables["semaphore"] = ObjectTable(headers=["Name"], rows=[["sem"]])

    data = _data(api.api_call("snapshot", "--no-details"))

    assert data["tables"] is None
    assert "tasks" in data and "heap" in data
    tables = _data(api.api_call("snapshot"))["tables"]
    assert isinstance(tables, dict)
    assert tables["semaphore"]["rows"] == [["sem"]]
    assert tables["semaphore"]["kind"] == "semaphore"


def test_table_verb_returns_the_merged_table_or_reports_the_kind(session):
    """``table <kind>`` answers with the adapter's merged table, else a coded error."""
    session.tables["mutex"] = ObjectTable(headers=["Name"], rows=[["m"]])

    envelope = api.api_call("table", "mutex")
    assert _data(envelope)["headers"] == ["Name"]
    assert _data(envelope)["kind"] == "mutex"
    missing = api.api_call("table", "queue")
    assert _error(missing)["code"] == "kind_not_enumerable"
    assert _error(api.api_call("table"))["code"] == "bad_args"


def test_heap_dump_verb_reports_no_heap_or_the_layout(session):
    """``heap-dump`` is coded when there is nothing to draw, else the block layout."""
    assert _error(api.api_call("heap-dump"))["code"] == "no_heap"

    from core.adapter import HeapMap, HeapMapBlock

    session.heap_map_value = HeapMap(
        algorithm="small_mem",
        base=0x1000,
        end=0x1100,
        blocks=[HeapMapBlock(0x1000, 0x40, True, "main")],
    )
    data = _data(api.api_call("heap-dump"))
    assert data["blocks"] == [[0x1000, 0x40, True, "main"]]
    assert data["owners"] is False
    palette = data["palette"]
    assert isinstance(palette, dict)
    owners = palette["owners"]
    assert isinstance(owners, dict)
    assert set(owners) == {"main"}


def test_the_verification_note_rides_every_envelope(session):
    """The one caveat a session can carry must survive into every answer."""
    note = "target RT-Thread version not exported; the requested version is unverified"
    adapter_api.reset()
    adapter_api.register(
        session, rtos="rtthread", version="4.0.5", verification_note=note
    )

    envelope = api.api_call("tasks")

    warnings = envelope["warnings"]
    assert isinstance(warnings, list)
    assert warnings[0] == note, "the session caveat leads the warnings"
    assert "scan note" in warnings, "the verb's own warnings still follow"


def test_a_verified_session_adds_no_warning(session):  # noqa: ARG001
    """The fixture registers without a note, so no caveat may appear."""
    envelope = api.api_call("tasks")

    warnings = _strings(envelope.get("warnings", []))
    assert "unverified" not in " ".join(warnings)


def test_snapshot_rejects_unknown_flags(session):  # noqa: ARG001
    snapshot = api.api_call("snapshot", "--no-stack-peak")

    assert snapshot["ok"] is False
    assert snapshot["error"]["code"] == "bad_args"  # type: ignore[index]
    assert "--no-heap" in snapshot["error"]["message"]  # type: ignore[index]


def test_check_verb_reports_findings_and_counts(session):
    """The check verb is the structured form of the system verdicts."""
    session.findings = [
        CheckFinding("TaskCount", "ok", scope="system"),
        CheckFinding("HeapCrossCheck", "fail", "blocks disagree", scope="system"),
    ]

    envelope = api.api_call("check")

    assert envelope["ok"] is True
    data = _data(envelope)
    assert data["counts"] == {"fail": 1, "ok": 1}
    assert _dict_list(data["findings"])[1]["detail"] == "blocks disagree"


def test_legacy_checks_verb_spelling_still_answers(session):
    """The pre-rename ``checks`` verb stays an alias of ``check``."""
    session.findings = [CheckFinding("Tick", "ok", scope="system")]

    envelope = api.api_call("checks")

    assert envelope["ok"] is True
    assert _data(envelope)["counts"] == {"ok": 1}


def test_check_verb_explains_an_adapter_without_system_checks(session):
    """An adapter without system checks still answers, with a capability note."""
    session.findings = None

    envelope = api.api_call("check")

    assert envelope["ok"] is True
    assert _data(envelope)["findings"] == []
    notes = _strings(envelope["warnings"])
    assert any("no system-level checks" in note for note in notes)


def test_uninitialized_and_unknown_verb_are_structured(session):  # noqa: ARG001
    """A plugin gets a code, never a GDB traceback.

    An unknown verb is diagnosed only when a session exists: before init the
    single actionable message is "initialize first", and checking the
    vocabulary would only add noise.
    """
    unknown = api.api_call("bogus")

    assert _error(unknown)["code"] == "unknown_verb"
    message = _error(unknown)["message"]
    assert isinstance(message, str)
    assert "tasks" in message

    adapter_api.reset()

    assert _error(api.api_call("tasks"))["code"] == "not_initialized"


def test_probe_and_current_backtrace_answer_before_init(monkeypatch):
    """Pre-init sessions answer ``probe`` and un-targeted ``backtrace``.

    Every RTOS-specific verb and targeted ``backtrace <task>`` still return
    ``not_initialized``.
    """
    adapter_api.reset()
    monkeypatch.setattr(
        api,
        "collect_candidates",
        lambda: [
            ProbeCandidate(
                rtos="rtthread",
                confidence="exact",
                version="4.0.5",
                evidence=("symbol: rt_thread_self",),
            )
        ],
    )

    envelope = api.api_call("probe")

    assert envelope["ok"] is True
    data = _data(envelope)
    assert data["resolved"] == {"rtos": "rtthread", "version": "4.0.5"}
    assert _dict_list(data["candidates"])[0]["confidence"] == "exact"

    report = BacktraceReport(
        task="<current>",
        address=0,
        architecture="riscv64",
        abi="lp64",
        method="cfi",
        source="current",
        complete=False,
        stop_reason="cfi-return-address-undefined",
        truncated=False,
        max_depth=8,
        bounds="recorded",
        frames=[BacktraceFrame(0, 0x80000100, "seed", "hdr_checkpoint")],
    )
    recorded_depths: list[int] = []
    monkeypatch.setattr(
        "core.backtrace.unwind_gdb.live_backtrace",
        lambda max_depth=32: recorded_depths.append(max_depth) or report,
    )
    bt_env = api.api_call("backtrace", "--max-depth", "8")
    assert bt_env["ok"] is True and bt_env["rtos"] is None
    assert _data(bt_env)["architecture"] == "riscv64"
    assert recorded_depths == [8]
    assert _error(api.api_call("bt", "worker1"))["code"] == "not_initialized"


def test_probe_reports_an_empty_target_as_a_capability_note(monkeypatch):
    """Nothing detected is still an answer, with the reason spelled out."""
    adapter_api.reset()
    monkeypatch.setattr(api, "collect_candidates", list)

    envelope = api.api_call("probe")

    assert envelope["ok"] is True
    assert _data(envelope)["candidates"] == []
    assert _data(envelope)["resolved"] is None
    notes = _strings(envelope["warnings"])
    assert any("no supported RTOS" in note for note in notes)
    # Reason: unit tests run outside GDB, where no symbol file can be
    # loaded -- exactly the session that deserves the `file` hint.
    assert any("`file <elf>`" in note for note in notes)


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


def test_dump_response_writes_the_envelope_and_reports_its_size(session, tmp_path):
    """The file transport's writer answers with the size the plugin checks.

    ``$gdr_dump`` is evaluated in a watch context, where only a scalar survives
    the adapter's value rendering, so the byte count is what proves the write
    happened; the file itself is read next.
    """
    session.table = ObjectTable(headers=["Name"], rows=[["worker1"]])
    target = tmp_path / "gdr-tasks.json"

    written = api.dump_response(str(target), "tasks")

    payload = target.read_text(encoding="utf-8")
    assert written == len(payload.encode("utf-8"))
    document = json.loads(payload)
    assert document["ok"] is True
    assert isinstance(document["schema_version"], int)


def test_dump_response_reports_an_unwritable_path(session, tmp_path):
    """An unwritable path is a value the caller reports, not an exception."""
    del session
    missing = tmp_path / "nope" / "core.json"

    assert api.dump_response(str(missing), "tasks") == -1


def test_envelope_keeps_optional_sections_absent_until_needed():
    """Warnings and errors stay out of a clean success envelope."""
    envelope = pack_envelope({"rows": []}, rtos="rtthread", generation=2)

    assert envelope["schema_version"] == GDR_API_SCHEMA_VERSION
    assert envelope["ok"] is True
    assert envelope["rtos"] == "rtthread"
    assert envelope["generation"] == 2
    assert "warnings" not in envelope
    assert "error" not in envelope


def test_envelope_carries_warnings_and_structured_errors():
    """A failed verb still reports its code instead of a traceback string."""
    envelope = pack_envelope(
        None,
        ok=False,
        error={"code": "not_initialized", "message": "init first"},
        warnings=["scan truncated"],
    )

    assert envelope["ok"] is False
    assert envelope["error"] == {"code": "not_initialized", "message": "init first"}
    assert envelope["warnings"] == ["scan truncated"]


def test_object_table_omits_typed_metadata_until_the_adapter_sets_it():
    """Text-only tables stay text-only in JSON."""
    rendered = object_table_to_dict(
        ObjectTable(headers=["Name"], rows=[["worker"]], elastic=("Name",))
    )

    assert rendered == {
        "headers": ["Name"],
        "rows": [["worker"]],
        "messages": [],
        "elastic": ["Name"],
    }


def test_object_table_serializes_column_specs_and_value_rows():
    """Plugin metadata rides beside the rendered text when present."""
    rendered = object_table_to_dict(
        ObjectTable(
            headers=["Addr"],
            rows=[["0x10"]],
            column_specs=(ColumnSpec("Addr", kind="addr", align="right"),),
            value_rows=[[16]],
        )
    )

    assert rendered["columns"] == [
        {
            "key": "Addr",
            "kind": "addr",
            "align": "right",
            "elastic": "full",
            "doc": "",
            "group": "basic",
        }
    ]
    assert rendered["value_rows"] == [[16]]


def test_object_detail_keeps_the_not_found_reason():
    """A missing object is data, not a missing envelope."""
    rendered = object_detail_to_dict(
        ObjectDetail(found=False, message="semaphore 'x': not found")
    )

    assert rendered == {
        "pairs": [],
        "found": False,
        "message": "semaphore 'x': not found",
    }


def _pairs(document: dict[str, object]) -> list[list[str]]:
    pairs = document["pairs"]
    assert isinstance(pairs, list)
    return pairs


def test_system_summary_pairs_match_the_text_renderer_order():
    """The JSON pairs and the vertical text block share one builder."""
    summary = SystemSummary(
        current_task="main",
        task_count=2,
        task_count_label="Threads",
        tick_count=9,
        scheduler_state="running",
        state_counts={"Ready": 1},
        object_counts={"timer": 1, "semaphore": 2},
        heap_allocator="small",
        heap_total=64,
        heap_status="ok",
        extra_pairs=[("Check", "ok")],
    )

    pairs = system_summary_pairs(summary)

    assert pairs[1] == ("Threads", "2")
    assert ("semaphore", "2") in pairs
    assert pairs.index(("semaphore", "2")) < pairs.index(("timer", "1"))
    assert ("Heap used", "N/A") in pairs
    assert pairs[-1] == ("Check", "ok")
    assert _pairs(system_summary_to_dict(summary))[0] == ["Current task", "main"]
    assert "Kernel version" not in [pair[0] for pair in pairs]


def test_heap_report_serializes_a_missing_table_as_null():
    """An allocator without a block table still has a stable shape."""
    rendered = heap_report_to_dict(
        HeapReport(pairs=[("Algorithm", "none")], messages=["no heap"])
    )

    assert rendered == {
        "pairs": [["Algorithm", "none"]],
        "messages": ["no heap"],
        "table": None,
        "table_title": "",
    }


def test_capabilities_dict_sorts_kinds():
    """Kind order is stable so envelope diffs do not churn."""
    rendered = capabilities_to_dict(
        Capabilities(
            kinds={
                "timer": KindCapability(enumerable=True, note="active only"),
                "queue": KindCapability(
                    enumerable=True, detail=True, columns=("Name",)
                ),
            }
        )
    )

    kinds = rendered["kinds"]
    assert isinstance(kinds, dict)
    assert list(kinds) == ["queue", "timer"]
    timer = kinds["timer"]
    assert isinstance(timer, dict)
    assert timer["note"] == "active only"


def test_chunk_payload_roundtrip_verifies_the_checksum():
    """Frames reassemble to the original payload, and a bad frame fails."""
    payload = "envelope-" * 40
    frames = chunk_payload(payload, chunk_size=GDR_API_CHUNK_SIZE)

    meta = json.loads(frames[0])
    assert meta["kind"] == "chunks"
    assert meta["total"] == len(frames) - 1
    assert all(len(frame) <= GDR_API_CHUNK_SIZE for frame in frames[1:])
    assert reassemble_chunks(frames[0], frames[1:]) == payload

    with pytest.raises(ValueError, match="checksum"):
        reassemble_chunks(frames[0], ["AAAA", *frames[2:]])


def test_reassemble_rejects_a_non_json_meta_frame():
    """A corrupted meta frame is a checksum failure, not a traceback."""
    with pytest.raises(ValueError, match="not JSON"):
        reassemble_chunks("not-json", ["AAAA"])


def test_transport_plan_switches_at_the_inline_limit():
    """Payloads at the limit stay inline; one extra byte chunks."""
    small = "x" * GDR_API_INLINE_LIMIT
    large = small + "y"

    assert transport_plan(small) == ("inline", small)
    kind, meta = transport_plan(large)
    assert kind == "chunked"
    assert json.loads(meta)["total"] > 1


def test_backtrace_verb_serializes_the_bounded_contract(session):
    """Frame index/PC/method, completeness and the stop reason cross the API."""
    session.backtrace_value = BacktraceReport(
        task="worker",
        address=0x2000,
        architecture="armv7-m",
        abi="aapcs32",
        method="cfi",
        source="saved",
        complete=False,
        stop_reason="max-depth",
        truncated=True,
        max_depth=2,
        bounds="recorded",
        frames=[
            BacktraceFrame(0, 0x1000, "seed", "foo+4"),
            BacktraceFrame(1, 0x1004, "cfi", "bar"),
        ],
        messages=["note"],
    )
    data = _data(api.api_call("backtrace", "worker", "--max-depth", "2"))
    assert data["task"] == "worker"
    assert (data["architecture"], data["abi"]) == ("armv7-m", "aapcs32")
    assert "port" not in data and "isa" not in data
    assert data["stop_reason"] == "max-depth"
    assert data["truncated"] is True
    frames = data["frames"]
    assert isinstance(frames, list)
    assert frames[0] == {
        "index": 0,
        "pc": 0x1000,
        "symbol": "foo+4",
        "source": None,
        "method": "seed",
    }
    assert backtrace_to_dict(session.backtrace_value)["max_depth"] == 2


def test_backtrace_verb_accepts_omitted_target_for_current_pc(session):
    recorded: list[tuple[str | None, int]] = []
    report = BacktraceReport(
        task="<current>",
        address=0,
        architecture="armv7-m",
        abi="aapcs32",
        method="cfi",
        source="current",
        complete=True,
        stop_reason="task-exit",
        truncated=False,
        max_depth=4,
        bounds="recorded",
        frames=[BacktraceFrame(0, 0x1000, "seed", "main+4")],
    )

    def _bt(target, max_depth=32):
        recorded.append((target, max_depth))
        return report

    session.backtrace = _bt
    data = _data(api.api_call("backtrace"))
    assert data["task"] == "<current>"
    data_limited = _data(api.api_call("backtrace", "--max-depth", "4"))
    assert data_limited["max_depth"] == 4
    assert recorded == [(None, 32), (None, 4)]

    err_env = api.api_call("backtrace", "")
    assert err_env["ok"] is False and _error(err_env)["code"] == "empty_name"

    bare_env = api.live_backtrace_envelope()
    assert bare_env["ok"] is False
    assert _error(bare_env)["code"] == "bare_metal_only"
    assert "bare-metal" in str(_error(bare_env)["message"])
