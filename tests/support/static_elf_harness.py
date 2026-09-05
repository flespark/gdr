"""GDB session for a static ELF with ``file`` only (no ``target remote``).

Used by the FreeRTOS snapshot lane: the ELF's ``.data`` holds pre-built
scheduler structures that GDB can decode without a live inferior.
"""

from __future__ import annotations

from pathlib import Path

import pexpect
import pytest

from tests.support.gdb_process import GdbProcess


class StaticElfSession(GdbProcess):
    """GDB session with ``file <elf>`` only, no ``target remote``.

    The ELF's ``.data`` holds pre-built scheduler structures that GDB can
    decode without a live inferior (FreeRTOS snapshot lane).
    """

    def __init__(self, gdb_binary: str, elf_path: Path, gdr_root: Path) -> None:
        super().__init__(gdb_binary, gdr_root)
        self.elf_path = elf_path

    def start(self) -> None:
        """Load the ELF, source GDR, and initialise the FreeRTOS adapter."""
        if not self.elf_path.exists():
            pytest.skip(f"missing snapshot ELF: {self.elf_path}")
        self._require_embedded_python()
        self._spawn()
        try:
            self.run("set architecture arm")
            self.run(f"file {self.elf_path}")
            self._source_gdr()
            self.run("gdr init freertos 11.1.0", timeout=20)
        except (pexpect.EOF, pexpect.TIMEOUT) as exc:
            raise RuntimeError(
                f"GDB failed while loading {self.elf_path}: {exc}"
            ) from exc
