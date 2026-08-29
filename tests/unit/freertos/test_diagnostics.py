"""Unit tests for the bounded raw list walks and consistency checks.

The checks are driven through module-level helpers (``read_bytes``,
``member_offset``, ``_mapped_ranges``, ``lookup_symbol``, ...) so every GDB
entry point stays monkeypatchable outside a GDB session, following the
pattern of ``test_heap.py``.
"""

from __future__ import annotations

import struct
import types

import freertos.diagnostics as diagnostics
from freertos.layout import FreeRtosConfig, build_layout

# 32-bit little-endian standard layout (mini list items, no integrity bytes):
#   List_t:        uxNumberOfItems@0, pxIndex@4, xListEnd@8
#   MiniListItem:  xItemValue@0, pxNext@4, pxPrevious@8   (xListEnd member)
#   ListItem_t:    xItemValue@0, pxNext@4, pxPrevious@8, pvOwner@12
_END_OFFSET = 8
_END_NEXT_OFFSET = 4
_ITEM_OWNER_OFFSET = 12
_ITEM_NEXT_OFFSET = 4
_ITEM_VALUE_OFFSET = 0


def _offsets(type_name: str, path: tuple[str | int, ...]) -> int | None:
    first = path[0]
    if not isinstance(first, str):
        return None
    mapping = {
        ("struct xLIST", "uxNumberOfItems"): 0,
        ("struct xLIST", "pxIndex"): 4,
        ("struct xLIST", "xListEnd"): _END_OFFSET,
        ("struct xMINI_LIST_ITEM", "xItemValue"): 0,
        ("struct xMINI_LIST_ITEM", "pxNext"): _END_NEXT_OFFSET,
        ("struct xLIST_ITEM", "xItemValue"): _ITEM_VALUE_OFFSET,
        ("struct xLIST_ITEM", "pxNext"): _ITEM_NEXT_OFFSET,
        ("struct xLIST_ITEM", "pvOwner"): _ITEM_OWNER_OFFSET,
    }
    return mapping.get((type_name, first))


def _layout(monkeypatch, **config):
    monkeypatch.setattr(
        diagnostics,
        "get_arch_info",
        lambda: types.SimpleNamespace(ptrsize=4, endian="little"),
    )
    monkeypatch.setattr(diagnostics, "_mapped_ranges", lambda: ())
    monkeypatch.setattr(diagnostics, "member_offset", _offsets)
    return build_layout(FreeRtosConfig(**config), (11, 1, 0))


def _patch_mem(monkeypatch, mem: dict[int, bytes]):
    spans = [(addr, addr + len(data), data) for addr, data in sorted(mem.items())]

    def fake_read_bytes(addr: int, size: int) -> bytes | None:
        for low, high, data in spans:
            if low <= addr and addr + size <= high:
                return data[addr - low : addr - low + size]
        return None

    monkeypatch.setattr(diagnostics, "read_bytes", fake_read_bytes)


def _list_mem(
    head: int,
    count: int,
    items: list[int],
    *,
    end_value: int = 0xFFFFFFFF,
    index: int | None = None,
    item_values: list[int] | None = None,
    item_nexts: list[int] | None = None,
    item_prevs: list[int] | None = None,
    item_owners: list[int] | None = None,
) -> dict[int, bytes]:
    """Memory image of a List_t + item chain on 32-bit little-endian."""
    end = head + _END_OFFSET
    index = end if index is None else index
    first = items[0] if items else end
    mem = {
        head: struct.pack("<II", count, index),
        end: struct.pack("<III", end_value, first, end),
    }
    values = item_values or [0] * len(items)
    if items:
        nexts = item_nexts if item_nexts is not None else items[1:] + [end]
        prevs = item_prevs if item_prevs is not None else [end] + items[:-1]
    else:
        nexts = item_nexts or []
        prevs = item_prevs or []
    owners = item_owners or [0] * len(items)
    for item, value, nxt, prv, owner in zip(
        items, values, nexts, prevs, owners, strict=True
    ):
        mem[item] = struct.pack("<IIII", value, nxt, prv, owner)
    return mem


