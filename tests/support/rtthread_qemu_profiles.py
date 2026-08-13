"""RT-Thread QEMU launch profiles kept separate from fixture assertions.

COUPLED: each target/version selected here must have a corresponding fixture
patch set under ``ci/rt-thread/patches/`` and a matching expectation profile in
``tests/support/rtthread_profiles.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.support.qemu_harness import QemuProfile


def get_rtthread_qemu_profile(gdr_root: Path) -> QemuProfile:
    """Return the selected legacy-compatible RT-Thread QEMU profile."""
    target = os.environ.get("GDR_QEMU_TARGET", "cortex-a9")
    version = os.environ.get(
        "GDR_VERSION", os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")
    )
    fixture_dir = gdr_root / ".." / "fixture" / target / version
    default_elf = fixture_dir / "rtthread_qemu.elf"
    default_bin = fixture_dir / "rtthread_qemu.bin"
    elf_path = Path(os.environ.get("GDR_ELF_PATH", str(default_elf)))
    if target == "cortex-a9":
        profile = QemuProfile(
            rtos="rtthread",
            version=version,
            target=target,
            qemu_binary="qemu-system-arm",
            machine="vexpress-a9",
            gdb_architecture="arm",
            elf_path=elf_path,
            firmware_path=elf_path,
            firmware_option="-kernel",
            ready_marker="GDR test fixture ready.",
            pointer_width=4,
            qemu_args=(),
            init_command=f"gdr init rtthread {version}",
        )
    elif target == "rv64":
        profile = QemuProfile(
            rtos="rtthread",
            version=version,
            target=target,
            qemu_binary="qemu-system-riscv64",
            machine="virt",
            gdb_architecture="riscv:rv64",
            elf_path=elf_path,
            firmware_path=Path(os.environ.get("GDR_FIRMWARE_PATH", str(default_bin))),
            firmware_option="-bios",
            ready_marker="GDR test fixture ready.",
            pointer_width=8,
            qemu_args=("-cpu", "rv64", "-m", "256M"),
            init_command=f"gdr init rtthread {version}",
        )
    else:
        raise RuntimeError(f"unknown GDR_QEMU_TARGET: {target}")
    return profile.with_paths(
        elf_path,
        Path(os.environ.get("GDR_FIRMWARE_PATH", str(profile.firmware_path))),
    )
