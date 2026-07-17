"""Pytest fixtures for QEMU-based closed-loop verification.

Manages the QEMU + GDB + GDR lifecycle:

1. Start a target-specific QEMU profile with ``-gdb tcp::1234`` and
   free-running (no ``-S``).
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
# Reason: prefer the in-repo fixture so local, CI, and any contributor see
# the same ELF; the legacy ~/Source path is a fallback for the maintainer's
# existing dev tree. CI overrides via GDR_ELF_PATH pointing at its build dir.
_FIXTURE_ELF = GDR_ROOT / "tests" / "fixtures" / "rtthread_qemu.elf"
_DEV_ELF = Path.home() / "Source/rt-thread/bsp/qemu-vexpress-a9/rtthread.elf"
ELF_PATH = Path(
    os.environ.get(
        "GDR_ELF_PATH",
        str(_FIXTURE_ELF) if _FIXTURE_ELF.exists() else str(_DEV_ELF),
    )
)
GDB_BIN = os.environ.get("GDR_GDB", "gdb")
RTTHREAD_VERSION = os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")

_TARGET_PROFILES = {
    "cortex-a9": {
        "qemu": "qemu-system-arm",
        "machine": "vexpress-a9",
        "gdb_arch": "arm",
        "firmware_option": "-kernel",
        "qemu_args": (),
        "needs_sd": True,
    },
    "rv64": {
        "qemu": "qemu-system-riscv64",
        "machine": "virt",
        "gdb_arch": "riscv:rv64",
        "firmware_option": "-bios",
        "qemu_args": ("-cpu", "rv64", "-m", "256M"),
        "needs_sd": False,
    },
}
TARGET_NAME = os.environ.get("GDR_QEMU_TARGET", "cortex-a9")
try:
    TARGET = _TARGET_PROFILES[TARGET_NAME]
except KeyError as exc:
    raise RuntimeError(f"unknown GDR_QEMU_TARGET: {TARGET_NAME}") from exc

QEMU_BIN = os.environ.get("GDR_QEMU", str(TARGET["qemu"]))
QEMU_MACHINE = os.environ.get("GDR_QEMU_MACHINE", str(TARGET["machine"]))
GDB_ARCH = os.environ.get("GDR_GDB_ARCH", str(TARGET["gdb_arch"]))
FIRMWARE_PATH = Path(os.environ.get("GDR_FIRMWARE_PATH", str(ELF_PATH)))
FIRMWARE_OPTION = str(TARGET["firmware_option"])
QEMU_ARGS = tuple(str(arg) for arg in TARGET["qemu_args"])
QEMU_NEEDS_SD = bool(TARGET["needs_sd"])
GDB_PORT = 1234
BOOT_WAIT = float(os.environ.get("GDR_BOOT_WAIT", "10"))
READY_MARKER = "GDR test fixture ready."

# GDB prompt regex — matches "(gdb) " at end of line
_GDB_PROMPT = r"\(gdb\)\s*$"

# ANSI escape sequence stripper (color codes, bracketed paste, etc.)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _check_tools():
    """Verify QEMU, GDB, symbols, and firmware are available; skip if not."""
    missing = []
    if not shutil.which(QEMU_BIN):
        missing.append(QEMU_BIN)
    if not shutil.which(GDB_BIN):
        missing.append(GDB_BIN)
    if not ELF_PATH.exists():
        missing.append(str(ELF_PATH))
    if not FIRMWARE_PATH.exists():
        missing.append(str(FIRMWARE_PATH))
    if missing:
        pytest.skip(f"missing tools/firmware: {', '.join(missing)}")


class QemuSession:
    """Manages a QEMU process with a GDB server."""

    def __init__(self):
        self._qemu: subprocess.Popen | None = None
        self._serial_log: Path = Path("/tmp/gdr_qemu_serial.log")

    def start(self):
        """Start QEMU free-running with GDB server."""
        self._serial_log.unlink(missing_ok=True)

        cmd = [
            QEMU_BIN,
            "-M",
            QEMU_MACHINE,
            *QEMU_ARGS,
            FIRMWARE_OPTION,
            str(FIRMWARE_PATH),
            "-serial",
            f"file:{self._serial_log}",
            "-nographic",
            "-monitor",
            "none",
            "-gdb",
            f"tcp::{GDB_PORT}",
        ]
        if QEMU_NEEDS_SD:
            sd_img = Path("/tmp/gdr_sd.bin")
            if not sd_img.exists():
                sd_img.write_bytes(b"\0" * 1024 * 64)
            cmd.extend(["-drive", f"file={sd_img},format=raw,if=sd"])
        self._qemu = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + BOOT_WAIT
        while time.monotonic() < deadline:
            if self._qemu.poll() is not None:
                raise RuntimeError(f"QEMU exited while booting: {cmd}")
            if self._serial_log.exists() and READY_MARKER in self._serial_log.read_text(
                errors="replace"
            ):
                return
            time.sleep(0.1)
        raise RuntimeError(f"QEMU did not emit {READY_MARKER!r} within {BOOT_WAIT}s")

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
        self.run(f"set architecture {GDB_ARCH}")
        self.run(f"file {self._elf_path}")
        self.run(f"target remote :{self._gdb_port}")
        self.run(f"source {self._gdr_root / 'gdr.py'}")
        self.run("rtthread threads")
        self.run(f"gdr init rtthread {RTTHREAD_VERSION}", timeout=20)

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