# ---------------------------------------------------------------------------
# walk_list_raw
# ---------------------------------------------------------------------------


def test_walk_list_raw_reports_cycle_not_silent_truncation(monkeypatch):
    """A cyclic chain is corrupt with the node address, never a clean stop."""
    head = 0x20000000
    item_a, item_b = 0x20000020, 0x20000030
    _patch_mem(
        monkeypatch,
        _list_mem(
            head,
            count=2,
            items=[item_a, item_b],
            item_nexts=[item_b, item_a],  # a -> b -> a closes the cycle
            item_owners=[0x1000, 0x2000],
        ),
    )
    layout = _layout(monkeypatch)

    walk = diagnostics.walk_list_raw(head, layout)

    assert walk.corrupt is True
    assert walk.truncated is False
    assert walk.corrupt_reason is not None
    assert "cycle" in walk.corrupt_reason
    assert f"{item_a:#x}" in walk.corrupt_reason
    assert walk.items == [item_a, item_b]


def test_walk_list_raw_truncates_at_traversal_bound(monkeypatch):
    """Hitting the bound is truncated, never a corrupt or clean short list."""
    head = 0x20000000
    items = [0x20000020 + i * 16 for i in range(6)]
    _patch_mem(
        monkeypatch,
        _list_mem(
            head,
            count=6,
            items=items,
            item_nexts=items[1:] + [head + _END_OFFSET],
        ),
    )
    layout = _layout(monkeypatch)

    walk = diagnostics.walk_list_raw(head, layout, max_count=5)

    assert walk.truncated is True
    assert walk.corrupt is False
    assert len(walk.items) == 5


def test_walk_list_raw_out_of_range_node_is_corrupt(monkeypatch):
    """A next pointer outside every loadable section stops the walk."""
    head = 0x20000000
    bogus = 0x10000000  # outside every mapped range
    _patch_mem(monkeypatch, _list_mem(head, count=1, items=[bogus]))
    layout = _layout(monkeypatch)
    monkeypatch.setattr(
        diagnostics, "_mapped_ranges", lambda: ((0x20000000, 0x20010000),)
    )

    walk = diagnostics.walk_list_raw(head, layout)

    assert walk.corrupt is True
    assert "outside every loadable section" in (walk.corrupt_reason or "")
    assert walk.items == []


def test_walk_list_raw_unreadable_head_is_corrupt(monkeypatch):
    """A list whose xListEnd.pxNext cannot be read is corrupt, not empty."""
    _patch_mem(monkeypatch, {})
    layout = _layout(monkeypatch)

    walk = diagnostics.walk_list_raw(0x20000000, layout)

    assert walk.corrupt is True
    assert "unreadable list head" in (walk.corrupt_reason or "")


def test_walk_list_raw_null_next_is_corrupt_not_a_clean_end(monkeypatch):
    """A NULL pxNext cuts the chain: corrupt, never a clean short list.

    A healthy chain only ever terminates at xListEnd (vListInsert links
    back toward the sentinel), so a zero pxNext is a cut chain.  Before the
    null guard the loop exited on the falsy node and the walk reported the
    partial chain as clean -- letting ListCount fabricate an "ok" verdict
    when uxNumberOfItems happened to match the walked length.
    """
    head = 0x20000000
    item_a = 0x20000020
    _patch_mem(
        monkeypatch,
        _list_mem(
            head,
            count=1,
            items=[item_a],
            item_nexts=[0],  # chain cut: pxNext == NULL instead of xListEnd
        ),
    )
    layout = _layout(monkeypatch)

    walk = diagnostics.walk_list_raw(head, layout)

    assert walk.corrupt is True
    assert walk.truncated is False
    assert (walk.corrupt_reason or "").startswith("null pxNext")
    assert f"{item_a:#x}" in (walk.corrupt_reason or "")
    # The cut must surface as a failure, not a clean comparison.
    results = dict(diagnostics.list_checks(head, layout))
    assert results["ListIntegrity"].startswith("fail:")
    assert results["ListCount"].startswith("fail:")


