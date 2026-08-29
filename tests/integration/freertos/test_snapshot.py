"""Static ELF snapshot checks (no QEMU; file-only GDB).

The snapshot ELF carries pre-initialised ``.data`` scheduler structures plus
the diagnostic *negative* fixtures a healthy kernel can never produce: a
timer on the overflow list, a heap whose free-list/linear/counter triple
disagrees, and corrupted scheduler lists.  A second, heap-only ELF carries
the free-list-member-with-allocated-bit corruption, because one heap symbol
set can only show one corruption.
"""

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
CACHED_HEAP_SNAPSHOT_ELF = FIXTURE_CACHE / "snapshot" / "snapshot_heap.elf"
SNAPSHOT_ELF = SNAPSHOT_DIR / "out" / "snapshot.elf"
HEAP_SNAPSHOT_ELF = SNAPSHOT_DIR / "out" / "snapshot_heap.elf"
GDB_BIN = os.environ.get("GDR_GDB", "gdb")

# The overflow timer's expiry wraps into the next tick epoch (timers.c
# prvInsertTimerInActiveList): real expiry = (2^tick_bits - tick) + expiry.
_TICK = 42
_OVERFLOW_EXPIRY = 5
_EXPECTED_OVERFLOW_EXPIRES_IN = (2**32 - _TICK) + _OVERFLOW_EXPIRY


def _snapshot_sources_newer_than(elf: Path) -> bool:
    """Whether any snapshot source is newer than a cached ELF.

    Mirrors ``ci/freertos/run-qemu-matrix.sh::fixture_sources_newer_than``:
    a stale cached fixture would silently run the *old* negative data against
    the new assertions -- the exact false-pass trap the live-lane fix
    addressed -- so the snapshot lane rebuilds when its sources changed.
    """
    if not elf.exists():
        return True
    sources = [
        SNAPSHOT_DIR / "snapshot.c",
        SNAPSHOT_DIR / "snapshot_heap.c",
        SNAPSHOT_DIR / "FreeRTOSConfig.h",
        SNAPSHOT_DIR / "linker.ld",
        BUILD_SCRIPT,
    ]
    return any(
        source.exists() and source.stat().st_mtime > elf.stat().st_mtime
        for source in sources
    )


def _ensure_elf(elf: Path, cached: Path, source: str, cache_name: str) -> Path:
    if os.environ.get("GDR_FORCE_BUILD") != "1":
        for candidate in (cached, elf):
            if candidate.exists() and not _snapshot_sources_newer_than(candidate):
                return candidate
    gcc = shutil.which("arm-none-eabi-gcc")
    if gcc is None:
        pytest.skip("arm-none-eabi-gcc not available for the snapshot lane")
    elf.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "bash",
        str(BUILD_SCRIPT),
        "--out-elf",
        str(elf),
        "--source",
        str(SNAPSHOT_DIR / source),
        "--cache-name",
        cache_name,
        "--cache-dir",
        str(cached.parent),
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
    return elf


@pytest.fixture(scope="module")
def snapshot_session():
    elf = _ensure_elf(SNAPSHOT_ELF, CACHED_SNAPSHOT_ELF, "snapshot.c", "snapshot.elf")
    session = StaticElfSession(GDB_BIN, elf, GDR_ROOT)
    session.start()
    yield session
    session.stop()


@pytest.fixture(scope="module")
def heap_snapshot_session():
    elf = _ensure_elf(
        HEAP_SNAPSHOT_ELF,
        CACHED_HEAP_SNAPSHOT_ELF,
        "snapshot_heap.c",
        "snapshot_heap.elf",
    )
    session = StaticElfSession(GDB_BIN, elf, GDR_ROOT)
    session.start()
    yield session
    session.stop()


def _pairs(output: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for line in output.splitlines()
        if ": " in line
        for key, value in (line.split(": ", 1),)
    }


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
    pairs = _pairs(detail)
    assert pairs["HighWater"] == "12", detail


def test_overflow_timer_expires_in_uses_next_epoch(snapshot_session):
    """The overflow-list timer's ExpiresIn is (2^tick_bits - tick) + expiry.

    Live fixtures cannot run ~2^32 ticks, so the overflow epoch formula
    (freertos.timers.timer_expires_in) gets its live evidence here: the value
    must be the wrapped-epoch number, never ``expiry - tick`` (-37) nor the
    bare wrapped ``expiry`` (5) read as if it were still in the current epoch.
    """
    timers = snapshot_session.run("freertos timers", timeout=20)
    row = next(line for line in timers.splitlines() if "gdr_tmr_ovf" in line)
    cells = row.split()
    assert cells[0] == "gdr_tmr_ovf", row
    assert cells[5] == str(_EXPECTED_OVERFLOW_EXPIRES_IN), row
    assert cells[5] != str(_OVERFLOW_EXPIRY - _TICK), row
    assert cells[5] != str(_OVERFLOW_EXPIRY), row
    current = next(line for line in timers.splitlines() if "gdr_tmr_cur" in line)
    # The current-epoch timer still renders a plain remaining-tick value.
    assert current.split()[5] == "58", current


