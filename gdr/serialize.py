"""GDR API envelope serialization (pure module, no ``gdb`` import).

The envelope is the stable machine-readable contract between GDR and
plugin-style clients. It carries a schema version independent of the GDR
release version, so a client can reject an incompatible GDR without
parsing release strings.

The chunk protocol lets the ``$gdr_json_chunk`` transport push arbitrary
payload sizes through DAP ``evaluate`` results, which are otherwise
bounded by GDB's print-element limit and MI varobj truncation rules.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence

from gdr.adapter_api import (
    Capabilities,
    CheckFinding,
    HeapReport,
    ObjectDetail,
    ObjectTable,
    SystemSummary,
)
from gdr.constants import (
    GDR_API_CHUNK_SIZE,
    GDR_API_INLINE_LIMIT,
    GDR_API_SCHEMA_VERSION,
)
from gdr.formatting import format_optional_int

# Transport thresholds live in gdr.constants; this module only consumes them.


def pack_envelope(
    data: object,
    *,
    ok: bool = True,
    error: dict[str, object] | None = None,
    rtos: str | None = None,
    rtos_version: str | None = None,
    gdr_version: str | None = None,
    generation: int = 0,
    warnings: Sequence[str] = (),
    elapsed_ms: float | None = None,
) -> dict[str, object]:
    """Build one API response envelope.

    Args:
        data: Verb-specific payload (usually a ``*_to_dict`` render).
        error: Structured failure ``{"code", "message"}``; ``ok`` is then
            ``False``. Plugins never scrape GDB stderr, so every failure
            mode the text renderers warn about has a code here.
        rtos: Session RTOS identity registered at init time.
        rtos_version: Session version identity registered at init time.
        gdr_version: GDR release version that produced the envelope.
        generation: Session-generation counter; a client drops responses
            that raced a newer stop or objfile event (see ``gdr.api``).
        warnings: Capability notes the text renderers used to print as
            ``info`` lines; plugins surface them the same way.
        elapsed_ms: Wall time of the verb, so a client can self-throttle.
    """
    envelope: dict[str, object] = {
        "schema_version": GDR_API_SCHEMA_VERSION,
        "ok": ok,
        "data": data,
        "rtos": rtos,
        "rtos_version": rtos_version,
        "gdr_version": gdr_version,
        "generation": generation,
        "elapsed_ms": elapsed_ms,
    }
    if warnings:
        envelope["warnings"] = list(warnings)
    if error is not None:
        envelope["error"] = error
    return envelope


def object_table_to_dict(
    table: ObjectTable, *, kind: str | None = None
) -> dict[str, object]:
    """Serialize a list table; typed metadata only when the adapter set it.

    ``ids`` are the rows' stable identities (the ``id_key`` column's machine
    value) and ``kind`` names the object family they belong to, so a client
    can keep a selection or expansion state across refreshes.  Both are
    omitted when the adapter declares no identity column.
    """
    result: dict[str, object] = {
        "headers": list(table.headers),
        "rows": [list(row) for row in table.rows],
        "messages": list(table.messages),
        "elastic": list(table.elastic),
    }
    if kind is not None:
        result["kind"] = kind
    if table.column_specs:
        result["columns"] = [
            {
                "key": spec.key,
                "kind": spec.kind,
                "align": spec.align,
                "elastic": spec.elastic,
                "doc": spec.doc,
            }
            for spec in table.column_specs
        ]
    if table.value_rows is not None:
        result["value_rows"] = table.value_rows
        if table.id_key in table.headers:
            index = table.headers.index(table.id_key)
            result["ids"] = [row[index] for row in table.value_rows]
    return result


def object_detail_to_dict(detail: ObjectDetail) -> dict[str, object]:
    """Serialize one vertical detail block.

    ``pairs`` stays the complete flat view the text renderer prints;
    ``sections`` adds the grouped verdict rows with per-row severity.
    """
    result: dict[str, object] = {
        "pairs": [[key, value] for key, value in detail.pairs],
        "found": detail.found,
        "message": detail.message,
    }
    if detail.sections:
        result["sections"] = [
            {
                "title": section.title,
                "rows": [
                    {"key": row.key, "value": row.value, "severity": row.severity}
                    for row in section.rows
                ],
            }
            for section in detail.sections
        ]
    return result


def system_summary_pairs(summary: SystemSummary) -> list[tuple[str, str]]:
    """The normalized vertical key/value pairs of a system summary.

    Lifted out of ``gdr.commands.render_system`` so the text renderer and
    the JSON serializer share one ordering/formatting source of truth.
    """
    pairs: list[tuple[str, str]] = [
        ("Kernel version", summary.kernel_version),
        ("Current task", summary.current_task or "N/A"),
        (
            summary.task_count_label,
            str(summary.task_count) if summary.task_count is not None else "N/A",
        ),
        (
            "Tick count",
            str(summary.tick_count) if summary.tick_count is not None else "N/A",
        ),
        ("Scheduler state", summary.scheduler_state),
    ]
    pairs.extend((state, str(count)) for state, count in summary.state_counts.items())
    pairs.extend(
        (kind, str(count)) for kind, count in sorted(summary.object_counts.items())
    )
    pairs.append(("Heap allocator", summary.heap_allocator or "N/A"))
    if summary.heap_used is not None or summary.heap_total is not None:
        pairs.append(("Heap used", format_optional_int(summary.heap_used)))
        pairs.append(("Heap total", format_optional_int(summary.heap_total)))
    if summary.heap_status is not None:
        pairs.append(("Heap status", summary.heap_status))
    pairs.extend(summary.extra_pairs)
    return pairs


def system_summary_to_dict(summary: SystemSummary) -> dict[str, object]:
    """Serialize a system summary as the same pairs the text renderer shows."""
    return {"pairs": [[key, value] for key, value in system_summary_pairs(summary)]}


def heap_report_to_dict(report: HeapReport) -> dict[str, object]:
    """Serialize one heap snapshot."""
    table = None
    if report.table is not None:
        table = object_table_to_dict(report.table)
    return {
        "pairs": [[key, value] for key, value in report.pairs],
        "messages": list(report.messages),
        "table": table,
    }


def capabilities_to_dict(capabilities: Capabilities) -> dict[str, object]:
    """Serialize adapter capability claims (structured missing-column reasons)."""
    return {
        "kinds": {
            kind: {
                "enumerable": claim.enumerable,
                "detail": claim.detail,
                "columns": list(claim.columns),
                "note": claim.note,
            }
            for kind, claim in sorted(capabilities.kinds.items())
        },
        "extra": capabilities.extra,
    }


def checks_to_dict(findings: Sequence[CheckFinding]) -> dict[str, object]:
    """Serialize system check findings plus their per-severity counts."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.verdict] = counts.get(finding.verdict, 0) + 1
    return {
        "findings": [
            {
                "name": finding.name,
                "verdict": finding.verdict,
                "detail": finding.detail,
                "scope": finding.scope,
            }
            for finding in findings
        ],
        "counts": dict(sorted(counts.items())),
    }


