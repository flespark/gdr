"""Independent xPSR/CPSR saved-frame status contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.stack_frame_parser import StatusKind, decode_profile_slots
from core.unwind_meta import Endian
from tests.unit.core.test_saved_context import _profile


def _decode(
    kind: StatusKind,
    status: int,
    *,
    pc: int = 0x1000,
    endian: Endian = "little",
    check: bool = True,
):
    context = _profile(status_kind=kind)
    words = (4, pc, 0x1101, status)
    blob = b"".join(word.to_bytes(4, endian) for word in words)

    def read(address, size):
        start = address - 0x2000
        return blob[start : start + size] if 0 <= start < len(blob) else None

    return decode_profile_slots(
        context,
        0x2000,
        read,
        endian=endian,
        stack_low=0x2000,
        stack_high=0x2100,
        check_ipsr_and_align=check,
    )


@pytest.mark.parametrize("endian", ["little", "big"])
@pytest.mark.parametrize("status,pc", [(0x13, 0x1000), (0x33, 0x1001), (0x1F, 0x1000)])
def test_cpsr_uses_arm_registers_and_its_own_instruction_state(endian, status, pc):
    decoded, reason = _decode("cpsr", status, pc=pc, endian=endian)
    assert reason == "ok" and decoded is not None
    assert decoded.status == status and decoded.code_pc == pc & ~1
    assert decoded.sp == 0x2010
    assert decoded.registers[13] == decoded.sp
    assert decoded.registers[14] == 0x1101 and decoded.registers[15] == pc
    assert decoded.registers[4] == 4


@pytest.mark.parametrize("check", [False, True])
def test_cpsr_low_mode_bits_are_not_ipsr_and_bit_nine_is_not_stack_padding(check):
    decoded, reason = _decode("cpsr", 0x213, check=check)
    assert reason == "ok" and decoded is not None
    assert decoded.sp == 0x2010 and decoded.registers[13] == 0x2010


@pytest.mark.parametrize(
    "status,pc,reason",
    [
        (0, 0x1000, "saved-frame-cpsr-mode-invalid"),
        (0x15, 0x1000, "saved-frame-cpsr-mode-invalid"),
        (0x1D, 0x1000, "saved-frame-cpsr-mode-invalid"),
        (0x01000013, 0x1000, "saved-frame-instruction-set-unsupported"),
        (0x01000033, 0x1000, "saved-frame-instruction-set-unsupported"),
        (0x13, 0x1001, "saved-frame-pc-misaligned"),
        (0x13, 0x1002, "saved-frame-pc-misaligned"),
    ],
)
def test_cpsr_rejects_reserved_modes_jazelle_and_misaligned_arm_pcs(status, pc, reason):
    decoded, actual = _decode("cpsr", status, pc=pc)
    assert decoded is None and actual == reason


def test_xpsr_keeps_thumb_ipsr_and_padding_checks():
    decoded, reason = _decode("xpsr", 0x01000200)
    assert reason == "ok" and decoded is not None
    assert decoded.sp == decoded.registers[13] == 0x2014
    assert _decode("xpsr", 0x01000013) == (None, "saved-frame-ipsr-invalid")
    assert _decode("xpsr", 0x33) == (None, "saved-frame-not-thumb")


def test_status_kind_defaults_preserve_existing_profiles_and_reject_invalid_kinds():
    assert _profile().status_kind == "xpsr"
    assert replace(_profile(), status_kind="cpsr").convention.family == "arm"
    with pytest.raises(ValueError, match="status kind"):
        _profile(status_kind="unknown")
    with pytest.raises(ValueError, match="CPSR requires"):
        _profile(status_kind="cpsr", status_offset=None)
