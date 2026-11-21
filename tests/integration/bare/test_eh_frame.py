"""Real RV64 .eh_frame unwinding without another metadata source or fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.elf import ElfImage
from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import (
    GdbSession,
    QemuProfile,
    QemuSession,
    check_tools,
)

ROOT = Path(__file__).resolve().parents[3]
SPEC = load_integration_spec()
pytestmark = pytest.mark.skipif(
    SPEC.rtos != "bare" or SPEC.variant != "eh-frame",
    reason="requires the bare eh-frame fixture lane",
)


@pytest.fixture(scope="module")
def eh_frame_image():
    output = SPEC.fixture_dir()
    required = (
        "unwind.elf",
        "unwind.bin",
        "frames.txt",
        "unwind_hdr.elf",
        "unwind_hdr.bin",
        "hdr_frames.txt",
        "unwind_reloc.elf",
        "unwind_reloc.sym.elf",
        "unwind_reloc.bin",
        "reloc_frames.txt",
    )
    if not all((output / name).is_file() for name in required):
        pytest.skip(
            "missing eh-frame fixture; use python3 ci/test.py run bare qemu-virt-rv64 none eh-frame"
        )
    image = ElfImage.from_path(output / "unwind.elf")
    assert image is not None and image.section(".eh_frame") is not None
    assert image.section(".debug_frame") is None
    assert image.section(".ARM.exidx") is None
    frames = (output / "frames.txt").read_text()
    assert "DW_CFA_offset_extended: r128" in frames
    assert "DW_CFA_offset_extended: r1" in frames
    assert "DW_CFA_def_cfa_expression" in frames
    assert "DW_CFA_expression: r1 (ra)" in frames
    assert "DW_CFA_val_expression: r128" in frames

    hdr_image = ElfImage.from_path(output / "unwind_hdr.elf")
    assert hdr_image is not None and hdr_image.gnu_eh_frame is not None
    assert hdr_image.section(".eh_frame_hdr") is not None
    hdr_dump = (output / "hdr_frames.txt").read_text()
    assert "Table Encoding Format:" in hdr_dump
    assert "0x3b" in hdr_dump

    reloc_image = ElfImage.from_path(output / "unwind_reloc.elf")
    assert reloc_image is not None and reloc_image.section(".eh_frame") is not None
    assert reloc_image.section(".debug_frame") is None
    reloc_dump = (output / "reloc_frames.txt").read_text()
    for code in ("0028", "Augmentation data:"):
        assert code in reloc_dump
    return output


def _start_qemu_gdb(image_dir: Path, stem: str, *, elf_name: str | None = None):
    profile = QemuProfile(
        rtos="none",
        version="none",
        target="unwind-rv64",
        qemu_binary="qemu-system-riscv64",
        machine="virt",
        gdb_architecture="riscv:rv64",
        elf_path=image_dir / (elf_name or f"{stem}.elf"),
        firmware_path=image_dir / f"{stem}.bin",
        firmware_option="-bios",
        ready_marker="",
        pointer_width=8,
        qemu_args=("-cpu", "rv64"),
        extra_env={"GDR_RTOS": "", "GDR_VERSION": ""},
    )
    check_tools(profile, SPEC.gdb)
    qemu = QemuSession(profile)
    session = GdbSession(SPEC.gdb, profile, qemu.gdb_port, ROOT)
    qemu.start(SPEC.boot_wait, paused=True)
    session.start()
    return qemu, session


@pytest.fixture
def eh_frame_session(eh_frame_image):
    qemu, session = _start_qemu_gdb(eh_frame_image, "unwind")
    try:
        yield session
    finally:
        session.stop()
        qemu.stop()


@pytest.fixture(scope="module")
def hdr_session(eh_frame_image):
    qemu, session = _start_qemu_gdb(eh_frame_image, "unwind_hdr")
    try:
        session.run("break hdr_checkpoint")
        output = session.run("continue", timeout=20)
        assert "Breakpoint 1" in output, output
        yield session
    finally:
        session.stop()
        qemu.stop()


@pytest.fixture(scope="module")
def reloc_session(eh_frame_image):
    qemu, session = _start_qemu_gdb(
        eh_frame_image, "unwind_reloc", elf_name="unwind_reloc.sym.elf"
    )
    try:
        session.run("break reloc_checkpoint")
        output = session.run("continue", timeout=20)
        assert "Breakpoint 1" in output, output
        yield session, eh_frame_image / "unwind_reloc.elf"
    finally:
        session.stop()
        qemu.stop()


def _trace(session, *, signal=False, chain="ordinary", damage="none"):
    if signal:
        checkpoint = "cfi_signal_checkpoint"
        session.run("set $pc = cfi_signal_entry")
    elif chain == "expression":
        checkpoint = "cfi_expr_checkpoint_ready"
        session.run("set $pc = cfi_expr_entry")
    else:
        checkpoint = "cfi_checkpoint"
    session.run(f"break {checkpoint}")
    output = session.run("continue", timeout=20)
    assert "Breakpoint 1" in output, output
    output = session.run_python(
        f"""
