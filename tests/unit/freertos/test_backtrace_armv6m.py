"""FreeRTOS ARM_CM0 exception integration acceptance, separate from ISA rules."""

import struct

import pytest

import freertos.backtrace_gdb as bridge
from core.unwind import UnwindResult
from core.unwind_arm import armv6m_exception_transition
from core.unwind_segments import ExceptionTransitionContext
from tests.unit.freertos.test_backtrace_gdb import (
    _handler_session,
    _live_gdb,
    _LiveFrame,
)


def _port(monkeypatch, *, version="11.1.0", cpuid=0x410CC200, **symbols):
    values = {
        "g_pfnVectors": 0,
        "__gdr_vector_table_end": 192,
        "__gdr_armv6m_irq_count": 32,
        **symbols,
    }
    reads = []

    def read(address, size):
        assert (address, size) == (0xE000ED00, 4)
        reads.append((address, size))
        return cpuid.to_bytes(4, "little") if isinstance(cpuid, int) else cpuid

    monkeypatch.setattr(bridge, "session_meta", lambda: ("freertos", version))
    monkeypatch.setattr(bridge, "read_bytes", read)
    monkeypatch.setattr(bridge, "linker_symbol_address", values.get)
    return reads


@pytest.mark.parametrize("cpuid", [0x410CC200, 0x411CC201])
@pytest.mark.parametrize("version", ["10.3.1", "11.1.0"])
def test_m0_capacity_requires_fixed_zero_vectors_and_explicit_count(
    monkeypatch, cpuid, version
):
    reads = _port(monkeypatch, version=version, cpuid=cpuid)
    assert bridge._armv6m_port_limit() == 47
    assert reads == [(0xE000ED00, 4)]


@pytest.mark.parametrize(
    "changes",
    [
        {"cpuid": None},
        {"cpuid": b"bad"},
        {"cpuid": 0x410FC231},
        {"cpuid": 0x410CC600},
        {"g_pfnVectors": None},
        {"g_pfnVectors": 0x100},
        {"__gdr_vector_table_end": None},
        {"__gdr_vector_table_end": 188},
        {"__gdr_armv6m_irq_count": None},
        {"__gdr_armv6m_irq_count": 0},
        {"__gdr_armv6m_irq_count": 31},
        {"__gdr_armv6m_irq_count": 33},
    ],
)
def test_unproven_m0_port_evidence_fails_closed(monkeypatch, changes):
    _port(monkeypatch, **changes)
    assert bridge._armv6m_port_limit() is None


@pytest.mark.parametrize("ipsr", [0, 1, 4, 6, 12, 48, 511, -1])
def test_invalid_fixed_vector_indices_do_not_read_memory(monkeypatch, ipsr):
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_: pytest.fail("invalid vector read")
    )
    assert bridge._armv6m_vector_entry(ipsr) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        (b"bad", None),
        (b"\x00\x10\x00\x00", None),
        (b"\x01\x10\x00\x00", 0x1000),
    ],
)
def test_fixed_vector_requires_readable_thumb_target(monkeypatch, raw, expected):
    def read(address, size):
        assert (address, size) == (24 * 4, 4)
        return raw

    monkeypatch.setattr(bridge, "read_bytes", read)
    assert bridge._armv6m_vector_entry(24) == expected


@pytest.mark.parametrize("ipsr", [1, 4, 5, 6, 7, 12, 13, 64, 511])
def test_m0_live_seed_rejects_reserved_ipsr(monkeypatch, ipsr):
    frame = _LiveFrame(sp=0x3000, msp=0x3000, psp=0x2100, xpsr=0x01000000 | ipsr)
    monkeypatch.setattr(bridge, "gdb", _live_gdb(frame))
    seed, reason = bridge._live_seed_detail("arm", "armv6-m")
    assert seed is None and reason == "exception-number-invalid"


@pytest.mark.parametrize(
    "ipsr,reason",
    [
        (11, "scheduler-transition-unproven"),
        (14, "scheduler-transition-unproven"),
        (3, "exception-frame-type-unproven"),
        (2, "exception-number-unverified"),
        (15, "exception-number-unverified"),
        (4, "exception-number-invalid"),
        (48, "exception-number-invalid"),
        (63, "exception-number-invalid"),
        (64, "exception-number-invalid"),
    ],
)
def test_m0_policy_refuses_unverified_handlers_before_unwind(monkeypatch, ipsr, reason):
    adapter, _ = _handler_session(monkeypatch, isa="armv6-m", exception_number=ipsr)
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x203FC000, 0x20400000))
    monkeypatch.setattr(bridge, "_armv6m_port_limit", lambda: 47)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == reason and report.frames == []


