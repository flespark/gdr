"""RTOS-neutral QEMU and persistent GDB test harness.

Profiles keep target-specific command lines and fixture contracts out of the
test lifecycle.  A session owns one QEMU process and one GDB connection so
GDB Python registrations remain available to every test in a suite.
"""

from __future__ import annotations

import contextlib
import os
import re
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

_GDB_PROMPT = r"\(gdb\)\s*$"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


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
    """Skip a hardware-backed test when its executable or image is absent."""
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
        """Launch QEMU and wait until the fixture has created its test objects."""
        command = self._command()
        log_file = self.qemu_log.open("wb")
        self._qemu = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + boot_wait
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


class GdbSession:
    """Persistent GDB process driven by pexpect."""

    def __init__(
        self,
        gdb_binary: str,
        profile: QemuProfile,
        gdb_port: int,
        gdr_root: Path,
    ) -> None:
        self._gdb_binary = gdb_binary
        self.profile = profile
        self._gdb_port = gdb_port
        self._gdr_root = gdr_root
        self._proc: pexpect.spawn | None = None
        self.source_output = ""

    def start(self) -> None:
        """Connect to QEMU, source GDR once, then initialise when requested."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._gdr_root)
        env.update(self.profile.extra_env)
        self._proc = pexpect.spawn(
            self._gdb_binary,
            ["-q"],
            env=env,
            encoding="utf-8",
            timeout=30,
            codec_errors="replace",
        )
        try:
            self._proc.expect(_GDB_PROMPT, timeout=10)
            self.run("set pagination off")
            self.run("set style enabled off")
            self.run(f"set architecture {self.profile.gdb_architecture}")
            self.run(f"file {self.profile.elf_path}")
            self.run(f"target remote :{self._gdb_port}")
            self.source_output = self.run(f"source {self._gdr_root / 'gdr.py'}")
            if "Traceback (most recent call last)" in self.source_output:
                raise RuntimeError(f"GDR failed while sourcing:\n{self.source_output}")
            if self.profile.init_command:
                self.run(self.profile.init_command, timeout=20)
        except (pexpect.EOF, pexpect.TIMEOUT) as exc:
            raise RuntimeError(
                f"GDB failed while connecting to {self.profile.target}: {exc}"
            ) from exc

    def stop(self) -> None:
        """Quit GDB and release its pseudo-terminal."""
        if self._proc is not None:
            with contextlib.suppress(pexpect.EOF, pexpect.TIMEOUT):
                self._proc.sendline("quit")
                self._proc.expect(pexpect.EOF, timeout=5)
            self._proc.close()
            self._proc = None

    def run(self, command: str, timeout: int = 15) -> str:
        """Run one GDB command and return its output excluding echo and prompt."""
        if self._proc is None:
            raise RuntimeError("GDB session not started")
        self._proc.sendline(command)
        self._proc.expect(_GDB_PROMPT, timeout=timeout)
        raw = _ANSI_RE.sub("", self._proc.before or "").replace("\r", "")
        lines = raw.split("\n", 1)
        if len(lines) > 1 and command.strip() in lines[0]:
            return lines[1]
        return raw

    def run_many(self, *commands: str) -> str:
        """Run commands in the persistent session and join their output."""
        return "\n".join(self.run(command) for command in commands)

    def run_python(self, code: str, timeout: int = 15) -> str:
        """Execute a multi-line Python block inside GDB."""
        if self._proc is None:
            raise RuntimeError("GDB session not started")
        self._proc.sendline("python")
        for line in code.strip().split("\n"):
            self._proc.sendline(line)
        self._proc.sendline("end")
        self._proc.expect(_GDB_PROMPT, timeout=timeout)
        return _ANSI_RE.sub("", self._proc.before or "").replace("\r", "")
