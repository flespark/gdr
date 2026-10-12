"""Unit tests for the pure API envelope and chunk protocol."""

from __future__ import annotations

import json

import pytest

from gdr.adapter_api import (
    Capabilities,
    ColumnSpec,
    HeapReport,
    KindCapability,
    ObjectDetail,
    ObjectTable,
    SystemSummary,
)
from gdr.constants import (
    GDR_API_CHUNK_SIZE,
    GDR_API_INLINE_LIMIT,
    GDR_API_SCHEMA_VERSION,
)
from gdr.serialize import (
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
            "elastic": False,
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
