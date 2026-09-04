"""Independent FreeRTOS fixture capability expectations.

COUPLED: ci/freertos/fixture/config/<variant>/FreeRTOSConfig.h and the
matching board under ci/freertos/fixture/board/<board>/. Keep this table
synchronized with those headers, but do not import freertos.layout or any
other production probe — a GDR regression must not silently update the
expected values.
"""

from __future__ import annotations

from dataclasses import dataclass

_SUPPORTED_VARIANTS = (
    "base",
    "full",
    "static-only",
    "static-dynamic",
    "trace-off",
    "heap-1",
    "heap-2",
    "heap-3",
    "heap-5",
    "heap-protector",
    "heap-5-protector",
    "registry-0",
    "pend-callback",
    "streams",
    "mpu",
    "rv64",
    "smp",
)

_SUPPORTED_BOARDS = ("b-l475e-iot01a", "mps2-an385", "mps2-an521", "qemu-virt-rv64")


@dataclass(frozen=True)
class FreeRtosTestProfile:
    """Expected ABI and fixture contract for one FreeRTOS test variant."""

    variant: str
    version: str
    target: str
    heap_kind: int | None
    trace_facility: bool
    static_allocation: bool
    static_and_dynamic: bool
    notification_array: bool
    mini_list_item: bool | None
    high_water_source: str
    runtime_stats: bool
    queue_sets: bool
    registry_size: int
    number_of_cores: int
    heap_protector: bool
    stream_buffers: bool
    batching_buffer: bool
    stack_watermark: bool


def _parse_version(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) < 3:
        raise ValueError(f"FreeRTOS version must be X.Y.Z, got {version!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _heap_kind_for(variant: str) -> int | None:
    mapping = {
        "heap-1": 1,
        "heap-2": 2,
        "heap-3": None,
        "heap-5": 5,
        "heap-5-protector": 5,
        # Reason: the build scripts link no MemMang source at all for
        # static-only (heap_source() returns empty), so no heap symbol exists
        # and detect_config() reports None -- there is no heap to identify.
        "static-only": None,
    }
    return mapping.get(variant, 4)


def list_freertos_variants() -> tuple[str, ...]:
    """Return every named fixture variant."""
    return _SUPPORTED_VARIANTS


def list_freertos_boards() -> tuple[str, ...]:
    """Return every named fixture board."""
    return _SUPPORTED_BOARDS


def get_freertos_test_profile(
    variant: str, version: str, target: str = "b-l475e-iot01a"
) -> FreeRtosTestProfile:
    """Return expectations independent from GDR's production layout probes."""
    if variant not in _SUPPORTED_VARIANTS:
        raise ValueError(f"unknown FreeRTOS fixture variant: {variant}")
    if target not in _SUPPORTED_BOARDS:
        raise ValueError(f"unknown FreeRTOS fixture board: {target}")
    major, minor, patch = _parse_version(version)
    v10_4 = (major, minor) >= (10, 4)
    v10_5 = (major, minor) >= (10, 5)
    v11 = major >= 11
    v11_1 = (major, minor) >= (11, 1)
    return FreeRtosTestProfile(
        variant=variant,
        version=version,
        target=target,
        heap_kind=_heap_kind_for(variant),
        trace_facility=variant != "trace-off",
        static_allocation=variant in {"static-only", "static-dynamic", "streams"},
        static_and_dynamic=variant in {"static-dynamic", "streams"},
        notification_array=v10_4,
        # Reason: configUSE_MINI_LIST_ITEM defaults to 1 from V10.5.0; earlier
        # kernels always used MiniListItem_t without the config switch, so the
        # DWARF shape is still "mini" (no pvOwner on xListEnd).
        mini_list_item=True if v10_5 or (major, minor, patch) >= (10, 3, 0) else None,
        high_water_source="pxEndOfStack" if variant == "full" else "scan",
        runtime_stats=variant == "full",
        queue_sets=variant == "full",
        registry_size=0 if variant == "registry-0" else 8,
        # Reason: the core count is a fixture fact written here independently
        # (never backfilled from freertos.layout's detect_config()); it is the
        # ground truth the live probe is compared against.  The smp variant is
        # only built for the dual-core mps2-an521 board.
        number_of_cores=2 if variant == "smp" else 1,
        heap_protector=(variant in {"heap-protector", "heap-5-protector"}) and v11,
        stream_buffers=True,
        batching_buffer=v11_1,
        # Reason: the kernel only memsets a new stack with tskSTACK_FILL_BYTE
        # when configUSE_TRACE_FACILITY, INCLUDE_uxTaskGetStackHighWaterMark[2]
        # or configCHECK_FOR_STACK_OVERFLOW > 1 is set (tasks.c prvInitialise
        # NewTask). trace-off sets none of them, so there is no watermark to
        # scan and the HighWater column must disappear instead of printing a
        # fabricated number.
        stack_watermark=variant != "trace-off",
    )
