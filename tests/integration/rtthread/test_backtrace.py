"""Closed-loop thread, FPU, IRQ, SysTick, scheduler, fault, and RISC-V backtraces."""

from __future__ import annotations

import contextlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from core.elf import ElfImage
from core.insn_thumb import decode_thumb
from rtthread.backtrace.arm_scheduler_switch import (
    SchedulerProfile,
    build_profile,
)
from rtthread.backtrace.riscv_scheduler_switch import (
    RiscvSchedulerProfile,
    build_riscv_profile,
)
from tests.support.gdb_process import has_embedded_python
from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import GdbSession, QemuSession, check_tools
from tests.support.rtthread_qemu_profiles import get_rtthread_qemu_profile

SPEC = load_integration_spec()
GDR_ROOT = Path(__file__).resolve().parents[3]

_ISA_BY_TARGET = {
    "rv64": "riscv64",
    "rv32": "riscv32",
    "mps2-an385": "armv7-m",
    "mps2-an386": "armv7e-m",
    "mps2-an505": "armv8-m.mainline",
    "cortex-a9": "armv7-a",
    "microbit": "armv6-m",
}
_ISA = _ISA_BY_TARGET.get(SPEC.target, "")
_M_PROFILE_TARGETS = {"microbit", "mps2-an385", "mps2-an386", "mps2-an505"}
_TIMER0_EXC, _TIMER1_EXC = (19, 20) if SPEC.target == "mps2-an505" else (24, 25)
_EXPECTED_BFAR = 0xA0000000 if SPEC.target == "mps2-an505" else 0x50000000
_PROFILE_CACHE: SchedulerProfile | None = None
_RV_PROFILE_CACHE: RiscvSchedulerProfile | None = None

pytestmark = pytest.mark.skipif(
    SPEC.rtos != "rtthread",
    reason="requires an RT-Thread fixture lane",
)


def _saved_skip():
    return pytest.mark.skipif(
        SPEC.target not in _ISA_BY_TARGET
        or (SPEC.target == "cortex-a9" and SPEC.version.startswith("3.")),
        reason="requires an RT-Thread lane with a verified saved-context layout",
    )


def _bt_fixture_skip():
    return pytest.mark.skipif(
        SPEC.target not in _ISA_BY_TARGET
        or (SPEC.target == "cortex-a9" and SPEC.version not in {"4.0.4", "4.0.5"}),
        reason="requires an RT-Thread lane with the extended backtrace fixture",
    )


def _fpu_skip(*targets: str):
    allowed = targets or ("mps2-an386", "mps2-an505")
    return pytest.mark.skipif(
        SPEC.variant not in {"fpu", "softfp"} or SPEC.target not in allowed,
        reason=f"requires RT-Thread FPU fixture on {allowed}",
    )


def _irq_skip(*, fpu_only: bool = False):
    invalid_an386 = SPEC.target == "mps2-an386" and SPEC.variant not in {
        "fpu",
        "soft",
        "softfp",
    }
    invalid_an505 = SPEC.target == "mps2-an505" and SPEC.variant not in {"", "fpu"}
    return pytest.mark.skipif(
        SPEC.target not in _M_PROFILE_TARGETS
        or invalid_an386
        or invalid_an505
        or (fpu_only and SPEC.variant not in {"fpu", "softfp"}),
        reason="requires an RT-Thread Arm M-profile IRQ fixture",
    )


def _systick_skip():
    return pytest.mark.skipif(
        SPEC.target not in {"mps2-an385", "mps2-an386", "mps2-an505"}
        or (
            SPEC.target == "mps2-an386"
            and SPEC.variant not in {"fpu", "soft", "softfp"}
        ),
        reason="requires an RT-Thread SysTick-driven M-profile lane",
    )


def _sched_skip():
    return pytest.mark.skipif(
        SPEC.target not in _M_PROFILE_TARGETS
        or SPEC.variant not in {"", "fpu", "soft", "softfp"}
        or (
            SPEC.target == "mps2-an386"
            and SPEC.variant not in {"fpu", "soft", "softfp"}
        ),
        reason="requires an RT-Thread Arm M-profile scheduler fixture lane",
    )


def _fault_skip(*, memmanage: bool = False):
    targets = (
        {"mps2-an385", "mps2-an386"}
        if memmanage
        else {"mps2-an385", "mps2-an386", "mps2-an505"}
    )
    return pytest.mark.skipif(
        SPEC.target not in targets
        or (
            SPEC.target == "mps2-an386"
            and SPEC.variant not in {"fpu", "soft", "softfp"}
        )
        or (SPEC.target == "mps2-an505" and SPEC.variant not in {"", "fpu"}),
        reason="requires an RT-Thread M-profile synchronous-fault fixture",
    )


def _riscv_skip():
    return pytest.mark.skipif(
        SPEC.target not in {"rv32", "rv64"},
        reason="requires an RT-Thread QEMU RISC-V fixture",
    )


def _json_line(output: str) -> dict:
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"no JSON payload in output:\n{output}")


def _envelope(output: str) -> dict:
    envelope = _json_line(output)
    assert envelope.get("schema_version", 3) == 3, json.dumps(envelope)
    assert envelope["ok"] is True, json.dumps(envelope)
    return envelope["data"]


def _backtrace(session, thread_name: str | None = None) -> dict:
    command = (
        f"gdr api backtrace {thread_name} --json"
        if thread_name is not None
        else "gdr api backtrace --json"
    )
    return _envelope(session.run(command, timeout=30))


def _clear_breakpoints(session) -> None:
    session.run_python("import gdb\nfor bp in gdb.breakpoints() or (): bp.delete()")


def _stop_symbol(session, name: str) -> None:
    session.run(f"tbreak {name}")
    output = session.run("continue", timeout=30)
    assert "Temporary breakpoint" in output, output


def _stop_at_address(session, address: int) -> None:
    _stop_symbol(session, f"*{address:#x}")


def _transitions(data: dict) -> list[dict]:
    return [frame for frame in data["frames"] if frame["method"] == "exception"]


def _words(session, address: int, count: int) -> tuple[int, ...]:
    raw = session.run_python(
        f"import gdb\nprint(gdb.selected_inferior().read_memory({address:#x}, {count * 4}).hex())"
    )
    blob = bytes.fromhex(raw.strip().splitlines()[-1])
    return struct.unpack(f"<{count}I", blob)


def _task_sp(session, name: str) -> int:
    return int(
        session.run(f'p/x $gdr_task("{name}").sp').strip().splitlines()[-1].split()[-1],
        16,
    )


def _profile() -> SchedulerProfile:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        profile = build_profile(ElfImage.from_path(SPEC.elf_path()))
        assert profile is not None, (
            f"unverified RT-Thread scheduler profile: {SPEC.target}"
        )
        _PROFILE_CACHE = profile
    return _PROFILE_CACHE


def _irq_frame(session, register: str, thread_symbol: str) -> dict:
    return _json_line(
        session.run_python(
            f"""
import json
frame = gdb.newest_frame()
base = int(frame.read_register('{register}'))
words = [int.from_bytes(bytes(gdb.selected_inferior().read_memory(base + 4*i, 4)), 'little') for i in range(8)]
thread = gdb.lookup_static_symbol('{thread_symbol}').value()
print(json.dumps({{
    'base': base, 'words': words, 'pc': words[6], 'status': words[7],
    'sp': base + 32 + (4 if words[7] & (1 << 9) else 0),
    'symbol': gdb.execute(f'info symbol {{words[6]:#x}}', to_string=True).split(' in section ')[0].strip(),
    'bounds': [int(thread['stack_addr']), int(thread['stack_addr']) + int(thread['stack_size'])],
    'live_ipsr': int(frame.read_register('xpsr')) & 0x1ff,
    'cfsr': int.from_bytes(bytes(gdb.selected_inferior().read_memory(0xE000ED28, 4)), 'little'),
    'hfsr': int.from_bytes(bytes(gdb.selected_inferior().read_memory(0xE000ED2C, 4)), 'little'),
}}))
"""
        )
    )


def _trace_irq_or_fault(session, name: str) -> dict:
    data = _backtrace(session, name)
    assert data["architecture"] == _ISA and data["source"] == "current", data
    return data


@contextlib.contextmanager
def _fault_gate(session):
    session.run("set var gdr_fault_gate = 1")
    try:
        yield
    finally:
        _clear_breakpoints(session)
        session.run("set var gdr_fault_gate = 0")


def _stop_at_fault(session, leaf: str) -> None:
    _stop_symbol(session, leaf)
    _stop_symbol(session, "HardFault_Handler")


def _systick_probe(session) -> dict:
    return _json_line(
        session.run_python(
            """
import json
frame = gdb.newest_frame()
base = int(frame.read_register('psp'))
words = [int.from_bytes(bytes(gdb.selected_inferior().read_memory(base + 4*i, 4)), 'little') for i in range(8)]
current = int(gdb.parse_and_eval('rt_current_thread'))
print(json.dumps({'base': base, 'pc': words[6], 'status': words[7],
                  'live_ipsr': int(frame.read_register('xpsr')) & 0x1ff,
                  'current': hex(current)}))
"""
        )
    )


