"""Single reader of closed-loop integration environment variables.

Matrix scripts and ad-hoc pytest invocations parameterize a session through
a small set of ``GDR_*`` / cache variables. QEMU profiles, snapshot tests
and integration modules call :func:`load_integration_spec` instead of
parsing ``os.environ`` themselves.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_FREERTOS_CACHE = Path.home() / "Project" / "gdr-fixture" / "freertos"
# Reason: CNB closed-loop hosts keep prebuilt RT-Thread firmware here.
_DEFAULT_RTTHREAD_CACHE = Path("/workspace/fixture/rtthread")

_FREERTOS_DEFAULT_VERSION = "10.3.1"
_FREERTOS_DEFAULT_TARGET = "b-l475e-iot01a"
_FREERTOS_DEFAULT_VARIANT = "base"
_RTTHREAD_DEFAULT_VERSION = "4.0.5"
_RTTHREAD_DEFAULT_TARGET = "cortex-a9"
# (rtos, target) pairs whose QEMU machine boots a raw BIN via -bios instead
# of the ELF via -kernel.
_RAW_BIN_BOOT_TARGETS = frozenset(
    {("freertos", "qemu-virt-rv64"), ("rtthread", "rv64")}
)


def _env_str(env: Mapping[str, str], name: str, default: str) -> str:
    # Reason: callers (and .pi/dtod/env.sh) export blank overrides to mean
    # "use the profile default"; a raw ``env.get`` would return that "" and
    # ``shutil.which("")`` then fails the tool check with an empty name.
    return env.get(name) or default


def _env_optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name) or ""
    return Path(value) if value else None


@dataclass(frozen=True)
class IntegrationSpec:
    """Closed-loop session selected by the environment.

    Attributes:
        rtos: ``rtthread`` or ``freertos``.
        version: Kernel version string passed to ``gdr init``.
        target: QEMU board / ISA selector.
        variant: FreeRTOS fixture variant; ``snapshot`` selects the file-only
            lane. Always empty for RT-Thread (no variant axis), so
            ``variant == "snapshot"`` alone identifies the snapshot lane.
        gdb: GDB binary with an embedded Python interpreter.
        boot_wait: Seconds to wait for the QEMU ready marker.
        force_build: Rebuild cached firmware even when it exists.
        fixture_cache: Root of the built-firmware cache: the compiled fixture
            images the builders install (ELF/BIN/MAP — never checked-out
            kernel sources; the builders clone sources under their own
            cache, e.g. ``FREERTOS_KERNEL_CACHE``). See :meth:`fixture_dir`
            for the layout.
        elf_override: Optional explicit ELF, wins over the cache layout; GDB
            loads it and a RISC-V lane boots its ``.bin`` sibling.
        qemu: Optional QEMU binary override.
    """

    rtos: str
    version: str
    target: str
    variant: str
    gdb: str
    boot_wait: float
    force_build: bool
    fixture_cache: Path
    elf_override: Path | None = None
    qemu: str | None = None

    def fixture_dir(self) -> Path:
        """Return the cache directory holding this session's built firmware.

        Layout: ``<root>/<target>/<version>/<variant>/freertos.elf`` for any
        FreeRTOS variant (the file-only ``snapshot`` cell included, which
        also holds ``snapshot_heap.elf``) and
        ``<root>/<target>/<version>/rtthread.elf`` for RT-Thread.
        """
        if self.rtos == "freertos":
            return self.fixture_cache / self.target / self.version / self.variant
        return self.fixture_cache / self.target / self.version

    def elf_path(self) -> Path:
        """Return the DWARF ELF GDB should load."""
        if self.elf_override is not None:
            return self.elf_override
        name = "freertos.elf" if self.rtos == "freertos" else "rtthread.elf"
        return self.fixture_dir() / name

    def firmware_path(self) -> Path:
        """Return the image QEMU boots.

        Reason: the RISC-V lanes load a raw binary via ``-bios`` (the ``virt``
        machine maps it at 0x80000000), so QEMU boots the ``.bin`` sibling of
        the DWARF ELF while GDB reads the ELF; every ARM lane boots the ELF
        itself via ``-kernel``. All builders and the cache install the pair
        side by side with the same stem, so the sibling rule needs no
        separate override knob.
        """
        if (self.rtos, self.target) in _RAW_BIN_BOOT_TARGETS:
            return self.elf_path().with_suffix(".bin")
        return self.elf_path()


def load_integration_spec(
    environ: Mapping[str, str] | None = None,
) -> IntegrationSpec:
    """Read the closed-loop session from ``environ`` (default: ``os.environ``).

    Empty values are treated as unset so a blank export means "use the
    default" rather than "use the empty string".
    """
    env = os.environ if environ is None else environ
    rtos = _env_str(env, "GDR_RTOS", "rtthread")
    if rtos == "freertos":
        variant = _env_str(env, "GDR_FIXTURE_VARIANT", _FREERTOS_DEFAULT_VARIANT)
        version = _env_str(env, "GDR_VERSION", _FREERTOS_DEFAULT_VERSION)
        target = _env_str(env, "GDR_QEMU_TARGET", _FREERTOS_DEFAULT_TARGET)
        cache = Path(
            _env_str(env, "FREERTOS_FIXTURE_CACHE", str(_DEFAULT_FREERTOS_CACHE))
        )
    elif rtos == "rtthread":
        variant = ""
        version = _env_str(env, "GDR_VERSION", _RTTHREAD_DEFAULT_VERSION)
        target = _env_str(env, "GDR_QEMU_TARGET", _RTTHREAD_DEFAULT_TARGET)
        cache = Path(
            _env_str(env, "RT_THREAD_FIXTURE_CACHE", str(_DEFAULT_RTTHREAD_CACHE))
        )
    else:
        raise RuntimeError(f"unknown GDR_RTOS: {rtos}")
    return IntegrationSpec(
        rtos=rtos,
        version=version,
        target=target,
        variant=variant,
        gdb=_env_str(env, "GDR_GDB", "gdb"),
        boot_wait=float(_env_str(env, "GDR_BOOT_WAIT", "10")),
        force_build=_env_str(env, "GDR_FORCE_BUILD", "0") == "1",
        fixture_cache=cache,
        elf_override=_env_optional_path(env, "GDR_ELF_PATH"),
        qemu=env.get("GDR_QEMU") or None,
    )
