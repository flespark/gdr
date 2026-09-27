"""Unit tests for the typed-cell and capability contract.

These cover the neutral core pieces every adapter uses: cell construction,
table assembly that keeps text and machine values aligned, verdict
classification, and serialization of the new metadata.  Adapter-specific
column sets are asserted next to their adapters.
"""

from __future__ import annotations

import pytest

from gdr.adapter_api import (
    CheckFinding,
    ColumnSpec,
    ObjectDetail,
    ObjectTable,
    address_cell,
    check_findings,
    count_cell,
    number_cell,
    raw_cell,
    sections_from_pairs,
    symbol_cell,
    text_cell,
    verdict_severity,
    waiters_cell,
)
from gdr.serialize import (
    checks_to_dict,
    object_detail_to_dict,
    object_table_to_dict,
)


def _dict_list(value: object) -> list[dict[str, object]]:
    """Narrow a serialized list of objects for typed assertions."""
    assert isinstance(value, list)
    result: list[dict[str, object]] = []
    for item in value:
        assert isinstance(item, dict)
        result.append(item)
    return result


def test_cell_helpers_pair_text_with_their_machine_value():
    """Each column kind carries the value a plugin needs without re-parsing."""
    assert text_cell("worker").value is None
    assert number_cell(7).value == 7
    assert number_cell(None).text == "N/A"
    assert address_cell(0x2000).text == "0x2000"
    assert symbol_cell(0x1000, "worker_entry").text == "<worker_entry>"
    assert symbol_cell(0x1000, "worker_entry").value == 0x1000
    assert waiters_cell("2@a,b", ["a", "b"]).value == ["a", "b"]
    assert waiters_cell("0", []).value == []
    assert waiters_cell("N/A").value is None
    assert raw_cell("auto", 0x8).value == 0x8


def test_count_cell_keeps_states_without_a_tick_count_valueless():
    """``overdue`` and ``N/A`` are states, not numbers to fabricate."""
    assert count_cell("42").value == 42
    assert count_cell("overdue").value is None
    assert count_cell(None).text == "N/A"
    assert count_cell(None).value is None


def test_from_cells_splits_text_and_values_and_omits_untyped_rows():
    """Text and value come from one path, so they cannot drift apart."""
    table = ObjectTable.from_cells(
        ["Name", "Count"],
        [[text_cell("worker"), number_cell(3)]],
    )

    assert table.rows == [["worker", "3"]]
    assert table.value_rows == [[None, 3]]

    text_only = ObjectTable.from_cells(["Name"], [[text_cell("worker")]])
    assert text_only.value_rows is None


def test_from_specs_derives_headers_and_elastic_priority():
    """The spec list is the single source for headers and shrink flags."""
    specs = (
        ColumnSpec("Name", elastic=True),
        ColumnSpec("Count", kind="int", align="right"),
        ColumnSpec("Addr", kind="addr", elastic=True),
    )
    table = ObjectTable.from_specs(
        specs,
        [[text_cell("w"), number_cell(1), address_cell(0x10)]],
        elastic=("Addr", "Name"),
        id_key="Addr",
    )

    assert table.headers == ["Name", "Count", "Addr"]
    assert table.elastic == ("Addr", "Name")
    assert table.value_rows == [[None, 1, 0x10]]


def test_table_assembly_rejects_drift_between_text_and_values():
    """A row appended to only one of the two views is a defect, not data."""
    with pytest.raises(RuntimeError, match="column_specs"):
        ObjectTable.from_cells(
            ["Name"],
            [[text_cell("w")]],
            column_specs=(ColumnSpec("Name"), ColumnSpec("Extra")),
        )

    with pytest.raises(RuntimeError, match="elastic columns"):
        ObjectTable.from_specs(
            (ColumnSpec("Name"),),
            [[text_cell("w")]],
            elastic=("Name",),
        )

    with pytest.raises(RuntimeError, match="cells for"):
        ObjectTable.from_cells(["Name", "Count"], [[text_cell("w")]])

    with pytest.raises(RuntimeError, match="must mirror rows"):
        ObjectTable(headers=["Name"], rows=[["w"]], value_rows=[])


def test_verdict_severity_classifies_both_documented_conventions():
    """Verdict-prefixed values and named verdicts both classify."""
    assert verdict_severity("TaskCount", "ok") == "ok"
    assert verdict_severity("ListIntegrity", "fail: chain broken") == "fail"
    assert verdict_severity("HeapCrossCheck", "skipped: unreadable") == "skipped"
    assert verdict_severity("OffsetCheck", "ok") == "ok"
    assert verdict_severity("SlotCheck", "N/A") == "skipped"
    assert verdict_severity("AlignmentCheck", "size 0 must be positive") == "fail"
    assert verdict_severity("Checks", "ok") == "ok"
    assert verdict_severity("Checks", "2 failed, 3 verified") == "fail"
    assert verdict_severity("Name", "worker1") == "info"


def test_sections_from_pairs_groups_only_verdict_rows():
    """A detail section highlights the checks, never the plain facts."""
    sections = sections_from_pairs(
        [("Name", "worker1"), ("OffsetCheck", "ok"), ("SlotCheck", "bad")]
    )

    assert len(sections) == 1
    assert sections[0].title == "Checks"
    assert [(row.key, row.severity) for row in sections[0].rows] == [
        ("OffsetCheck", "ok"),
        ("SlotCheck", "fail"),
    ]
    assert sections_from_pairs([("Name", "worker1")]) == []


def test_check_findings_parses_verdicts_into_structured_rows():
    """The checks verb payload needs no string splitting on the client."""
    findings = check_findings(
        [("TaskCount", "ok"), ("Heap", "fail: blocks disagree")], scope="system"
    )

    assert [finding.verdict for finding in findings] == ["ok", "fail"]
    assert findings[0].detail == ""
    assert findings[1].detail == "blocks disagree"
    assert findings[1].scope == "system"


def test_serialized_table_carries_columns_values_kind_and_ids():
    """The envelope's table shape is what a UI builds a grid from."""
    table = ObjectTable.from_specs(
        (ColumnSpec("Name"), ColumnSpec("Addr", kind="addr")),
        [[text_cell("worker"), address_cell(0x2000)]],
        id_key="Addr",
    )

    rendered = object_table_to_dict(table, kind="task")

    assert rendered["kind"] == "task"
    assert rendered["value_rows"] == [[None, 0x2000]]
    assert rendered["ids"] == [0x2000]
    assert _dict_list(rendered["columns"])[1]["kind"] == "addr"


def test_serialized_detail_keeps_flat_pairs_and_adds_severity():
    """pairs stays the text contract; sections adds the grouped verdicts."""
    pairs = [("Name", "worker1"), ("OffsetCheck", "ok")]
    detail = ObjectDetail(pairs=pairs, sections=sections_from_pairs(pairs))

    rendered = object_detail_to_dict(detail)

    assert rendered["pairs"] == [["Name", "worker1"], ["OffsetCheck", "ok"]]
    assert rendered["sections"] == [
        {
            "title": "Checks",
            "rows": [{"key": "OffsetCheck", "value": "ok", "severity": "ok"}],
        }
    ]


def test_serialized_checks_report_findings_and_counts():
    """Counts let a badge render without walking the findings."""
    rendered = checks_to_dict(
        [
            CheckFinding("TaskCount", "ok"),
            CheckFinding("Heap", "fail", "disagree"),
            CheckFinding("TimerList", "fail", "broken"),
        ]
    )

    assert rendered["counts"] == {"fail": 2, "ok": 1}
    assert _dict_list(rendered["findings"])[1]["detail"] == "disagree"
