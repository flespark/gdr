"""Quiet automatic checks, with concrete failures and correct scheduler gates."""

from freertos import diagnostics, heap
from gdr.adapter_api import merged_detail_table
from tests.unit.freertos.test_diagnostics import _list_mem, _patch_system


def test_next_unblock_is_not_checked_before_scheduler_start(monkeypatch):
    layout = _patch_system(
        monkeypatch, {"xSchedulerRunning": 0, "xNextTaskUnblockTime": 0}, 0, {}, None
    )
    assert diagnostics._next_unblock_check(layout) == (
        "NextUnblockTime",
        "skipped: scheduler not started",
    )


def test_earlier_next_unblock_cache_is_legitimate(monkeypatch):
    mem = _list_mem(0x3000, 1, [0x3200], item_values=[10])
    layout = _patch_system(
        monkeypatch,
        {
            "uxSchedulerSuspended": 0,
            "xNextTaskUnblockTime": 5,
            "pxDelayedTaskList": object(),
        },
        1,
        mem,
        0x3000,
    )
    assert diagnostics._next_unblock_check(layout) == ("NextUnblockTime", "ok")


def test_late_next_unblock_locates_the_delayed_item(monkeypatch):
    mem = _list_mem(0x3000, 1, [0x3200], item_values=[10])
    layout = _patch_system(
        monkeypatch,
        {
            "uxSchedulerSuspended": 0,
            "xNextTaskUnblockTime": 42,
            "pxDelayedTaskList": object(),
        },
        1,
        mem,
        0x3000,
    )
    result = diagnostics._next_unblock_check(layout)[1]
    assert "42 ticks" in result and "10 ticks" in result and "0x3200" in result
    assert "miss this deadline" in result


def test_problem_columns_keep_diagnostic_group():
    table = merged_detail_table(
        "queue", [[("Name", "q"), ("Check[WritePtr]", "fail: pcWriteTo=0x2000")]]
    )
    assert table.column_specs[-1].group == "diag"


def test_heap_address_mismatch_names_missing_blocks():
    geom = heap.HeapGeometry(kind=4, algorithm="heap_4", allocated_bitmask=0x80000000)
    free_list = heap.HeapWalk(
        blocks=[heap.HeapBlock(0x2000, 16, False)], free_bytes=16, free_blocks=1
    )
    linear = heap.HeapWalk(
        blocks=[heap.HeapBlock(0x2100, 16, False)], free_bytes=16, free_blocks=1
    )
    result = heap.cross_validate(free_list, linear, 16, geom)
    assert "missing from free-list=0x2100" in result
    assert "unexpected on free-list=0x2000" in result


def test_heap_corrupt_reason_survives_cross_validation():
    geom = heap.HeapGeometry(kind=4, algorithm="heap_4")
    free_list = heap.HeapWalk(corrupt=True, corrupt_reason="free-list cycle at 0x2000")
    result = heap.cross_validate(free_list, heap.HeapWalk(), 0, geom)
    assert "free-list cycle at 0x2000" in result