import json
from dataclasses import replace
from core.backtrace.unwind import ElfImage, SeedFrame, UnwindMetadata, unwind_chain
from core.gdb_bridge import loaded_object_path, read_bytes
elf = ElfImage.from_path(loaded_object_path())
section = elf.section('.eh_frame')
damage = {damage!r}
if damage == 'missing':
    elf = replace(elf, sections=tuple(s for s in elf.sections if s.name != '.eh_frame'))
elif damage != 'none':
    data = bytearray(section.data)
    if damage == 'truncated':
        data = data[:3]
    elif damage == 'expression':
        expression = bytes((0x13, 0x72, 0x08, 0x06))
        assert data.count(expression) == 1
        data = data.replace(expression, bytes((0x9c, 0x72, 0x08, 0x06)))
    else:
        encoding = bytes((122, 82, 0, 1, 0x7c, 1, 1, 0x1b))
        assert data.count(encoding) == 2
        data = data.replace(encoding, encoding[:-1] + bytes((0x2b,)))
    elf = replace(elf, sections=tuple(replace(s, data=bytes(data)) if s.name == '.eh_frame' else s for s in elf.sections))
metadata = UnwindMetadata.from_elf(elf)
frame = gdb.newest_frame()
registers = {{number: int(frame.read_register('x' + str(number))) for number in range(32)}}
pc, sp = int(frame.pc()), registers[2]
assert pc == elf.symbol_address({checkpoint!r}), hex(pc)
result = unwind_chain(
    SeedFrame(pc, sp, registers, source='current'), metadata, read=read_bytes,
    stack_low=elf.symbol_address('stack_low'), stack_high=elf.symbol_address('stack_top'),
    allow_frame_pointer=False,
)
print(json.dumps({{
    'reason': result.stop_reason, 'complete': result.complete, 'truncated': result.truncated,
    'frames': [{{'pc': f.pc, 'lookup': f.address_in_block, 'method': f.method,
        'symbol': elf.function_containing(f.address_in_block).name if elf.function_containing(f.address_in_block) else None}} for f in result.frames],
    'tables': len(metadata.cfi_tables), 'ehabi': metadata.ehabi is not None,
    'resume': elf.symbol_address('cfi_signal_resume'),
}}))
"""
    )
    for line in output.splitlines():
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(output)


def test_real_eh_frame_handles_extended_offsets_and_virtual_return_column(
    eh_frame_session,
):
    result = _trace(eh_frame_session)
    assert result["tables"] == 1 and not result["ehabi"]
    assert [f["symbol"] for f in result["frames"]] == [
        "cfi_checkpoint",
        "cfi_leaf",
        "cfi_middle",
        "cfi_root",
    ], result
    assert all(f["method"] == "cfi" for f in result["frames"][1:])
    assert all(f["lookup"] == f["pc"] - 1 for f in result["frames"][1:])
    assert result["reason"] == "cfi-return-address-undefined"
    assert not result["complete"] and not result["truncated"]


def test_real_eh_frame_executes_all_three_bounded_expression_rules(
    eh_frame_session,
):
    result = _trace(eh_frame_session, chain="expression")
    assert result["tables"] == 1 and not result["ehabi"]
    assert [f["symbol"] for f in result["frames"]] == [
        "cfi_expr_checkpoint",
        "cfi_expr_leaf",
        "cfi_expr_middle",
        "cfi_expr_root",
    ], result
    assert all(f["method"] == "cfi" for f in result["frames"][1:])
    assert all(f["lookup"] == f["pc"] - 1 for f in result["frames"][1:])
    assert result["reason"] == "cfi-return-address-undefined"
    assert not result["complete"] and not result["truncated"]


def test_real_signal_cie_retains_the_exact_boundary_pc(eh_frame_session):
    result = _trace(eh_frame_session, signal=True)
    assert [f["symbol"] for f in result["frames"]] == [
        "cfi_signal_checkpoint",
        "cfi_signal_resume",
    ], result
    assert (
        result["frames"][1]["lookup"] == result["frames"][1]["pc"] == result["resume"]
    )
    assert result["reason"] == "cfi-return-address-undefined"
    assert not result["complete"]


def test_forbidden_real_expression_stops_without_fallback(eh_frame_session):
    result = _trace(eh_frame_session, chain="expression", damage="expression")
    assert len(result["frames"]) == 1 and result["reason"] == "cfi-expression-forbidden"
    assert not result["complete"] and not result["truncated"]


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("missing", "no-unwind-info"),
        ("truncated", "cfi-truncated-length"),
        ("textrel", "cfi-fde-pointer-unavailable"),
    ],
)
def test_eh_frame_damage_cannot_be_masked_by_another_source(
    eh_frame_session, damage, reason
):
    result = _trace(eh_frame_session, damage=damage)
    assert len(result["frames"]) == 1 and result["reason"] == reason, result
    assert not result["complete"] and not result["truncated"]


def test_real_pt_gnu_eh_frame_hdr_binary_search_unwinds_on_demand(hdr_session):
    output = hdr_session.run_python(
        """
