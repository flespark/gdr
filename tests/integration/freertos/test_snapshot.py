"""Static ELF snapshot checks (no QEMU; file-only GDB)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.static_elf_harness import StaticElfSession

GDR_ROOT = Path(__file__).resolve().parents[3]
FREERTOS_CI_DIR = GDR_ROOT / "ci" / "freertos"
SNAPSHOT_DIR = FREERTOS_CI_DIR / "snapshot"
BUILD_SCRIPT = FREERTOS_CI_DIR / "build-fixture-snapshot.sh"
_DEFAULT_CACHE = Path.home() / "Project" / "gdr-fixture" / "freertos"
FIXTURE_CACHE = Path(os.environ.get("FREERTOS_FIXTURE_CACHE", str(_DEFAULT_CACHE)))
CACHED_SNAPSHOT_ELF = FIXTURE_CACHE / "snapshot" / "snapshot.elf"
SNAPSHOT_ELF = SNAPSHOT_DIR / "out" / "snapshot.elf"
GDB_BIN = os.environ.get("GDR_GDB", "gdb")


def _ensure_snapshot_elf() -> Path:
    if os.environ.get("GDR_FORCE_BUILD") != "1":
        for candidate in (CACHED_SNAPSHOT_ELF, SNAPSHOT_ELF):
            if candidate.exists():
                return candidate
    gcc = shutil.which("arm-none-eabi-gcc")
    if gcc is None:
        pytest.skip("arm-none-eabi-gcc not available for the snapshot lane")
    SNAPSHOT_ELF.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "bash",
        str(BUILD_SCRIPT),
        "--out-elf",
        str(SNAPSHOT_ELF),
    ]
    kernel = os.environ.get("FREERTOS_KERNEL_DIR")
    if kernel:
        command += ["--kernel-dir", kernel]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    # Reason: a broken snapshot build must fail the lane. Skipping here is how
    # the C layer previously stayed "green" in CI while decoding nothing.
    assert result.returncode == 0, (
        f"snapshot build failed ({result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    return SNAPSHOT_ELF


@pytest.fixture(scope="module")
def snapshot_session():
    elf = _ensure_snapshot_elf()
    session = StaticElfSession(GDB_BIN, elf, GDR_ROOT)
    session.start()
    yield session
    session.stop()


def test_smp_tasks_table_shows_two_running_cores(snapshot_session):
    """The snapshot has idle0 on core 0 and idle1 on core 1."""
    tasks = snapshot_session.run("freertos tasks", timeout=20)
    assert "idle0" in tasks, tasks
    assert "idle1" in tasks, tasks
    assert tasks.count("Running") >= 2, tasks
    assert "0" in tasks and "1" in tasks, tasks


def test_smp_task_detail_shows_run_state_and_affinity(snapshot_session):
    """frt task idle0 reports the SMP-only detail keys."""
    detail = snapshot_session.run("freertos task idle0", timeout=20)
    assert "RunState:" in detail, detail
    assert "CoreAffinity:" in detail, detail


def test_smp_yielding_without_core_is_blocked_not_ready(snapshot_session):
    """xTaskRunState==-2 with no pxCurrentTCBs match falls through to Blocked."""
    tasks = snapshot_session.run("freertos tasks", timeout=20)
    yielder = next(line for line in tasks.splitlines() if "yielder" in line)
    assert "Blocked" in yielder, yielder
    assert "Ready" not in yielder.split()


def test_runtime_percent_uses_array_total(snapshot_session):
    """V11 ulTotalRunTime[N] is summed across cores (400 / 4000 = 10.0%)."""
    detail = snapshot_session.run("freertos task idle0", timeout=20)
    assert "Runtime:" in detail, detail
    assert "Runtime%:" in detail, detail
    assert "10.0%" in detail, detail


def test_high_water_scan_counts_untouched_fill_words(snapshot_session):
    """The 0xa5a5a5a5 prefill below pxTopOfStack is reported as free words.

    idle0's stack holds 12 untouched fill words followed by 4 used words, so
    a watermark scan that stops at the first non-fill byte (or counts bytes
    instead of words) cannot produce 12.
    """
    detail = snapshot_session.run("freertos task idle0", timeout=20)
    pairs = {
        key.strip(): value.strip()
        for line in detail.splitlines()
        if ": " in line
        for key, value in (line.split(": ", 1),)
    }
    assert pairs["HighWater"] == "12", detail


def test_snapshot_negative_placeholder(snapshot_session):
    """Entry point for diagnostic negative cases.

    Corrupted lists, mismatched counters and failed heap cross-checks cannot be
    produced by a healthy kernel, so they are added here as pre-initialised
    ``.data`` structures once the diagnostics commands land.
    """
    assert snapshot_session.elf_path.exists()
