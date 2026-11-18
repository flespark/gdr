"""Pure GDB inspection helpers for merged FreeRTOS backtrace integration tests."""

from __future__ import annotations

import json
import struct

from freertos.arm_scheduler_switch import SchedulerProfile


def json_line(output: str) -> dict:
    """Return the first JSON object line from GDB/pytest output."""
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return json.loads(candidate)
    raise AssertionError(f"no JSON payload in output:\n{output}")


def envelope_data(output: str) -> dict:
    """Parse a schema_version=3 ok=True envelope and return its ``data`` dict."""
    envelope = json_line(output)
    assert envelope["schema_version"] == 3, envelope
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def read_u32_words(session, address: int, count: int) -> tuple[int, ...]:
    """Read ``count`` little-endian 32-bit words from inferior memory."""
    raw = session.run_python(
        f"import gdb\nprint(gdb.selected_inferior().read_memory({address:#x}, {count * 4}).hex())"
    )
    blob = bytes.fromhex(raw.strip().splitlines()[-1])
    return struct.unpack(f"<{count}I", blob)


def task_top_of_stack(session, name: str) -> int:
    """Read a task's ``pxTopOfStack`` through ``$gdr_task``."""
    return int(
        session.run_python(
            "import gdb\n"
            f"v = gdb.parse_and_eval('$gdr_task(\"{name}\")')\n"
            "print(int(v['pxTopOfStack']))\n"
        )
        .strip()
        .splitlines()[-1]
    )


def raw_irq_state(session, frame_register: str, isa: str) -> dict:
    """Capture raw hardware exception frame, bounds, and STKALIGN state."""
    return json_line(
        session.run_python(
            f"""
import gdb, json

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {{address:#x}}", to_string=True).split(" in section ")[0].strip()

frame_base = int(gdb.parse_and_eval("${frame_register}"))
psp = int(gdb.parse_and_eval("$psp"))
msp = int(gdb.parse_and_eval("$msp"))
sp = int(gdb.parse_and_eval("$sp"))
xpsr = int(gdb.parse_and_eval("$xpsr"))
control = int(gdb.parse_and_eval("$control"))
tcb = gdb.parse_and_eval("pxCurrentTCB")
if tcb.type.code == gdb.TYPE_CODE_PTR:
    tcb = tcb.dereference()
words = [u32(frame_base + 4 * index) for index in range(8)]
frame_pc = words[6]
frame_lr = words[5]
frame_xpsr = words[7]
restored_sp = frame_base + 0x20 + (4 if frame_xpsr & (1 << 9) else 0)
stkalign = True if {isa!r} == "armv6-m" else bool(u32(0xE000ED14) & (1 << 9))
print(json.dumps({{
    "frame_base": frame_base,
    "psp": psp,
    "msp": msp,
    "sp": sp,
    "xpsr": xpsr,
    "control": control,
    "words": words,
    "frame_pc": frame_pc,
    "frame_pc_symbol": symbol(frame_pc),
    "frame_lr": frame_lr,
    "frame_lr_symbol": symbol(frame_lr),
    "frame_xpsr": frame_xpsr,
    "restored_sp": restored_sp,
    "stack_base": int(tcb["pxStack"]),
    "stack_high": int(tcb["pxEndOfStack"]) + 4,
    "irq_low": int(gdb.parse_and_eval("&__gdr_irq_stack_low")),
    "irq_high": int(gdb.parse_and_eval("&__gdr_irq_stack_top")),
    "stkalign": stkalign,
}}))
"""
        )
    )


def raw_fault_frame(session) -> dict:
    """Capture stacked PSP fault frame plus CFSR/HFSR registers."""
    return json_line(
        session.run_python(
            """
import gdb, json

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {address:#x}", to_string=True).split(" in section ")[0].strip()

psp = int(gdb.parse_and_eval("$psp"))
msp = int(gdb.parse_and_eval("$msp"))
xpsr = int(gdb.parse_and_eval("$xpsr"))
words = [u32(psp + 4 * index) for index in range(8)]
tcb = gdb.parse_and_eval("pxCurrentTCB")
if tcb.type.code == gdb.TYPE_CODE_PTR:
    tcb = tcb.dereference()
print(json.dumps({
    "psp": psp,
    "msp": msp,
    "xpsr": xpsr,
    "words": words,
    "frame_pc": words[6],
    "frame_pc_symbol": symbol(words[6]),
    "frame_xpsr": words[7],
    "stack_base": int(tcb["pxStack"]),
    "stack_high": int(tcb["pxEndOfStack"]) + 4,
    "cfsr": u32(0xE000ED28),
    "hfsr": u32(0xE000ED2C),
}))
"""
        )
    )