def _sched_state(session) -> dict:
    return _json_line(
        session.run_python(
            """
import gdb, json

def u32(addr):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(addr, 4)), "little")

def read_name(tcb_val):
    name_field = tcb_val["name"]
    raw = bytes(gdb.selected_inferior().read_memory(int(name_field.address), int(name_field.type.sizeof)))
    return raw.split(b"\\x00", 1)[0].decode("ascii", errors="replace")

thread_type = gdb.lookup_type("struct rt_thread")
sp_off = next(int(f.bitpos) // 8 for f in thread_type.fields() if f.name == "sp")
current = int(gdb.parse_and_eval("rt_current_thread"))
from_ptr = int(gdb.parse_and_eval("rt_interrupt_from_thread"))
to_ptr = int(gdb.parse_and_eval("rt_interrupt_to_thread"))
flag = int(gdb.parse_and_eval("rt_thread_switch_interrupt_flag"))

from_tcb = (from_ptr - sp_off) if from_ptr else 0
to_tcb = (to_ptr - sp_off) if to_ptr else 0
from_name = read_name(gdb.Value(from_tcb).cast(thread_type.pointer())) if from_tcb else None
to_name = read_name(gdb.Value(to_tcb).cast(thread_type.pointer())) if to_tcb else None

container_sym = (
    gdb.lookup_static_symbol("rt_object_container")
    or gdb.lookup_static_symbol("_object_container")
    or gdb.lookup_global_symbol("rt_object_container")
    or gdb.lookup_global_symbol("_object_container")
)
try:
    thread_info = next(
        int(f.enumval)
        for f in gdb.lookup_type("enum rt_object_info_type").fields()
        if f.name == "RT_Object_Info_Thread"
    )
except gdb.error:
    thread_info = 0
container = container_sym.value()[thread_info]
head = int(container["object_list"].address)
list_off = next(int(f.bitpos) // 8 for f in thread_type.fields() if f.name == "list")
other_name = None
node = u32(head)
seen = 0
while node != head and seen < 64:
    seen += 1
    tcb = node - list_off
    if tcb not in (from_tcb, to_tcb, current):
        name = read_name(gdb.Value(tcb).cast(thread_type.pointer()))
        if name and not name.startswith("gdr_un"):
            other_name = name
            break
    node = u32(node)

psp = int(gdb.parse_and_eval("$psp"))
r1 = int(gdb.parse_and_eval("$r1")) & 0xFFFFFFFF
print(json.dumps({
    "pc": int(gdb.parse_and_eval("$pc")) & ~1,
    "xpsr": int(gdb.parse_and_eval("$xpsr")) & 0xFFFFFFFF,
    "psp": psp,
    "psp_pc": (u32(psp + 24) & ~1) if psp else None,
    "r1": r1,
    "flag": flag,
    "from_ptr": from_ptr,
    "to_ptr": to_ptr,
    "current": current,
    "from_name": from_name,
    "to_name": to_name,
    "other_name": other_name,
}))
"""
        )
    )


def _stop_at_addr(session, address: int) -> dict:
    session.run(f"tbreak *{address:#x}")
    output = session.run("continue", timeout=30)
    assert "breakpoint" in output.lower(), output
    return _sched_state(session)


def _wait_non_first_pendsv(session, address: int, attempts: int = 20) -> dict:
    for _ in range(attempts):
        state = _stop_at_addr(session, address)
        if (
            state["from_ptr"] != 0
            and state["to_ptr"] != 0
            and state["from_name"]
            and state["to_name"]
            and state["from_name"] != state["to_name"]
            and (address != _profile().pendsv_entry or state["flag"] == 1)
        ):
            return state
    raise AssertionError(f"no steady-state PendSV switch observed at {address:#x}")


def _assert_pendsv_owner_frames(data: dict, pendsv_pc: int) -> None:
    prof = _profile()
    frames = data["frames"]
    assert len(frames) >= 2, data
    assert prof.pendsv_start <= pendsv_pc < prof.pendsv_end, data
    assert frames[0]["pc"] == pendsv_pc, data
    assert frames[0]["method"] == "seed", data
    assert frames[0]["symbol"], data
    assert frames[1]["method"] == "exception", data
    assert (
        len(_transitions(data)) == 1 and _transitions(data)[0]["pc"] == frames[1]["pc"]
    ), data


def _assert_no_pendsv_frame(data: dict) -> None:
    prof = _profile()
    assert not any(
        prof.pendsv_start <= f["pc"] < prof.pendsv_end for f in data["frames"]
    ), data


def _assert_completed_owner(
    data: dict,
    *,
    stage: str,
    sources: set[str],
) -> None:
    dumped = json.dumps(data)
    assert data["architecture"] == _ISA, dumped
    assert data["complete"] is True, dumped
    assert data["stop_reason"] in {"task-exit", "task-entry"}, dumped
    assert data["source"] in sources, dumped
    assert len(data["frames"]) >= 1, dumped
    assert any(f"scheduler stage {stage} " in msg for msg in data["messages"]), dumped


def _rv_hold_and_break(session, stop_symbol: str) -> None:
    session.run("set var gdr_irq_hold = 0")
    _stop_symbol(session, "gdr_bt_live_leaf")
    session.run("set var gdr_irq_hold = 1")
    session.run("stepi 4")
    bp_out = session.run(
        f"tbreak {stop_symbol} if rt_current_thread == &worker3_thread"
    )
    assert "Temporary breakpoint" in bp_out, bp_out
    out = session.run("continue", timeout=15)
    assert "Temporary breakpoint" in out, out


def _symbol_address(session, symbol: str) -> int:
    return int(
        _json_line(
            session.run_python(
                f'import gdb, json\nprint(json.dumps({{"addr": int(gdb.parse_and_eval("&{symbol}"))}}))'
            )
        )["addr"]
    )


@pytest.fixture(scope="module")
def first_start_session():
    if (
        SPEC.target not in _M_PROFILE_TARGETS
        or SPEC.variant not in {"", "fpu", "soft", "softfp"}
        or (
            SPEC.target == "mps2-an386"
            and SPEC.variant not in {"fpu", "soft", "softfp"}
        )
    ):
        pytest.skip("requires an RT-Thread Arm M-profile scheduler fixture lane")
    profile = replace(get_rtthread_qemu_profile(GDR_ROOT, SPEC), init_command=None)
    check_tools(profile, SPEC.gdb)
    if not has_embedded_python(SPEC.gdb):
        pytest.skip("GDB has no embedded Python")
    qemu = QemuSession(profile)
    qemu.start(SPEC.boot_wait, paused=True)
    session = GdbSession(SPEC.gdb, profile, qemu.gdb_port, GDR_ROOT)
    try:
        session.start()
        session.run(f"gdr init rtthread {SPEC.version}", timeout=30)
        yield session
    finally:
        session.stop()
        qemu.stop()


@_saved_skip()
def test_blocked_thread_backtrace_reaches_the_entry(gdb_session):
    """A sleeping worker unwinds through the scheduler to its entry and exit."""
    data = _backtrace(gdb_session, "worker1")
    assert data["architecture"] == _ISA, data
    assert "port" not in data and "isa" not in data
    assert data["bounds"] == "recorded" and data["source"] == "saved", data
    expected_methods = {
        "rv64": {"cfi"},
        "rv32": {"cfi"},
        "cortex-a9": {"ehabi"}
        if SPEC.version in {"4.0.4", "4.0.5"}
        else {"ehabi", "cfi"},
    }.get(SPEC.target, {"leaf"})
    assert data["method"] in expected_methods, data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("rt_thread_mdelay" in function for function in functions), functions
    assert any("worker1_entry" in function for function in functions), functions
    delay = next(i for i, name in enumerate(functions) if "rt_thread_mdelay" in name)
    entry = next(i for i, name in enumerate(functions) if "worker1_entry" in name)
    assert delay < entry < len(functions) - 1, functions
    assert "thread_exit" in functions[-1], functions
    exit_name = functions[-1].split("+", 1)[0].strip()
    root = _json_line(
        gdb_session.run_python(
            f"import gdb, json; sym = gdb.lookup_static_symbol({exit_name!r}) or gdb.lookup_global_symbol({exit_name!r}); print(json.dumps({{'pc': int(sym.value().address)}}))"
        )
    )
    assert data["frames"][-1]["pc"] == root["pc"], data
    assert [frame["index"] for frame in data["frames"]] == list(range(len(functions)))
    assert all(
        frame["method"]
        in (
            expected_methods
            | ({"leaf"} if SPEC.target in {"rv32", "rv64"} else {"ehabi", "leaf"})
        )
        for frame in data["frames"][1:]
    ), data
    if SPEC.target in _M_PROFILE_TARGETS:
        assert any("instruction leaf" in message for message in data["messages"]), data
    assert data["stop_reason"] == "task-exit"
    assert data["complete"] is True
    assert not data["truncated"], data


@_saved_skip()
def test_text_backtrace_is_bounded_and_clean(gdb_session):
    """The text route renders at most the requested number of frames."""
    text = gdb_session.run("rtt backtrace worker1 --max-depth 1", timeout=30)
    assert "Traceback" not in text
    assert "worker1:" in text
    assert "max-depth" in text
    data = _envelope(
        gdb_session.run("gdr api backtrace worker1 --max-depth 1 --json", timeout=30)
    )
    assert len(data["frames"]) == 1
    assert data["stop_reason"] == "max-depth"
    assert data["truncated"] and not data["complete"], data


@_saved_skip()
def test_missing_thread_name_is_structured(gdb_session):
    """A missing thread is a stop reason, not an exception or empty success."""
    data = _backtrace(gdb_session, "no_such_thread")
    assert data["stop_reason"] == "task-not-found"
    assert data["frames"] == []
    assert not data["complete"]


@_bt_fixture_skip()
@pytest.mark.skipif(
    SPEC.target == "rv64", reason="requires the initial-context fixture"
)
def test_initial_thread_has_an_independent_entry_root(gdb_session):
    data = _backtrace(gdb_session, "gdr_unstarted")
    assert data["source"] == "unstarted" and data["complete"], data
    assert data["stop_reason"] == "task-entry" and len(data["frames"]) == 1, data
    assert "gdr_unstarted_entry" in data["frames"][0]["symbol"], data