import json
from core.gdb_bridge import loaded_object_path, read_bytes
from core.backtrace.unwind import ElfImage, SeedFrame, UnwindMetadata, unwind_chain

elf = ElfImage.from_path(loaded_object_path())
metadata = UnwindMetadata.from_elf(elf)
table = metadata.cfi_tables[0]
frame = gdb.newest_frame()
registers = {n: int(frame.read_register(f"x{n}")) for n in range(32)}
pc, sp = int(frame.pc()), registers[2]
assert pc == elf.symbol_address("hdr_checkpoint"), hex(pc)
eager_count_before = len(table._fdes)
result = unwind_chain(
    SeedFrame(pc, sp, registers, source="current"),
    metadata,
    read=read_bytes,
    stack_low=elf.symbol_address("stack_low"),
    stack_high=elf.symbol_address("stack_top"),
    allow_frame_pointer=False,
)
print(json.dumps({
    "indexed": table.indexed,
    "index_error": table.index_error,
    "eager_before": eager_count_before,
    "cached_fdes": len(table._indexed_fdes),
    "miss_none": table.row_for(0x1000) is None,
    "reason": result.stop_reason,
    "frames": [
        elf.function_containing(f.address_in_block).name
        if elf.function_containing(f.address_in_block)
        else None
        for f in result.frames
    ],
}))
"""
    )
    payload = next(
        json.loads(line) for line in output.splitlines() if line.startswith("{")
    )
    assert payload["indexed"] is True and payload["index_error"] is None
    assert payload["eager_before"] == 0
    assert payload["cached_fdes"] == 4
    assert payload["miss_none"] is True
    assert payload["reason"] == "cfi-return-address-undefined"
    assert payload["frames"] == [
        "hdr_checkpoint",
        "hdr_leaf",
        "hdr_middle",
        "hdr_root",
    ]


@pytest.mark.parametrize(
    "mode,expected_idx_err,expected_stop",
    [
        ("bad_version", "eh-hdr-version-unsupported", "cfi-return-address-undefined"),
        ("unsorted", "eh-hdr-unsorted", "cfi-return-address-undefined"),
        ("truncated_hdr", "eh-hdr-truncated", "cfi-return-address-undefined"),
        ("truncated_eh", "eh-hdr-fde-out-of-range", "cfi-truncated-length"),
    ],
)
def test_real_eh_frame_hdr_degrades_safely_without_masking(
    hdr_session, mode, expected_idx_err, expected_stop
):
    output = hdr_session.run_python(
        f"""