def test_walk_list_raw_null_head_next_is_corrupt(monkeypatch):
    """A NULL xListEnd.pxNext is a cut head, not an empty list."""
    head = 0x20000000
    mem = _list_mem(head, count=0, items=[])
    end = head + _END_OFFSET
    # Overwrite xListEnd.pxNext with a readable zero.
    mem[end] = struct.pack("<III", 0xFFFFFFFF, 0, end)
    _patch_mem(monkeypatch, mem)
    layout = _layout(monkeypatch)

    walk = diagnostics.walk_list_raw(head, layout)

    assert walk.corrupt is True
    assert (walk.corrupt_reason or "").startswith("null pxNext")
    assert walk.items == []


def test_walk_list_raw_no_head_is_incomplete(monkeypatch):
    """A missing head address is an incomplete reason, not corruption."""
    layout = _layout(monkeypatch)

    walk = diagnostics.walk_list_raw(None, layout)

    assert walk.incomplete_reason == "no list head address"
    assert walk.corrupt is False


# ---------------------------------------------------------------------------
# list_checks
# ---------------------------------------------------------------------------


def test_list_count_mismatch_reports_both_numbers(monkeypatch):
    """uxNumberOfItems and the walked length both appear in the failure."""
    head = 0x20000000
    items = [0x20000020, 0x20000030]
    _patch_mem(
        monkeypatch,
        _list_mem(
            head,
            count=3,  # three declared, only two walkable
            items=items,
        ),
    )
    layout = _layout(monkeypatch, smp=True)

    results = dict(diagnostics.list_checks(head, layout))

    status = results["ListCount"]
    assert status.startswith("fail:")
    assert "3" in status and "2" in status
    assert results["ListIntegrity"] == "ok"
    assert results["ListIndex"] == "ok"
    assert results["ListInit"] == "ok"


def test_list_checks_pass_on_a_healthy_list(monkeypatch):
    """A consistent list yields every check ok (SMP, integrity bytes off)."""
    head = 0x20000000
    items = [0x20000020, 0x20000030]
    _patch_mem(monkeypatch, _list_mem(head, count=2, items=items))
    layout = _layout(monkeypatch, smp=True)

    results = dict(diagnostics.list_checks(head, layout))

    assert results == {
        "ListInit": "ok",
        "ListCount": "ok",
        "ListIndex": "ok",
        "ListIntegrity": "ok",
        "ListIntegrityBytes": (
            "skipped: configUSE_LIST_DATA_INTEGRITY_CHECK_BYTES off"
        ),
    }


def test_list_index_check_is_skipped_on_single_core(monkeypatch):
    """On a single core pxIndex legitimately parks on a rotation cursor."""
    head = 0x20000000
    _patch_mem(
        monkeypatch,
        _list_mem(
            head,
            count=1,
            items=[0x20000020],
            index=0x20000020,  # parked on the item, not xListEnd
        ),
    )
    layout = _layout(monkeypatch, smp=False)

    results = dict(diagnostics.list_checks(head, layout))

    assert results["ListIndex"].startswith("skipped:")
    assert "single core" in results["ListIndex"]


def test_list_index_mismatch_is_fail_on_smp(monkeypatch):
    """pxIndex != &xListEnd on SMP violates the rotation invariant."""
    head = 0x20000000
    item = 0x20000020
    _patch_mem(
        monkeypatch,
        _list_mem(head, count=1, items=[item], index=item),
    )
    layout = _layout(monkeypatch, smp=True)

    results = dict(diagnostics.list_checks(head, layout))

    assert results["ListIndex"].startswith("fail:")
    assert f"{item:#x}" in results["ListIndex"]
    assert f"{head + _END_OFFSET:#x}" in results["ListIndex"]


def test_list_init_failure_reports_sentinel_mismatch(monkeypatch):
    """A xListEnd that lost its portMAX_DELAY sentinel is a failure."""
    head = 0x20000000
    _patch_mem(
        monkeypatch,
        _list_mem(head, count=0, items=[], end_value=0x1234),
    )
    layout = _layout(monkeypatch, smp=True)

    results = dict(diagnostics.list_checks(head, layout))

    assert results["ListInit"].startswith("fail:")
    assert "0x1234" in results["ListInit"]
    assert "0xffffffff" in results["ListInit"]


