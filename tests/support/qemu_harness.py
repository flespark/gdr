"""RTOS-neutral QEMU and persistent GDB test harness.

Profiles keep target-specific command lines and fixture contracts out of the
test lifecycle.  A session owns one QEMU process and one GDB connection so
GDB Python registrations remain available to every test in a suite.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pexpect
import pytest

from tests.support.gdb_process import GdbProcess


@dataclass(frozen=True)
class QemuProfile:
    """Configuration and fixture contract for a QEMU test target."""

    rtos: str
    version: str
    target: str
    qemu_binary: str
    machine: str
    gdb_architecture: str
    elf_path: Path
    firmware_path: Path
    firmware_option: str
    ready_marker: str
    pointer_width: int
    qemu_args: tuple[str, ...] = ()
    init_command: str | None = None
    serial_args: tuple[str, ...] = ("-serial", "{serial_log}")
    extra_env: dict[str, str] = field(default_factory=dict)

    def with_paths(self, elf_path: Path, firmware_path: Path) -> QemuProfile:
        """Return this profile with paths resolved from environment overrides."""
        return QemuProfile(
            rtos=self.rtos,
            version=self.version,
            target=self.target,
            qemu_binary=self.qemu_binary,
            machine=self.machine,
            gdb_architecture=self.gdb_architecture,
            elf_path=elf_path,
            firmware_path=firmware_path,
            firmware_option=self.firmware_option,
            ready_marker=self.ready_marker,
            pointer_width=self.pointer_width,
            qemu_args=self.qemu_args,
            init_command=self.init_command,
            serial_args=self.serial_args,
            extra_env=self.extra_env,
        )


def find_free_tcp_port() -> int:
    """Reserve and release an ephemeral loopback TCP port for QEMU GDB."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def check_tools(profile: QemuProfile, gdb_binary: str) -> None:
    """Skip a hardware-backed test when its executable or image is absent.

    Unlike the gdb-binary presence check, the embedded-Python probe runs a
    real ``python`` command in GDB: xPack's ``arm-none-eabi-gdb`` ships
    without Python while its ``-py3`` sibling has it, and a later failure
    would be an obscure pre-connection error instead of a clear skip.
    """
    from tests.support.gdb_process import has_embedded_python

    missing: list[str] = []
    if not shutil.which(profile.qemu_binary):
        missing.append(profile.qemu_binary)
    if not shutil.which(gdb_binary):
        missing.append(gdb_binary)
    if not profile.elf_path.exists():
        missing.append(str(profile.elf_path))
    if not profile.firmware_path.exists():
        missing.append(str(profile.firmware_path))
    if missing:
        pytest.skip(f"missing tools/firmware: {', '.join(missing)}")
    if not has_embedded_python(gdb_binary):
        pytest.skip(f"GDB has no embedded Python: {gdb_binary}")


class QemuSession:
    """Manage a free-running QEMU target and its GDB server."""

    def __init__(self, profile: QemuProfile, gdb_port: int | None = None) -> None:
        self.profile = profile
        self.gdb_port = gdb_port if gdb_port is not None else find_free_tcp_port()
        self._qemu: subprocess.Popen[bytes] | None = None
        self._temp_dir = Path(tempfile.mkdtemp(prefix="gdr-qemu-"))
        self.serial_log = self._temp_dir / "serial.log"
        self.qemu_log = self._temp_dir / "qemu.log"

    def _command(self) -> list[str]:
        serial_args = [
            value.format(serial_log=f"file:{self.serial_log}")
            for value in self.profile.serial_args
        ]
        command = [
            self.profile.qemu_binary,
            "-M",
            self.profile.machine,
            *self.profile.qemu_args,
            self.profile.firmware_option,
            str(self.profile.firmware_path),
            *serial_args,
            "-nographic",
            "-monitor",
            "none",
            "-gdb",
            f"tcp::{self.gdb_port}",
        ]
        return command

    def _logs(self) -> str:
        def read_log(path: Path) -> str:
            return (
                path.read_text(errors="replace") if path.exists() else "<not created>"
            )

        return f"serial output:\n{read_log(self.serial_log)}\nQEMU output:\n{read_log(self.qemu_log)}"

    def start(self, boot_wait: float) -> None:
        """Launch QEMU and wait until the fixture has created its test objects.

        Every failure path stops the process before raising. Reason: the caller
        registers teardown *after* start() returns (``session.start(...)`` then
        ``yield`` in the pytest fixture), so an exception here used to leave a
        live QEMU behind. Those orphans accumulate silently across runs -- a
        boot-timeout lane leaked one per attempt -- and a leaked process keeps
        its stdout pipe open, which later wedges any tool that waits for the
        pipe to close.
        """
        command = self._command()
        log_file = self.qemu_log.open("wb")
        self._qemu = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Reason: a hard interpreter exit (Ctrl-C, pytest internal error) never
        # runs fixture teardown; this net is idempotent because stop() clears
        # the handle.
        atexit.register(self.stop)
        deadline = time.monotonic() + boot_wait
        try:
            while time.monotonic() < deadline:
                if self._qemu.poll() is not None:
                    raise RuntimeError(
                        "QEMU exited while booting "
                        f"(exit={self._qemu.returncode}): {command}\n{self._logs()}"
                    )
                if self.profile.ready_marker in self._logs():
                    return
                time.sleep(0.1)
            raise RuntimeError(
                f"QEMU did not emit {self.profile.ready_marker!r} within {boot_wait}s. "
                f"Command: {command}\n{self._logs()}"
            )
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """Terminate QEMU, escalating only after a bounded graceful wait."""
        if self._qemu is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._qemu.pid, signal.SIGTERM)
            try:
                self._qemu.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._qemu.kill()
                self._qemu.wait(timeout=5)
            self._qemu = None


class GdbSession(GdbProcess):
    """Persistent GDB session connected to a QEMU target."""

    def __init__(
        self,
        gdb_binary: str,
        profile: QemuProfile,
        gdb_port: int,
        gdr_root: Path,
    ) -> None:
        super().__init__(gdb_binary, gdr_root)
        self.profile = profile
        self._gdb_port = gdb_port
        self.extra_env = profile.extra_env

    def start(self) -> None:
        """Connect to QEMU, source GDR once, then initialise when requested."""
        self._require_embedded_python()
        self._spawn()
        try:
            self.run(f"set architecture {self.profile.gdb_architecture}")
            self.run(f"file {self.profile.elf_path}")
            self.run(f"target remote :{self._gdb_port}")
            self._source_gdr()
            if self.profile.init_command:
                self.run(self.profile.init_command, timeout=20)
        except (pexpect.EOF, pexpect.TIMEOUT) as exc:
            raise RuntimeError(
                f"GDB failed while connecting to {self.profile.target}: {exc}"
            ) from exc
