"""GDB session for a static ELF with ``file`` only (no ``target remote``).

Used by the FreeRTOS snapshot lane: the ELF's ``.data`` holds pre-built
scheduler structures that GDB can decode without a live inferior.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pexpect
import pytest

_GDB_PROMPT = r"\(gdb\)\s*$"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _has_embedded_python(gdb_binary: str) -> bool:
    """Return whether GDB can execute the embedded Python interpreter."""
    result = subprocess.run(
        [
            gdb_binary,
            "--nx",
            "--quiet",
            "--batch",
            "--ex",
            'python print("GDR_PYTHON_OK")',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "GDR_PYTHON_OK" in result.stdout


class StaticElfSession:
    """GDB session with ``file <elf>`` only, no ``target remote``."""

    def __init__(self, gdb_binary: str, elf_path: Path, gdr_root: Path) -> None:
        self._gdb_binary = gdb_binary
        self.elf_path = elf_path
        self._gdr_root = gdr_root
        self._proc: pexpect.spawn | None = None
        self._script_dir = Path(tempfile.mkdtemp(prefix="gdr-static-gdbpy-"))
        self._script_seq = 0
        self.source_output = ""

    def start(self) -> None:
        """Load the ELF, source GDR, and initialise the FreeRTOS adapter."""
        if not shutil.which(self._gdb_binary):
            pytest.skip(f"missing GDB: {self._gdb_binary}")
        if not _has_embedded_python(self._gdb_binary):
            pytest.skip(f"GDB has no embedded Python: {self._gdb_binary}")
        if not self.elf_path.exists():
            pytest.skip(f"missing snapshot ELF: {self.elf_path}")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._gdr_root)
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
            self.run("set width 160")
            self.run("set architecture arm")
            self.run(f"file {self.elf_path}")
            self.source_output = self.run(f"source {self._gdr_root / 'gdr.py'}")
            if "Traceback (most recent call last)" in self.source_output:
                raise RuntimeError(f"GDR failed while sourcing:\n{self.source_output}")
            self.run("gdr init freertos 11.1.0", timeout=20)
        except (pexpect.EOF, pexpect.TIMEOUT) as exc:
            raise RuntimeError(
                f"GDB failed while loading {self.elf_path}: {exc}"
            ) from exc

    def stop(self) -> None:
        """Quit GDB and release its pseudo-terminal."""
        if self._proc is not None:
            with contextlib.suppress(pexpect.EOF, pexpect.TIMEOUT):
                self._proc.sendline("quit")
                self._proc.expect(pexpect.EOF, timeout=5)
            self._proc.close()
            self._proc = None
        shutil.rmtree(self._script_dir, ignore_errors=True)

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
        """Execute a multi-line Python block inside GDB via a sourced file."""
        if self._proc is None:
            raise RuntimeError("GDB session not started")
        self._script_seq += 1
        script = self._script_dir / f"block-{self._script_seq}.py"
        script.write_text(f"{code.strip()}\n", encoding="utf-8")
        return self.run(f"source {script}", timeout=timeout)
