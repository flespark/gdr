"""Self-consistency of the FreeRTOS fixture variant table."""

from __future__ import annotations

from pathlib import Path

from tests.support.freertos_fixture_profiles import (
    get_freertos_test_profile,
    list_freertos_boards,
    list_freertos_variants,
)

_REPO = Path(__file__).resolve().parents[3]


def test_variant_names_are_unique():
    names = list_freertos_variants()
    assert len(names) == len(set(names))
    assert names


def test_every_variant_has_config_directory():
    for variant in list_freertos_variants():
        path = _REPO / "ci" / "freertos" / "fixture" / "config" / variant
        assert path.is_dir(), path
        assert (path / "FreeRTOSConfig.h").is_file(), path


def test_every_board_has_port_directory():
    for board in list_freertos_boards():
        path = _REPO / "ci" / "freertos" / "fixture" / "board" / board
        assert path.is_dir(), path
        assert (path / "gdr_board.h").is_file(), path


def test_version_ranges_monotonic_with_capabilities():
    """Later kernel versions never drop notification-array / mini-list / batching."""
    earlier = get_freertos_test_profile("base", "10.3.1")
    mid = get_freertos_test_profile("base", "10.4.6")
    later = get_freertos_test_profile("base", "10.5.1")
    v11 = get_freertos_test_profile("base", "11.1.0")
    assert earlier.notification_array is False
    assert mid.notification_array is True
    assert later.notification_array is True
    assert later.mini_list_item is True
    assert v11.batching_buffer is True
    assert earlier.batching_buffer is False


def test_static_only_and_static_dynamic_are_distinct():
    only = get_freertos_test_profile("static-only", "10.3.1")
    both = get_freertos_test_profile("static-dynamic", "10.3.1")
    assert only.static_allocation is True
    assert only.static_and_dynamic is False
    assert both.static_allocation is True
    assert both.static_and_dynamic is True


def test_heap_variants_map_to_independent_kinds():
    assert get_freertos_test_profile("heap-1", "10.3.1").heap_kind == 1
    assert get_freertos_test_profile("heap-2", "10.3.1").heap_kind == 2
    assert get_freertos_test_profile("heap-3", "10.3.1").heap_kind is None
    assert get_freertos_test_profile("base", "10.3.1").heap_kind == 4
    assert get_freertos_test_profile("heap-5", "10.3.1").heap_kind == 5


def test_static_only_has_no_heap_manager():
    """static-only links no MemMang source, so detection reports None."""
    assert get_freertos_test_profile("static-only", "10.3.1").heap_kind is None


def test_mpu_pool_has_a_live_variant():
    """The mpu variant (single-core CM33 with configENABLE_MPU) is registered
    and boots live on mps2-an521 (the xKernelObjectPool channel's first live
    fixture); it is not the old 'mpu-pool' name -- the pool symbol is reached
    through the mpu variant's build.  The MPU wrappers v2 TCB declares
    ucStaticallyAllocated whenever dynamic allocation is on, so the probed
    static_and_dynamic is True even though the fixture never creates a
    static task."""
    assert "mpu" in list_freertos_variants()
    assert "mpu-pool" not in list_freertos_variants()
    profile = get_freertos_test_profile("mpu", "11.3.1", "mps2-an521")
    assert profile.static_and_dynamic is True
    assert profile.static_allocation is False


def test_tick_widths_by_variant():
    """Only tick16 declares a 16-bit tick; rv64 is 64-bit (RISC-V port)."""
    assert get_freertos_test_profile("tick16", "10.3.1").tick_bits == 16
    assert get_freertos_test_profile("tick16", "11.1.0", "mps2-an385").tick_bits == 16
    assert get_freertos_test_profile("rv64", "11.1.0", "qemu-virt-rv64").tick_bits == 64
    for variant in list_freertos_variants():
        if variant in {"tick16", "rv64"}:
            continue
        profile = get_freertos_test_profile(variant, "11.1.0")
        assert profile.tick_bits == 32, variant


def test_stack_watermark_is_off_only_without_trace_facility():
    """Only trace-off drops the 0xa5 prefill, hence the HighWater column."""
    assert get_freertos_test_profile("trace-off", "10.3.1").stack_watermark is False
    for variant in list_freertos_variants():
        if variant == "trace-off":
            continue
        profile = get_freertos_test_profile(variant, "10.3.1")
        assert profile.stack_watermark is True, variant
