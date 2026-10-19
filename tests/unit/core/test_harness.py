"""Unit tests for the closed-loop integration environment loader."""

from __future__ import annotations

import atexit
from pathlib import Path

import pytest

from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import (
    QemuProfile,
    QemuSession,
)


def test_rtthread_defaults():
    spec = load_integration_spec({})
    assert spec.rtos == "rtthread"
    assert spec.version == "4.0.5"
    assert spec.target == "cortex-a9"
    assert spec.variant == ""
    assert (
        spec.elf_path() == spec.fixture_cache / "cortex-a9" / "4.0.5" / "rtthread.elf"
    )
    assert spec.firmware_path() == spec.elf_path()


def test_rtthread_rv64_firmware_is_the_bin():
    spec = load_integration_spec(
        {"GDR_RTOS": "rtthread", "GDR_QEMU_TARGET": "rv64", "GDR_VERSION": "4.1.1"}
    )
    assert spec.firmware_path() == spec.fixture_dir() / "rtthread.bin"
    assert spec.elf_path().name == "rtthread.elf"


def test_blank_overrides_mean_defaults():
    spec = load_integration_spec({"GDR_RTOS": "", "GDR_VERSION": "", "GDR_GDB": ""})
    assert spec.rtos == "rtthread"
    assert spec.version == "4.0.5"
    assert spec.gdb == "gdb"


def test_freertos_live_cache_layout():
    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_QEMU_TARGET": "mps2-an385",
            "GDR_VERSION": "10.4.6",
            "GDR_FIXTURE_VARIANT": "base",
            "FREERTOS_FIXTURE_CACHE": "/tmp/cache",
        }
    )
    assert spec.fixture_dir() == Path("/tmp/cache/mps2-an385/10.4.6/base")
    assert spec.elf_path() == spec.fixture_dir() / "freertos.elf"
    assert spec.firmware_path() == spec.elf_path()


def test_freertos_rv64_firmware_is_the_bin():
    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_QEMU_TARGET": "qemu-virt-rv64",
            "GDR_VERSION": "11.1.0",
            "GDR_FIXTURE_VARIANT": "rv64",
        }
    )
    assert spec.firmware_path() == spec.fixture_dir() / "freertos.bin"


def test_snapshot_is_an_ordinary_cache_cell():
    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_FIXTURE_VARIANT": "snapshot",
            "GDR_QEMU_TARGET": "mps2-an521",
            "GDR_VERSION": "11.1.0",
            "FREERTOS_FIXTURE_CACHE": "/tmp/cache",
        }
    )
    assert spec.variant == "snapshot"
    assert spec.fixture_dir() == Path("/tmp/cache/mps2-an521/11.1.0/snapshot")
    assert spec.elf_path() == spec.fixture_dir() / "freertos.elf"


def test_elf_override_repoints_gdb_and_the_qemu_boot_image():
    # ARM lane: QEMU boots the same ELF via -kernel.
    arm = load_integration_spec(
        {"GDR_RTOS": "freertos", "GDR_ELF_PATH": "/tmp/custom.elf"}
    )
    assert arm.elf_path() == Path("/tmp/custom.elf")
    assert arm.firmware_path() == Path("/tmp/custom.elf")
    # RV64 lane: QEMU boots the raw .bin sibling via -bios.
    rv64 = load_integration_spec(
        {
            "GDR_RTOS": "rtthread",
            "GDR_QEMU_TARGET": "rv64",
            "GDR_ELF_PATH": "/tmp/build/rtthread.elf",
        }
    )
    assert rv64.elf_path() == Path("/tmp/build/rtthread.elf")
    assert rv64.firmware_path() == Path("/tmp/build/rtthread.bin")


def test_unknown_rtos_is_an_error():
    with pytest.raises(RuntimeError, match="unknown GDR_RTOS"):
        load_integration_spec({"GDR_RTOS": "zephyr"})


def test_rtthread_profile_uses_loader_paths():
    from tests.support.rtthread_qemu_profiles import get_rtthread_qemu_profile

    spec = load_integration_spec(
        {
            "GDR_RTOS": "rtthread",
            "GDR_QEMU_TARGET": "rv64",
            "GDR_VERSION": "4.1.1",
            "RT_THREAD_FIXTURE_CACHE": "/tmp/rtt",
        }
    )
    profile = get_rtthread_qemu_profile(Path("/unused"), spec)
    assert profile.machine == "virt"
    assert profile.firmware_option == "-bios"
    assert profile.elf_path == Path("/tmp/rtt/rv64/4.1.1/rtthread.elf")
    assert profile.firmware_path == Path("/tmp/rtt/rv64/4.1.1/rtthread.bin")


def test_freertos_profile_derives_machine_from_target():
    from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile

    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_QEMU_TARGET": "qemu-virt-rv64",
            "GDR_VERSION": "11.1.0",
            "GDR_FIXTURE_VARIANT": "rv64",
            "FREERTOS_FIXTURE_CACHE": "/tmp/frt",
        }
    )
    profile = get_freertos_qemu_profile(Path("/unused"), spec)
    assert profile.machine == "virt"
    assert profile.firmware_option == "-bios"
    assert profile.elf_path == Path("/tmp/frt/qemu-virt-rv64/11.1.0/rv64/freertos.elf")
    assert profile.firmware_path == Path(
        "/tmp/frt/qemu-virt-rv64/11.1.0/rv64/freertos.bin"
    )


