"""Unit tests for the RTOS-neutral derived-value helpers (gdr/derive.py).

The three helpers are shared by the FreeRTOS and RT-Thread adapters; each
test pins the exact output the corresponding adapter relies on, so switching
an adapter to the core implementation cannot drift its rendered cells.
"""

from gdr.derive import fill_watermark, timer_expires_in, waiter_cell


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
