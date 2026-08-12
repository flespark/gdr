"""Unit tests for the width-aware table formatting core."""

from __future__ import annotations

from gdr.table import DEFAULT_WIDTH, format_table


def test_empty_table_renders_as_empty():
    """An empty row set keeps the stable ``(empty)`` placeholder."""
    assert format_table([], ["Name"]) == "(empty)\n"


def test_natural_table_within_width_is_unchanged():
    """A table that fits the target width is rendered untouched."""
    rows = [["worker", "20"], ["idle", "3"]]
    headers = ["Name", "Prio"]
    assert format_table(rows, headers, width=80) == (
        "Name    Prio\n------  ----\nworker  20  \nidle    3   \n"
    )


def test_no_elastic_columns_never_truncate():
    """Without elastic metadata the table keeps its natural widths."""
    rows = [["very-long-name", "123456"]]
    headers = ["Name", "Value"]
    out = format_table(rows, headers, width=10)
    assert "very-long-name" in out
    assert "123456" in out
    assert ".." not in out


def test_single_elastic_column_truncates_with_two_dots():
    """An elastic column shrinks and cells truncate with ``..``."""
    out = format_table(
        [["2:worker,logger", "3"]],
        ["Waiters", "Value"],
        elastic=("Waiters",),
        width=14,
    )
    assert "2:wor.." in out
    assert "3" in out


def test_waiters_count_is_preserved_before_truncation():
    """The leading count of a Waiters cell survives right truncation."""
    out = format_table(
        [["2:worker,logger,idle,main", "3"]],
        ["Waiters", "Value"],
        elastic=("Waiters",),
        width=14,
    )
    waiters_cell = next(
        line for line in out.splitlines() if line.lstrip().startswith("2:")
    ).split()[0]
    assert waiters_cell.startswith("2:")
    assert waiters_cell.endswith("..")


def test_multi_column_priority_truncation_shrinks_waiters_before_name():
    """Waiters shrinks first; a lower-priority Name column keeps its width."""
    rows = [["2:worker,logger", "long-thread-name"]]
    headers = ["Waiters", "Name"]
    out = format_table(rows, headers, elastic=("Waiters", "Name"), width=25)

    # Waiters was already shrunk to its minimum; Name still fits naturally.
    assert "long-thread-name" in out
    assert "2:wor.." in out


def test_same_priority_shrinks_widest_column_first():
    """Callback and Entry share a rank; the wider one is cut first."""
    rows = [["aaaaaaaa", "bbbbbbbbbb"]]
    headers = ["Callback", "Entry"]
    out = format_table(rows, headers, elastic=("Callback", "Entry"), width=18)
    # Callback cannot shrink below its 8-char header; Entry (rank 1) shrinks.
    assert "aaaaaaaa" in out
    assert "bbbbbb.." in out


def test_elastic_header_is_never_truncated():
    """Elastic columns keep at least the header width (min width rule)."""
    rows = [["worker1", "2:aaa"]]
    headers = ["Waiters", "Name"]
    out = format_table(rows, headers, elastic=("Waiters",), width=14)
    assert "Waiters" in out


def test_elastic_minimum_width_never_below_five():
    """Elastic columns floor at 5 columns even for short headers."""
    rows = [["abcdefghij", "x"]]
    headers = ["Name", "Value"]
    out = format_table(rows, headers, elastic=("Name",), width=9)
    # Name min width = max(len("Name")=4, 5) = 5 -> "ab..x"? 3 chars + "..".
    assert "ab" in out


def test_min_still_too_wide_restores_natural_table():
    """When elastic minimums cannot fit, the natural table is restored."""
    rows = [["2:worker,logger,idle", "1234567890"]]
    headers = ["Waiters", "Value"]
    out = format_table(rows, headers, elastic=("Waiters",), width=10)
    assert "2:worker,logger,idle" in out
    assert "1234567890" in out
    assert ".." not in out


def test_non_elastic_numeric_columns_never_truncate():
    """Numbers and addresses stay untruncated even when width is tight."""
    rows = [["worker1", "0x12345678", "20"]]
    headers = ["Name", "Addr", "Prio"]
    out = format_table(rows, headers, elastic=("Name",), width=20)
    assert "0x12345678" in out
    assert "20" in out


def test_default_width_is_120():
    """The formatting default matches the 120-column fallback."""
    assert DEFAULT_WIDTH == 120


def test_elastic_columns_missing_from_headers_are_ignored():
    """Elastic names that are not headers never affect output."""
    rows = [["worker1", "20"]]
    headers = ["Name", "Prio"]
    out = format_table(rows, headers, elastic=("Waiters", "Name"), width=30)
    assert "worker1" in out
    assert "20" in out
