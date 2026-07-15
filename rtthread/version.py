"""RT-Thread version validation and target-version probing."""

from __future__ import annotations

import re

from gdr.gdb_bridge import eval_safe, read_int, warn

SUPPORTED_MIN = (4, 0, 0)
SUPPORTED_MAX = (4, 1, 1)


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Parse a three-part version string such as ``"4.0.5"``."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return None
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def validate_version(version: str) -> tuple[int, int, int]:
    """Validate the RT-Thread version argument accepted by this adapter."""
    parsed = parse_version(version)
    if parsed is None:
        warn(f"invalid RT-Thread version: {version!r}")
        warn("expected full RT-Thread version form, e.g. 4.0.5")
        raise SystemExit(1)

    if not (SUPPORTED_MIN <= parsed <= SUPPORTED_MAX):
        warn(f"unsupported RT-Thread version: {version!r}")
        warn("currently verified: 4.0.0 through 4.1.1")
        raise SystemExit(1)

    return parsed


def _version_from_num(value: int) -> tuple[int, int, int] | None:
    """Decode known RT-Thread numeric version encodings."""
    if value <= 0:
        return None

    # RT_VER_NUM uses packed hex form, e.g. 0x40005 -> 4.0.5.
    if value >= 0x10000:
        return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF

    # RTTHREAD_VERSION uses decimal form, e.g. 40005 -> 4.0.5.
    major = value // 10000
    minor = (value % 10000) // 100
    patch = value % 100
    return major, minor, patch


def detect_target_version() -> tuple[int, int, int] | None:
    """Best-effort RT-Thread version detection from exported constants."""
    for expr in ("RT_VER_NUM", "RTTHREAD_VERSION", "gdr_rtthread_version_num"):
        detected = _version_from_num(read_int(eval_safe(expr)) or 0)
        if detected is not None:
            return detected
    return None


def check_version(version: str) -> None:
    """Validate requested version and compare with target when available."""
    expected = validate_version(version)
    detected = detect_target_version()
    if detected is None:
        warn("target RT-Thread version not exported; cannot verify version")
        return
    if detected != expected:
        actual = ".".join(str(part) for part in detected)
        warn(f"RT-Thread version mismatch: expected {version}, target is {actual}")
        raise SystemExit(1)
