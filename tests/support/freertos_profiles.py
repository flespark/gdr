"""Independent FreeRTOS fixture expectations for QEMU integration tests."""

from __future__ import annotations

import os
from pathlib import Path

from tests.support.qemu_harness import QemuProfile

_CACHE_ELF = "freertos.elf"
_SIBLING_ELF = "freertos_b_l475e_iot01a.elf"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def resolve_freertos_fixture_dir(gdr_root: Path, target: str, version: str) -> Path:
    """Return the firmware directory for one target/version pair.

    ``FREERTOS_FIXTURE_CACHE`` is a cache root of
    ``<target>/<version>/freertos.elf``. Without it, profiles fall back to the
    repo-sibling ``fixture/`` directory.
    """
    cache = os.environ.get("FREERTOS_FIXTURE_CACHE")
    if cache:
        return Path(cache) / target / version
    return gdr_root / ".." / "fixture"


def get_freertos_qemu_profile(gdr_root: Path) -> QemuProfile:
    """Build the locked B-L475E-IOT01A profile from standard overrides."""
    version = os.environ.get("GDR_VERSION", "10.3.1")
    target = os.environ.get("GDR_QEMU_TARGET", "b-l475e-iot01a")
    if target != "b-l475e-iot01a":
        raise RuntimeError(f"unknown FreeRTOS QEMU target: {target}")
    fixture_dir = resolve_freertos_fixture_dir(gdr_root, target, version)
    using_cache = bool(os.environ.get("FREERTOS_FIXTURE_CACHE"))
    default_elf = fixture_dir / (_CACHE_ELF if using_cache else _SIBLING_ELF)
    elf_path = _env_path("GDR_ELF_PATH", default_elf)
    firmware_path = _env_path("GDR_FIRMWARE_PATH", elf_path)
    return QemuProfile(
        rtos="freertos",
        version=version,
        target=target,
        qemu_binary=os.environ.get("GDR_QEMU", "qemu-system-arm"),
        machine=os.environ.get("GDR_QEMU_MACHINE", "b-l475e-iot01a"),
        gdb_architecture="arm",
        elf_path=elf_path,
        firmware_path=firmware_path,
        firmware_option="-kernel",
        ready_marker="GDR FreeRTOS fixture ready.",
        pointer_width=4,
        init_command=f"gdr init freertos {version}",
        qemu_args=("-semihosting-config", "enable=on,target=native"),
        extra_env={"GDR_RTOS": "", "GDR_VERSION": ""},
    )
