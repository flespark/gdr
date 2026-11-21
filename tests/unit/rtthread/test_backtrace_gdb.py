"""RT-Thread orchestration rejects unverified ports and missing bounds."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

import rtthread.backtrace.backtrace_gdb as bridge
from core.backtrace.unwind import SeedFrame, UnwindFrame, UnwindMetadata, UnwindResult
from core.backtrace.unwind_cfi import CfiTable
from core.elf import ElfImage
from core.stack_frame_parser import StackContextSelection
from rtthread.adapter import Thread
from rtthread.backtrace.backtrace import (
    ARMV7EM_FPU,
    ARMV7EM_INTEGER,
    ARMV7M_INTEGER,
    ARMV8M_MAINLINE_FPU,
    ARMV8M_MAINLINE_INTEGER,
    RISCV32_INTEGER,
    RISCV64_INTEGER,
)
from rtthread.backtrace.scheduler_seed import SchedulerSeed
from rtthread.layout import RT_THREAD_STACK_FILL


@pytest.mark.parametrize(
    "isa,context",
    [
        ("armv8-m.mainline", ARMV8M_MAINLINE_INTEGER),
        ("riscv32", RISCV32_INTEGER),
    ],
)
def test_unverified_context_never_reads_target_stack(monkeypatch, isa, context):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(name="worker", address=0x4000),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection(
            isa,
            "riscv" if isa.startswith("riscv") else "arm",
            replace(context, validation="file-only"),
            "unverified-port",
        ),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unverified context read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.architecture == isa and report.stop_reason == "unverified-port"
    assert report.frames == [] and not report.complete


@pytest.mark.parametrize("base,size", [(0, 1024), (0x2000, 0)])
def test_verified_port_still_needs_task_bounds(monkeypatch, base, size):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(
            name="worker", address=0x4000, stack_addr=base, stack_size=size
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("riscv64", "riscv", RISCV64_INTEGER, "ok"),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unbounded read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.stop_reason == "stack-bounds-unavailable"
    assert report.frames == [] and not report.complete


def test_unverified_port_selection_precedes_stack_watermark_reads(monkeypatch):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(name="worker", address=0x4000),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("armv7-m", "arm", None, "unverified-port"),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("unverified stack read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.stop_reason == "unverified-port"


def test_verified_arm_port_selection_precedes_stack_watermark_reads(monkeypatch):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda *_args, **_kwargs: Thread(name="worker", address=0x4000),
    )
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection(
            "armv8-m.mainline", "arm", None, "unverified-port"
        ),
    )
    monkeypatch.setattr(
        bridge, "read_bytes", lambda *_args: pytest.fail("stack watermark read")
    )
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert report.stop_reason == "unverified-port"


@pytest.mark.parametrize(
    "low_edge,high_edge,expected",
    [
        (bytes([RT_THREAD_STACK_FILL]) * 16, b"\0" * 16, False),
        (b"\0" * 16, bytes([RT_THREAD_STACK_FILL]) * 16, True),
        (b"\0" * 16, b"\0" * 16, None),
    ],
)
def test_stack_direction_inference_reads_only_recorded_edges(
    monkeypatch, low_edge, high_edge, expected
):
    reads = []

    def read(address, size):
        reads.append((address, size))
        return low_edge if address == 0x2000 else high_edge

    monkeypatch.setattr(bridge, "read_bytes", read)
    assert bridge._stack_growth(0x2000, 0x2100) is expected
    assert reads == [(0x2000, 16), (0x20F0, 16)]


def test_symbols_and_source_use_lookup_pc_without_changing_resume_pc(monkeypatch):
    thread = Thread(
        name="worker",
        address=0x4000,
        stack_addr=0x2000,
        stack_size=0x200,
        stack_grows_up=False,
    )
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(bridge, "value_to_thread", lambda *_args, **_kwargs: thread)
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("riscv64", "riscv", RISCV64_INTEGER, "ok"),
    )
    monkeypatch.setattr(bridge, "get_current_thread", lambda: None)
    monkeypatch.setattr(bridge, "eval_identifier", lambda _name: None)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (8, "little"))
    monkeypatch.setattr(
        bridge,
        "decode_backtrace_context",
        lambda *_args, **_kwargs: (SeedFrame(0x1004, 0x2040, {2: 0x2040}), "ok"),
    )
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: None)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda *_args, **_kwargs: UnwindResult(
            (UnwindFrame(0, 0x1004, "seed"), UnwindFrame(1, 0x1100, "cfi", 0x10FF)),
            "cfi-return-address-undefined",
            False,
            False,
            "saved",
            "cfi",
        ),
    )
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda pc: f"function_{pc:x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda pc: f"source_{pc:x}")
    report = bridge.backtrace(SimpleNamespace(layout=object()), "worker")
    assert [frame.pc for frame in report.frames] == [0x1004, 0x1100]
    assert [frame.symbol for frame in report.frames] == [
        "function_1004",
        "function_10ff",
    ]
    assert [frame.source for frame in report.frames] == ["source_1004", "source_10ff"]


def test_saved_pc_setup_reset_and_live_riscv_seed(monkeypatch):
    from core.backtrace.backtrace_gdb import (
        cached_unwind_metadata,
        live_seed,
    )
    from core.backtrace.unwind_arm import (
        arm_ccr_stkalign,
        arm_cpuid,
        arm_external_exception_limit,
        armv6m_vector_entry,
        armv7m_vector_entry,
    )

    adapter = SimpleNamespace(version="4.0.5")
    bridge.reset_adapter(adapter)
    monkeypatch.setattr(bridge, "loaded_object_path", lambda: None)
    assert bridge._backtrace_context().reason == "port-undetermined"
    assert bridge.saved_pc_setup(adapter, False) is None
    assert bridge.saved_pc_setup(adapter, True) is None
    assert bridge._unwind_metadata(adapter) is None

    regs = {"sp": 0x2080, "ra": 0x1004, "s0": 0x2090}
    for index in range(1, 12):
        regs[f"s{index}"] = 0x2000 + index
    frame = SimpleNamespace(
        pc=lambda: 0x1000,
        read_register=lambda name: regs[name],
    )
    fake_gdb = SimpleNamespace(error=RuntimeError, newest_frame=lambda: frame)
    seed = live_seed("riscv", "riscv64", root_pc=0x900, gdb_module=fake_gdb)
    assert seed is not None and seed.pc == 0x1000 and seed.sp == 0x2080

    arm_regs = {f"r{i}": i for i in range(13)} | {
        "sp": 0x2000,
        "lr": 0x1005,
    }
    arm_frame = SimpleNamespace(
        pc=lambda: 0x1001,
        read_register=lambda name: arm_regs[name],
    )
    fake_arm_gdb = SimpleNamespace(error=RuntimeError, newest_frame=lambda: arm_frame)
    arm_seed = live_seed(
        "arm",
        "armv7-a",
        root_pc=0x800,
        require_exception_registers=False,
        gdb_module=fake_arm_gdb,
    )
    assert arm_seed is not None and arm_seed.pc == 0x1000 and arm_seed.sp == 0x2000

    mem = {
        0xE000ED14: (1 << 9).to_bytes(4, "little"),
        0xE000E004: (1).to_bytes(4, "little"),
        0xE000ED00: (0x410CC200).to_bytes(4, "little"),
        0xE000ED08: (0x08000000).to_bytes(4, "little"),
        0x08000000 + 4 * 15: (0x08000101).to_bytes(4, "little"),
        4 * 15: (0x00000201).to_bytes(4, "little"),
    }

    def reader(addr: int, _size: int) -> bytes | None:
        return mem.get(addr)

    assert arm_ccr_stkalign(read=reader) is True
    assert arm_external_exception_limit(read=reader) == 79
    assert arm_cpuid(read=reader) == 0x410CC200
    assert armv6m_vector_entry(15, read=reader) == 0x200
    assert armv7m_vector_entry(15, read=reader) == 0x08000100
    adapter = SimpleNamespace()
    assert cached_unwind_metadata(adapter, path_reader=lambda: None) is None


def _arm_current(monkeypatch, *, pending=0, exception=24, psp=0x2080):
    thread = Thread(
        name="worker",
        address=0x4000,
        entry=0x1200,
        stack_addr=0x2000,
        stack_size=0x400,
        stack_grows_up=False,
    )
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(bridge, "value_to_thread", lambda *_args, **_kwargs: thread)
    monkeypatch.setattr(
        bridge,
        "_backtrace_context",
        lambda: StackContextSelection("armv7-m", "arm", ARMV7M_INTEGER, "ok"),
    )
    monkeypatch.setattr(bridge, "get_current_thread", lambda: 0x4000)
    monkeypatch.setattr(bridge, "value_address", lambda value: value)
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: pending if name == "rt_thread_switch_interrupt_flag" else None,
    )
    monkeypatch.setattr(bridge, "read_int", lambda value: value)
    seed = SeedFrame(
        0x1100,
        0x3080,
        {13: 0x3080, 14: 0xFFFFFFFD},
        source="current",
        process_sp=psp,
        exception_number=exception,
    )
    monkeypatch.setattr(bridge, "_live_seed_detail", lambda *_args: (seed, "ok"))
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: None)
    symbols = {"__StackLimit": 0x3000, "__StackTop": 0x3400}
    words = {0xE000ED08: 0x1000, 0xE000E004: 0, 0xE000ED14: 1 << 9, 0x1060: 0x1101}
    monkeypatch.setattr(bridge, "linker_symbol_address", symbols.get)
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, size: words.get(address, 0).to_bytes(size, "little"),
    )
    return SimpleNamespace(layout=object())


@pytest.mark.parametrize(
    "pending,reason",
    [(None, "scheduler-state-unavailable"), (1, "scheduler-profile-unverified")],
)
def test_pending_arm_switch_cannot_select_a_task_seed(monkeypatch, pending, reason):
    adapter = _arm_current(monkeypatch, pending=pending)
    monkeypatch.setattr(
        bridge, "_live_seed_detail", lambda *_args: pytest.fail("unstable owner read")
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == reason and report.frames == []


@pytest.mark.parametrize(
    "exception,psp,reason",
    [
        (24, 0x2500, "exception-task-context-conflict"),
        (1, 0x2080, "exception-number-invalid"),
        (14, 0x2080, "scheduler-transition-unproven"),
    ],
)
def test_arm_irq_ownership_and_exception_policy_precede_unwinding(
    monkeypatch, exception, psp, reason
):
    adapter = _arm_current(monkeypatch, exception=exception, psp=psp)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda *_args, **_kwargs: pytest.fail("unproven exception unwind"),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.stop_reason == reason and report.frames == []


def test_arm_irq_supplies_shared_segments_hooks_and_vector_entry_proof(monkeypatch):
    adapter = _arm_current(monkeypatch)
    seen = {}

    def unwind(seed, metadata, **kwargs):
        assert metadata is None and seed.at_exception_entry
        seen.update(kwargs)
        return UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "max-depth",
            False,
            True,
            "current",
            "seed",
        )

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    report = bridge.backtrace(adapter, "worker")
    assert report.truncated and seen["current_segment"] == "exception:24"
    assert [
        (segment.kind, segment.low, segment.high) for segment in seen["segments"]
    ] == [
        ("task", 0x2000, 0x2400),
        ("exception", 0x3000, 0x3400),
    ]
    assert seen["exception_detector"](0xFFFFFFFD)
    assert callable(seen["exception_hook"]) and callable(seen["exception_guard"])
    assert seen["stkalign"] is True


def test_arm_systick_tick_is_admitted_as_a_proven_periodic_interrupt(monkeypatch):
    adapter = _arm_current(monkeypatch, exception=15)
    seen = {}

    def unwind(seed, _metadata, **kwargs):
        seen.update(kwargs)
        return UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "max-depth",
            False,
            True,
            "current",
            "seed",
        )

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    report = bridge.backtrace(adapter, "worker")
    assert report.truncated and seen["current_segment"] == "exception:15"
    assert callable(seen["exception_guard"])
    assert seen["exception_guard"](15, 0x1000) is None
    assert seen["exception_guard"](11, 0x1000) == "scheduler-transition-unproven"
    assert seen["exception_guard"](4, 0x1000) == "exception-frame-type-unproven"


@pytest.mark.parametrize(
    "number,reason",
    [
        (2, None),
        (12, None),
        (15, None),
        (24, None),
        (11, "scheduler-transition-unproven"),
        (14, "scheduler-transition-unproven"),
        (3, "exception-frame-type-unproven"),
        (1, "exception-number-invalid"),
    ],
)
def test_systick_aware_guard_admits_system_and_external_exceptions(number, reason):
    assert bridge._systick_aware_guard(number, 0x1000) == reason
    assert bridge._systick_aware_guard(12, 0x1000, "armv6-m") == (
        "exception-number-invalid"
    )
    assert bridge._systick_aware_guard(2, 0x1000, "armv6-m") is None


@pytest.mark.parametrize(
    "number,limit,reason",
    [
        (15, 15, None),
        (24, 47, None),
        (48, 47, "exception-number-unimplemented"),
        (11, 47, "scheduler-transition-unproven"),
    ],
)
def test_systick_capacity_guard_keeps_external_bounds(number, limit, reason):
    assert bridge._systick_capacity_guard(limit, "armv7-m")(number, 0x1000) == reason


@pytest.mark.parametrize(
    "control,psplim,tz,reason",
    [
        (2, 0, 0, "ok"),
        (6, 0, 0, "unsupported-extension-context"),
        (3, 0, 0, "unsupported-extension-context"),
        (2, 0x2000, 0, "unsupported-extension-context"),
        (2, 0, 1, "unsupported-extension-context"),
        (2, 0, None, "port-configuration-unavailable"),
    ],
)
def test_armv8m_current_seed_requires_integer_single_security_context(
    monkeypatch, control, psplim, tz, reason
):
    seed = SeedFrame(0x1000, 0x2080, {13: 0x2080}, source="current", process_sp=0x2080)
    monkeypatch.setattr(
        bridge, "_core_live_seed_detail", lambda *_args, **_kwargs: (seed, "ok")
    )
    frame = SimpleNamespace(
        read_register=lambda name: {"control": control, "psplim": psplim}[name]
    )
    monkeypatch.setattr(
        bridge, "gdb", SimpleNamespace(newest_frame=lambda: frame, error=RuntimeError)
    )
    monkeypatch.setattr(bridge, "eval_identifier", lambda name: name)
    monkeypatch.setattr(bridge, "read_int", lambda _value: tz)
    result, actual = bridge._live_seed_detail("arm", "armv8-m.mainline", None)
    assert actual == reason
    assert (result is not None) == (reason == "ok")


@pytest.mark.parametrize(
    "exc,sp,psp,psplim,reason",
    [
        (0, 0x2080, 0x2080, 0x2000, "ok"),
        (0, 0x2080, 0x2080, 0x2080, "ok"),
        (0, 0x2080, 0x2080, 0x2004, "unsupported-extension-context"),
        (0, 0x2080, 0x2080, 0x1FF8, "unsupported-extension-context"),
        (0, 0x2080, 0x2080, 0x2088, "unsupported-extension-context"),
        (19, 0x3000, 0x2080, 0x2000, "ok"),
        (19, 0x3000, 0x2080, 0x2088, "unsupported-extension-context"),
        (19, 0x3000, 0x2080, 0x2004, "unsupported-extension-context"),
    ],
)
def test_armv8m_current_seed_admits_in_bounds_aligned_psplim(
    monkeypatch, exc, sp, psp, psplim, reason
):
    seed = SeedFrame(
        0x1000,
        sp,
        {13: sp},
        source="current",
        exception_number=exc,
        process_sp=psp,
    )
    monkeypatch.setattr(
        bridge, "_core_live_seed_detail", lambda *_args, **_kwargs: (seed, "ok")
    )
    frame = SimpleNamespace(
        read_register=lambda name: {"control": 2, "psplim": psplim}[name]
    )
    monkeypatch.setattr(
        bridge, "gdb", SimpleNamespace(newest_frame=lambda: frame, error=RuntimeError)
    )
    monkeypatch.setattr(bridge, "eval_identifier", lambda name: name)
    monkeypatch.setattr(bridge, "read_int", lambda _value: 0)
    result, actual = bridge._live_seed_detail(
        "arm", "armv8-m.mainline", None, 0x2000, 0x2400
    )
    assert actual == reason
    assert (result is not None) == (reason == "ok")


def test_armv8m_missing_extension_registers_refuse_the_live_seed(monkeypatch):
    seed = SeedFrame(0x1000, 0x2080, {13: 0x2080}, source="current", process_sp=0x2080)
    monkeypatch.setattr(
        bridge, "_core_live_seed_detail", lambda *_args, **_kwargs: (seed, "ok")
    )

    def read(_name):
        raise RuntimeError("unavailable")

    frame = SimpleNamespace(read_register=read)
    monkeypatch.setattr(
        bridge, "gdb", SimpleNamespace(newest_frame=lambda: frame, error=RuntimeError)
    )
    assert bridge._live_seed_detail("arm", "armv8-m.mainline", None) == (
        None,
        "live-registers-unavailable",
    )


class _Words(dict):
    def read(self, address: int, size: int) -> bytes | None:
        values = []
        for offset in range(0, size, 4):
            value = self.get(address + offset)
            if value is None:
                return None
            values.append(value.to_bytes(4, "little"))
        return b"".join(values)


def _armv7em_pair_selection():
    basic = replace(ARMV7EM_INTEGER, validation="live")
    fpu = replace(ARMV7EM_FPU, validation="live")
    return StackContextSelection(
        "armv7e-m", "arm", basic, "ok", ("test",), "aapcs32", fpu
    )


def _armv8m_mainline_pair_selection():
    basic = replace(ARMV8M_MAINLINE_INTEGER, validation="live")
    fpu = replace(ARMV8M_MAINLINE_FPU, validation="live")
    return StackContextSelection(
        "armv8-m.mainline", "arm", basic, "ok", ("test",), "aapcs32", fpu
    )


def _saved_report(monkeypatch, thread, selection, memory):
    monkeypatch.setattr(bridge, "find_thread", lambda *_args: object())
    monkeypatch.setattr(bridge, "value_to_thread", lambda *_args, **_kwargs: thread)
    monkeypatch.setattr(bridge, "_backtrace_context", lambda: selection)
    monkeypatch.setattr(bridge, "get_current_thread", lambda: None)
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: 0 if name == "rt_thread_switch_interrupt_flag" else None,
    )
    monkeypatch.setattr(bridge, "read_int", lambda value: value)
    monkeypatch.setattr(bridge, "read_bytes", memory.read)
    monkeypatch.setattr(bridge, "arch_or_default", lambda: (4, "little"))
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: None)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    seeds = []

    def unwind(seed, _metadata, **_kwargs):
        seeds.append(seed)
        return UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "max-depth",
            False,
            False,
            seed.source,
            "seed",
        )

    monkeypatch.setattr(bridge, "unwind_chain", unwind)
    return bridge.backtrace(SimpleNamespace(layout=object()), "worker"), seeds


def _thread_at(top):
    return Thread(
        name="worker",
        address=0x4000,
        entry=0x1000,
        stack_addr=0x2000,
        stack_size=0x2000,
        stack_grows_up=False,
        sp=top,
    )


def test_armv7em_wiring_dispatches_fpu_by_flag(monkeypatch):
    top = 0x3000
    memory = _Words()
    memory[top] = 1
    for index in range(8):
        memory[top + 4 + index * 4] = 0x40 + index
    for index in range(16):
        memory[top + 36 + index * 4] = 0x55000000 + index
    memory[top + 100] = 0x123
    memory[top + 104] = 0
    memory[top + 108] = 0
    memory[top + 112] = 0
    memory[top + 116] = 0
    memory[top + 120] = 0x1331
    memory[top + 124] = 0x1235
    memory[top + 128] = 0x01000000
    thread = _thread_at(top)
    report, seeds = _saved_report(
        monkeypatch, thread, _armv7em_pair_selection(), memory
    )
    assert len(seeds) == 1
    assert seeds[0].sp == top + 204
    assert seeds[0].source == "saved"
    assert [frame.pc for frame in report.frames] == [0x1234]


def test_armv7em_wiring_refuses_foreign_flag(monkeypatch):
    top = 0x3000
    memory = _Words()
    memory[top] = 2
    thread = _thread_at(top)
    report, seeds = _saved_report(
        monkeypatch, thread, _armv7em_pair_selection(), memory
    )
    assert seeds == []
    assert report.stop_reason == "unsupported-extension-context"
    assert report.frames == []


def test_armv8m_mainline_wiring_dispatches_fpu_by_exc_return(monkeypatch):
    top = 0x3000
    memory = _Words()
    memory[top] = 0
    memory[top + 4] = 0xFFFFFFED
    memory[top + 8] = 0
    memory[top + 12] = 0
    for index in range(8):
        memory[top + 16 + index * 4] = 0x40 + index
    for index in range(16):
        memory[top + 48 + index * 4] = 0x55000000 + index
    memory[top + 112] = 0x123
    memory[top + 116] = 0
    memory[top + 120] = 0
    memory[top + 124] = 0
    memory[top + 128] = 0
    memory[top + 132] = 0x1331
    memory[top + 136] = 0x1235
    memory[top + 140] = 0x01000000
    thread = _thread_at(top)
    report, seeds = _saved_report(
        monkeypatch, thread, _armv8m_mainline_pair_selection(), memory
    )
    assert len(seeds) == 1
    assert seeds[0].sp == top + 216
    assert seeds[0].source == "saved"
    assert [frame.pc for frame in report.frames] == [0x1234]


def test_armv8m_mainline_wiring_refuses_secure_header(monkeypatch):
    top = 0x3000
    memory = _Words()
    memory[top] = 0
    memory[top + 4] = 0xFFFFFFBC
    memory[top + 8] = 0
    memory[top + 12] = 0
    thread = _thread_at(top)
    report, seeds = _saved_report(
        monkeypatch, thread, _armv8m_mainline_pair_selection(), memory
    )
    assert seeds == []
    assert report.stop_reason == "unsupported-extension-context"
    assert report.frames == []


@pytest.mark.parametrize(
    "complete,raw_reason,expected_reason",
    [
        (True, "task-exit", "synchronous-fault"),
        (False, "max-depth", "max-depth"),
    ],
)
def test_arm_precise_fault_preserves_failure_or_marks_synchronous_fault(
    monkeypatch, complete, raw_reason, expected_reason
):
    adapter = _arm_current(monkeypatch, exception=6)
    words = {
        0xE000ED08: 0x1000,
        0xE000E004: 0,
        0xE000ED14: 1 << 9,
        0xE000ED28: 0x00010000,
        0xE000ED2C: 0,
    }
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, size: words.get(address, 0).to_bytes(size, "little"),
    )
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _pc: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda seed, _metadata, **kwargs: UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            raw_reason,
            complete,
            not complete,
            "current",
            "seed",
            segments=kwargs["segments"],
        ),
    )
    report = bridge.backtrace(adapter, "worker")
    assert report.complete is complete
    assert report.stop_reason == expected_reason
    assert len(report.segments) == 2
    assert any("synchronous fault UsageFault:UNDEFINSTR" in m for m in report.messages)


def test_apply_hardfault_trampoline_recovers_psp_software_frame_and_refuses_bad_marker(
    monkeypatch,
):
    from core.backtrace.unwind import UnwindMetadata
    from core.backtrace.unwind_segments import (
        ExceptionTransitionContext,
        ExceptionTransitionResult,
        StackSegment,
    )
    from rtthread.stack_frame_profile import HardFaultTrampolineProfile

    profile = HardFaultTrampolineProfile(
        start=0x5ADC,
        end=0x5B12,
        msr_psp_after_pc=0x5AFC,
        push_lr_after_pc=0x5B04,
        pop_lr_after_pc=0x5B0C,
        software_bytes=36,
        r4_offset=4,
    )
    monkeypatch.setattr(bridge, "analyze_hardfault_trampoline", lambda _elf: profile)
    meta = UnwindMetadata(
        elf=cast(ElfImage, SimpleNamespace(pointer_size=4, endian="little")),
        arch="arm",
        cfi_tables=(),
        ehabi=None,
        return_register=14,
        frame_pointer_register=7,
    )
    seed = SeedFrame(
        0x58D8,
        0x20003FD0,
        {13: 0x20003FD0, 14: 0x5B09},
        source="current",
        process_sp=0x20001000,
        exception_number=3,
    )
    task_seg = StackSegment(
        ident="task:0x100",
        owner="worker",
        low=0x20000800,
        high=0x20001800,
        source="task-recorded",
        stack="process",
        kind="task",
    )
    exc_seg = StackSegment(
        ident="exception:3",
        owner="exception:3",
        low=0x20003000,
        high=0x20004000,
        source="linker",
        stack="main",
        kind="exception",
    )
    captured = []

    def base_hook(ctx: ExceptionTransitionContext):
        captured.append(ctx)
        assert ctx.process_sp is not None
        return ExceptionTransitionResult(
            segment=task_seg,
            pc=0x1234,
            sp=ctx.process_sp + 32,
            registers=ctx.registers,
            method="exception",
            source="armv7m-exception-frame",
            value=ctx.value,
            bytes_read=32,
            evidence="basic",
        )

    updated_meta, wrapped = bridge._apply_hardfault_trampoline(
        seed, meta, "exception:3", base_hook
    )
    assert updated_meta is not None and len(updated_meta.cfi_tables) == 1
    row_pushed = updated_meta.cfi_tables[0].row_for(0x5B07)
    assert row_pushed is not None and row_pushed.cfa.offset == 4
    row_unpushed = updated_meta.cfi_tables[0].row_for(0x5AE0)
    assert row_unpushed is not None and row_unpushed.cfa.offset == 0

    sw_words = [0xFFFFFFFD, *(0x40 + i for i in range(8))]
    sw_bytes = b"".join(w.to_bytes(4, "little") for w in sw_words)
    ctx = ExceptionTransitionContext(
        value=0xFFFFFFFD,
        registers={14: 0xFFFFFFFD},
        frame_sp=0x20003FE0,
        current_segment=exc_seg,
        segments={"task:0x100": task_seg, "exception:3": exc_seg},
        process_sp=0x20001000,
        read=lambda addr, size: sw_bytes if addr == 0x20001000 and size == 36 else None,
        stkalign=True,
        remaining_exception_bytes=256,
    )
    res = wrapped(ctx)
    assert isinstance(res, ExceptionTransitionResult)
    assert res.bytes_read == 68
    assert res.sp == 0x20001000 + 36 + 32
    assert res.registers[4] == 0x40 and res.registers[11] == 0x47
    assert "rtthread-hardfault-software-frame=36B" in res.evidence

    bad_marker = b"\x00\x00\x00\x00" + sw_bytes[4:]
    assert (
        wrapped(replace(ctx, read=lambda _a, _n: bad_marker))
        == "exception-context-unproven"
    )
    assert (
        wrapped(replace(ctx, remaining_exception_bytes=16))
        == "exception-read-budget-exceeded"
    )
    assert (
        wrapped(replace(ctx, process_sp=0x200007F0)) == "exception-frame-outside-bounds"
    )


def test_apply_switch_return_cfi_adds_lr_return_only_for_switch_return():
    meta = UnwindMetadata(
        elf=cast(ElfImage, SimpleNamespace(pointer_size=4, endian="little")),
        arch="arm",
        cfi_tables=(),
        ehabi=None,
        return_register=14,
        frame_pointer_register=7,
    )
    seed = SeedFrame(0x5B2B, 0x20001000, {13: 0x20001000, 14: 0x1235}, source="current")
    switch_dec = cast(SchedulerSeed, SimpleNamespace(stage="switch-return", seed=seed))
    updated = bridge._apply_switch_return_cfi(meta, switch_dec)
    assert updated is not None and len(updated.cfi_tables) == 1
    row = updated.cfi_tables[0].row_for(0x5B2A)
    assert row is not None and row.cfa.register == 13 and row.cfa.offset == 0
    assert row.return_register == 14

    other_dec = cast(SchedulerSeed, SimpleNamespace(stage="pendsv-entry", seed=seed))
    assert bridge._apply_switch_return_cfi(meta, other_dec) is meta


def test_current_pc_backtrace_tracks_outgoing_pendsv_and_boot(monkeypatch):
    from core.backtrace.unwind import UnwindFrame, UnwindResult
    from core.backtrace.unwind_arm import ArmSnapshot
    from rtthread.backtrace.arm_scheduler_switch import (
        SchedulerProfile,
        SchedulerStage,
    )

    adapter = _arm_current(monkeypatch, exception=0)
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda pc: f"fn_{pc:#x}")
    monkeypatch.setattr(bridge, "source_location_at", lambda _pc: None)
    monkeypatch.setattr(
        bridge,
        "unwind_chain",
        lambda seed, _metadata, **_kwargs: UnwindResult(
            (UnwindFrame(0, seed.pc, "seed"),),
            "task-exit",
            True,
            False,
            seed.source,
            "seed",
        ),
    )
    # Normal current thread.
    report = bridge.backtrace(adapter)
    assert report.task == "worker" and report.source == "current" and report.complete

    # During PendSV early save window, omitted target picks rt_interrupt_from_thread (old).
    old_val = object()
    old_thread = SimpleNamespace(
        name="outgoing",
        address=0x5000,
        sp=0x20005000,
        entry=0x1100,
        stack_addr=0x20005000,
        stack_size=0x400,
        stack_grows_up=False,
        oncpu=None,
    )
    from core.backtrace.unwind_cfi import CfaRule, CfiRow, RegisterRule
    from core.elf import EM_ARM, CodeSymbol, ElfImage, ElfSection

    boot_row = CfiRow(
        cfa=CfaRule(13, 4),
        registers={14: RegisterRule("offset", -4)},
        return_register=14,
    )
    meta = UnwindMetadata(
        elf=ElfImage(
            pointer_size=4,
            endian="little",
            machine=EM_ARM,
            sections=(
                ElfSection(
                    name=".text",
                    address=0x1000,
                    data=b"\x70\x47\x00\x00\x70\x47",
                    flags=0x6,
                    section_type=1,
                ),
            ),
            symbols=(CodeSymbol("boot_leaf", 0x1004, 2),),
        ),
        arch="arm",
        cfi_tables=(
            cast(CfiTable, SimpleNamespace(row_for=lambda _pc: boot_row, error=None)),
        ),
        ehabi=None,
        return_register=14,
        frame_pointer_register=7,
    )
    profile = SchedulerProfile(
        port="armv7-m",
        isa="armv7-m",
        fpu=False,
        switch_start=0x3000,
        switch_end=0x3010,
        switch_return=0x300E,
        pendsv_start=0x3100,
        pendsv_end=0x3140,
        switch_to_start=0x3140,
        switch_to_end=0x3160,
        pendsv_entry=0x3100,
        pendsv_save_pending=0x3110,
        pendsv_save_written=0x3120,
        pendsv_switch_to=0x3124,
        pendsv_saved_sp=0x3128,
        pendsv_restored_pending_psp=0x3130,
        pendsv_psp_written=0x3134,
        pendsv_exception_return=0x313C,
        flag_symbol=0x20000100,
        from_symbol=0x20000104,
        to_symbol=0x20000108,
        trustzone_symbol=None,
        stages={0x3100: SchedulerStage("pendsv-entry", "PendSV_Handler", "entry")},
    )
    monkeypatch.setattr(bridge, "_unwind_metadata", lambda _adapter: meta)
    monkeypatch.setattr(bridge, "build_scheduler_profile", lambda _elf: profile)
    monkeypatch.setattr(bridge, "member_offset", lambda _t, _f: 8)
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: {
            "rt_thread_switch_interrupt_flag": 1,
            "rt_interrupt_from_thread": 0x5008,
        }.get(name),
    )
    monkeypatch.setattr(bridge, "read_int", lambda val: val)
    monkeypatch.setattr(
        bridge,
        "find_thread",
        lambda target, _layout: old_val if target == "0x5000" else None,
    )
    orig_v2t = bridge.value_to_thread
    monkeypatch.setattr(
        bridge,
        "value_to_thread",
        lambda v, *args, **kwargs: (
            old_thread if v is old_val else orig_v2t(v, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_arm_snapshot",
        lambda: (
            ArmSnapshot(
                0x3100,
                0x203FF000,
                0x203FF000,
                0x20004000,
                0x0100000E,
                {index: index for index in range(16)},
            ),
            "ok",
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_scheduler_decision",
        lambda **_kwargs: bridge.SchedulerSeed(
            SeedFrame(0x1234, 0x20005100, {}, source="current"),
            "pendsv-entry",
            "old",
            "entry",
        ),
    )
    pendsv_report = bridge.backtrace(adapter)
    assert pendsv_report.task == "outgoing"
    assert any(
        "scheduler stage pendsv-entry (old)" in m for m in pendsv_report.messages
    )

    # Pre-scheduler boot unwind when rt_current_thread is None.
    monkeypatch.setattr(bridge, "get_current_thread", lambda: None)
    monkeypatch.setattr(
        bridge,
        "eval_identifier",
        lambda name: 0 if name == "rt_thread_switch_interrupt_flag" else None,
    )
    boot_regs = {index: index for index in range(13)} | {
        13: 0x20001FE0,
        14: 0x0,
        15: 0x1004,
    }
    monkeypatch.setattr(
        bridge,
        "read_bytes",
        lambda address, size: (
            b"\x00\x00\x00\x00" if address == 0x20001FE0 and size == 4 else None
        ),
    )
    monkeypatch.setattr(
        bridge,
        "_arm_snapshot",
        lambda: (
            ArmSnapshot(0x1004, 0x20001FE0, 0x20001FE0, 0, 0x01000000, boot_regs),
            "ok",
        ),
    )
    monkeypatch.setattr(
        bridge,
        "linker_symbol_address",
        lambda name: {"_sstack": 0x20001000, "_estack": 0x20002000}.get(name),
    )
    boot = bridge.backtrace(adapter)
    assert boot.task == "<current>"
    assert [frame.pc for frame in boot.frames] == [0x1004]
    assert boot.stop_reason == "return-address-zero"
