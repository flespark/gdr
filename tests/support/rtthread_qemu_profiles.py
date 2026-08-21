"""RT-Thread QEMU launch profiles kept separate from fixture assertions.

COUPLED: each target/version selected here must have a corresponding fixture
patch set under ``ci/rt-thread/patches/`` and a matching expectation profile in
``tests/support/rtthread_profiles.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.support.qemu_harness import QemuProfile

_ELF_NAME = "rtthread.elf"
_BIN_NAME = "rtthread.bin"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def resolve_rtthread_fixture_dir(gdr_root: Path, target: str, version: str) -> Path:
    """Return the firmware directory for one target/version pair.

    ``RT_THREAD_FIXTURE_CACHE`` is a cache root of
    ``<target>/<version>/rtthread.elf``. Without it, profiles fall back to the
    repo-sibling ``fixture/<target>/<version>/`` directory.
    """
    cache = os.environ.get("RT_THREAD_FIXTURE_CACHE")
    if cache:
        return Path(cache) / target / version
    return gdr_root / ".." / "fixture" / target / version


def get_rtthread_qemu_profile(gdr_root: Path) -> QemuProfile:
    """Return the selected legacy-compatible RT-Thread QEMU profile."""
    target = os.environ.get("GDR_QEMU_TARGET", "cortex-a9")
    version = os.environ.get(
        "GDR_VERSION", os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")
    )
    fixture_dir = resolve_rtthread_fixture_dir(gdr_root, target, version)
    default_elf = fixture_dir / _ELF_NAME
    default_bin = fixture_dir / _BIN_NAME
    elf_path = _env_path("GDR_ELF_PATH", default_elf)
    if target == "cortex-a9":
        firmware_path = _env_path("GDR_FIRMWARE_PATH", elf_path)
        return QemuProfile(
            rtos="rtthread",
            version=version,
            target=target,
            qemu_binary="qemu-system-arm",
            machine="vexpress-a9",
            gdb_architecture="arm",
            elf_path=elf_path,
            firmware_path=firmware_path,
            firmware_option="-kernel",
            ready_marker="GDR test fixture ready.",
            pointer_width=4,
            qemu_args=(),
            init_command=f"gdr init rtthread {version}",
        )
    if target == "rv64":
        firmware_path = _env_path("GDR_FIRMWARE_PATH", default_bin)
        return QemuProfile(
            rtos="rtthread",
            version=version,
            target=target,
            qemu_binary="qemu-system-riscv64",
            machine="virt",
            gdb_architecture="riscv:rv64",
            elf_path=elf_path,
            firmware_path=firmware_path,
            firmware_option="-bios",
            ready_marker="GDR test fixture ready.",
            pointer_width=8,
            qemu_args=("-cpu", "rv64", "-m", "256M"),
            init_command=f"gdr init rtthread {version}",
        )
    raise RuntimeError(f"unknown GDR_QEMU_TARGET: {target}")