def test_overflow_timer_detail_names_its_list(snapshot_session):
    """frt timer gdr_tmr_ovf resolves the overflow epoch and the owner.

    The daemon list pointers swap on tick overflow, so the List label must
    name the underlying symbol; OwnerCheck verifies pvOwner against the
    object.  ``Check[TimerQueueItemSize]`` is expected here: the fixture
    provides the daemon handle (not xTimerQueue) to keep the queue
    discovery channel closed, so the daemon queue check cannot run.
    """
    detail = snapshot_session.run("freertos timer gdr_tmr_ovf", timeout=20)
    assert "List: overflow(xActiveTimerList2)" in detail, detail
    assert "OwnerCheck: ok" in detail, detail
    assert "ExpiresIn: " + str(_EXPECTED_OVERFLOW_EXPIRES_IN) in detail, detail
    assert "Checks: ok (3 verified)" in detail, detail
    assert "Check[TimerQueueItemSize]: skipped: unreadable" in detail, detail


def test_heap_cross_check_reports_three_numbers(snapshot_session):
    """The mismatch cell proves the no-plausible-numbers rule on real data.

    The free-list bytes (16) differ from the linear walk (48) differ from
    the kernel counter (64): the free list skips a block the linear walk
    sees as free.  CrossCheck must report all three concrete numbers and
    FreeSize must keep the kernel counter untouched by the walks.
    """
    output = snapshot_session.run("freertos heap", timeout=20)
    assert "CrossCheck: mismatch: free-list 16 vs linear 48 vs counter 64" in output, (
        output
    )
    pairs = _pairs(output)
    assert pairs["FreeSize"] == "64", output
    # The block table comes from the linear walk: both blocks, both free.
    assert "linear walk: 2 block(s), 2 free in 1 hole(s)" in output, output


def test_system_reports_corrupt_heap_status(snapshot_session):
    """frt system classifies the mismatched heap as corrupt."""
    output = snapshot_session.run("freertos system", timeout=20)
    assert "Heap status: corrupt" in output, output
    assert (
        "Check[HeapCrossCheck]: mismatch: free-list 16 vs linear 48 vs counter 64"
        in output
    ), output
    # The rest of the system stays consistent on the snapshot.
    assert "Task count: 8" in output, output
    assert "Scheduler state: running" in output, output


def test_free_list_allocated_bit_is_reported_as_corrupt(heap_snapshot_session):
    """A free-list member carrying the size_t MSB is a corrupt walk.

    cross_validate refuses to compare numbers over a corrupt walk (heap.py),
    so the verdict is ``unavailable: corrupt walk`` and the Blocks cell
    carries the ``(corrupt)`` suffix -- never a plausible-looking number.
    """
    output = heap_snapshot_session.run("freertos heap", timeout=20)
    assert "CrossCheck: unavailable: corrupt walk" in output, output
    assert "Blocks: 0 (corrupt)" in output, output
    assert "FreeSize: 16" in output, output
    # The linear walk still tiles the extent: one allocated 16-byte block.
    assert "linear walk: 1 block(s), 0 free in 0 hole(s)" in output, output


def test_corrupt_list_walk_is_reported_not_hung(snapshot_session):
    """A cyclic container list is reported, and the command still returns.

    The negcycle task claims a cyclic list as its container; the checks must
    surface the cycle (with its address) instead of hanging on the walk or
    silently reporting a short healthy list.
    """
    detail = snapshot_session.run("freertos task negcycle", timeout=20)
    assert "Check[ListIntegrity]: list cycle at 0x" in detail, detail
    assert "Check[ListCount]: walk corrupt (list cycle at 0x" in detail, detail
    assert "Check[ItemOwner]: event item pvOwner=0x" in detail, detail
    # The negative TCB's stack is all-zero while the population (idle tasks)
    # proves this build prefills, so the fill check must fail too (the
    # renderer strips the ``fail:`` prefix on Check[...] rows).
    assert "Check[StackFillPresent]: no 0xa5 fill at the stack base" in detail, detail
    assert "Traceback" not in detail


def test_list_count_mismatch_surfaces_in_checks(snapshot_session):
    """uxNumberOfItems and the walked length both appear in Check[ListCount]."""
    detail = snapshot_session.run("freertos task negcount", timeout=20)
    assert "Check[ListCount]: uxNumberOfItems 3 != 2 walked" in detail, detail
    assert "Traceback" not in detail


def test_list_index_violation_surfaces_on_smp(snapshot_session):
    """pxIndex parked off &xListEnd is an SMP violation, not a rotation."""
    detail = snapshot_session.run("freertos task negindex", timeout=20)
    assert "Check[ListIndex]: pxIndex 0x" in detail, detail
    assert "!= &xListEnd 0x" in detail, detail


def test_out_of_range_list_node_is_reported(snapshot_session):
    """A pxNext outside every loadable section stops the walk, not GDB."""
    detail = snapshot_session.run("freertos task negoff", timeout=20)
    assert "outside every loadable section" in detail, detail
    assert "Traceback" not in detail


def test_healthy_task_checks_pass(snapshot_session):
    """A scheduler-resident task's list checks all verify."""
    detail = snapshot_session.run("freertos task idle0", timeout=20)
    assert "Checks: ok (7 verified, 1 n/a)" in detail, detail
    assert "Check[" not in detail, detail
