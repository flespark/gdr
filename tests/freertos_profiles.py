"""Independent FreeRTOS fixture expectations for QEMU integration tests."""

from __future__ import annotations

import os
from pathlib import Path

from tests.qemu_harness import QemuProfile

FREE_RTOS_VERSION = os.environ.get("GDR_VERSION", "10.3.1")
FREE_RTOS_TARGET = os.environ.get("GDR_QEMU_TARGET", "b-l475e-iot01a")
READY_MARKER = "GDR FreeRTOS fixture ready."


def get_freertos_qemu_profile(gdr_root: Path) -> QemuProfile:
    """Build the locked B-L475E-IOT01A profile from standard overrides."""
    if FREE_RTOS_TARGET != "b-l475e-iot01a":
        raise RuntimeError(f"unknown FreeRTOS QEMU target: {FREE_RTOS_TARGET}")
    default_elf = gdr_root / "tests" / "fixtures" / "freertos_b_l475e_iot01a.elf"
    elf_path = Path(os.environ.get("GDR_ELF_PATH", str(default_elf)))
    firmware_path = Path(os.environ.get("GDR_FIRMWARE_PATH", str(elf_path)))
    return QemuProfile(
        rtos="freertos",
        version=FREE_RTOS_VERSION,
        target=FREE_RTOS_TARGET,
        qemu_binary=os.environ.get("GDR_QEMU", "qemu-system-arm"),
        machine=os.environ.get("GDR_QEMU_MACHINE", "b-l475e-iot01a"),
        gdb_architecture="arm",
        elf_path=elf_path,
        firmware_path=firmware_path,
        firmware_option="-kernel",
        ready_marker=READY_MARKER,
        pointer_width=4,
        qemu_args=("-semihosting-config", "enable=on,target=native"),
        extra_env={"GDR_RTOS": "", "GDR_VERSION": ""},
    )