def test_list_count_skips_on_truncated_walk(monkeypatch):
    """A truncated walk never produces a fabricated count comparison."""
    head = 0x20000000
    items = [0x20000020 + i * 16 for i in range(6)]
    _patch_mem(
        monkeypatch,
        _list_mem(
            head,
            count=3,
            items=items,
            item_nexts=items[1:] + [head + _END_OFFSET],
        ),
    )
    layout = _layout(monkeypatch, smp=True)

    results = dict(diagnostics.list_checks(head, layout, max_count=5))

    assert results["ListCount"].startswith("skipped: traversal bound reached")
    assert results["ListIntegrity"].startswith("skipped: traversal bound reached")
    assert "5" not in results["ListCount"] or "3" not in results["ListCount"]


def test_list_integrity_bytes_follows_tick_width():
    """The magic value follows TickType_t width (projdefs.h)."""
    assert diagnostics._integrity_magic(16) == 0x5A5A
    assert diagnostics._integrity_magic(32) == 0x5A5A5A5A
    assert diagnostics._integrity_magic(64) == 0x5A5A5A5A5A5A5A5A


def _integrity_offsets(type_name: str, path: tuple[str | int, ...]) -> int | None:
    """Offsets with the integrity-check members present (list.h order).

    With ``configUSE_LIST_DATA_INTEGRITY_CHECK_BYTES`` the List_t gains
    ``xListIntegrityValue1`` before ``uxNumberOfItems`` and
    ``xListIntegrityValue2`` after ``xListEnd``; this stub mirrors a layout
    where the pair sits behind ``xListEnd`` (any consistent placement
    exercises the same read path).
    """
    base = _offsets(type_name, path)
    if base is not None:
        return base
    first = path[0] if path and isinstance(path[0], str) else ""
    return {
        ("struct xLIST", "xListIntegrityValue1"): 24,
        ("struct xLIST", "xListIntegrityValue2"): 28,
    }.get((type_name, first))


def _add_integrity_bytes(mem: dict[int, bytes], head: int, v1: int, v2: int) -> None:
    mem[head + 24] = struct.pack("<I", v1)
    mem[head + 28] = struct.pack("<I", v2)


def test_list_integrity_bytes_ok_when_magic_matches(monkeypatch):
    """A list carrying both pdINTEGRITY_CHECK_VALUE fields verifies ok."""
    head = 0x20000000
    items = [0x20000020]
    mem = _list_mem(head, count=1, items=items)
    _add_integrity_bytes(mem, head, 0x5A5A5A5A, 0x5A5A5A5A)
    _patch_mem(monkeypatch, mem)
    layout = _layout(monkeypatch, smp=True, list_integrity_check=True)
    # Reason: _layout installs the base offset table; the integrity offsets
    # must be patched afterwards or they clobber each other.
    monkeypatch.setattr(diagnostics, "member_offset", _integrity_offsets)

    results = dict(diagnostics.list_checks(head, layout))

    assert results["ListIntegrityBytes"] == "ok"
    assert results["ListCount"] == "ok"


def test_list_integrity_bytes_fail_reports_both_values(monkeypatch):
    """A wrong magic value is a failure spelling out both read values."""
    head = 0x20000000
    mem = _list_mem(head, count=0, items=[])
    _add_integrity_bytes(mem, head, 0x5A5A5A5A, 0xDEADBEEF)
    _patch_mem(monkeypatch, mem)
    layout = _layout(monkeypatch, smp=True, list_integrity_check=True)
    # Reason: _layout installs the base offset table; the integrity offsets
    # must be patched afterwards or they clobber each other.
    monkeypatch.setattr(diagnostics, "member_offset", _integrity_offsets)

    results = dict(diagnostics.list_checks(head, layout))

    status = results["ListIntegrityBytes"]
    assert status.startswith("fail:")
    assert "0x5a5a5a5a" in status
    assert "0xdeadbeef" in status


