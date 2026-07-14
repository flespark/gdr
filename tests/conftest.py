"""Pytest fixtures for QEMU-based closed-loop verification.

Manages the QEMU + GDB + GDR lifecycle:

1. Start QEMU with ``-gdb tcp::1234`` and free-running (no ``-S``).
2. Wait for the RT-Thread kernel to boot and create test objects.
3. Spawn a persistent GDB process (via pexpect), connect to QEMU,
   source ``gdr.py`` once, and reuse across all tests.

Tests use the ``gdb_session`` fixture to run GDB commands and capture
output for assertion.  The persistent session keeps convenience
functions and pretty-printers registered across tests.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pexpect
import pytest

# Paths
GDR_ROOT = Path(__file__).resolve().parent.parent
ELF_PATH = Path(
    os.environ.get(
        "GDR_ELF_PATH",
        str(Path.home() / "Source/rt-thread/bsp/qemu-vexpress-a9/rtthread.elf"),
    )
)
GDB_BIN = os.environ.get("GDR_GDB", "gdb")
QEMU_BIN = os.environ.get("GDR_QEMU", "qemu-system-arm")

# QEMU config
QEMU_MACHINE = "vexpress-a9"
GDB_PORT = 1234
BOOT_WAIT = 4  # seconds for kernel to boot and create objects

# GDB prompt regex — matches "(gdb) " at end of line
_GDB_PROMPT = r"\(gdb\)\s*$"

# ANSI escape sequence stripper (color codes, bracketed paste, etc.)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _check_tools():
    """Verify QEMU, GDB, and ELF are available; skip tests if not."""
    missing = []
    if not shutil.which(QEMU_BIN):
        missing.append(QEMU_BIN)
    if not shutil.which(GDB_BIN):
        missing.append(GDB_BIN)
    if not ELF_PATH.exists():
        missing.append(str(ELF_PATH))
    if missing:
        pytest.skip(f"missing tools/firmware: {', '.join(missing)}")


class QemuSession:
    """Manages a QEMU process with a GDB server."""

    def __init__(self):
        self._qemu: subprocess.Popen | None = None
        self._serial_log: Path = Path("/tmp/gdr_qemu_serial.log")

    def start(self):
        """Start QEMU free-running with GDB server."""
        sd_img = Path("/tmp/gdr_sd.bin")
        if not sd_img.exists():
            sd_img.write_bytes(b"\0" * 1024 * 64)

        cmd = [
            QEMU_BIN,
            "-M",
            QEMU_MACHINE,
            "-kernel",
            str(ELF_PATH),
            "-serial",
            f"file:{self._serial_log}",
            "-nographic",
            "-monitor",
            "none",
            "-gdb",
            f"tcp::{GDB_PORT}",
            "-drive",
            f"file={sd_img},format=raw,if=sd",
        ]
        self._qemu = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(BOOT_WAIT)

    def stop(self):
        """Terminate the QEMU process."""
        if self._qemu is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._qemu.pid, signal.SIGTERM)
            try:
                self._qemu.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._qemu.kill()
            self._qemu = None


class GdbSession:
    """Persistent GDB process driven via pexpect.

    One GDB process is spawned per session, connected to QEMU's GDB
    server, with ``gdr.py`` sourced once.  Tests call ``run()`` to
    execute commands and capture output.
    """

    def __init__(self, gdb_bin: str, elf_path: Path, gdb_port: int, gdr_root: Path):
        self._gdb_bin = gdb_bin
        self._elf_path = elf_path
        self._gdb_port = gdb_port
        self._gdr_root = gdr_root
        self._proc: pexpect.spawn | None = None

    def start(self):
        """Spawn GDB, connect to QEMU, source gdr.py."""
        env = os.environ.copy()
        env["GDR_RTOS"] = "rtthread"
        env["GDR_VERSION"] = "4.0"
        env["PYTHONPATH"] = str(self._gdr_root)

        self._proc = pexpect.spawn(
            self._gdb_bin,
            ["-q"],
            env=env,
            encoding="utf-8",
            timeout=30,
            codec_errors="replace",
        )
        # Wait for initial GDB prompt
        self._proc.expect(_GDB_PROMPT, timeout=10)

        # Connect to QEMU and load GDR
        self.run("set pagination off")
        self.run("set style enabled off")
        self.run("set architecture arm")
        self.run(f"file {self._elf_path}")
        self.run(f"target remote :{self._gdb_port}")
        # Source gdr.py — this may take a moment (config probe, layout build)
        self.run(f"source {self._gdr_root / 'gdr.py'}", timeout=20)

    def stop(self):
        """Quit GDB and clean up."""
        if self._proc is not None:
            with contextlib.suppress(pexpect.EOF, pexpect.TIMEOUT):
                self._proc.sendline("quit")
                self._proc.expect(pexpect.EOF, timeout=5)
            self._proc.close()
            self._proc = None

    def run(self, cmd: str, timeout: int = 15) -> str:
        """Send a GDB command and return output before the next prompt.

        The output includes GDB's response to *cmd* but not the
        command echo or the ``(gdb)`` prompt itself.

        Args:
            cmd: GDB command string.
            timeout: Seconds to wait for GDB to respond.

        Returns:
            GDB output as a string (may be empty).
        """
        if self._proc is None:
            raise RuntimeError("GDB session not started")

        self._proc.sendline(cmd)
        self._proc.expect(_GDB_PROMPT, timeout=timeout)
        # _proc.before contains: command echo + output
        raw = self._proc.before or ""
        # Strip ANSI escape sequences (color codes, bracketed paste, etc.)
        raw = _ANSI_RE.sub("", raw)
        # Strip carriage returns (PTY artifact)
        raw = raw.replace("\r", "")
        # Strip the command echo (first line)
        lines = raw.split("\n", 1)
        if len(lines) > 1 and cmd.strip() in lines[0]:
            return lines[1]
        return raw

    def run_many(self, *cmds: str) -> str:
        """Run multiple commands and return combined output."""
        return "\n".join(self.run(c) for c in cmds)

    def run_python(self, code: str, timeout: int = 15) -> str:
        """Execute a multi-line Python block in GDB and return output.

        Args:
            code: Python source code (without ``python``/``end`` keywords).
            timeout: Seconds to wait for GDB to respond.

        Returns:
            GDB output from the Python block.
        """
        if self._proc is None:
            raise RuntimeError("GDB session not started")

        self._proc.sendline("python")
        for line in code.strip().split("\n"):
            self._proc.sendline(line)
        self._proc.sendline("end")
        self._proc.expect(_GDB_PROMPT, timeout=timeout)
        raw = self._proc.before or ""
        raw = _ANSI_RE.sub("", raw)
        raw = raw.replace("\r", "")
        return raw


@pytest.fixture(scope="session")
def qemu():
    """Session-scoped QEMU instance.

    Tests share one QEMU boot.  Each test gets a fresh GDB connection
    via ``gdb_session``.
    """
    _check_tools()
    session = QemuSession()
    session.start()
    yield session
    session.stop()


@pytest.fixture(scope="session")
def gdb(qemu):
    """Session-scoped persistent GDB session.

    Spawns one GDB process, connects to QEMU, sources gdr.py once.
    Reused across all tests for speed and registration persistence.
    """
    session = GdbSession(GDB_BIN, ELF_PATH, GDB_PORT, GDR_ROOT)
    session.start()
    yield session
    session.stop()


@pytest.fixture
def gdb_session(gdb):
    """Per-test GDB command runner.

    Returns the session-scoped :class:`GdbSession` instance.  Tests
    call ``gdb_session.run("rtthread threads")`` to execute commands
    and capture output.
    """
    return gdb