def test_snapshot_has_no_qemu_profile():
    from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile

    spec = load_integration_spec(
        {"GDR_RTOS": "freertos", "GDR_FIXTURE_VARIANT": "snapshot"}
    )
    with pytest.raises(RuntimeError, match="no QEMU profile"):
        get_freertos_qemu_profile(Path("/unused"), spec)


def _profile(tmp_path: Path) -> QemuProfile:
    elf = tmp_path / "fake.elf"
    elf.write_bytes(b"\x7fELF")
    return QemuProfile(
        rtos="freertos",
        version="10.3.1",
        target="fake",
        qemu_binary="/usr/bin/true",
        machine="fake",
        gdb_architecture="arm",
        elf_path=elf,
        firmware_path=elf,
        firmware_option="-kernel",
        ready_marker="NEVER APPEARS",
        pointer_width=4,
    )


class _FakeProc:
    """Popen stand-in that reports a live process until killed."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self.waited = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):  # noqa: ARG002 (Popen-compatible signature)
        self.waited += 1
        self.returncode = self.returncode if self.returncode is not None else -15
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _patch_spawn(monkeypatch, proc: _FakeProc) -> list[int]:
    """Replace Popen/killpg so no real process is involved."""
    killed_groups: list[int] = []
    monkeypatch.setattr(
        "tests.support.qemu_harness.subprocess.Popen",
        lambda *_a, **_k: proc,
    )
    monkeypatch.setattr(
        "tests.support.qemu_harness.os.killpg",
        lambda pid, _sig: killed_groups.append(pid),
    )
    return killed_groups


def test_boot_timeout_stops_qemu_before_raising(monkeypatch, tmp_path):
    """A boot timeout must not leave the process behind.

    The pytest fixture registers teardown only after ``start()`` returns, so an
    exception raised here used to leak one QEMU per failing attempt.
    """
    proc = _FakeProc()
    killed = _patch_spawn(monkeypatch, proc)
    session = QemuSession(_profile(tmp_path))

    with pytest.raises(RuntimeError, match="did not emit"):
        session.start(0.01)

    assert killed == [proc.pid]
    assert session._qemu is None


def test_early_exit_stops_qemu_before_raising(monkeypatch, tmp_path):
    """QEMU dying during boot is also cleaned up, not just reported."""
    proc = _FakeProc()
    proc.returncode = 1  # already dead on the first poll
    killed = _patch_spawn(monkeypatch, proc)
    session = QemuSession(_profile(tmp_path))

    with pytest.raises(RuntimeError, match="exited while booting"):
        session.start(5.0)

    assert killed == [proc.pid]
    assert session._qemu is None


def test_stop_is_idempotent(monkeypatch, tmp_path):
    """The atexit net and an explicit teardown must not double-kill."""
    proc = _FakeProc()
    killed = _patch_spawn(monkeypatch, proc)
    session = QemuSession(_profile(tmp_path))

    with pytest.raises(RuntimeError):
        session.start(0.01)
    session.stop()  # explicit fixture teardown after the failure
    session.stop()  # atexit net

    assert killed == [proc.pid]


def test_paused_start_waits_for_gdb_port_without_a_ready_marker(monkeypatch, tmp_path):
    """Reset-entry tests must not wait for firmware output while QEMU is paused."""
    proc = _FakeProc()
    _patch_spawn(monkeypatch, proc)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "tests.support.qemu_harness.subprocess.Popen",
        lambda command, **_kwargs: commands.append(command) or proc,
    )

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "tests.support.qemu_harness.socket.create_connection",
        lambda *_args, **_kwargs: _Connection(),
    )
    session = QemuSession(_profile(tmp_path))
    try:
        session.start(0.1, paused=True)
        assert "-S" in commands[0]
        assert session._qemu is proc
    finally:
        session.stop()


def test_paused_start_timeout_cleans_up_qemu(monkeypatch, tmp_path):
    """A paused QEMU whose GDB port never listens has the same teardown net."""
    proc = _FakeProc()
    killed = _patch_spawn(monkeypatch, proc)

    def not_listening(*_args, **_kwargs):
        raise OSError("not listening")

    monkeypatch.setattr(
        "tests.support.qemu_harness.socket.create_connection", not_listening
    )
    session = QemuSession(_profile(tmp_path))
    with pytest.raises(RuntimeError):
        session.start(0.01, paused=True)
    assert killed == [proc.pid]
    assert session._qemu is None


def test_start_registers_an_atexit_net(monkeypatch, tmp_path):
    """A hard interpreter exit never runs fixture teardown; atexit must."""
    proc = _FakeProc()
    _patch_spawn(monkeypatch, proc)
    registered: list[object] = []
    monkeypatch.setattr(atexit, "register", registered.append)
    session = QemuSession(_profile(tmp_path))

    with pytest.raises(RuntimeError):
        session.start(0.01)

    assert session.stop in registered