@_bt_fixture_skip()
@pytest.mark.skipif(SPEC.target == "rv64", reason="requires the live-chain fixture")
def test_current_thread_backtrace_preserves_the_halted_state(gdb_session):
    _stop_symbol(gdb_session, "gdr_bt_live_stop")
    state_code = f"""
import hashlib, json
thread = gdb.lookup_static_symbol('worker3_thread').value()
frame = gdb.newest_frame()
names = ('sp', 'ra', *[f'x{{index}}' for index in range(3, 32)]) if {SPEC.target == "rv32"!r} else ('sp', 'lr', *[f'r{{index}}' for index in range(13)], *(('cpsr',) if {SPEC.target == "cortex-a9"!r} else ('xpsr', 'msp', 'psp')))
registers = {{name: int(frame.read_register(name)) for name in names}}
low = int(thread['stack_addr'])
high = low + int(thread['stack_size'])
print(json.dumps({{'pc': int(frame.pc()), 'function': frame.name(), 'registers': registers,
                  'bounds': [low, high], 'thread': int(thread.address),
                  'tcb': bytes(gdb.selected_inferior().read_memory(int(thread.address), thread.type.sizeof)).hex(),
                  'hash': hashlib.sha256(bytes(gdb.selected_inferior().read_memory(low, high-low))).hexdigest()}}))
"""
    before = _json_line(gdb_session.run_python(state_code))
    data = _backtrace(gdb_session, "worker3")
    after = _json_line(gdb_session.run_python(state_code))
    assert before == after
    assert data["source"] == "current" and data["address"] == before["thread"], data
    assert before["bounds"][0] <= before["registers"]["sp"] <= before["bounds"][1]
    assert data["frames"][0]["pc"] == before["pc"] & ~1, data
    assert before["function"] == "gdr_bt_live_leaf", before
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    leaf = next(
        i
        for i, name in enumerate(functions)
        if "gdr_bt_live_leaf" in name or "gdr_bt_live_stop" in name
    )
    middle = next(i for i, name in enumerate(functions) if "gdr_bt_live_middle" in name)
    entry = next(i for i, name in enumerate(functions) if "worker3_entry" in name)
    assert leaf < middle < entry < len(functions) - 1, functions
    assert data["complete"] and data["stop_reason"] == "task-exit", data


@_saved_skip()
def test_omitted_target_backtrace_unwinds_current_halted_pc(gdb_session):
    """Running ``rtt bt`` or ``gdr api backtrace`` without a thread uses current PC."""
    cur_name = gdb_session.run_python(
        """
from rtthread.navigation import get_current_thread
cur = get_current_thread()
raw = bytes(gdb.selected_inferior().read_memory(int(cur["name"].address), int(cur["name"].type.sizeof)))
print(raw.split(b"\\x00", 1)[0].decode("ascii", errors="replace"))
"""
    ).strip()
    assert cur_name
    cli_out = gdb_session.run("rtt bt", timeout=30)
    assert "[gdr] warning: usage:" not in cli_out, cli_out
    assert f"{cur_name}:" in cli_out, cli_out

    current_data = _envelope(gdb_session.run("gdr api backtrace --json"))
    explicit_data = _backtrace(gdb_session, cur_name)
    assert current_data["task"] == cur_name
    assert current_data["source"] == "current"
    assert current_data["frames"] == explicit_data["frames"]
    assert current_data["stop_reason"] == explicit_data["stop_reason"]


@_fpu_skip()
def test_fpu_context_registers_are_enabled_on_target(gdb_session):
    fpccr = int(
        gdb_session.run("p/x *(unsigned int *)0xE000EF34").split("=")[1].strip(), 16
    )
    assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
    cpacr = int(
        gdb_session.run("p/x *(unsigned int *)0xE000ED88").split("=")[1].strip(), 16
    )
    assert cpacr & 0x00F00000 == 0x00F00000, f"CPACR={cpacr:#x}"


@_fpu_skip()
def test_fpu_saved_slot_names_the_extended_frame(gdb_session):
    top = _task_sp(gdb_session, "worker_fpu")
    if SPEC.target == "mps2-an505":
        header = _words(gdb_session, top, 4)
        assert header[0] == 0, [f"{w:#x}" for w in header]
        assert header[1] == 0xFFFFFFED, [f"{w:#x}" for w in header]
        assert header[2] == 0, [f"{w:#x}" for w in header]
        assert header[3] & 1 == 0, f"CONTROL={header[3]:#x}"
    else:
        flag = _words(gdb_session, top, 1)[0]
        assert flag == 1, f"flag={flag:#x}"
    data = _backtrace(gdb_session, "worker_fpu")
    assert data["source"] == "saved", data
    tasks_data = _envelope(gdb_session.run("gdr api tasks --json", timeout=30))
    headers = tasks_data["headers"]
    pc_idx = headers.index("PC")
    name_idx = headers.index("Name")
    state_idx = headers.index("State")
    fpu_idx = next(
        i for i, r in enumerate(tasks_data["rows"]) if r[name_idx] == "worker_fpu"
    )
    assert tasks_data["rows"][fpu_idx][pc_idx] != "N/A", tasks_data["rows"][fpu_idx]
    assert tasks_data["value_rows"][fpu_idx][pc_idx] == data["frames"][0]["pc"], (
        tasks_data["value_rows"][fpu_idx],
        data,
    )
    w1_idx = next(
        i for i, r in enumerate(tasks_data["rows"]) if r[name_idx] == "worker1"
    )
    assert tasks_data["rows"][w1_idx][pc_idx] != "N/A", tasks_data["rows"][w1_idx]
    running_row = next(r for r in tasks_data["rows"] if r[state_idx] == "Running")
    assert running_row[pc_idx] != "N/A", running_row


@_fpu_skip()
def test_fpu_task_backtrace_is_complete(gdb_session):
    data = _backtrace(gdb_session, "worker_fpu")
    assert data["architecture"] == _ISA, data
    assert data["abi"] == ("aapcs32-vfp" if SPEC.variant == "fpu" else "aapcs32"), data
    assert data["source"] == "saved", data
    assert data["bounds"] == "recorded", data
    assert data["method"] == "leaf", data
    assert data["stop_reason"] == "task-exit", data
    assert data["complete"] and not data["truncated"], data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("worker_fpu_entry" in function for function in functions), functions


@_fpu_skip("mps2-an386")
def test_basic_flag_thread_backtrace_is_complete(gdb_session):
    top = _task_sp(gdb_session, "worker1")
    flag = _words(gdb_session, top, 1)[0]
    assert flag == 0, f"flag={flag:#x}"
    data = _backtrace(gdb_session, "worker1")
    assert data["architecture"] == "armv7e-m", data
    assert data["source"] == "saved", data
    assert data["complete"] and not data["truncated"], data
    functions = [frame["symbol"] or "" for frame in data["frames"]]
    assert any("worker1_entry" in function for function in functions), functions


@_fpu_skip("mps2-an505")
def test_fpu_switch_request_window_refuses_unverified_profile_live(gdb_session):
    _stop_symbol(gdb_session, "PendSV_Handler")
    data = _envelope(
        gdb_session.run_python("""
import rtthread.backtrace.unwind_gdb as bridge
original = bridge.build_scheduler_profile
try:
    bridge.build_scheduler_profile = lambda _elf: None
    print(gdb.execute('gdr api backtrace worker_fpu --json', to_string=True))
finally:
    bridge.build_scheduler_profile = original
""")
    )
    assert not data["complete"], data
    assert data["stop_reason"] == "scheduler-profile-unverified", data
    assert data["frames"] == [], data


@_irq_skip()
def test_single_irq_returns_to_the_exact_task_pc(gdb_session):
    _stop_symbol(gdb_session, "gdr_irq_single_stop")
    raw = _irq_frame(gdb_session, "psp", "gdr_irq_thread")
    assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
    assert raw["bounds"][0] <= raw["base"] < raw["sp"] <= raw["bounds"][1]
    data = _trace_irq_or_fault(gdb_session, "gdr_irq")
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
    assert "gdr_irq_wait" in transitions[0]["symbol"], data
    assert any("EXC_RETURN=0xfffffffd" in message for message in data["messages"]), data


@_irq_skip(fpu_only=True)
def test_fp_isr_grows_the_msp_frame_with_lazy_stacking(gdb_session):
    _stop_symbol(gdb_session, "gdr_irq_fp_stop")
    raw = _irq_frame(gdb_session, "psp", "gdr_irq_thread")
    assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
    assert raw["bounds"][0] <= raw["base"] < raw["sp"] <= raw["bounds"][1]
    fpccr = int(
        gdb_session.run("p/x *(unsigned int *)0xE000EF34").split("=")[1].strip(), 16
    )
    assert fpccr & 0xC0000000 == 0xC0000000, f"FPCCR={fpccr:#x}"
    data = _trace_irq_or_fault(gdb_session, "gdr_irq")
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
    assert "gdr_irq" in transitions[0]["symbol"], data
    assert any("EXC_RETURN=0xffffffed" in message for message in data["messages"]), data
    assert any("extended" in message for message in data["messages"]), data


@_irq_skip()
def test_switch_request_window_refuses_unverified_profile_live(gdb_session):
    gdb_session.run("tbreak PendSV_Handler")
    gdb_session.run("continue", timeout=60)
    data = _envelope(
        gdb_session.run_python("""
import rtthread.backtrace.unwind_gdb as bridge
original = bridge.build_scheduler_profile
try:
    bridge.build_scheduler_profile = lambda _elf: None
    print(gdb.execute('gdr api backtrace gdr_irq --json', to_string=True))
finally:
    bridge.build_scheduler_profile = original
""")
    )
    assert not data["complete"], data
    assert data["stop_reason"] == "scheduler-profile-unverified", data
    assert data["frames"] == [], data


@_irq_skip()
def test_nested_handler_entry_recovers_both_hardware_frames(gdb_session):
    _stop_symbol(gdb_session, "*Timer1_Handler")
    nested = _irq_frame(gdb_session, "msp", "gdr_irq_thread")
    task = _irq_frame(gdb_session, "psp", "gdr_irq_thread")
    assert (
        nested["live_ipsr"] == _TIMER1_EXC and nested["status"] & 0x1FF == _TIMER0_EXC
    )
    assert task["status"] & 0x1FF == 0
    data = _trace_irq_or_fault(gdb_session, "gdr_irq")
    transitions = _transitions(data)
    assert [frame["pc"] for frame in transitions] == [nested["pc"], task["pc"]], data
    assert data["complete"] and data["stop_reason"] == "task-exit", data
    assert "gdr_irq_nested_wait" in transitions[0]["symbol"], data
    assert any(
        f"exception:{_TIMER1_EXC} -> exception:{_TIMER0_EXC}" in message
        for message in data["messages"]
    ), data


