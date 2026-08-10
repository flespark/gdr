"""Phase 1 closed-loop checks for the FreeRTOS B-L475E-IOT01A fixture."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS") != "freertos",
    reason="requires the FreeRTOS QEMU profile",
)


def test_freertos_kernel_types_are_visible_to_gdb(gdb_session):
    """The fixture retains DWARF for all three Phase 1 kernel structures."""
    output = gdb_session.run_many(
        "ptype struct tskTaskControlBlock",
        "ptype struct QueueDefinition",
        "ptype struct tmrTimerControl",
    )

    assert "tskTaskControlBlock" in output
    assert "QueueDefinition" in output
    assert "tmrTimerControl" in output
    assert "No struct type named" not in output


def test_freertos_profile_uses_32_bit_pointers_and_persistent_gdb(
    gdb_session, qemu_profile
):
    """The ARM profile and persistent connection survive sequential commands."""
    pointer_output = gdb_session.run("p sizeof(void *)")
    expressions = gdb_session.run_many("p 1 + 1", "p 2 + 2")

    assert str(qemu_profile.pointer_width) in pointer_output
    assert "2" in expressions
    assert "4" in expressions