# ---------------------------------------------------------------------------
# Chunked transport: DAP evaluate results are strings bounded by GDB's
# print/varobj limits, so oversized envelopes travel as base64 frames.
# Base64 keeps every frame printable ASCII with no quotes or control bytes,
# which no evaluate-result escaping can mangle.
# ---------------------------------------------------------------------------


def chunk_payload(payload: str, chunk_size: int = GDR_API_CHUNK_SIZE) -> list[str]:
    """Split *payload* into chunked-transport frames.

    Frame 0 is a meta JSON frame (schema, total, chunk size, sha256);
    frames 1..total are base64 slices of the UTF-8 payload.
    """
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    slices = [encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)]
    meta = json.dumps(
        {
            "kind": "chunks",
            "schema_version": GDR_API_SCHEMA_VERSION,
            "total": len(slices),
            "chunk_size": chunk_size,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }
    )
    return [meta, *slices]


def reassemble_chunks(meta_frame: str, slices: Sequence[str]) -> str:
    """Inverse of :func:`chunk_payload`; verifies the sha256 frame.

    Raises:
        ValueError: The meta frame is not JSON, or the checksum disagrees.
    """
    try:
        meta = json.loads(meta_frame)
    except json.JSONDecodeError as exc:
        raise ValueError(f"chunk meta frame is not JSON: {exc}") from exc
    payload = base64.b64decode("".join(slices)).decode("utf-8")
    expected = meta["sha256"]
    actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if actual != expected:
        raise ValueError(f"chunk reassembly checksum mismatch: {actual} != {expected}")
    return payload


def transport_plan(
    payload: str, inline_limit: int = GDR_API_INLINE_LIMIT
) -> tuple[str, str]:
    """Decide how a payload should cross the DAP evaluate channel.

    Returns ``("inline", payload)`` when it fits in one evaluate result,
    else ``("chunked", meta_frame)``; the caller then drives
    ``$gdr_json_chunk`` for frames 1..total.
    """
    if len(payload) <= inline_limit:
        return "inline", payload
    return "chunked", chunk_payload(payload)[0]
