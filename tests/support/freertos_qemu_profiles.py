"""FreeRTOS QEMU launch profiles kept separate from fixture assertions.

COUPLED: each target/variant selected here must have a fixture config under
``ci/freertos/fixture/config/<variant>/`` plus a board under
``ci/freertos/fixture/board/<target>/``, and a matching capability profile in
``tests.support.freertos_fixture_profiles``.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.loader import IntegrationSpec, load_integration_spec
from tests.support.qemu_harness import QemuProfile

_KNOWN_TARGETS = ("b-l475e-iot01a", "mps2-an385", "mps2-an521", "qemu-virt-rv64")
_MACHINES = {
    "mps2-an385": "mps2-an385",
    "mps2-an521": "mps2-an521",
    "b-l475e-iot01a": "b-l475e-iot01a",
    "qemu-virt-rv64": "virt",
}


def get_freertos_qemu_profile(
    gdr_root: Path, spec: IntegrationSpec | None = None
) -> QemuProfile:
    """Build a FreeRTOS QEMU profile from :func:`load_integration_spec`."""
    del gdr_root  # paths come from the shared cache layout, not the repo tree
    spec = spec if spec is not None else load_integration_spec()
    if spec.variant == "snapshot":
        raise RuntimeError("snapshot lane has no QEMU profile")
    if spec.target not in _KNOWN_TARGETS:
        raise RuntimeError(f"unknown FreeRTOS QEMU target: {spec.target}")
    is_rv64 = spec.target == "qemu-virt-rv64"
    # Reason: mps2-an521 is fixed at two CPUs (default_cpus == min == max),
    # so an explicit -smp 2 is redundant and -smp 1 would be rejected;
    # leave the machine default alone.  The RISC-V lane uses QEMU's RISC-V
    # semihosting (ebreak sequence) for the ready marker, which needs the
    # semihosting switch just like the ARM lanes; -bios loads the raw binary
    # at 0x80000000 while GDB reads the ELF.
    qemu_args = (
        ("-cpu", "rv64", "-m", "256M", "-semihosting-config", "enable=on")
        if is_rv64
        else ("-semihosting-config", "enable=on,target=native")
    )
    qemu_binary = spec.qemu or ("qemu-system-riscv64" if is_rv64 else "qemu-system-arm")
    return QemuProfile(
        rtos="freertos",
        version=spec.version,
        target=spec.target,
        qemu_binary=qemu_binary,
        machine=_MACHINES[spec.target],
        gdb_architecture="riscv:rv64" if is_rv64 else "arm",
        elf_path=spec.elf_path(),
        firmware_path=spec.firmware_path(),
        firmware_option="-bios" if is_rv64 else "-kernel",
        ready_marker="GDR FreeRTOS fixture ready.",
        pointer_width=8 if is_rv64 else 4,
        init_command=f"gdr init freertos {spec.version}",
        qemu_args=qemu_args,
        extra_env={"GDR_RTOS": "", "GDR_VERSION": ""},
    )