import json
from dataclasses import replace
from core.gdb_bridge import loaded_object_path, read_bytes
from core.backtrace.unwind import ElfImage, SeedFrame, UnwindMetadata, unwind_chain

elf = ElfImage.from_path(loaded_object_path())
mode = {mode!r}
addr, hdr = elf.gnu_eh_frame
buf = bytearray(hdr)
if mode == "bad_version":
    buf[0] = 99
    mut = replace(
        elf,
        gnu_eh_frame=(addr, bytes(buf)),
        sections=tuple(
            replace(s, data=bytes(buf)) if s.name == ".eh_frame_hdr" else s
            for s in elf.sections
        ),
    )
elif mode == "unsorted":
    # Swap the first two 8-byte (loc, fde) entries inside the 36-entry table.
    e0 = bytes(buf[12:20])
    e1 = bytes(buf[20:28])
    buf[12:20] = e1
    buf[20:28] = e0
    mut = replace(
        elf,
        gnu_eh_frame=(addr, bytes(buf)),
        sections=tuple(
            replace(s, data=bytes(buf)) if s.name == ".eh_frame_hdr" else s
            for s in elf.sections
        ),
    )
elif mode == "truncated_hdr":
    short = bytes(buf[:16])
    mut = replace(
        elf,
        gnu_eh_frame=(addr, short),
        sections=tuple(
            replace(s, data=short) if s.name == ".eh_frame_hdr" else s
            for s in elf.sections
        ),
    )
else:
    mut = replace(
        elf,
        sections=tuple(
            replace(s, data=s.data[:3]) if s.name == ".eh_frame" else s
            for s in elf.sections
        ),
    )
metadata = UnwindMetadata.from_elf(mut)
table = metadata.cfi_tables[0]
frame = gdb.newest_frame()
registers = {{n: int(frame.read_register(f"x{{n}}")) for n in range(32)}}
result = unwind_chain(
    SeedFrame(int(frame.pc()), registers[2], registers, source="current"),
    metadata,
    read=read_bytes,
    stack_low=elf.symbol_address("stack_low"),
    stack_high=elf.symbol_address("stack_top"),
    allow_frame_pointer=False,
)
print(json.dumps({{
    "indexed": table.indexed,
    "index_error": table.index_error,
    "reason": result.stop_reason,
    "frames": len(result.frames),
}}))
"""
    )
    payload = next(
        json.loads(line) for line in output.splitlines() if line.startswith("{")
    )
    assert payload["indexed"] is False
    assert payload["index_error"] == expected_idx_err
    assert payload["reason"] == expected_stop
    assert payload["frames"] == (1 if mode == "truncated_eh" else 4)


def test_real_encoded_pointers_require_explicit_context_and_unwind_live(reloc_session):
    session, reloc_elf = reloc_session
    output = session.run_python(
        f"""
import json
from core.gdb_bridge import read_bytes
from core.backtrace.unwind import ElfImage, EncodedPointerContext, SeedFrame, UnwindMetadata, unwind_chain

elf = ElfImage.from_path({str(reloc_elf)!r})
frame = gdb.newest_frame()
registers = {{n: int(frame.read_register(f"x{{n}}")) for n in range(32)}}
pc, sp = int(frame.pc()), registers[2]
assert pc == elf.symbol_address("reloc_checkpoint"), hex(pc)