@pytest.mark.parametrize(
    "state,bounds,limit,reason",
    [
        (None, (0x203FC000, 0x20400000), 47, "scheduler-state-unavailable"),
        (False, (0x203FC000, 0x20400000), 47, "scheduler-not-running"),
        (True, None, 47, "exception-stack-bounds-unavailable"),
        (True, (0x1000, 0x2000), 47, "exception-stack-outside-bounds"),
        (True, (0x203FC000, 0x20400000), None, "exception-capacity-unavailable"),
    ],
)
def test_m0_missing_ownership_proof_never_unwinds(
    monkeypatch, state, bounds, limit, reason
):
    adapter, _ = _handler_session(monkeypatch, isa="armv6-m")
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: state)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: bounds)
    monkeypatch.setattr(bridge, "_armv6m_port_limit", lambda: limit)
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == reason and report.frames == []


def test_m0_live_routing_uses_own_hook_alignment_and_vector_proof(monkeypatch):
    adapter, _ = _handler_session(monkeypatch, isa="armv6-m")
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x203FC000, 0x20400000))
    monkeypatch.setattr(bridge, "_armv6m_port_limit", lambda: 47)
    monkeypatch.setattr(bridge, "_armv6m_vector_entry", lambda _: 0x1004)
    for name in ("_ccr_stkalign", "_external_exception_limit", "_vector_entry"):
        monkeypatch.setattr(bridge, name, lambda *_: pytest.fail("v7-M register path"))

    def unwind(seed, _metadata, **kwargs):
        assert seed.at_exception_entry
        assert kwargs["exception_hook"] is armv6m_exception_transition
        assert kwargs["stkalign"] is True
        guard = kwargs["exception_guard"]
        assert guard(24, 0x1000) is None
        assert guard(48, 0x1000) == "exception-number-invalid"
        assert guard(63, 0x1000) == "exception-number-invalid"
        assert guard(64, 0x1000) == "exception-number-invalid"
        assert guard(14, 0x1000) == "scheduler-transition-unproven"
        return UnwindResult((), "no-unwind-info", False, False, "current", "seed")

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    report = bridge.backtrace(adapter, "worker")
    assert report.architecture == "armv6-m"
    assert report.stop_reason == "no-unwind-info"


@pytest.mark.parametrize(
    "ipsr,reason",
    [
        (4, "exception-number-invalid"),
        (48, "exception-number-invalid"),
        (63, "exception-number-invalid"),
        (64, "exception-number-invalid"),
        (11, "scheduler-transition-unproven"),
        (14, "scheduler-transition-unproven"),
        (3, "exception-frame-type-unproven"),
        (2, "exception-number-unverified"),
        (15, "exception-number-unverified"),
        (24, None),
        (25, None),
    ],
)
def test_installed_m0_hook_checks_restored_handler_policy(monkeypatch, ipsr, reason):
    adapter, _ = _handler_session(monkeypatch, isa="armv6-m")
    monkeypatch.setattr(bridge, "_scheduler_running", lambda: True)
    monkeypatch.setattr(bridge, "_irq_stack_bounds", lambda: (0x203FC000, 0x20400000))
    monkeypatch.setattr(bridge, "_armv6m_port_limit", lambda: 47)
    monkeypatch.setattr(bridge, "_armv6m_vector_entry", lambda _: None)

    def unwind(_seed, _metadata, **kwargs):
        segments = {segment.ident: segment for segment in kwargs["segments"]}
        current = segments[kwargs["current_segment"]]
        raw = struct.pack("<8I", 0, 1, 2, 3, 12, 0xFFFFFFFD, 0x1008, 0x01000000 | ipsr)

        def read(address, size):
            assert (address, size) == (0x203FFF00, 32)
            return raw

        context = ExceptionTransitionContext(
            value=0xFFFFFFF1,
            registers={},
            frame_sp=0x203FFF00,
            current_segment=current,
            segments=segments,
            process_sp=0x2100,
            read=read,
            stkalign=kwargs["stkalign"],
            remaining_exception_bytes=256,
            guard=kwargs["exception_guard"],
        )
        result = kwargs["exception_hook"](context)
        if reason is not None:
            assert result == reason
        else:
            assert result is not None and not isinstance(result, str)
            assert result.segment.ident == f"exception:{ipsr}"
            assert result.pc == 0x1008 and result.exact_pc
        return UnwindResult((), "no-unwind-info", False, False, "current", "seed")

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    assert bridge.backtrace(adapter, "worker").stop_reason == "no-unwind-info"
