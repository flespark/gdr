"""Real RV64 .eh_frame unwinding without another metadata source or fallback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.unwind_meta import ElfImage
from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import (
    GdbSession,
    QemuProfile,
    QemuSession,
    check_tools,
)

ROOT = Path(__file__).resolve().parents[2]
SPEC = load_integration_spec()


@pytest.fixture(scope="module")
def eh_frame_image(tmp_path_factory):
    toolchain = os.environ.get("XPACK_RISCV_TOOLCHAIN_PATH", "")
    compiler = (
        str(Path(toolchain) / "riscv-none-elf-gcc")
        if toolchain
        else "riscv-none-elf-gcc"
    )
    if shutil.which(compiler) is None:
        pytest.skip(f"missing compiler: {compiler}")
    output = tmp_path_factory.mktemp("eh-frame")
    result = subprocess.run(
        ["bash", str(ROOT / "ci/unwind/build-fixture.sh"), str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    image = ElfImage.from_path(output / "unwind.elf")
    assert image is not None and image.section(".eh_frame") is not None
    assert image.section(".debug_frame") is None
    assert image.section(".ARM.exidx") is None
    frames = (output / "frames.txt").read_text()
    assert "DW_CFA_offset_extended: r128" in frames
    assert "DW_CFA_offset_extended: r1" in frames
    return output


@pytest.fixture
def eh_frame_session(eh_frame_image):
    profile = QemuProfile(
        rtos="none",
        version="none",
        target="unwind-rv64",
        qemu_binary="qemu-system-riscv64",
        machine="virt",
        gdb_architecture="riscv:rv64",
        elf_path=eh_frame_image / "unwind.elf",
        firmware_path=eh_frame_image / "unwind.bin",
        firmware_option="-bios",
        ready_marker="",
        pointer_width=8,
        qemu_args=("-cpu", "rv64"),
        extra_env={"GDR_RTOS": "", "GDR_VERSION": ""},
    )
    check_tools(profile, SPEC.gdb)
    qemu = QemuSession(profile)
    session = GdbSession(SPEC.gdb, profile, qemu.gdb_port, ROOT)
    try:
        qemu.start(SPEC.boot_wait, paused=True)
        session.start()
        yield session
    finally:
        session.stop()
        qemu.stop()


def _trace(session, *, signal=False, damage="none"):
    checkpoint = "cfi_signal_checkpoint" if signal else "cfi_checkpoint"
    if signal:
        session.run("set $pc = cfi_signal_entry")
    session.run(f"break {checkpoint}")
    output = session.run("continue", timeout=20)
    assert "Breakpoint 1" in output, output
    output = session.run_python(
        f"""
import json
from dataclasses import replace
from core.unwind import ElfImage, SeedFrame, UnwindMetadata, unwind_chain
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
