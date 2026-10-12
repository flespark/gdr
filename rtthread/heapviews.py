"""RT-Thread heap views: the snapshot record, the walk's Blocks line and verdicts.

Pure formatting over :mod:`rtthread.diagnostics` results, split out of
``rtthread.adapter`` so the adapter keeps the object converters and command
shapes. The adapter owns the target reads; this module only interprets them.
"""

from __future__ import annotations

from dataclasses import dataclass

from gdr.adapter_api import CheckFinding
from rtthread import diagnostics


@dataclass
class HeapInfo:
    """One snapshot+walk collection shared by ``rtt system`` and ``rtt heap``.

    ``used``/``total`` are the snapshot counters, replaced by exact walk
    bytes only when a closed small_mem/memheap walk is allowed to fill a
    missing value. ``max_used`` stays snapshot-only. ``walk`` is the bounded
    block walk, or ``None`` when the chain is unresolvable.
    """

    algorithm: str
    used: int | None = None
    total: int | None = None
    max_used: int | None = None
    from_walk: bool = False
    walk: diagnostics.HeapWalk | None = None


@dataclass
class HeapDetail:
    """Formatted heap block line from a bounded system-heap walk.

    ``pairs`` holds the Blocks line; ``status`` is heap health
    (``good`` / ``corrupt`` / ``overrun``), copied onto
    ``SystemSummary.heap_status``; a truncated walk is only a Blocks suffix.
    """

    pairs: list[tuple[str, str]]
    status: str | None = "good"


def _heap_status(
    walk: diagnostics.HeapWalk | None,
    used: int | None = None,
    total: int | None = None,
) -> str | None:
    """Classify heap health; a truncated walk is not a status value.

    ``truncated`` only means the bounded walk hit ``GDR_MAX_TRAVERSAL_COUNT``.
    ``overrun`` is used exceeding total. Corrupt walks win over overrun.
    ``None`` means the system command should omit Heap status.
    """
    if walk is not None and walk.corrupt:
        return "corrupt"
    if used is not None and total is not None and used > total:
        return "overrun"
    if walk is None:
        return None
    return "good"


def _heap_detail_from_walk(
    walk: diagnostics.HeapWalk | None,
    used: int | None = None,
    total: int | None = None,
) -> HeapDetail | None:
    """Assemble the Blocks line from one bounded walk result."""
    if walk is None:
        return None
    status = _heap_status(walk, used, total)
    if walk.corrupt:
        suffix = " (corrupt)"
    elif walk.truncated:
        suffix = " (truncated)"
    else:
        suffix = ""
    return HeapDetail(
        pairs=[
            (
                "Blocks",
                f"{walk.used_blocks} used, {walk.free_blocks} free, "
                f"{walk.used_blocks + walk.free_blocks} total{suffix}",
            ),
        ],
        status=status,
    )


def _heap_accounting_finding(info: HeapInfo) -> CheckFinding:
    """Cross-check the heap snapshot counters against exact chain accounting.

    The walk reports exact chain bytes only when it closed cleanly, so this is
    a real cross-check rather than a restated snapshot: a disagreement means
    one of the two sources is stale or the chain is not the whole heap.

    An overrun (used above total) is caught first because it needs no walk.
    """
    used, total = info.used, info.total
    if used is not None and total is not None and used > total:
        return CheckFinding(
            "HeapAccounting", "fail", f"used {used} B exceeds total {total} B", "system"
        )
    walk = info.walk
    if walk is None or walk.used_bytes is None or walk.total_bytes is None:
        return CheckFinding(
            "HeapAccounting", "skipped", "exact chain accounting unavailable", "system"
        )
    if used is None or total is None:
        return CheckFinding(
            "HeapAccounting", "skipped", "snapshot counters unavailable", "system"
        )
    if walk.used_bytes == used and walk.total_bytes == total:
        return CheckFinding("HeapAccounting", "ok", scope="system")
    return CheckFinding(
        "HeapAccounting",
        "fail",
        f"snapshot used {used} B of {total} B != walk used {walk.used_bytes} B "
        f"of {walk.total_bytes} B",
        "system",
    )


def _heap_chain_finding(walk: diagnostics.HeapWalk | None) -> CheckFinding:
    """Verdict for the bounded block-chain walk."""
    if walk is None:
        return CheckFinding(
            "HeapChain", "skipped", "block chain not resolvable", "system"
        )
    if walk.corrupt:
        return CheckFinding("HeapChain", "fail", "the block chain is corrupt", "system")
    if walk.truncated:
        return CheckFinding(
            "HeapChain", "skipped", "the walk hit the traversal bound", "system"
        )
    return CheckFinding("HeapChain", "ok", scope="system")