@_irq_skip()
def test_alignment_padding_is_restored_without_custom_fixture_cfi(gdb_session):
    _stop_symbol(gdb_session, "gdr_irq_align_stop")
    raw = _irq_frame(gdb_session, "psp", "gdr_irq_thread")
    assert raw["status"] & (1 << 9) and raw["base"] % 8 == 0
    assert raw["sp"] == raw["base"] + 36 and raw["sp"] % 8 == 4
    data = _trace_irq_or_fault(gdb_session, "gdr_irq")
    assert len(_transitions(data)) == 1 and _transitions(data)[0]["pc"] == raw["pc"]
    assert data["complete"] and data["stop_reason"] == "task-exit", data


@_irq_skip()
def test_irq_queries_do_not_change_registers_stacks_or_scheduler_memory(gdb_session):
    _stop_symbol(gdb_session, "gdr_irq_nested_stop")
    code = """
import hashlib, json
frame = gdb.newest_frame()
registers = {name: int(frame.read_register(name)) for name in ('sp','lr','psp','msp','xpsr','control')}
thread = gdb.lookup_static_symbol('gdr_irq_thread').value()
spans = [(int(thread['stack_addr']), int(thread['stack_size'])),
         (int(gdb.parse_and_eval('&__StackLimit')), int(gdb.parse_and_eval('&__StackTop')) - int(gdb.parse_and_eval('&__StackLimit')))]
print(json.dumps({'registers': registers, 'pc': int(frame.pc()),
                  'current': int(gdb.parse_and_eval('rt_current_thread')),
                  'tick': int(gdb.parse_and_eval('rt_tick')),
                  'pending': int(gdb.parse_and_eval('rt_thread_switch_interrupt_flag')),
                  'hashes': [hashlib.sha256(bytes(gdb.selected_inferior().read_memory(base, size))).hexdigest() for base,size in spans]}))
"""
    before = _json_line(gdb_session.run_python(code))
    data = _trace_irq_or_fault(gdb_session, "gdr_irq")
    after = _json_line(gdb_session.run_python(code))
    assert before == after and before["pending"] == 0
    assert len(_transitions(data)) == 2 and data["complete"], data


@_irq_skip()
def test_pending_switch_refuses_task_ownership_without_writing_the_target(gdb_session):
    _stop_symbol(gdb_session, "gdr_irq_single_stop")
    result = _json_line(
        gdb_session.run_python("""
import json
import rtthread.backtrace.unwind_gdb as bridge
original = bridge.read_int
targets = {
    int(gdb.parse_and_eval(f"&{name}"))
    for name in (
        "rt_thread_switch_interrupt_flag",
        "rt_interrupt_from_thread",
        "rt_interrupt_to_thread",
    )
}
try:
    bridge.read_int = lambda value: (
        1
        if getattr(value, "address", None) is not None and int(value.address) in targets
        else original(value)
    )
    print(gdb.execute('gdr api backtrace gdr_irq --json', to_string=True))
finally:
    bridge.read_int = original
""")
    )
    assert result["ok"] is True, json.dumps(result)
    assert result["data"]["stop_reason"] == "scheduler-owner-inconsistent", result
    assert result["data"]["frames"] == []


@_irq_skip()
def test_partial_msp_override_cannot_enable_an_unbounded_walk(gdb_session):
    _stop_symbol(gdb_session, "gdr_irq_single_stop")
    result = _json_line(
        gdb_session.run_python("""
import rtthread.backtrace.unwind_gdb as bridge
original = bridge.linker_symbol_address
try:
    bridge.linker_symbol_address = lambda name: 0x203fc000 if name == '__gdr_irq_stack_low' else None
    print(gdb.execute('gdr api backtrace gdr_irq --json', to_string=True))
finally:
    bridge.linker_symbol_address = original
""")
    )
    assert result["data"]["stop_reason"] == "exception-stack-bounds-unavailable", result
    assert result["data"]["frames"] == []


@_systick_skip()
def test_systick_tick_crosses_into_the_interrupted_task(gdb_session):
    raw = data = None
    for _ in range(10):
        _stop_symbol(gdb_session, "SysTick_Handler")
        raw = _systick_probe(gdb_session)
        assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
        assert raw["live_ipsr"] == 15, raw
        data = _backtrace(gdb_session, raw["current"])
        if data["task"] == "tidle0":
            break
    assert data is not None and data["task"] == "tidle0", data
    assert data["architecture"] == _ISA and data["source"] == "current", data
    assert data["bounds"] == "recorded", data
    transitions = _transitions(data)
    assert len(transitions) == 1, data
    assert transitions[0]["pc"] == raw["pc"], data
    assert transitions[0]["symbol"], data
    assert data["complete"] and data["stop_reason"] == "task-exit", data


@_systick_skip()
def test_systick_inside_rt_tick_increase_unwinds_handler_call_chain(gdb_session):
    raw = data = None
    for _ in range(10):
        _stop_symbol(gdb_session, "rt_tick_increase")
        raw = _systick_probe(gdb_session)
        assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
        assert raw["live_ipsr"] == 15, raw
        data = _backtrace(gdb_session, raw["current"])
        if data["task"] == "tidle0":
            break
    assert data is not None and data["task"] == "tidle0", data
    symbols = [frame["symbol"] or "" for frame in data["frames"]]
    assert "rt_tick_increase" in symbols[0], symbols
    assert any("SysTick_Handler" in sym for sym in symbols), symbols
    transitions = _transitions(data)
    assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
    assert data["complete"] and data["stop_reason"] == "task-exit", data


@_sched_skip()
def test_first_thread_start_stages_on_real_boot(first_start_session):
    session = first_start_session
    prof = _profile()

    ambig_state = _stop_at_addr(session, prof.switch_to_start)
    assert ambig_state["from_ptr"] == 0, ambig_state
    ambig = _backtrace(session, ambig_state["to_name"] or "main")
    assert ambig["complete"] is False, ambig
    assert ambig["frames"] == [], ambig
    assert (
        ambig["stop_reason"] == "scheduler-stage-ambiguous:rt_hw_context_switch_to"
    ), ambig

    entry_state = _stop_at_addr(session, prof.pendsv_entry)
    assert entry_state["from_ptr"] == 0, entry_state
    to_name = entry_state["to_name"]
    assert to_name in {"main", "timer", "tidle0"}, entry_state
    entry_data = _backtrace(session, to_name)
    _assert_completed_owner(entry_data, stage="pendsv-entry", sources={"unstarted"})
    _assert_pendsv_owner_frames(entry_data, prof.pendsv_entry)

    for stage_name, addr, expected_sources in (
        ("pendsv-switch-to", prof.pendsv_switch_to, {"unstarted"}),
        ("pendsv-saved-sp", prof.pendsv_saved_sp, {"unstarted"}),
        (
            "pendsv-restored-pending-psp",
            prof.pendsv_restored_pending_psp,
            {"unstarted"},
        ),
        ("pendsv-psp-written", prof.pendsv_psp_written, {"unstarted"}),
        ("pendsv-exception-return", prof.pendsv_exception_return, {"unstarted"}),
    ):
        state = _stop_at_addr(session, addr)
        assert state["from_ptr"] == 0, state
        data = _backtrace(session, to_name)
        _assert_completed_owner(data, stage=stage_name, sources=expected_sources)
        _assert_pendsv_owner_frames(data, addr)


@_sched_skip()
def test_pendsv_stages_across_one_switch_cycle(gdb_session):
    try:
        _run_pendsv_stages_across_one_switch_cycle(gdb_session)
    finally:
        _stop_symbol(gdb_session, "gdr_bt_live_stop")


def _run_pendsv_stages_across_one_switch_cycle(gdb_session):
    prof = _profile()
    entry = _wait_non_first_pendsv(gdb_session, prof.pendsv_entry)
    from_name = entry["from_name"]
    to_name = entry["to_name"]
    other_name = entry["other_name"]
    assert from_name and to_name and other_name, entry

    old_entry = _backtrace(gdb_session, from_name)
    _assert_completed_owner(old_entry, stage="pendsv-entry", sources={"current"})
    _assert_pendsv_owner_frames(old_entry, prof.pendsv_entry)
    assert old_entry["frames"][1]["pc"] == entry["psp_pc"], old_entry
    cur_entry = _backtrace(gdb_session)
    assert cur_entry["task"] == from_name and cur_entry["frames"] == old_entry["frames"]
    new_entry = _backtrace(gdb_session, to_name)
    _assert_completed_owner(
        new_entry, stage="pendsv-entry", sources={"saved", "unstarted"}
    )
    _assert_no_pendsv_frame(new_entry)
    other_entry = _backtrace(gdb_session, other_name)
    _assert_completed_owner(
        other_entry, stage="pendsv-entry", sources={"saved", "unstarted"}
    )
    _assert_no_pendsv_frame(other_entry)

    pending = _stop_at_addr(gdb_session, prof.pendsv_save_pending)
    assert pending["from_name"] == from_name and pending["to_name"] == to_name
    old_pending = _backtrace(gdb_session, from_name)
    _assert_completed_owner(
        old_pending, stage="pendsv-save-pending", sources={"current"}
    )
    _assert_pendsv_owner_frames(old_pending, prof.pendsv_save_pending)
    new_pending = _backtrace(gdb_session, to_name)
    _assert_completed_owner(
        new_pending, stage="pendsv-save-pending", sources={"saved", "unstarted"}
    )
    _assert_no_pendsv_frame(new_pending)

    if prof.pendsv_save_written is not None:
        written = _stop_at_addr(gdb_session, prof.pendsv_save_written)
        assert written["from_name"] == from_name and written["to_name"] == to_name
        old_written = _backtrace(gdb_session, from_name)
        _assert_completed_owner(
            old_written, stage="pendsv-save-written", sources={"saved-buffer"}
        )
        _assert_pendsv_owner_frames(old_written, prof.pendsv_save_written)
        new_written = _backtrace(gdb_session, to_name)
        _assert_completed_owner(
            new_written, stage="pendsv-save-written", sources={"saved", "unstarted"}
        )
        _assert_no_pendsv_frame(new_written)

    for stage_name, addr, old_sources, new_sources in (
        ("pendsv-switch-to", prof.pendsv_switch_to, {"saved"}, {"saved", "unstarted"}),
        ("pendsv-saved-sp", prof.pendsv_saved_sp, {"saved"}, {"saved", "unstarted"}),
        (
            "pendsv-restored-pending-psp",
            prof.pendsv_restored_pending_psp,
            {"saved"},
            {"current", "unstarted"},
        ),
        (
            "pendsv-psp-written",
            prof.pendsv_psp_written,
            {"saved"},
            {"current", "unstarted"},
        ),
        (
            "pendsv-exception-return",
            prof.pendsv_exception_return,
            {"saved"},
            {"current", "unstarted"},
        ),
    ):
        state = _stop_at_addr(gdb_session, addr)
        assert state["from_name"] == from_name and state["to_name"] == to_name
        old_data = _backtrace(gdb_session, from_name)
        _assert_completed_owner(old_data, stage=stage_name, sources=old_sources)
        _assert_no_pendsv_frame(old_data)
        new_data = _backtrace(gdb_session, to_name)
        _assert_completed_owner(new_data, stage=stage_name, sources=new_sources)
        _assert_pendsv_owner_frames(new_data, addr)


