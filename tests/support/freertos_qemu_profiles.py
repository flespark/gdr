"""FreeRTOS QEMU launch profiles kept separate from fixture assertions.

COUPLED: each target/variant selected here must have a fixture config under
``ci/freertos/fixture/config/<variant>/`` plus a board under
``ci/freertos/fixture/board/<target>/``, and a matching capability profile in
``tests/support/freertos_fixture_profiles.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.support.qemu_harness import QemuProfile

_ELF_NAME = "freertos.elf"
# Reason: the build scripts install every fixture into this cache root, so the
# test-side default has to be the same path (overridable per host/CI).
_DEFAULT_FIXTURE_CACHE = Path.home() / "Project" / "gdr-fixture" / "freertos"
_KNOWN_TARGETS = ("b-l475e-iot01a", "mps2-an385", "mps2-an521")


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def _env_str(name: str, default: str) -> str:
    # Reason: callers (and .pi/dtod/env.sh) export blank overrides to mean
    # "use the profile default"; os.environ.get would return that "" and
    # shutil.which("") then fails the tool check with an empty name.
    return os.environ.get(name) or default


def resolve_freertos_fixture_dir(
    _gdr_root: Path, target: str, version: str, variant: str = "base"
) -> Path:
    """Return the firmware directory for one target/version/variant triple.

    ``FREERTOS_FIXTURE_CACHE`` overrides the default cache root
    ``~/Project/gdr-fixture/freertos``. Layout is
    ``<cache>/<target>/<version>/<variant>/freertos.elf``.
    """
    cache = Path(os.environ.get("FREERTOS_FIXTURE_CACHE", str(_DEFAULT_FIXTURE_CACHE)))
    return cache / target / version / variant


def get_freertos_qemu_profile(gdr_root: Path) -> QemuProfile:
    """Build a FreeRTOS QEMU profile from standard overrides."""
    version = _env_str("GDR_VERSION", "10.3.1")
    target = _env_str("GDR_QEMU_TARGET", "b-l475e-iot01a")
    variant = _env_str("GDR_FIXTURE_VARIANT", "base")
    if target not in _KNOWN_TARGETS:
        raise RuntimeError(f"unknown FreeRTOS QEMU target: {target}")
    fixture_dir = resolve_freertos_fixture_dir(gdr_root, target, version, variant)
    elf_path = _env_path("GDR_ELF_PATH", fixture_dir / _ELF_NAME)
    firmware_path = _env_path("GDR_FIRMWARE_PATH", elf_path)
    machine = _env_str(
        "GDR_QEMU_MACHINE",
        {
            "mps2-an385": "mps2-an385",
            "mps2-an521": "mps2-an521",
            "b-l475e-iot01a": "b-l475e-iot01a",
        }[target],
    )
    # Reason: mps2-an521 is fixed at two CPUs (default_cpus == min == max),
    # so an explicit -smp 2 is redundant and -smp 1 would be rejected;
    # leave the machine default alone.
    qemu_args = ("-semihosting-config", "enable=on,target=native")
    return QemuProfile(
        rtos="freertos",
        version=version,
        target=target,
        qemu_binary=_env_str("GDR_QEMU", "qemu-system-arm"),
        machine=machine,
        gdb_architecture="arm",
        elf_path=elf_path,
        firmware_path=firmware_path,
        firmware_option="-kernel",
        ready_marker="GDR FreeRTOS fixture ready.",
        pointer_width=4,
        init_command=f"gdr init freertos {version}",
        qemu_args=qemu_args,
        extra_env={"GDR_RTOS": "", "GDR_VERSION": ""},
    )
