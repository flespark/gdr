"""Explicit firmware coordinates shared by local and hosted verification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cache import CACHE


@dataclass(frozen=True)
class Lane:
    """One kernel, board and configuration; never a host path."""

    rtos: str
    target: str
    version: str
    variant: str = ""

    @property
    def key(self) -> str:
        return "/".join(
            filter(None, (self.rtos, self.target, self.version, self.variant))
        )

    @property
    def directory(self) -> Path:
        return CACHE / "fixtures" / self.key

    @property
    def architecture(self) -> str:
        if "rv32" in self.target:
            return "riscv:rv32"
        if "rv64" in self.target:
            return "riscv:rv64"
        return "arm"

    @property
    def qemu(self) -> str | None:
        if self.variant == "snapshot":
            return None
        return {
            "arm": "qemu-system-arm",
            "riscv:rv32": "qemu-system-riscv32",
            "riscv:rv64": "qemu-system-riscv64",
        }[self.architecture]

    @property
    def tag(self) -> str:
        if self.rtos == "bare":
            return "bare"
        if self.rtos == "rtthread":
            return f"v{self.version}"
        return "V10.3.1-kernel-only" if self.version == "10.3.1" else f"V{self.version}"

    @property
    def artifacts(self) -> tuple[str, ...]:
        if self.variant == "eh-frame":
            return (
                "unwind.elf",
                "unwind.bin",
                "frames.txt",
                "unwind_hdr.elf",
                "unwind_hdr.bin",
                "hdr_frames.txt",
                "unwind_reloc.elf",
                "unwind_reloc.sym.elf",
                "unwind_reloc.bin",
                "reloc_frames.txt",
            )
        if self.variant == "snapshot":
            return ("freertos.elf", "snapshot_heap.elf")
        if self.rtos == "freertos":
            return ("freertos.elf", "freertos.bin")
        if self.target in {"rv32", "rv64"}:
            return ("rtthread.elf", "rtthread.bin", ".debug-macros")
        return ("rtthread.elf", ".debug-macros")


FREERTOS_CONFIG_VARIANTS = (
    "base",
    "full",
    "static-only",
    "static-dynamic",
    "trace-off",
    "heap-1",
    "heap-2",
    "heap-3",
    "heap-5",
    "heap-protector",
    "registry-0",
    "pend-callback",
    "streams",
    "tick16",
)
RTTHREAD_A9 = tuple(
    Lane("rtthread", "cortex-a9", v)
    for v in (
        "3.1.0",
        "3.1.1",
        "3.1.2",
        "3.1.3",
        "3.1.4",
        "3.1.5",
        "4.0.0",
        "4.0.5",
        "4.1.1",
    )
)
RTTHREAD_RV64 = tuple(
    Lane("rtthread", "rv64", v) for v in ("4.0.4", "4.0.5", "4.1.0", "4.1.1")
)
RTTHREAD_PORTS = (
    Lane("rtthread", "mps2-an385", "4.0.5"),
    Lane("rtthread", "microbit", "4.0.5"),
    Lane("rtthread", "mps2-an505", "4.0.5"),
    Lane("rtthread", "mps2-an505", "4.0.5", "fpu"),
    Lane("rtthread", "mps2-an386", "4.0.5", "fpu"),
    Lane("rtthread", "rv32", "4.0.5"),
)
FREERTOS_CONFIG = tuple(
    Lane("freertos", "mps2-an385", "10.3.1", v) for v in FREERTOS_CONFIG_VARIANTS
)
FREERTOS_VERSION = (
    *(Lane("freertos", "mps2-an385", v, "base") for v in ("10.4.6", "10.5.1")),
    Lane("freertos", "qemu-virt-rv32", "10.3.1", "base"),
    Lane("freertos", "qemu-virt-rv32", "10.4.6", "base"),
    Lane("freertos", "qemu-virt-rv64", "10.4.6", "rv64"),
    Lane("freertos", "qemu-virt-rv32", "11.3.1", "base"),
    Lane("freertos", "qemu-virt-rv64", "11.3.1", "rv64"),
    *(
        Lane("freertos", "mps2-an385", "11.1.0", v)
        for v in (
            "base",
            "full",
            "heap-protector",
            "heap-2",
            "heap-5-protector",
            "streams",
            "pend-callback",
            "tick16",
        )
    ),
)
FREERTOS_PORTS = (
    Lane("freertos", "mps2-an521", "11.3.1", "smp"),
    Lane("freertos", "mps2-an521", "11.3.1", "mpu"),
    Lane("freertos", "mps2-an386", "11.3.1", "fpu"),
    Lane("freertos", "mps2-an505", "11.3.1", "full"),
    Lane("freertos", "qemu-virt-rv64", "11.1.0", "rv64"),
    Lane("freertos", "microbit", "11.1.0", "full"),
    Lane("freertos", "qemu-virt-rv32", "11.1.0", "base"),
    Lane("freertos", "mps2-an521", "11.1.0", "snapshot"),
)
BARE = (Lane("bare", "qemu-virt-rv64", "none", "eh-frame"),)

GROUPS = {
    "rtthread-a9": RTTHREAD_A9,
    "rtthread-rv64": RTTHREAD_RV64,
    "rtthread-ports": RTTHREAD_PORTS,
    "freertos-config": FREERTOS_CONFIG,
    "freertos-version": FREERTOS_VERSION,
    "freertos-ports": FREERTOS_PORTS,
    "bare": BARE,
    "gdb12": (Lane("rtthread", "cortex-a9", "4.0.5"),),
}
GROUPS["rtthread"] = RTTHREAD_A9 + RTTHREAD_RV64 + RTTHREAD_PORTS
GROUPS["freertos"] = FREERTOS_CONFIG + FREERTOS_VERSION + FREERTOS_PORTS
GROUPS["all"] = GROUPS["rtthread"] + GROUPS["freertos"] + BARE
GROUPS["smoke"] = (
    Lane("rtthread", "cortex-a9", "4.0.5"),
    Lane("rtthread", "rv64", "4.0.5"),
    *RTTHREAD_PORTS,
    Lane("freertos", "mps2-an385", "11.1.0", "base"),
    *FREERTOS_PORTS,
    *BARE,
)
SUPPORTED = tuple(
    dict.fromkeys(
        (
            *GROUPS["all"],
            *(
                Lane("rtthread", "cortex-a9", v)
                for v in ("4.0.1", "4.0.2", "4.0.3", "4.0.4", "4.1.0")
            ),
        )
    )
)


def matrix_build_only(lane: Lane) -> bool:
    """Retain the A9 matrix's compile-only releases; explicit run runs tests."""
    return (
        lane.rtos == "rtthread"
        and lane.target == "cortex-a9"
        and lane.version in {"3.1.1", "3.1.2", "3.1.4"}
    )


def select(rtos: str, target: str, version: str, variant: str | None = None) -> Lane:
    """Validate coordinates before creating directories or launching tools."""
    version = version.lstrip("vV")
    matches = [
        lane
        for lane in SUPPORTED
        if (lane.rtos, lane.target, lane.version) == (rtos, target, version)
    ]
    if variant is None:
        defaults = {"qemu-virt-rv64": "rv64", "microbit": "full"}
        variant = "" if rtos == "rtthread" else defaults.get(target, "base")
        if len(matches) == 1:
            variant = matches[0].variant
    for lane in matches:
        if lane.variant == variant:
            return lane
    raise ValueError(
        f"unsupported lane: {rtos}/{target}/{version}/{variant}; run 'list'"
    )