def sched_state(session, prof: SchedulerProfile, r4_off: int, sw_size: int) -> dict:
    """Inspect TCB handles, saved slots, live registers, and fixture gates."""
    return json_line(
        session.run_python(
            f"""
import gdb, json

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {{address:#x}}", to_string=True).split(" in section ")[0].strip()

def tcb(name):
    address = int(gdb.parse_and_eval(name))
    value = gdb.parse_and_eval(f"(struct tskTaskControlBlock*){{address:#x}}")
    top = int(value["pxTopOfStack"])
    return {{
        "addr": address,
        "top": top,
        "base": int(value["pxStack"]),
        "end": int(value["pxEndOfStack"]) + 4,
        "saved": [u32(top + {r4_off} + 4 * index) for index in range(8)],
        "frame": [u32(top + {sw_size} + 4 * index) for index in range(8)],
    }}

handles = {{
    name: int(gdb.parse_and_eval(name))
    for name in ("gdr_sched_task_handle", "gdr_sched_tick_handle",
                 "gdr_sched_irq_handle", "gdr_sched_first_handle")
}}
tcbs = {{name: tcb(name) for name in handles}}
current = int(gdb.parse_and_eval("pxCurrentTCB"))
psp_val = int(gdb.parse_and_eval("$psp"))
print(json.dumps({{
    "pc": int(gdb.parse_and_eval("$pc")),
    "sp": int(gdb.parse_and_eval("$sp")),
    "msp": int(gdb.parse_and_eval("$msp")),
    "psp": psp_val,
    "psp_frame": [u32(psp_val + 4 * index) for index in range(8)] if psp_val else [],
    "saved_buffer": [u32(psp_val - {sw_size} + {r4_off} + 4 * index) for index in range(8)] if psp_val else [],
    "xpsr": int(gdb.parse_and_eval("$xpsr")),
    "lr": int(gdb.parse_and_eval("$lr")) & 0xFFFFFFFF,
    "regs": {{f"r{{index}}": int(gdb.parse_and_eval(f"$r{{index}}")) & 0xFFFFFFFF for index in range(13)}},
    "current": current,
    "current_symbol": symbol(current),
    "handles": handles,
    "tcbs": tcbs,
    "gate": int(gdb.parse_and_eval("gdr_sched_gate")),
    "phase": int(gdb.parse_and_eval("gdr_sched_phase")),
    "done": int(gdb.parse_and_eval("gdr_sched_done")),
    "irq_requested": int(gdb.parse_and_eval("gdr_sched_irq_requested")),
    "yield_count": int(gdb.parse_and_eval("gdr_sched_yield_count")),
    "first_started": int(gdb.parse_and_eval("gdr_sched_first_started")),
    "tick_count": int(gdb.parse_and_eval("xTickCount")),
    "pendsv_pc_symbol": symbol(int(gdb.parse_and_eval("&PendSV_Handler"))),
    "svc_pc_symbol": symbol(int(gdb.parse_and_eval("&{prof.svc_symbol}"))),
}}))
"""
        )
    )


def frame_at(session, address: int) -> dict:
    """Decode one 8-word basic hardware frame at ``address``."""
    return json_line(
        session.run_python(
            f"""
import gdb, json

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

def symbol(address):
    return gdb.execute(f"info symbol {{address:#x}}", to_string=True).split(" in section ")[0].strip()

words = [u32({address:#x} + 4 * index) for index in range(8)]
print(json.dumps({{
    "base": {address:#x},
    "words": words,
    "pc": words[6],
    "pc_symbol": symbol(words[6]),
    "xpsr": words[7],
    "ipsr": words[7] & 0x1FF,
}}))
"""
        )
    )


def compiled_sched_oracle(session, prof: SchedulerProfile) -> dict:
    """Read compiled scheduler transition instructions directly from target memory."""
    return json_line(
        session.run_python(
            f"""
import gdb, json

def u16(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 2)), "little")

def u32(address):
    return int.from_bytes(bytes(gdb.selected_inferior().read_memory(address, 4)), "little")

pendsv = int(gdb.parse_and_eval("&PendSV_Handler"))
svc = int(gdb.parse_and_eval("&{prof.svc_symbol}"))
start = int(gdb.parse_and_eval("&{prof.start_first_symbol}"))
switch = int(gdb.parse_and_eval("&vTaskSwitchContext"))
print(json.dumps({{
    "pendsv": pendsv, "svc": svc, "start": start, "switch": switch,
    "pendsv_entry": u32(pendsv),
    "pendsv_save": u32({prof.pendsv_save_pending:#x}),
    "pendsv_psp": u32({prof.pendsv_restored_pending_psp:#x}),
    "pendsv_return": u16({prof.pendsv_exception_return:#x}),
    "svc_entry": u32(svc),
    "svc_restored": u32({prof.svc_restored_pending_psp:#x}),
    "svc_return": u16({prof.svc_exception_return:#x}),
    "start_svc": u16({prof.start_first_svc:#x}),
    "switch_update": u16({prof.switch_update:#x}),
    "switch_after": u16({prof.switch_after_update:#x}),
    "sys_tick": int(gdb.parse_and_eval("&SysTick_Handler")),
    "timer0": int(gdb.parse_and_eval("&Timer0_Handler")),
    "timer1": int(gdb.parse_and_eval("&Timer1_Handler")),
}}))
"""
        )
    )


