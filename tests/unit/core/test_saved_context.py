"""Saved-context construction contracts and validation gates."""

from dataclasses import FrozenInstanceError, replace

import pytest

from core.stack_frame_parser import SavedStackContext
from freertos import backtrace as frt
from rtthread import backtrace as rtt


def _profile(**changes):
    return SavedStackContext(
        **(
            dict(
                profile_id="test/integer",
                pointer_size=4,
                registers=((4, 0),),
                pc_offset=4,
                lr_offset=8,
                status_offset=12,
                sp_offset=16,
                context_span=(0, 16),
            )
            | changes
        )
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"profile_id": ""}, "invalid saved-context profile"),
        ({"registers": ((-1, 0),)}, "invalid saved-context register map"),
        ({"registers": ((4, 0), (5, 0))}, "overlapping"),
        ({"pc_offset": 8}, "overlapping"),
        ({"status_offset": 0}, "overlapping"),
        ({"sp_slot_offset": 0}, "overlapping"),
        ({"pc_offset": 16}, "outside span"),
        ({"registers": ((4, -4),)}, "outside span"),
        ({"context_span": (0, 0)}, "invalid saved-context span"),
        ({"context_span": (0, 15)}, "invalid saved-context span"),
        ({"status_offset": 2}, "invalid Arm status slot"),
        ({"fill_word": -1}, "exceeds slot width"),
        ({"fill_word": 1 << 32}, "exceeds slot width"),
        ({"initial_registers": (0,)}, "invalid initial-register proof"),
        ({"initial_registers": (1 << 32,) * 8}, "exceeds slot width"),
        ({"slot_aliases": (("pc", "pc"),)}, "invalid saved-context slot alias"),
        ({"slot_aliases": (("pc", "unknown"),)}, "invalid saved-context slot alias"),
        ({"slot_aliases": (("pc", "lr"),)}, "invalid saved-context slot alias"),
        (
            {"pc_offset": 8, "slot_aliases": (("pc", "lr"), ("lr", "pc"))},
            "invalid saved-context slot alias",
        ),
        ({"validation": "unknown"}, "invalid saved-context validation"),
    ],
)
def test_invalid_profiles_are_rejected(changes, reason):
    with pytest.raises(ValueError, match=reason):
        _profile(**changes)


def test_explicit_slot_aliases_and_negative_buffer_offsets():
    alias = _profile(pc_offset=8, slot_aliases=(("pc", "lr"),))
    assert alias.pc_offset == alias.lr_offset
    context = _profile(
        registers=((4, -20),),
        pc_offset=-16,
        lr_offset=-12,
        status_offset=-8,
        sp_offset=0,
        sp_slot_offset=-4,
        sp_slot_bias=32,
        context_span=(-20, 0),
        context_in_tcb=True,
    )
    assert context.context_in_tcb and context.context_span == (-20, 0)
    with pytest.raises(FrozenInstanceError):
        context.profile_id = "changed"


@pytest.mark.parametrize(
    "validation,verified", [("synthetic", False), ("file-only", False), ("live", True)]
)
def test_only_live_validation_passes_the_runtime_gate(validation, verified):
    context = _profile(validation=validation)
    assert context.validation == validation and context.verified is verified


@pytest.mark.parametrize(
    "context,validation",
    [
        (frt.ARMV6M_INTEGER, "live"),
        (frt.ARMV7M_INTEGER, "live"),
        (frt.ARMV8M_MAINLINE_SMP_INTEGER, "live"),
        (frt.ARMV8M_MAINLINE_MPU_INTEGER, "live"),
        (frt.RISCV32_INTEGER, "live"),
        (frt.RISCV64_INTEGER, "live"),
        (rtt.ARMV6M_INTEGER, "live"),
        (rtt.ARMV7M_INTEGER, "live"),
        (rtt.ARMV7A_INTEGER, "synthetic"),
        (rtt.ARMV7A_LWP_INTEGER, "live"),
        (rtt.ARMV8M_MAINLINE_INTEGER, "synthetic"),
        (rtt.RISCV32_INTEGER, "synthetic"),
        (rtt.RISCV64_INTEGER, "live"),
    ],
)
def test_every_registered_profile_retains_validation_and_slot_contracts(
    context, validation
):
    assert replace(context) == context
    assert context.validation == validation
    assert context.verified == (validation == "live")
    low, high = context.context_span
    offsets = [
        context.pc_offset,
        context.lr_offset,
        *(offset for _, offset in context.registers),
    ]
    if context.status_offset is not None:
        offsets.append(context.status_offset)
    if context.sp_slot_offset is not None:
        offsets.append(context.sp_slot_offset)
    assert all(
        low <= offset and offset + context.pointer_size <= high for offset in offsets
    )
    assert all(offset % context.pointer_size == 0 for offset in offsets)
