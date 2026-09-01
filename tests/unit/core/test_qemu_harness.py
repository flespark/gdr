"""Unit tests for the RTOS-neutral QEMU/GDB harness lifecycle.

These cover process lifetime only -- the parts that must hold even when a lane
fails -- because a leaked QEMU is invisible to the failing test itself and only
surfaces later as an unrelated hang (its stdout pipe never closes).
"""

from __future__ import annotations

import atexit
from pathlib import Path

import pytest

from tests.support.qemu_harness import QemuProfile, QemuSession


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
