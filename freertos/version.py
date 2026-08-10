"""FreeRTOS version parsing and explicit compatibility policy."""

from __future__ import annotations

import re

from gdr.gdb_bridge import eval_safe, read_int, warn

Version = tuple[int, int, int]

SUPPORTED_RANGES: tuple[tuple[Version, Version], ...] = (
    ((10, 3, 0), (10, 3, 1)),
    ((10, 5, 0), (10, 6, 2)),
    ((11, 0, 0), (11, 1, 0)),
)
INTERNAL_PROFILES: tuple[tuple[Version, Version], ...] = (
    ((10, 4, 0), (10, 4, 99)),
    ((11, 2, 0), (11, 3, 99)),
)


def profile_for_version(value: Version | str) -> str:
    """Return the explicit layout/build profile selected by a version."""
    parsed = parse_version(value) if isinstance(value, str) else value
    if parsed is None:
        return "unsupported"
    if any(lo <= parsed <= hi for lo, hi in SUPPORTED_RANGES):
        return "public"
    if any(lo <= parsed <= hi for lo, hi in INTERNAL_PROFILES):
        return "internal"
    return "unsupported"


def parse_version(value: str) -> Version | None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value.strip()):
        return None
    return tuple(int(part) for part in value.strip().split("."))  # type: ignore[return-value]


def _range_text() -> str:
    return ", ".join(
        f"{a[0]}.{a[1]}.{a[2]}-{b[0]}.{b[1]}.{b[2]}" for a, b in SUPPORTED_RANGES
    )


def validate_version(value: str) -> Version:
    parsed = parse_version(value)
    if parsed is None:
        warn(f"invalid FreeRTOS version: {value!r}; expected X.Y.Z")
        raise SystemExit(1)
    if not any(lo <= parsed <= hi for lo, hi in SUPPORTED_RANGES):
        internal = any(lo <= parsed <= hi for lo, hi in INTERNAL_PROFILES)
        suffix = " (internal layout/build profile only)" if internal else ""
        warn(f"unsupported FreeRTOS version: {value!r}{suffix}")
        warn(f"supported public ranges: {_range_text()}")
        raise SystemExit(1)
    return parsed


def _decode(value: int) -> Version | None:
    if value <= 0:
        return None
    # FreeRTOS_tskVersion is commonly a packed decimal or hexadecimal number.
    if value >= 0x10000:
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    if value >= 10000:
        return (value // 10000, (value // 100) % 100, value % 100)
    return None


def detect_target_version() -> Version | None:
    for expr in (
        "gdr_freertos_version_num",
        "tskKERNEL_VERSION",
        "FREERTOS_KERNEL_VERSION",
    ):
        detected = _decode(read_int(eval_safe(expr)) or 0)
        if detected:
            return detected
    return None


def check_version(value: str) -> Version:
    expected = validate_version(value)
    actual = detect_target_version()
    if actual is None:
        warn(
            "target FreeRTOS version is not exported; requested version is not guessed"
        )
    elif actual != expected:
        text = ".".join(map(str, actual))
        warn(f"FreeRTOS version mismatch: requested {value}, target exports {text}")
        raise SystemExit(1)
    return expected