@_sched_skip()
def test_switch_return_and_ambiguous_handler_stages(gdb_session):
    try:
        _run_switch_return_and_ambiguous_handler_stages(gdb_session)
    finally:
        _stop_symbol(gdb_session, "gdr_bt_live_stop")


def _run_switch_return_and_ambiguous_handler_stages(gdb_session):
    prof = _profile()

    sw_ret = _wait_non_first_pendsv(gdb_session, prof.switch_return)
    old_ret = _backtrace(gdb_session, sw_ret["from_name"])
    _assert_completed_owner(old_ret, stage="switch-return", sources={"current"})
    new_ret = _backtrace(gdb_session, sw_ret["to_name"])
    _assert_completed_owner(
        new_ret, stage="switch-return", sources={"saved", "unstarted"}
    )

    elf = ElfImage.from_path(SPEC.elf_path())
    assert elf is not None
    ambig_pendsv = None
    addr = prof.pendsv_entry
    while addr < prof.pendsv_save_pending:
        insn = decode_thumb(elf, addr, prof.pendsv_save_pending)
        assert not isinstance(insn, str), insn
        if insn.address > prof.pendsv_entry and insn.address not in prof.stages:
            ambig_pendsv = insn.address
            break
        addr += insn.size
    assert ambig_pendsv is not None
    ambig_state = _stop_at_addr(gdb_session, ambig_pendsv)
    target_name = ambig_state["to_name"] or ambig_state["from_name"] or "tidle0"
    ambig_data = _backtrace(gdb_session, target_name)
    assert ambig_data["complete"] is False, ambig_data
    assert ambig_data["frames"] == [], ambig_data
    assert ambig_data["stop_reason"] == "scheduler-stage-ambiguous:PendSV_Handler", (
        ambig_data
    )

    ambig_switch = prof.switch_start
    assert ambig_switch not in prof.stages
    sw_state = _stop_at_addr(gdb_session, ambig_switch)
    sw_target = sw_state["to_name"] or sw_state["from_name"] or "tidle0"
    sw_ambig = _backtrace(gdb_session, sw_target)
    assert sw_ambig["complete"] is False, sw_ambig
    assert sw_ambig["frames"] == [], sw_ambig
    assert (
        sw_ambig["stop_reason"] == "scheduler-stage-ambiguous:rt_hw_context_switch"
    ), sw_ambig