# ---------------------------------------------------------------------------
# task_checks
# ---------------------------------------------------------------------------


class _FakeItem:
    def __init__(self, owner, container=None, address=0):
        self.owner = owner
        self.container = container
        self.address = address


class _FakeTcb:
    def __init__(self, state_item, event_item, stack_base, top, stack_end=None):
        self.state_item = state_item
        self.event_item = event_item
        self.stack_base = stack_base
        self.top_of_stack = top
        self.stack_end = stack_end


def _patch_task(
    monkeypatch,
    tcb,
    mem: dict[int, bytes],
    **config,
):
    def fake_read_field(obj, _sl, field):
        if obj is tcb:
            return {
                "state_list_item": tcb.state_item,
                "event_list_item": tcb.event_item,
                "stack_base": tcb.stack_base,
                "top_of_stack": tcb.top_of_stack,
                "stack_end": tcb.stack_end,
            }.get(field)
        if obj is tcb.state_item:
            return {
                "owner": tcb.state_item.owner,
                "container": tcb.state_item.container,
            }.get(field)
        if obj is tcb.event_item:
            return {"owner": tcb.event_item.owner}.get(field)
        return None

    monkeypatch.setattr(diagnostics, "read_field", fake_read_field)
    _patch_mem(monkeypatch, mem)
    return _layout(monkeypatch, **config)


def test_item_owner_mismatch_reports_pvowner(monkeypatch):
    """A wrong pvOwner is reported with the concrete owner address."""
    head = 0x20000000
    item_a = 0x20000020
    mem = _list_mem(head, count=1, items=[item_a])
    mem[0x1000] = b"\xa5" * 128
    tcb = _FakeTcb(
        state_item=_FakeItem(owner=0x20000040, container=head, address=item_a),
        event_item=_FakeItem(owner=0x20000050, address=0x20000030),
        stack_base=0x1000,
        top=0x1080,
    )
    layout = _patch_task(monkeypatch, tcb, mem, smp=True)

    results = dict(diagnostics.task_checks(tcb, 0x20000040, layout))

    status = results["ItemOwner"]
    assert status.startswith("fail:")
    assert "event item pvOwner=0x20000050" in status
    assert "0x20000050" in status
    assert results["ItemContainer"] == "ok"
    assert results["StackFillPresent"] == "ok"


def test_item_container_not_reachable_is_fail(monkeypatch):
    """A linked item whose container walk never reaches it is a failure.

    Covers the ``not reachable`` branch: the claimed container is a healthy
    list, but the item is not on it (the item was moved or the container
    pointer is stale).
    """
    head = 0x20000000
    other_item = 0x20000020  # the list holds this item, not ours
    mem = _list_mem(head, count=1, items=[other_item])
    mem[0x1000] = b"\xa5" * 64
    tcb = _FakeTcb(
        state_item=_FakeItem(owner=0x400, container=head, address=0x20000040),
        event_item=_FakeItem(owner=0x400, address=0x20000030),
        stack_base=0x1000,
        top=0x1040,
    )
    layout = _patch_task(monkeypatch, tcb, mem, smp=True)

    results = dict(diagnostics.task_checks(tcb, 0x400, layout))

    status = results["ItemContainer"]
    assert status.startswith("fail:")
    assert "not reachable" in status
    assert "0x20000040" in status


def test_item_owner_ok_when_both_items_stamped(monkeypatch):
    """Both list items carrying the task address pass the owner check."""
    head = 0x20000000
    item_a = 0x20000020
    mem = _list_mem(head, count=1, items=[item_a])
    mem[0x1000] = b"\xa5" * 128
    tcb = _FakeTcb(
        state_item=_FakeItem(owner=0x400, container=head, address=item_a),
        event_item=_FakeItem(owner=0x400, address=0x20000030),
        stack_base=0x1000,
        top=0x1080,
    )
    layout = _patch_task(monkeypatch, tcb, mem, smp=True)

    results = dict(diagnostics.task_checks(tcb, 0x400, layout))

    assert results["ItemOwner"] == "ok"
    assert results["ItemContainer"] == "ok"
    assert results["StackFillPresent"] == "ok"