def run_with(ctx):
    md = UnwindMetadata.from_elf(elf, pointer_context=ctx)
    res = unwind_chain(
        SeedFrame(pc, sp, registers, source="current"),
        md,
        read=read_bytes,
        stack_low=elf.symbol_address("stack_low"),
        stack_high=elf.symbol_address("stack_top"),
        allow_frame_pointer=False,
    )
    return {{
        "reason": res.stop_reason,
        "frames": [
            elf.function_containing(f.address_in_block).name
            if elf.function_containing(f.address_in_block)
            else None
            for f in res.frames
        ],
    }}

text_base = elf.symbol_address("text_base")
data_base = elf.symbol_address("data_base")
func_base = elf.symbol_address("reloc_funcrel")
full = EncodedPointerContext(
    text_base=text_base,
    data_base=data_base,
    function_base=func_base,
    read=read_bytes,
)
missing_each = {{
    "none": run_with(None),
    "no_text": run_with(EncodedPointerContext(data_base=data_base, function_base=func_base, read=read_bytes)),
    "no_data": run_with(EncodedPointerContext(text_base=text_base, function_base=func_base, read=read_bytes)),
    "no_func": run_with(EncodedPointerContext(text_base=text_base, data_base=data_base, read=read_bytes)),
    "no_read": run_with(EncodedPointerContext(text_base=text_base, data_base=data_base, function_base=func_base)),
}}
print(json.dumps({{"full": run_with(full), "missing": missing_each}}))
"""
    )
    payload = next(
        json.loads(line) for line in output.splitlines() if line.startswith("{")
    )
    assert payload["full"]["reason"] == "cfi-return-address-undefined"
    assert payload["full"]["frames"] == [
        "reloc_indirect",
        "reloc_funcrel",
        "reloc_middle",
        "reloc_root",
    ]
    expected_missing_frames = {
        "none": 1,
        "no_read": 1,
        "no_func": 2,
        "no_data": 3,
        "no_text": 4,
    }
    for case, outcome in payload["missing"].items():
        assert outcome["reason"] == "cfi-fde-pointer-unavailable", (case, outcome)
        assert len(outcome["frames"]) == expected_missing_frames[case], (case, outcome)


def test_real_indirect_pointer_zero_slot_in_ram_fails_closed(reloc_session):
    session, reloc_elf = reloc_session
    output = session.run_python(
        f"""
import json
from core.gdb_bridge import read_bytes
from core.backtrace.unwind import ElfImage, EncodedPointerContext, SeedFrame, UnwindMetadata, unwind_chain

elf = ElfImage.from_path({str(reloc_elf)!r})
slot = elf.symbol_address("indirect_slot")
saved = gdb.selected_inferior().read_memory(slot, 8).tobytes()
try:
    gdb.selected_inferior().write_memory(slot, b"\\x00" * 8)
    ctx = EncodedPointerContext(
        text_base=elf.symbol_address("text_base"),
        data_base=elf.symbol_address("data_base"),
        function_base=elf.symbol_address("reloc_funcrel"),
        read=read_bytes,
    )
    md = UnwindMetadata.from_elf(elf, pointer_context=ctx)
    frame = gdb.newest_frame()
    registers = {{n: int(frame.read_register(f"x{{n}}")) for n in range(32)}}
    res = unwind_chain(
        SeedFrame(int(frame.pc()), registers[2], registers, source="current"),
        md,
        read=read_bytes,
        stack_low=elf.symbol_address("stack_low"),
        stack_high=elf.symbol_address("stack_top"),
        allow_frame_pointer=False,
    )
finally:
    gdb.selected_inferior().write_memory(slot, saved)
print(json.dumps({{"reason": res.stop_reason, "frames": len(res.frames)}}))
"""
    )
    payload = next(
        json.loads(line) for line in output.splitlines() if line.startswith("{")
    )
    assert payload == {"reason": "cfi-fde-pointer-unavailable", "frames": 1}