@_fault_skip()
def test_precise_usagefault_escalation_is_synchronous(gdb_session):
    with _fault_gate(gdb_session):
        _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
        raw = _irq_frame(gdb_session, "psp", "gdr_fault_thread")
        assert raw["status"] & 0x1FF == 0 and raw["status"] & (1 << 24)
        assert raw["live_ipsr"] == 3, raw
        assert raw["bounds"][0] <= raw["base"] < raw["bounds"][1], raw
        assert raw["cfsr"] & 0x00010000 and raw["hfsr"] & 0x40000000, raw
        data = _trace_irq_or_fault(gdb_session, "gdr_fault")
        assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
        transitions = _transitions(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
        assert "gdr_fault" in transitions[0]["symbol"], data
        assert any("UNDEFINSTR" in m for m in data["messages"]), data


@_fault_skip()
def test_precise_bus_fault_reports_the_faulting_data_address(gdb_session):
    with _fault_gate(gdb_session):
        _stop_at_fault(gdb_session, "gdr_fault_bus_stop")
        raw = _irq_frame(gdb_session, "psp", "gdr_fault_thread")
        bfar = int(
            gdb_session.run("p/x *(unsigned int *)0xE000ED38").split("=")[1].strip(),
            16,
        )
        assert raw["live_ipsr"] == 3, raw
        assert raw["cfsr"] & 0x00008200 == 0x00008200, raw
        assert bfar == _EXPECTED_BFAR, hex(bfar)
        assert "gdr_fault_bus_leaf" in raw["symbol"], raw
        data = _trace_irq_or_fault(gdb_session, "gdr_fault")
        assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
        transitions = _transitions(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
        assert any("BusFault" in m for m in data["messages"]), data
        assert any(f"BFAR={_EXPECTED_BFAR:#x}" in m for m in data["messages"]), data


@_fault_skip(memmanage=True)
def test_precise_memmanage_escalation_reports_the_blocked_region(gdb_session):
    with _fault_gate(gdb_session):
        _stop_at_fault(gdb_session, "gdr_fault_mm_stop")
        raw = _irq_frame(gdb_session, "psp", "gdr_fault_thread")
        mmfar = int(
            gdb_session.run("p/x *(unsigned int *)0xE000ED34").split("=")[1].strip(),
            16,
        )
        assert raw["live_ipsr"] == 3, raw
        assert raw["cfsr"] & 0x00000082 == 0x00000082, raw
        assert mmfar == 0x60000000, hex(mmfar)
        assert "gdr_fault_memmanage_leaf" in raw["symbol"], raw
        data = _trace_irq_or_fault(gdb_session, "gdr_fault")
        assert data["complete"] and data["stop_reason"] == "synchronous-fault", data
        transitions = _transitions(data)
        assert len(transitions) == 1 and transitions[0]["pc"] == raw["pc"], data
        assert any("DACCVIOL" in m for m in data["messages"]), data
        assert any("MMFAR=0x60000000" in m for m in data["messages"]), data


@_fault_skip()
def test_fault_backtrace_preserves_target_registers_and_stacks(gdb_session):
    with _fault_gate(gdb_session):
        _stop_at_fault(gdb_session, "gdr_fault_ud_stop")
        code = """
import hashlib, json
frame = gdb.newest_frame()
registers = {name: int(frame.read_register(name)) for name in ('sp','lr','psp','msp','xpsr','control')}
thread = gdb.lookup_static_symbol('gdr_fault_thread').value()
spans = [(int(thread['stack_addr']), int(thread['stack_size'])),
         (int(gdb.parse_and_eval('&__StackLimit')), int(gdb.parse_and_eval('&__StackTop')) - int(gdb.parse_and_eval('&__StackLimit')))]
print(json.dumps({'registers': registers, 'pc': int(frame.pc()),
                  'current': int(gdb.parse_and_eval('rt_current_thread')),
                  'tick': int(gdb.parse_and_eval('rt_tick')),
                  'hashes': [hashlib.sha256(bytes(gdb.selected_inferior().read_memory(base, size))).hexdigest() for base,size in spans]}))
"""
        before = _json_line(gdb_session.run_python(code))
        data = _trace_irq_or_fault(gdb_session, "gdr_fault")
        after = _json_line(gdb_session.run_python(code))
        assert before == after
        assert len(_transitions(data)) == 1 and data["complete"], data


@_fault_skip()
def test_fault_backtrace_inside_c_handler_and_hook_unwinds_trampoline_and_psp_frame(
    gdb_session,
):
    with _fault_gate(gdb_session):
        _stop_symbol(gdb_session, "gdr_fault_ud_stop")
        _stop_symbol(gdb_session, "rt_hw_hard_fault_exception")
        c_data = _trace_irq_or_fault(gdb_session, "gdr_fault")
        assert c_data["complete"] and c_data["stop_reason"] == "synchronous-fault", (
            c_data
        )
        c_symbols = [frame["symbol"] or "" for frame in c_data["frames"]]
        assert "rt_hw_hard_fault_exception" in c_symbols[0], c_symbols
        assert any(
            sym.startswith(("HardFault_Handler", "_update_done", "update_done"))
            for sym in c_symbols
        ), c_symbols
        assert any(frame["method"] == "exception" for frame in c_data["frames"]), c_data
        assert any("gdr_fault_undefinstr_leaf" in sym for sym in c_symbols), c_symbols
        assert any("gdr_fault_trigger" in sym for sym in c_symbols), c_symbols
        assert "thread_exit" in c_symbols[-1], c_symbols

        if SPEC.target == "mps2-an505":
            _stop_symbol(gdb_session, "gdr_fault_hook")
            hook_data = _trace_irq_or_fault(gdb_session, "gdr_fault")
            assert (
                hook_data["complete"]
                and hook_data["stop_reason"] == "synchronous-fault"
            ), hook_data
            hook_symbols = [frame["symbol"] or "" for frame in hook_data["frames"]]
            assert "gdr_fault_hook" in hook_symbols[0], hook_symbols
            assert any("rt_hw_hard_fault_exception" in sym for sym in hook_symbols), (
                hook_symbols
            )
            assert any(
                sym.startswith(("HardFault_Handler", "_update_done", "update_done"))
                for sym in hook_symbols
            ), hook_symbols
            assert any(
                frame["method"] == "exception" for frame in hook_data["frames"]
            ), hook_data
            assert any("gdr_fault_undefinstr_leaf" in sym for sym in hook_symbols), (
                hook_symbols
            )
            assert any("gdr_fault_trigger" in sym for sym in hook_symbols), hook_symbols
            assert "thread_exit" in hook_symbols[-1], hook_symbols


@_riscv_skip()
def test_riscv_trap_callee_unwinds_to_multiframe_worker_task(gdb_session):
    try:
        _rv_hold_and_break(gdb_session, "rt_tick_increase")
        gdb_session.run("stepi 4")
        raw = _json_line(
            gdb_session.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
mask = (1 << (64 if gdb.lookup_type('rt_ubase_t').sizeof == 8 else 32)) - 1
print(json.dumps({
    "mcause": int(frame.read_register("mcause")) & mask,
    "mepc": int(frame.read_register("mepc")) & mask,
    "nest": int(gdb.parse_and_eval("rt_interrupt_nest")),
    "switch_flag": int(gdb.parse_and_eval("rt_thread_switch_interrupt_flag")),
    "thread": int(gdb.parse_and_eval("&worker3_thread")),
}))
"""
            )
        )
        assert raw["nest"] == 1 and raw["switch_flag"] == 0, raw

        data = _backtrace(gdb_session, "worker3")
        assert data["architecture"] == _ISA, data
        assert data["source"] == "current", data
        assert data["stop_reason"] == "task-exit", (
            data["stop_reason"],
            data["messages"],
            data["frames"],
        )
        assert data["complete"] is True, data

        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        assert any("rt_tick_increase" in sym for sym in symbols), symbols
        assert any("handle_trap" in sym for sym in symbols), symbols
        assert any("gdr_bt_live_leaf" in sym for sym in symbols), symbols
        assert any("gdr_bt_live_middle" in sym for sym in symbols), symbols
        assert any("worker3_entry" in sym for sym in symbols), symbols
        assert "thread_exit" in symbols[-1], symbols

        transitions = _transitions(data)
        assert len(transitions) == 1, data
        assert transitions[0]["pc"] == raw["mepc"], (transitions, raw)
        assert "gdr_bt_live_leaf" in (transitions[0]["symbol"] or ""), transitions
        evidence = " ".join(data["messages"])
        assert f"exception:{raw['mcause']:#x} -> task:{raw['thread']:#x}" in evidence, (
            data
        )
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


@_riscv_skip()
def test_riscv_raw_trap_entry_recovers_worker_stack(gdb_session):
    try:
        trap_entry = _symbol_address(gdb_session, "trap_entry")
        _rv_hold_and_break(gdb_session, f"*{trap_entry:#x}")
        raw = _json_line(
            gdb_session.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
mask = (1 << (64 if gdb.lookup_type('rt_ubase_t').sizeof == 8 else 32)) - 1
print(json.dumps({"mepc": int(frame.read_register("mepc")) & mask}))
"""
            )
        )
        data = _backtrace(gdb_session, "worker3")
        assert data["stop_reason"] == "task-exit", (
            data["stop_reason"],
            data["messages"],
            data["frames"],
        )
        assert data["complete"] is True, data
        symbols = [frame["symbol"] or "" for frame in data["frames"]]
        assert "trap_entry" in symbols[0], symbols
        assert data["frames"][1]["method"] == "exception", data
        assert data["frames"][1]["pc"] == raw["mepc"], (data, raw)
        assert any("gdr_bt_live_leaf" in sym for sym in symbols[1:]), symbols
        assert any("gdr_bt_live_middle" in sym for sym in symbols[1:]), symbols
        assert "thread_exit" in symbols[-1], symbols

        sync_check = _json_line(
            gdb_session.run_python(
                """
import dataclasses, json
from core.adapter import active
import rtthread.backtrace.unwind_gdb as bt

orig = bt._riscv_snapshot
def forced(cause):
    def _wrap(*, xlen=32):
        snap, reason = orig(xlen=xlen)
        if snap is None:
            return None, reason
        return dataclasses.replace(snap, mcause=cause, mtval=0x1234), "ok"
    return _wrap

bt._riscv_snapshot = forced(2)
try:
    rep = bt.backtrace(active(), "worker3")
finally:
    bt._riscv_snapshot = orig

bt._riscv_snapshot = forced(24)
try:
    bad = bt.backtrace(active(), "worker3")
finally:
    bt._riscv_snapshot = orig

print(json.dumps({
    "complete": rep.complete,
    "stop_reason": rep.stop_reason,
    "frame1_pc": rep.frames[1].pc if len(rep.frames) > 1 else None,
    "frame1_method": rep.frames[1].method if len(rep.frames) > 1 else None,
    "messages": rep.messages,
    "bad_complete": bad.complete,
    "bad_reason": bad.stop_reason,
}))
"""
            )
        )
        assert sync_check["complete"] is True, sync_check
        assert sync_check["stop_reason"] == "synchronous-fault", sync_check
        assert sync_check["frame1_method"] == "exception", sync_check
        assert sync_check["frame1_pc"] == raw["mepc"], (sync_check, raw)
        assert any("Exception:IllegalInstruction" in m for m in sync_check["messages"])
        assert sync_check["bad_complete"] is False, sync_check
        assert sync_check["bad_reason"] == "exception-frame-type-unproven", sync_check
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


def _rv_profile() -> RiscvSchedulerProfile:
    global _RV_PROFILE_CACHE
    if _RV_PROFILE_CACHE is None:
        profile, reason = build_riscv_profile(ElfImage.from_path(SPEC.elf_path()))
        assert profile is not None, reason
        _RV_PROFILE_CACHE = profile
    return _RV_PROFILE_CACHE


def _rv_thread_name(session, expr: str, *, from_sp_ptr: bool = False) -> str:
    body = (
        f"off = member_offset('struct rt_thread', ('sp',))\nval = gdb.Value((int(gdb.parse_and_eval({expr!r})) & ((1 << (64 if gdb.lookup_type('rt_ubase_t').sizeof == 8 else 32)) - 1)) - off).cast(gdb.lookup_type('struct rt_thread').pointer())"
        if from_sp_ptr
        else f"val = gdb.parse_and_eval({expr!r})"
    )
    return session.run_python(
        f"import gdb\nfrom core.layout import member_offset\n{body}\nprint(val['name'].string().rstrip('\\x00'))"
    ).strip()


@_riscv_skip()
def test_riscv_first_start_switch_to_stages():
    profile = replace(get_rtthread_qemu_profile(GDR_ROOT, SPEC), init_command=None)
    check_tools(profile, SPEC.gdb)
    if not has_embedded_python(SPEC.gdb):
        pytest.skip("GDB has no embedded Python")
    prof = _rv_profile()
    qemu = QemuSession(profile)
    qemu.start(SPEC.boot_wait, paused=True)
    session = GdbSession(SPEC.gdb, profile, qemu.gdb_port, GDR_ROOT)
    try:
        session.start()
        session.run(f"gdr init rtthread {SPEC.version}", timeout=30)
        _stop_at_address(session, prof.switch_to_start)
        to_name = _rv_thread_name(session, "rt_current_thread")
        for req in (None, to_name):
            data = _backtrace(session, req)
            _assert_completed_owner(
                data, stage="switch-to-entry", sources={"unstarted"}
            )
            assert data["task"] == to_name, data
            assert data["stop_reason"] == "task-entry", data

        session.run("stepi 1")
        _assert_completed_owner(
            _backtrace(session), stage="switch-to-sp-loaded", sources={"unstarted"}
        )

        _stop_at_address(session, prof.exit_start)
        _assert_completed_owner(
            _backtrace(session), stage="restore-window", sources={"unstarted"}
        )

        _stop_at_address(session, prof.exit_mret_pc)
        _assert_completed_owner(
            _backtrace(session), stage="restore-sp-popped", sources={"unstarted"}
        )
    finally:
        _clear_breakpoints(session)
        session.stop()
        qemu.stop()


@_riscv_skip()
def test_riscv_context_switch_and_restore_stages(gdb_session):
    prof = _rv_profile()
    gdb_session.run("set var gdr_irq_hold = 0")
    try:
        _stop_at_address(gdb_session, prof.switch_start)
        owners = _json_line(
            gdb_session.run_python(
                """
import gdb, json
from core.layout import member_offset
frame = gdb.newest_frame()
mask = (1 << (64 if gdb.lookup_type('rt_ubase_t').sizeof == 8 else 32)) - 1
off = member_offset("struct rt_thread", ("sp",))
t = gdb.lookup_type("struct rt_thread").pointer()
a0 = int(frame.read_register("a0")) & mask
a1 = int(frame.read_register("a1")) & mask
from_t = gdb.Value(a0 - off).cast(t)
to_t = gdb.parse_and_eval("rt_current_thread")
print(json.dumps({
    "from_name": from_t["name"].string().rstrip("\\x00"),
    "to_name": to_t["name"].string().rstrip("\\x00"),
    "a1_match": (int(to_t) & mask) + off == a1,
}))
"""
            )
        )
        assert owners["a1_match"] and owners["from_name"] != owners["to_name"], owners
        from_name = owners["from_name"]
        to_name = owners["to_name"]

        for req in (None, from_name):
            d = _backtrace(gdb_session, req)
            _assert_completed_owner(
                d, stage="context-switch-entry", sources={"current"}
            )
            assert d["task"] == from_name, d
        to_entry = _backtrace(gdb_session, to_name)
        _assert_completed_owner(
            to_entry, stage="context-switch-entry", sources={"saved", "unstarted"}
        )

        # Stop inside rt_hw_context_switch after `csrr a0, mstatus` clobbers a0.
        clobber_next = prof.switch_a0_clobber_pc + 4
        _stop_at_address(gdb_session, clobber_next)
        d_sav = _backtrace(gdb_session)
        _assert_completed_owner(
            d_sav, stage="context-switch-saving", sources={"current"}
        )
        assert d_sav["task"] == from_name, d_sav
        _assert_completed_owner(
            _backtrace(gdb_session, to_name),
            stage="context-switch-saving",
            sources={"saved", "unstarted"},
        )

        # After `LOAD sp, (a1)`, incoming thread owns live sp while outgoing is saved.
        _stop_at_address(gdb_session, prof.switch_load_to_pc)
        gdb_session.run("stepi 1")
        d_loaded = _backtrace(gdb_session)
        _assert_completed_owner(
            d_loaded,
            stage="context-switch-sp-loaded",
            sources={"saved", "unstarted"},
        )
        assert d_loaded["task"] == to_name, d_loaded
        _assert_completed_owner(
            _backtrace(gdb_session, from_name),
            stage="context-switch-sp-loaded",
            sources={"saved"},
        )

        _stop_at_address(gdb_session, prof.exit_start)
        _assert_completed_owner(
            _backtrace(gdb_session),
            stage="restore-window",
            sources={"saved", "unstarted"},
        )

        _stop_at_address(gdb_session, prof.exit_mret_pc)
        d_pop = _backtrace(gdb_session)
        _assert_completed_owner(
            d_pop, stage="restore-sp-popped", sources={"current", "unstarted"}
        )
        assert d_pop["task"] == to_name, d_pop
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


@_riscv_skip()
def test_riscv_trap_prologue_and_epilogue_stages(gdb_session):
    prof = _rv_profile()
    try:
        # 1. trap-prologue-saving before and after `mv s0, sp` + ISR stack switch.
        _rv_hold_and_break(gdb_session, f"*{prof.trap_start:#x}")
        gdb_session.run("stepi 2")
        for req in (None, "worker3"):
            d_pre = _backtrace(gdb_session, req)
            _assert_completed_owner(
                d_pre, stage="trap-prologue-saving", sources={"current"}
            )
            assert any(
                "gdr_bt_live_leaf" in (f["symbol"] or "") for f in d_pre["frames"]
            ), d_pre

        _stop_at_address(gdb_session, prof.trap_call_start)
        d_s0 = _backtrace(gdb_session)
        # At trap_call_start stage transitions to trap-dispatch (crosses exception hook).
        assert d_s0["complete"] is True and d_s0["task"] == "worker3", d_s0

        # 2. trap-epilogue without switch + spurious_interrupt non-switch restore.
        bp_no_sw = gdb_session.run(
            f"tbreak *{prof.trap_mv_sp_pc:#x} if rt_current_thread == &worker3_thread && rt_thread_switch_interrupt_flag == 0"
        )
        assert "Temporary breakpoint" in bp_no_sw, bp_no_sw
        assert "Temporary breakpoint" in gdb_session.run("continue", timeout=15)
        d_nosw = _backtrace(gdb_session)
        _assert_completed_owner(
            d_nosw, stage="trap-epilogue-before-switch-store", sources={"saved"}
        )
        assert any(
            "gdr_bt_live_leaf" in (f["symbol"] or "") for f in d_nosw["frames"]
        ), d_nosw

        if prof.epc_mode == "deferred-mepc" and prof.spurious_mret_pc is not None:
            _stop_at_address(gdb_session, prof.spurious_start)
            d_spur = _backtrace(gdb_session)
            _assert_completed_owner(d_spur, stage="restore-window", sources={"saved"})
            assert d_spur["task"] == "worker3", d_spur
            _stop_at_address(gdb_session, prof.spurious_mret_pc)
            d_smret = _backtrace(gdb_session)
            _assert_completed_owner(
                d_smret, stage="restore-sp-popped", sources={"current"}
            )
            assert d_smret["task"] == "worker3", d_smret

        # 3. trap-epilogue WITH pending switch: flag=1 -> sw zero,flag -> store_from -> load_to.
        gdb_session.run("set var gdr_irq_hold = 0")
        bp_sw = gdb_session.run(
            f"tbreak *{prof.trap_mv_sp_pc:#x} if rt_thread_switch_interrupt_flag != 0"
        )
        assert "Temporary breakpoint" in bp_sw, bp_sw
        assert "Temporary breakpoint" in gdb_session.run("continue", timeout=20)
        from_name = _rv_thread_name(
            gdb_session, "rt_interrupt_from_thread", from_sp_ptr=True
        )
        to_name = _rv_thread_name(
            gdb_session, "rt_interrupt_to_thread", from_sp_ptr=True
        )
        assert from_name and to_name and from_name != to_name, (from_name, to_name)

        d_sw_before = _backtrace(gdb_session)
        _assert_completed_owner(
            d_sw_before, stage="trap-epilogue-before-switch-store", sources={"saved"}
        )
        assert d_sw_before["task"] == from_name, d_sw_before
        _assert_completed_owner(
            _backtrace(gdb_session, to_name),
            stage="trap-epilogue-before-switch-store",
            sources={"saved", "unstarted"},
        )

        # Past `sw zero, flag` and `store sp, (from_thread)` -> trap-epilogue-before-switch-load.
        _stop_at_address(gdb_session, prof.trap_load_to_pc)
        d_load = _backtrace(gdb_session)
        _assert_completed_owner(
            d_load, stage="trap-epilogue-before-switch-load", sources={"saved"}
        )
        assert d_load["task"] == from_name, d_load

        # Step past `LOAD sp, (to_thread)`: incoming thread now owns live sp!
        gdb_session.run("stepi 1")
        d_after = _backtrace(gdb_session)
        assert d_after["complete"] is True and d_after["task"] == to_name, d_after
        assert any(
            (
                "scheduler stage trap-epilogue-after-switch-load " in m
                or "scheduler stage restore-window " in m
            )
            for m in d_after["messages"]
        ), d_after
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


@_riscv_skip()
def test_riscv_trap_nested_e310_and_broken_profile_fail_closed(gdb_session):
    prof = _rv_profile()
    try:
        _rv_hold_and_break(gdb_session, f"*{prof.trap_mv_sp_pc:#x}")
        broken = _json_line(
            gdb_session.run_python(
                """
import json
from core.adapter import active
import rtthread.backtrace.unwind_gdb as bt

original = bt.build_riscv_scheduler_profile
bt.build_riscv_scheduler_profile = lambda _elf: (None, "scheduler-profile-mismatch:test")
try:
    report = bt.backtrace(active(), "worker3")
finally:
    bt.build_riscv_scheduler_profile = original
print(json.dumps({"complete": report.complete, "stop_reason": report.stop_reason}))
"""
            )
        )
        assert broken == {
            "complete": False,
            "stop_reason": "scheduler-profile-mismatch:test",
        }, broken

        if prof.epc_mode == "deferred-mepc":
            nested = _json_line(
                gdb_session.run_python(
                    """
import json
from core.adapter import active
import rtthread.backtrace.unwind_gdb as bt

orig_eval = bt.eval_identifier
orig_read = bt.read_int
nest_token = object()
bt.eval_identifier = lambda name: nest_token if name == "rt_interrupt_nest" else orig_eval(name)
bt.read_int = lambda val: 1 if val is nest_token else orig_read(val)
try:
    report = bt.backtrace(active(), "worker3")
finally:
    bt.eval_identifier = orig_eval
    bt.read_int = orig_read
print(json.dumps({"complete": report.complete, "stop_reason": report.stop_reason}))
"""
                )
            )
            assert nested == {
                "complete": False,
                "stop_reason": "nested-trap-epc-unproven",
            }, nested
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


@_riscv_skip()
def test_riscv_synchronous_fault_unwinds_and_unknown_cause_fails_closed(gdb_session):
    """RISC-V synchronous faults unwind across trap frames while unknown causes fail closed."""
    xlen = 64 if SPEC.target == "rv64" else 32
    try:
        _rv_hold_and_break(gdb_session, "handle_trap")
        gdb_session.run("stepi 2")
        probe = _json_line(
            gdb_session.run_python(
                f"""
import dataclasses, json
from core.adapter import active
import rtthread.backtrace.unwind_gdb as bt

orig_snap = bt._riscv_snapshot
def forced_cause(cause):
    def _wrap(*, xlen=32):
        snap, reason = orig_snap(xlen=xlen)
        if snap is None:
            return None, reason
        return dataclasses.replace(snap, mcause=cause, mtval=0xDEADBEEF), "ok"
    return _wrap

real_snap, _ = orig_snap(xlen={xlen})
bt._riscv_snapshot = forced_cause(2)
try:
    sync_rep = bt.backtrace(active(), "worker3")
finally:
    bt._riscv_snapshot = orig_snap

bt._riscv_snapshot = forced_cause(24)
try:
    bad_rep = bt.backtrace(active(), "worker3")
finally:
    bt._riscv_snapshot = orig_snap

print(json.dumps({{
    "mepc": real_snap.mepc if real_snap is not None else None,
    "complete": sync_rep.complete,
    "stop_reason": sync_rep.stop_reason,
    "methods": [f.method for f in sync_rep.frames],
    "pcs": [f.pc for f in sync_rep.frames],
    "symbols": [f.symbol or "" for f in sync_rep.frames],
    "messages": sync_rep.messages,
    "bad_complete": bad_rep.complete,
    "bad_reason": bad_rep.stop_reason,
}}))
"""
            )
        )
        assert probe["complete"] is True, probe
        assert probe["stop_reason"] == "synchronous-fault", probe
        assert "exception" in probe["methods"], probe
        exc_idx = probe["methods"].index("exception")
        assert probe["pcs"][exc_idx] == probe["mepc"], probe
        assert any("Exception:IllegalInstruction" in m for m in probe["messages"]), (
            probe
        )
        assert any("worker3_entry" in sym for sym in probe["symbols"]), probe
        assert probe["bad_complete"] is False, probe
        assert probe["bad_reason"] == "exception-frame-type-unproven", probe
    finally:
        _clear_breakpoints(gdb_session)
        gdb_session.run("set var gdr_irq_hold = 0")


@pytest.mark.skipif(
    SPEC.target != "mps2-an505",
    reason="requires RT-Thread Armv8-M Mainline PSPLIM fixture",
)
def test_armv8m_nonzero_psplim_admits_valid_and_refuses_invalid_bounds(gdb_session):
    """Valid 8B-aligned PSPLIM passes saved and live unwinds; invalid bounds refuse."""
    saved_probe = _json_line(
        gdb_session.run_python(
            """
import gdb, json
from core.adapter import active
import rtthread.backtrace.unwind_gdb as bt

thread = gdb.lookup_static_symbol("worker1_thread").value()
sp = int(thread["sp"])
low = int(thread["stack_addr"])
inf = gdb.selected_inferior()
orig_psplim_bytes = bytes(inf.read_memory(sp + 8, 4))
valid_psplim = (low + 7) & ~7

def _write_slot(val):
    inf.write_memory(sp + 8, val.to_bytes(4, "little"))

try:
    _write_slot(valid_psplim)
    ok_rep = bt.backtrace(active(), "worker1")
    _write_slot(valid_psplim + 1)
    unaligned_rep = bt.backtrace(active(), "worker1")
    exc_ret = int.from_bytes(inf.read_memory(sp + 4, 4), "little")
    caller_sp = sp + (216 if exc_ret == 0xFFFFFFED else 80)
    _write_slot(caller_sp + 8)
    above_rep = bt.backtrace(active(), "worker1")
finally:
    inf.write_memory(sp + 8, orig_psplim_bytes)

print(json.dumps({
    "ok_complete": ok_rep.complete,
    "ok_stop": ok_rep.stop_reason,
    "unaligned_reason": unaligned_rep.stop_reason,
    "above_reason": above_rep.stop_reason,
}))
"""
        )
    )
    assert saved_probe == {
        "ok_complete": True,
        "ok_stop": "task-exit",
        "unaligned_reason": "unsupported-extension-context",
        "above_reason": "unsupported-extension-context",
    }, saved_probe

    _stop_symbol(gdb_session, "gdr_bt_live_stop")
    live_info = _json_line(
        gdb_session.run_python(
            """
import gdb, json
frame = gdb.newest_frame()
print(json.dumps({
    "psplim": int(frame.read_register("psplim")),
    "control": int(frame.read_register("control")),
    "tz": int(gdb.parse_and_eval("rt_trustzone_current_context")),
}))
"""
        )
    )
    assert live_info["control"] == 2 and (live_info["psplim"] & 0x7) == 0, live_info
    ok_live = _backtrace(gdb_session, "worker3")
    assert ok_live["complete"] is True and ok_live["stop_reason"] == "task-exit", (
        ok_live
    )
    try:
        gdb_session.run("set var rt_trustzone_current_context = 1")
        bad_live = _backtrace(gdb_session, "worker3")
        assert bad_live["complete"] is False, bad_live
        assert bad_live["stop_reason"] == "unsupported-extension-context", bad_live
    finally:
        gdb_session.run(f"set var rt_trustzone_current_context = {live_info['tz']}")


@contextlib.contextmanager
def _isolated_mprofile_session():
    profile = replace(get_rtthread_qemu_profile(GDR_ROOT, SPEC), init_command=None)
    check_tools(profile, SPEC.gdb)
    if not has_embedded_python(SPEC.gdb):
        pytest.skip("GDB has no embedded Python")
    qemu = QemuSession(profile)
    qemu.start(SPEC.boot_wait, paused=True)
    session = GdbSession(SPEC.gdb, profile, qemu.gdb_port, GDR_ROOT)
    try:
        session.start()
        session.run(f"gdr init rtthread {SPEC.version}", timeout=30)
        yield session
    finally:
        session.stop()
        qemu.stop()


@_irq_skip()
def test_mprofile_nmi_and_debug_monitor_cross_stack():
    """Hardware NMI (IPSR=2) and DebugMonitor (IPSR=12) unwind across MSP->PSP."""
    with _isolated_mprofile_session() as gdb:
        _stop_symbol(gdb, "gdr_bt_live_stop")
        pre = _json_line(
            gdb.run_python(
                'import gdb, json; print(json.dumps({"pc": int(gdb.newest_frame().read_register("pc")) & ~1}))'
            )
        )
        gdb.run("tbreak Default_Handler")
        gdb.run("set *(unsigned int *)0xE000ED04 = 0x80000000")
        nmi_hit = gdb.run("continue", timeout=15)
        assert "Temporary breakpoint" in nmi_hit, nmi_hit
        ipsr_info = _json_line(
            gdb.run_python(
                'import gdb, json; x = int(gdb.newest_frame().read_register("xpsr")); print(json.dumps({"xpsr": x, "ipsr": x & 0x1FF}))'
            )
        )
        assert ipsr_info["ipsr"] == 2, ipsr_info
        nmi_bt = _backtrace(gdb, "worker3")
        assert nmi_bt["complete"] is True and nmi_bt["stop_reason"] == "task-exit", (
            nmi_bt
        )
        exc_nmi = next(f for f in nmi_bt["frames"] if f["method"] == "exception")
        assert exc_nmi["pc"] == pre["pc"], (exc_nmi, pre)

        if SPEC.target in {"mps2-an385", "mps2-an386", "mps2-an505"}:
            dm_xpsr = (ipsr_info["xpsr"] & ~0x1FF) | 12
            gdb.run(f"set $xpsr = {dm_xpsr:#x}")
            dm_bt = _backtrace(gdb, "worker3")
            assert dm_bt["complete"] is True and dm_bt["stop_reason"] == "task-exit", (
                dm_bt
            )
            exc_dm = next(f for f in dm_bt["frames"] if f["method"] == "exception")
            assert exc_dm["pc"] == pre["pc"], (exc_dm, pre)


@_sched_skip()
def test_boot_after_current_thread_and_rt_schedule_c_window():
    """Boot MSP after rt_current_thread assignment and mid-rt_schedule C window unwind cleanly."""
    with _isolated_mprofile_session() as gdb:
        # 1. Pre-first-task: rt_system_scheduler_start already set rt_current_thread, SP still MSP.
        _stop_symbol(gdb, "rt_schedule_remove_thread")
        boot_probe = _json_line(
            gdb.run_python(
                """
import gdb, json
frame = gdb.newest_frame()
cur = int(gdb.parse_and_eval("rt_current_thread"))
print(json.dumps({
    "current": cur,
    "cur_name": gdb.parse_and_eval("rt_current_thread->name").string().rstrip("\\x00") if cur else "",
    "flag": int(gdb.parse_and_eval("rt_thread_switch_interrupt_flag")),
    "ipsr": int(frame.read_register("xpsr")) & 0x1FF,
    "psp": int(frame.read_register("psp")),
}))
"""
            )
        )
        assert boot_probe["current"] != 0 and boot_probe["flag"] == 0, boot_probe
        assert boot_probe["ipsr"] == 0 and boot_probe["psp"] == 0, boot_probe

        boot_bt = _backtrace(gdb)
        boot_syms = [f["symbol"] or "" for f in boot_bt["frames"]]
        assert boot_bt["task"] == "<current>" and boot_bt["source"] == "current", (
            boot_bt
        )
        assert "rt_schedule_remove_thread" in boot_syms[0], boot_bt
        assert any("rt_system_scheduler_start" in s for s in boot_syms), boot_bt
        assert any("Reset_Handler" in s for s in boot_syms), boot_bt
        assert boot_bt["complete"] is True or boot_bt["stop_reason"] in {
            "cfi-missing",
            "no-unwind-info",
            "return-address-zero",
        }, (boot_bt["complete"], boot_bt["stop_reason"], boot_syms)

        to_bt = _backtrace(gdb, boot_probe["cur_name"])
        assert to_bt["complete"] is True and to_bt["source"] == "unstarted", to_bt

        # 2. Mid-rt_schedule C switch window: rt_current_thread swapped to to_thread while flag == 0.
        prof = _profile()
        _stop_at_address(gdb, prof.pendsv_exception_return)
        _clear_breakpoints(gdb)
        sched_probe = None
        for _ in range(30):
            _stop_symbol(gdb, "rt_schedule_remove_thread")
            probe_src = """
import gdb, json
from core.adapter import active
from rtthread.adapter import value_to_thread
from rtthread.navigation import get_current_thread, iter_threads

frame = gdb.newest_frame()
cur = get_current_thread()
layout = active().layout
cur_t = value_to_thread(cur, layout, collect_stack_metrics=False)
xpsr = int(frame.read_register("xpsr"))
sp = int(frame.read_register("psp" if (xpsr & 0x1FF) else "sp"))
owner = None
for v in iter_threads(layout):
    t = value_to_thread(v, layout, collect_stack_metrics=False)
    if t.stack_addr <= sp <= t.stack_addr + t.stack_size:
        owner = t
        break
print(json.dumps({
    "cur_name": cur_t.name,
    "cur_addr": cur_t.address,
    "owner_name": owner.name if owner else None,
    "owner_addr": owner.address if owner else 0,
    "flag": int(gdb.parse_and_eval("rt_thread_switch_interrupt_flag")),
}))
"""
            cand = _json_line(gdb.run_python(probe_src))
            if (
                cand["owner_addr"]
                and cand["owner_addr"] != cand["cur_addr"]
                and cand["flag"] == 0
            ):
                sched_probe = cand
                break
        assert sched_probe is not None, "did not observe rt_schedule owner handoff"

        from_name = sched_probe["owner_name"]
        to_name = sched_probe["cur_name"]
        cur_bt = _backtrace(gdb)
        assert cur_bt["complete"] is True and cur_bt["source"] == "current", cur_bt
        assert cur_bt["task"] == from_name, (cur_bt, sched_probe)
        cur_syms = [f["symbol"] or "" for f in cur_bt["frames"]]
        assert "rt_schedule_remove_thread" in cur_syms[0], cur_syms
        assert any("rt_schedule" in s for s in cur_syms), cur_syms

        from_bt = _backtrace(gdb, from_name)
        assert from_bt["frames"] == cur_bt["frames"], (from_bt, cur_bt)

        to_saved = _backtrace(gdb, to_name)
        assert to_saved["complete"] is True, to_saved
        assert to_saved["source"] in {"saved", "unstarted"}, to_saved
        assert not any(
            "rt_schedule_remove_thread" in (f["symbol"] or "")
            for f in to_saved["frames"]
        ), to_saved