def current_task_name(session) -> str:
    """Return the active core-0 FreeRTOS task name."""
    return session.run_python(
        """
from core.adapter import active
from freertos.navigation import current_tasks, iter_tasks
from core.gdb_bridge import value_address
layout = active().layout
cur_addr = dict(current_tasks(layout))[0]
for value, _state, _core in iter_tasks(layout):
    if value_address(value) == cur_addr:
        print(value["pcTaskName"].string().rstrip("\\x00"))
        break
"""
    ).strip()


def verify_readonly_backtrace(session, task: str, *, include_tick: bool) -> dict:
    """Run ``freertos.backtrace_gdb.backtrace`` and assert registers/stacks stay unchanged."""
    extra = (
        ', str(gdb.parse_and_eval("pxCurrentTCB")), int(gdb.parse_and_eval("xTickCount"))'
        if include_tick
        else ""
    )
    return json_line(
        session.run_python(
            f"""
import gdb, hashlib, json
from core.adapter import active
import freertos.backtrace_gdb as bt

def snapshot():
    frame = gdb.newest_frame()
    registers = [int(frame.read_register(name)) for name in
                 [*(f"r{{i}}" for i in range(13)), "sp", "lr", "pc",
                  "msp", "psp", "xpsr", "control", "primask"]]
    task = gdb.parse_and_eval("pxCurrentTCB").dereference()
    spans = [(int(task["pxStack"]), int(task["pxEndOfStack"]) + 4),
             (int(gdb.parse_and_eval("&__gdr_irq_stack_low")),
              int(gdb.parse_and_eval("&__gdr_irq_stack_top")))]
    memory = [hashlib.sha256(bytes(gdb.selected_inferior().read_memory(low, high - low))).hexdigest()
              for low, high in spans]
    return (registers, memory{extra})

events = []
def changed(event):
    events.append(type(event).__name__)
registries = (gdb.events.cont, gdb.events.memory_changed, gdb.events.register_changed)
for registry in registries:
    registry.connect(changed)
try:
    before = snapshot()
    report = bt.backtrace(active(), {task!r})
    after = snapshot()
finally:
    for registry in registries:
        registry.disconnect(changed)
print(json.dumps({{
    "unchanged": before == after,
    "complete": report.complete,
    "stop": report.stop_reason,
    "transitions": len(report.transitions),
    "events": events,
}}))
"""
        )
    )


def m0_fixed_vector_probe(session) -> dict:
    """Verify Armv6-M reads CPUID and fixed vector entry without V7M registers."""
    return json_line(
        session.run_python(
            """
import json
from core.adapter import active
import freertos.backtrace_gdb as bt
original = bt.read_bytes
reads = []
def guarded(address, size):
    assert address not in (0xE000E004, 0xE000ED08, 0xE000ED14), hex(address)
    reads.append((address, size))
    return original(address, size)
bt.read_bytes = guarded
try:
    report = bt.backtrace(active(), "gdr_irq")
    limit = bt._armv6m_port_limit()
finally:
    bt.read_bytes = original
print(json.dumps({"complete": report.complete, "limit": limit,
                  "cpuid": (0xE000ED00, 4) in reads, "vector": (24 * 4, 4) in reads,
                  "transitions": len(report.transitions)}))
"""
        )
    )


def systick_probe(session) -> dict:
    """Return interrupted task name and live IPSR at a SysTick halt."""
    return json_line(
        session.run_python(
            """
import gdb, json
frame = gdb.newest_frame()
tcb = gdb.parse_and_eval("pxCurrentTCB")
if tcb.type.code == gdb.TYPE_CODE_PTR:
    tcb = tcb.dereference()
print(json.dumps({
    "task": tcb["pcTaskName"].string().rstrip("\\x00"),
    "ipsr": int(frame.read_register("xpsr")) & 0x1ff,
}))
"""
        )
    )


