"""RT-Thread QEMU launch profiles kept separate from fixture assertions.

COUPLED: each target/version selected here must have a corresponding fixture
patch set under ``ci/rt-thread/patches/`` and a matching expectation profile in
``tests.support.rtthread_fixture_profiles``.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.loader import IntegrationSpec, load_integration_spec
from tests.support.qemu_harness import QemuProfile


def get_rtthread_qemu_profile(
    gdr_root: Path, spec: IntegrationSpec | None = None
) -> QemuProfile:
    """Return the selected RT-Thread QEMU profile."""
    del gdr_root  # paths come from the shared cache layout, not the repo tree
    spec = spec if spec is not None else load_integration_spec()
    if spec.target == "cortex-a9":
        return QemuProfile(
            rtos="rtthread",
            version=spec.version,
            target=spec.target,
            qemu_binary=spec.qemu or "qemu-system-arm",
            machine="vexpress-a9",
            gdb_architecture="arm",
            elf_path=spec.elf_path(),
            firmware_path=spec.firmware_path(),
            firmware_option="-kernel",
            ready_marker="GDR test fixture ready.",
            pointer_width=4,
            qemu_args=(),
            init_command=f"gdr init rtthread {spec.version}",
        )
    if spec.target == "rv64":
        return QemuProfile(
            rtos="rtthread",
            version=spec.version,
            target=spec.target,
            qemu_binary=spec.qemu or "qemu-system-riscv64",
            machine="virt",
            gdb_architecture="riscv:rv64",
            elf_path=spec.elf_path(),
            firmware_path=spec.firmware_path(),
            firmware_option="-bios",
            ready_marker="GDR test fixture ready.",
            pointer_width=8,
            qemu_args=("-cpu", "rv64", "-m", "256M"),
            init_command=f"gdr init rtthread {spec.version}",
        )
    raise RuntimeError(f"unknown GDR_QEMU_TARGET: {spec.target}")