def test_stack_fill_present_fails_on_zero_window(monkeypatch):
    """A zeroed stack window is a corruption, not a config choice."""
    head = 0x20000000
    item_a = 0x20000020
    mem = _list_mem(head, count=1, items=[item_a])
    mem[0x1000] = b"\x00" * 64
    monkeypatch.setattr(diagnostics, "_build_prefills_stacks", lambda _layout: True)
    tcb = _FakeTcb(
        state_item=_FakeItem(owner=0x400, container=0, address=item_a),
        event_item=_FakeItem(owner=0x400, address=0x20000030),
        stack_base=0x1000,
        top=0x1040,
    )
    layout = _patch_task(monkeypatch, tcb, mem, smp=True)

    results = dict(diagnostics.task_checks(tcb, 0x400, layout))

    assert results["StackFillPresent"].startswith("fail:")
    assert "0xa5" in results["StackFillPresent"]
    assert results["ItemContainer"] == "skipped: not linked"


def test_stack_fill_present_skips_when_prefill_off(monkeypatch):
    """A non-prefilled build is skipped, never a false corruption."""
    head = 0x20000000
    item_a = 0x20000020
    mem = _list_mem(head, count=1, items=[item_a])
    mem[0x1000] = b"\x00" * 64
    monkeypatch.setattr(diagnostics, "_build_prefills_stacks", lambda _layout: False)
    tcb = _FakeTcb(
        state_item=_FakeItem(owner=0x400, container=head, address=item_a),
        event_item=_FakeItem(owner=0x400, address=0x20000030),
        stack_base=0x1000,
        top=0x1040,
    )
    layout = _patch_task(monkeypatch, tcb, mem, smp=True)

    results = dict(diagnostics.task_checks(tcb, 0x400, layout))

    assert results["StackFillPresent"].startswith("skipped: stacks not prefilled")


def test_build_prefills_stacks_probes_the_population(monkeypatch):
    """The prefill probe follows the data: any filled task proves the build."""
    filled = _FakeTcb(
        state_item=_FakeItem(owner=0x400, container=0, address=0x20000020),
        event_item=_FakeItem(owner=0x400, address=0x20000030),
        stack_base=0x1000,
        top=0x1040,
    )
    plain = _FakeTcb(
        state_item=_FakeItem(owner=0x400, container=0, address=0x20000040),
        event_item=_FakeItem(owner=0x400, address=0x20000050),
        stack_base=0x2000,
        top=0x2040,
    )

    def fake_read_field(obj, _sl, field):
        return (
            getattr(
                obj,
                {
                    "stack_base": "stack_base",
                    "top_of_stack": "top_of_stack",
                    "stack_end": "stack_end",
                }.get(field, ""),
                None,
            )
            if field in ("stack_base", "top_of_stack", "stack_end")
            else None
        )

    monkeypatch.setattr(diagnostics, "read_field", fake_read_field)
    monkeypatch.setattr(
        diagnostics,
        "read_bytes",
        lambda addr, _size: b"\xa5" * 64 if addr == 0x1000 else b"\x00" * 64,
    )
    layout = _layout(monkeypatch)

    monkeypatch.setattr(
        diagnostics, "iter_tasks", lambda _l: iter([(plain, "Ready", None)])
    )
    assert diagnostics._build_prefills_stacks(layout) is False

    monkeypatch.setattr(
        diagnostics,
        "iter_tasks",
        lambda _l: iter([(plain, "Ready", None), (filled, "Ready", None)]),
    )
    assert diagnostics._build_prefills_stacks(layout) is True


# ---------------------------------------------------------------------------
# system_checks
# ---------------------------------------------------------------------------


