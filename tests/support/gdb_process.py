"""Shared persistent-GDB process plumbing for the integration harnesses.

``GdbSession`` (QEMU target) and the FreeRTOS ``StaticElfSession`` both
drive one long-lived GDB process through pexpect; everything except the
``start()`` command sequence is identical (spawn, run/run_many/run_python,
stop, embedded-Python probe).  This base class owns that shared 80%
so the two harnesses cannot drift on prompt handling or ANSI stripping.
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


def has_embedded_python(gdb_binary: str) -> bool:
    """Return whether GDB can execute the embedded Python interpreter.

    Only ``ci/check-gdb-python.sh`` used to probe this; every closed-loop
    lane (live QEMU and the static snapshot) needs it, so the check moved
    into the shared harness and both session types skip early instead of
    failing mid-test.
    """
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


class GdbProcess:
    """One persistent GDB process driven by pexpect.

    Subclasses implement :meth:`start` (the GDB commands that set up the
    session: ``set architecture`` / ``file`` / ``target remote`` / ``source
    gdr.py`` / init); the run/stop plumbing and the embedded-Python gate are
    shared.
    """

    def __init__(self, gdb_binary: str, gdr_root: Path) -> None:
        self._gdb_binary = gdb_binary
        self._gdr_root = gdr_root
        self._proc: pexpect.spawn | None = None
        self._script_dir = Path(tempfile.mkdtemp(prefix="gdr-gdbpy-"))
        self._script_seq = 0
        self.source_output = ""

    # -- subclass contract -------------------------------------------------
    def start(self) -> None:
        raise NotImplementedError

    # -- shared plumbing --------------------------------------------------
    def _spawn(self) -> pexpect.spawn:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._gdr_root)
        env.update(getattr(self, "extra_env", {}))
        self._proc = pexpect.spawn(
            self._gdb_binary,
            ["-q"],
            env=env,
            encoding="utf-8",
            timeout=30,
            codec_errors="replace",
        )
        self._proc.expect(_GDB_PROMPT, timeout=10)
        self.run("set pagination off")
        self.run("set style enabled off")
        self.run("set width 160")
        return self._proc

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
        """Execute a multi-line Python block inside GDB via a sourced file.

        The block is written to a temporary file and sourced instead of being
        typed into GDB's ``python`` prompt.

        Reason: feeding dozens of lines through the pseudo-terminal deadlocks
        once the pty buffer fills, because nothing drains GDB's echo while we
        are still writing (observed on macOS: ``os_write`` blocks forever).
        Sourcing a file keeps the terminal traffic to a single short line.
        """
        if self._proc is None:
            raise RuntimeError("GDB session not started")
        self._script_seq += 1
        script = self._script_dir / f"block-{self._script_seq}.py"
        script.write_text(f"{code.strip()}\n", encoding="utf-8")
        return self.run(f"source {script}", timeout=timeout)

    def _require_embedded_python(self) -> None:
        """Skip when GDB lacks Python (both live and snapshot lanes need it)."""
        if not self._gdb_binary:
            pytest.skip("missing GDB")
        if not shutil.which(self._gdb_binary):
            pytest.skip(f"missing GDB: {self._gdb_binary}")
        if not has_embedded_python(self._gdb_binary):
            pytest.skip(f"GDB has no embedded Python: {self._gdb_binary}")

    def _source_gdr(self) -> None:
        """Source gdr.py and surface a traceback as a hard failure."""
        self.source_output = self.run(f"source {self._gdr_root / 'gdr.py'}")
        if "Traceback (most recent call last)" in self.source_output:
            raise RuntimeError(f"GDR failed while sourcing:\n{self.source_output}")


# Re-exported prompt/ANSI helpers for any external caller that anchored on
# the old module-level names.
GDB_PROMPT = _GDB_PROMPT
ANSI_RE = _ANSI_RE
