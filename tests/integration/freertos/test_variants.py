"""Live detect_config() vs independent fixture capability table."""

from __future__ import annotations

import os

import pytest

from tests.support.freertos_fixture_profiles import get_freertos_test_profile

_VERSION = os.environ.get("GDR_VERSION", "10.3.1")
_TARGET = os.environ.get("GDR_QEMU_TARGET", "b-l475e-iot01a")
_VARIANT = os.environ.get("GDR_FIXTURE_VARIANT", "base")
_PROFILE = get_freertos_test_profile(_VARIANT, _VERSION, _TARGET)

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS") != "freertos",
    reason="requires the FreeRTOS QEMU profile",
)

_COMPARED_FIELDS = (
    "heap_kind",
    "trace_facility",
    "static_allocation",
    "static_and_dynamic",
    "notification_array",
    "runtime_stats",
    "queue_sets",
    "registry_size",
    "number_of_cores",
    "heap_protector",
    "stream_buffers",
)


def test_heap_protector_uses_a_nonzero_canary(gdb_session):
    """V11 pointer protection must not silently degrade to identity XOR."""
    if not _PROFILE.heap_protector:
        pytest.skip("requires the V11 heap-protector fixture")
    output = gdb_session.run_python(
        """
import gdb
canary = int(gdb.parse_and_eval("xHeapCanary"))
encoded_null = int(gdb.parse_and_eval("pxEnd->pxNextFreeBlock"))
print(f"canary={canary}")
print(f"encoded_null={encoded_null}")
"""
    )
    values = {
        key: int(value)
        for line in output.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }
    assert values["canary"] != 0, output
    assert values["encoded_null"] == values["canary"], output


def test_detect_config_matches_profile(gdb_session):
    """detect_config() equals the independent profile, field by field."""
    output = gdb_session.run_python(
        """
from freertos.layout import detect_config
cfg = detect_config()
print(f"heap_kind={cfg.heap_kind}")
print(f"trace_facility={cfg.trace_facility}")
print(f"static_allocation={cfg.static_allocation}")
print(f"static_and_dynamic={cfg.static_and_dynamic}")
print(f"notification_array={cfg.notification_array}")
print(f"runtime_stats={cfg.runtime_counter_bits is not None and 'ulRunTimeCounter' in cfg.tcb_fields}")
print(f"queue_sets={cfg.queue_sets}")
print(f"registry_size={cfg.queue_registry_size}")
print(f"number_of_cores={cfg.number_of_cores}")
print(f"heap_protector={cfg.heap_protector}")
print(f"stream_buffers={cfg.stream_buffers}")
print(f"stack_end={cfg.stack_end_field}")
"""
    )
    probed = {
        key: value
        for line in output.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }

    def _coerce(name: str, raw: str):
        expected = getattr(_PROFILE, name)
        if isinstance(expected, bool):
            return raw == "True"
        if expected is None:
            return None if raw in {"None", ""} else int(raw)
        if isinstance(expected, int):
            return int(raw)
        return raw

    for field in _COMPARED_FIELDS:
        actual = _coerce(field, probed[field])
        assert actual == getattr(_PROFILE, field), (
            f"{field}: detect_config={actual!r} profile={getattr(_PROFILE, field)!r}\n"
            f"{output}"
        )
    if _PROFILE.high_water_source == "pxEndOfStack":
        assert probed["stack_end"] == "pxEndOfStack"
    else:
        assert probed["stack_end"] in {"None", ""}