def _patch_system(
    monkeypatch, symbols: dict, task_count: int | None, mem, delayed_head
):
    monkeypatch.setattr(
        diagnostics,
        "lookup_symbol",
        lambda name: symbols.get(name),
    )
    if task_count is None:
        monkeypatch.setattr(diagnostics, "iter_tasks", lambda _layout: (_ for _ in ()))
    else:
        monkeypatch.setattr(
            diagnostics,
            "iter_tasks",
            lambda _layout: [object() for _ in range(task_count)],
        )
    if delayed_head is not None:
        delayed = types.SimpleNamespace()
        monkeypatch.setattr(
            diagnostics,
            "safe_dereference",
            lambda value: (
                delayed if value is symbols.get("pxDelayedTaskList") else None
            ),
        )
        monkeypatch.setattr(
            diagnostics,
            "value_address",
            lambda value: delayed_head if value is delayed else 0,
        )
        _patch_mem(monkeypatch, mem)
        monkeypatch.setattr(diagnostics, "member_offset", _offsets)
    else:
        monkeypatch.setattr(diagnostics, "safe_dereference", lambda _value: None)
    return _layout(monkeypatch)


def test_system_checks_pass_on_a_healthy_snapshot(monkeypatch):
    """A consistent system yields TaskCount/SchedulerSuspended/NextUnblockTime ok."""
    delayed_head = 0x3000
    delayed_values = [10]
    mem = _list_mem(
        delayed_head,
        count=1,
        items=[0x3200],
        item_values=delayed_values,
        item_owners=[0x1000],
    )
    heap = types.SimpleNamespace(cross_check="ok")
    monkeypatch.setattr(diagnostics, "heap_snapshot", lambda _layout: heap)
    layout = _patch_system(
        monkeypatch,
        symbols={
            "uxCurrentNumberOfTasks": 4,
            "uxSchedulerSuspended": 0,
            "xNextTaskUnblockTime": 10,
            "pxDelayedTaskList": object(),
        },
        task_count=4,
        mem=mem,
        delayed_head=delayed_head,
    )

    results = dict(diagnostics.system_checks(layout))

    assert results["TaskCount"] == "ok"
    assert results["SchedulerSuspended"] == "ok"
    assert results["NextUnblockTime"] == "ok"
    assert results["HeapCrossCheck"] == "ok"


def test_system_task_count_mismatch_reports_both_numbers(monkeypatch):
    """The counter and the discovered count both appear on a mismatch."""
    heap = types.SimpleNamespace(cross_check="ok")
    monkeypatch.setattr(diagnostics, "heap_snapshot", lambda _layout: heap)
    layout = _patch_system(
        monkeypatch,
        symbols={
            "uxCurrentNumberOfTasks": 8,
            "uxSchedulerSuspended": 0,
            "xNextTaskUnblockTime": 10,
            "pxDelayedTaskList": object(),
        },
        task_count=7,
        mem={},
        delayed_head=None,
    )

    results = dict(diagnostics.system_checks(layout))

    status = results["TaskCount"]
    assert status.startswith("fail:")
    assert "8" in status and "7" in status


def test_system_scheduler_suspended_nonzero_is_fail(monkeypatch):
    """A nonzero suspend counter is surfaced with its value."""
    heap = types.SimpleNamespace(cross_check="ok")
    monkeypatch.setattr(diagnostics, "heap_snapshot", lambda _layout: heap)
    layout = _patch_system(
        monkeypatch,
        symbols={
            "uxCurrentNumberOfTasks": 4,
            "uxSchedulerSuspended": 2,
            "xNextTaskUnblockTime": 10,
            "pxDelayedTaskList": object(),
        },
        task_count=4,
        mem={},
        delayed_head=None,
    )

    results = dict(diagnostics.system_checks(layout))

    status = results["SchedulerSuspended"]
    assert status.startswith("fail:")
    assert "2" in status


