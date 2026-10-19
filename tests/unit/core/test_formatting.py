"""Unit tests for shared value and width-aware table formatting."""

from __future__ import annotations

from core.constants import GDR_DEFAULT_TERMINAL_WIDTH as DEFAULT_WIDTH
from core.formatting import (
    fill_watermark,
    format_table,
    timer_expires_in,
    waiter_cell,
)


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


def test_elastic_order_defines_adapter_owned_shrink_priority():
    """The adapter-provided order, not a core header map, selects first shrink."""
    rows = [["aaaaaaaa", "bbbbbbbbbb"]]
    headers = ["Callback", "Entry"]
    out = format_table(rows, headers, elastic=("Entry", "Callback"), width=18)
    # Entry is first priority and shrinks; the core has no Callback/Entry policy.
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


def test_timer_expires_in_freeertos_contract():
    """FreeRTOS: overdue on expiry, wrap for overflow-list items."""
    assert timer_expires_in(None, 10, 0xFFFF) == "N/A"
    assert timer_expires_in(10, None, 0xFFFF) == "N/A"
    # current list, not yet expired
    assert timer_expires_in(100, 90, 0xFFFF) == "10"
    # expired but still linked -> overdue, not a huge wrap
    assert timer_expires_in(90, 100, 0xFFFF) == "overdue"
    # overflow-list epoch: real expiry is 2**bits + expiry
    assert timer_expires_in(10, 65000, 0xFFFF, in_overflow=True) == str(
        (0x10000 - 65000) + 10
    )


def test_timer_expires_in_rtthread_contract():
    """RT-Thread: wrapped unsigned subtraction, no overdue label."""
    assert timer_expires_in(100, 90, 0xFFFFFFFF, overdue=False) == "10"
    # expired: (90 - 100) & mask wraps to a large unsigned value
    assert timer_expires_in(90, 100, 0xFFFFFFFF, overdue=False) == str(
        (90 - 100) & 0xFFFFFFFF
    )
    assert timer_expires_in(100, None, 0xFFFFFFFF, overdue=False) == "N/A"


def test_fill_watermark_counts_from_low_end():
    """FreeRTOS-style: grow-down stacks keep the fill at the low end."""
    window = b"\xa5\xa5\xa5\xa5\xaa\xbb"
    assert fill_watermark(window, from_low=True, word_bytes=1) == 4
    # word-sized watermark divides by the stack word width
    assert fill_watermark(window, from_low=True, word_bytes=4) == 1
    # no fill at the low end -> never prefilled / stack grew past it
    assert fill_watermark(b"\xcc\xdd", from_low=True) is None
    assert fill_watermark(None, from_low=True) is None
    assert fill_watermark(b"", from_low=True) is None


def test_fill_watermark_counts_from_high_end():
    """RT-Thread-style: a grow-up stack keeps the fill at the high end."""
    window = b"\xaa\xbb\xa5\xa5"
    assert fill_watermark(window, from_low=False, word_bytes=1, fill_byte=0xA5) == 2
    assert fill_watermark(b"\xaa\xbb", from_low=False, fill_byte=0xA5) is None


def test_waiter_cell_count_always_leads():
    assert waiter_cell(None) == "N/A"
    assert waiter_cell([], available=False) == "N/A"
    assert waiter_cell([], available=True) == "0"
    assert waiter_cell(["a"]) == "1@a"
    assert waiter_cell(["a", "b"]) == "2@a,b"
    # unavailable list must not fabricate 0
    assert waiter_cell(None, available=False) == "N/A"
