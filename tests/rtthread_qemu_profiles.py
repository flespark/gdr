"""RT-Thread QEMU launch profiles kept separate from fixture assertions."""

from __future__ import annotations

import os
from pathlib import Path

from tests.qemu_harness import QemuProfile


def get_rtthread_qemu_profile(gdr_root: Path) -> QemuProfile:
    """Return the selected legacy-compatible RT-Thread QEMU profile."""
    target = os.environ.get("GDR_QEMU_TARGET", "cortex-a9")
    version = os.environ.get(
        "GDR_VERSION", os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")
    )
    fixture_elf = gdr_root / "tests" / "fixtures" / "rtthread_qemu.elf"
    legacy_elf = Path.home() / "Source/rt-thread/bsp/qemu-vexpress-a9/rtthread.elf"
    default_elf = fixture_elf if fixture_elf.exists() else legacy_elf
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
            firmware_path=Path(os.environ.get("GDR_FIRMWARE_PATH", str(elf_path))),
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