def test_system_next_unblock_time_mismatch_reports_both_numbers(monkeypatch):
    """xNextTaskUnblockTime vs the delayed-list front value."""
    delayed_head = 0x3000
    mem = _list_mem(
        delayed_head,
        count=1,
        items=[0x3200],
        item_values=[10],
        item_owners=[0x1000],
    )
    heap = types.SimpleNamespace(cross_check="ok")
    monkeypatch.setattr(diagnostics, "heap_snapshot", lambda _layout: heap)
    layout = _patch_system(
        monkeypatch,
        symbols={
            "uxCurrentNumberOfTasks": 4,
            "uxSchedulerSuspended": 0,
            "xNextTaskUnblockTime": 42,
            "pxDelayedTaskList": object(),
        },
        task_count=4,
        mem=mem,
        delayed_head=delayed_head,
    )

    results = dict(diagnostics.system_checks(layout))

    status = results["NextUnblockTime"]
    assert status.startswith("fail:")
    assert "42" in status and "10" in status


def test_system_heap_cross_check_reports_mismatch(monkeypatch):
    """A heap mismatch becomes a fail with the three concrete numbers."""
    heap = types.SimpleNamespace(
        cross_check="mismatch: free-list 16 vs linear 48 vs counter 64"
    )
    monkeypatch.setattr(diagnostics, "heap_snapshot", lambda _layout: heap)
    layout = _patch_system(
        monkeypatch,
        symbols={
            "uxCurrentNumberOfTasks": 4,
            "uxSchedulerSuspended": 0,
            "xNextTaskUnblockTime": 10,
            "pxDelayedTaskList": object(),
        },
        task_count=4,
        mem={},
        delayed_head=None,
    )

    results = dict(diagnostics.system_checks(layout))

    status = results["HeapCrossCheck"]
    assert status.startswith("fail:")
    assert "16" in status and "48" in status and "64" in status


def test_system_heap_cross_check_unavailable_is_skipped(monkeypatch):
    """An unavailable heap verdict is a structural skip, not a failure."""
    heap = types.SimpleNamespace(cross_check="unavailable: heap not initialised")
    monkeypatch.setattr(diagnostics, "heap_snapshot", lambda _layout: heap)
    layout = _patch_system(
        monkeypatch,
        symbols={
            "uxCurrentNumberOfTasks": 4,
            "uxSchedulerSuspended": 0,
            "xNextTaskUnblockTime": 10,
            "pxDelayedTaskList": object(),
        },
        task_count=4,
        mem={},
        delayed_head=None,
    )

    results = dict(diagnostics.system_checks(layout))

    assert results["HeapCrossCheck"] == "skipped: heap not initialised"


# ---------------------------------------------------------------------------
# event_checks
# ---------------------------------------------------------------------------


def test_event_waiter_satisfied_ok_when_nobody_ready(monkeypatch):
    """Unsatisfied waiters are fine: the check reports ok."""
    monkeypatch.setattr(diagnostics, "read_path", lambda _v, _p: 0x1)
    waiter = types.SimpleNamespace(satisfied=False, task="gdr_evw")
    monkeypatch.setattr(diagnostics, "_iter_waiters", lambda _v, _l, _b: [waiter])
    layout = _layout(monkeypatch)

    results = dict(diagnostics.event_checks(object(), layout))

    assert results["EventWaiterSatisfied"] == "ok"


def test_event_waiter_satisfied_fail_on_mid_unblock(monkeypatch):
    """A satisfied waiter still listed is the mid-unblock transient."""
    monkeypatch.setattr(diagnostics, "read_path", lambda _v, _p: 0x3)
    waiter = types.SimpleNamespace(satisfied=True, task="gdr_evw")
    monkeypatch.setattr(diagnostics, "_iter_waiters", lambda _v, _l, _b: [waiter])
    layout = _layout(monkeypatch)

    results = dict(diagnostics.event_checks(object(), layout))

    status = results["EventWaiterSatisfied"]
    assert status.startswith("fail:")
    assert "gdr_evw" in status


def test_event_waiter_satisfied_skips_on_unreadable_bits(monkeypatch):
    """An unreadable event group is skipped, not a failure."""
    monkeypatch.setattr(diagnostics, "read_path", lambda _v, _p: None)
    layout = _layout(monkeypatch)

    results = dict(diagnostics.event_checks(object(), layout))

    assert results["EventWaiterSatisfied"] == "skipped: unreadable"