def riscv_trap_state(session) -> dict:
    """Read current TCB, mcause, mepc, and PC on QEMU RISC-V."""
    return json_line(
        session.run_python(
            """
import gdb, json
frame = gdb.newest_frame()
mask = (1 << (64 if 'rv64' in gdb.current_progspace().filename else 32)) - 1
tcb = int(gdb.parse_and_eval("pxCurrentTCB"))
mcause = int(frame.read_register("mcause")) & mask
mepc = int(frame.read_register("mepc")) & mask
print(json.dumps({"tcb": tcb, "mcause": mcause, "mepc": mepc, "pc": int(frame.pc()) & mask}))
"""
        )
    )


def scheduler_profile_override(session, reason: str, task: str | None = None) -> dict:
    """Temporarily force `build_scheduler_profile` to fail and return the bt summary."""
    return json_line(
        session.run_python(
            f"""
import json, gdb
import freertos.backtrace_gdb as bt
from core.adapter import active

original = bt.build_scheduler_profile
bt.build_scheduler_profile = lambda _elf: (None, {reason!r})
try:
    name = {task!r}
    if name is None:
        tcb = gdb.parse_and_eval("pxCurrentTCB")
        name = tcb["pcTaskName"].string().rstrip("\\\\x00")
    report = bt.backtrace(active(), name)
finally:
    bt.build_scheduler_profile = original
print(json.dumps({{
    "stop_reason": report.stop_reason,
    "frames": len(report.frames),
    "complete": report.complete,
}}))
"""
        )
    )


def encode_riscv_jal(pc: int, target: int, *, rd: int = 0) -> bytes:
    """Encode one 32-bit RISC-V ``jal`` instruction jumping from ``pc`` to ``target``."""
    offset = target - pc
    word = (
        (((offset >> 20) & 1) << 31)
        | (((offset >> 1) & 0x3FF) << 21)
        | (((offset >> 11) & 1) << 20)
        | (((offset >> 12) & 0xFF) << 12)
        | ((rd & 31) << 7)
        | 111
    ) & 0xFFFFFFFF
    return struct.pack("<I", word)


def riscv_isr_yield_owner_probe(session, active_tcb: int) -> dict:
    """Verify interrupted-task resolution when ``pxCurrentTCB`` already switched in ISR."""
    return json_line(
        session.run_python(
            f"""
import json
from core.adapter import active
from freertos.adapter import iter_converted_tasks
import freertos.backtrace_gdb as bt

ad = active()
other = next(t for t in iter_converted_tasks(ad.layout) if t.address != {active_tcb:#x})
orig_tcb = bt._current_tcb_address
orig_current = bt.current_tasks
bt._current_tcb_address = lambda: other.address
bt.current_tasks = lambda _layout: [(0, other.address)]
try:
    current_rep = bt.backtrace(ad)
    switched_rep = bt.backtrace(ad, other.name)
finally:
    bt._current_tcb_address = orig_tcb
    bt.current_tasks = orig_current

print(json.dumps({{
    "current_task": current_rep.task,
    "current_source": current_rep.source,
    "current_complete": current_rep.complete,
    "switched_source": switched_rep.source,
    "switched_complete": switched_rep.complete,
}}))
"""
        )
    )


def riscv_vectored_mtvec_probe(
    session,
    *,
    task: str,
    trap_entry: int,
    vec_base: int = 0x80040000,
    cause_code: int = 7,
    xlen: int = 32,
) -> dict:
    """Verify vectored ``mtvec`` unwinding via a synthetic ``jal`` vector slot."""
    slot_pc = vec_base + 4 * cause_code
    jal_bytes = encode_riscv_jal(slot_pc, trap_entry)
    return json_line(
        session.run_python(
            f"""
import dataclasses, json
from core.adapter import active
import freertos.backtrace_gdb as bt

jal = bytes.fromhex({jal_bytes.hex()!r})
orig_snap = bt._riscv_snapshot
orig_read = bt.read_bytes
def vectored_snap(*, xlen={xlen}):
    snap, reason = orig_snap(xlen=xlen)
    return (None, reason) if snap is None else (dataclasses.replace(snap, mtvec={vec_base:#x} | 1), "ok")
bt._riscv_snapshot = vectored_snap
bt.read_bytes = lambda addr, sz: jal[:sz] if addr == {slot_pc:#x} and sz <= 4 else orig_read(addr, sz)
try:
    rep = bt.backtrace(active(), {task!r})
finally:
    bt._riscv_snapshot = orig_snap
    bt.read_bytes = orig_read

print(json.dumps({{"complete": rep.complete, "stop_reason": rep.stop_reason}}))
"""
        )
    )
