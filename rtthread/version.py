"""RT-Thread version policy and target-version probing."""

from __future__ import annotations

from gdr.gdb_bridge import eval_safe, read_int, warn
from gdr.version import (
    Version,
    VersionRange,
    decode_version,
    format_version,
    parse_version,
    version_in_ranges,
)

SUPPORTED_RANGES: tuple[VersionRange, ...] = (
    ((3, 1, 0), (3, 1, 5)),
    ((4, 0, 0), (4, 1, 1)),
)
TARGET_VERSION_SYMBOLS = (
    ("RT_VER_NUM", ("packed-hex", "decimal")),
    ("RTTHREAD_VERSION", ("decimal", "packed-hex")),
    ("gdr_rtthread_version_num", ("decimal", "packed-hex")),
)


def validate_version(version: str) -> Version:
    """Validate the RT-Thread version argument accepted by this adapter."""
    parsed = parse_version(version)
    if parsed is None:
        warn(f"invalid RT-Thread version: {version!r}")
        warn("expected full RT-Thread version form, e.g. 4.0.5")
        raise SystemExit(1)
    if not version_in_ranges(parsed, SUPPORTED_RANGES):
        warn(f"unsupported RT-Thread version: {version!r}")
        warn("currently verified: 3.1.0 through 3.1.5, and 4.0.0 through 4.1.1")
        raise SystemExit(1)
    return parsed


def detect_target_version() -> Version | None:
    """Best-effort RT-Thread version detection from exported constants."""
    for expression, encodings in TARGET_VERSION_SYMBOLS:
        detected = decode_version(
            read_int(eval_safe(expression)) or 0,
            encodings,
            SUPPORTED_RANGES,
        )
        if detected is not None:
            return detected
    return None


def check_version(version: str) -> Version:
    """Validate requested version and compare with target when available."""
    expected = validate_version(version)
    detected = detect_target_version()
    if detected is None:
        warn("target RT-Thread version not exported; cannot verify version")
        return expected
    if detected != expected:
        warn(
            f"RT-Thread version mismatch: expected {version}, "
            f"target is {format_version(detected)}"
        )
        raise SystemExit(1)
    return expected
