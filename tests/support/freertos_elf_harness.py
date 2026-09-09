"""GDB session for a static FreeRTOS ELF with ``file`` only (no ``target remote``).

Used by the FreeRTOS snapshot lane: the ELF's ``.data`` holds pre-built
scheduler structures that GDB can decode without a live inferior.
"""

from __future__ import annotations

from pathlib import Path

import pexpect
import pytest

from tests.support.gdb_process import GdbProcess

_SNAPSHOT_VERSION = "11.1.0"


class StaticElfSession(GdbProcess):
    """GDB session with ``file <elf>`` only, no ``target remote``.

    The ELF's ``.data`` holds pre-built scheduler structures that GDB can
    decode without a live inferior (FreeRTOS snapshot lane).
    """

    def __init__(
        self,
        gdb_binary: str,
        elf_path: Path,
        gdr_root: Path,
        version: str = _SNAPSHOT_VERSION,
    ) -> None:
        super().__init__(gdb_binary, gdr_root)
        self.elf_path = elf_path
        self.version = version
        # Reason: the matrix exports GDR_RTOS/GDR_VERSION so live pytest can
        # select a profile; blank them here so ``source gdr.py`` does not
        # auto-init before the explicit ``gdr init`` below.
        self.extra_env = {"GDR_RTOS": "", "GDR_VERSION": ""}

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
            self.run(f"gdr init freertos {self.version}", timeout=20)
        except (pexpect.EOF, pexpect.TIMEOUT) as exc:
            raise RuntimeError(
                f"GDB failed while loading {self.elf_path}: {exc}"
            ) from exc
