"""FreeRTOS version policy and target-version probing."""

from __future__ import annotations

from gdr.gdb_bridge import lookup_symbol, read_int, read_macro_int, warn
from gdr.version import (
    Version,
    VersionRange,
    decode_version,
    format_version,
    parse_version,
    version_in_ranges,
)

SUPPORTED_RANGES: tuple[VersionRange, ...] = (
    ((10, 3, 0), (10, 3, 1)),
    ((10, 5, 0), (10, 6, 2)),
    ((11, 0, 0), (11, 1, 0)),
)
TARGET_VERSION_SYMBOLS = (
    ("gdr_freertos_version_num", ("decimal", "packed-hex")),
    ("tskKERNEL_VERSION", ("decimal", "packed-hex")),
    ("FREERTOS_KERNEL_VERSION", ("decimal", "packed-hex")),
)


def _range_text() -> str:
    return ", ".join(
        f"{format_version(lower)}-{format_version(upper)}"
        for lower, upper in SUPPORTED_RANGES
    )


def validate_version(value: str) -> Version:
    parsed = parse_version(value)
    if parsed is None:
        warn(f"invalid FreeRTOS version: {value!r}; expected X.Y.Z")
        raise SystemExit(1)
    if not version_in_ranges(parsed, SUPPORTED_RANGES):
        warn(f"unsupported FreeRTOS version: {value!r}")
        warn(f"supported public ranges: {_range_text()}")
        raise SystemExit(1)
    return parsed


def detect_target_version() -> Version | None:
    for expression, encodings in TARGET_VERSION_SYMBOLS:
        detected = decode_version(
            read_int(lookup_symbol(expression)) or read_macro_int(expression) or 0,
            encodings,
            SUPPORTED_RANGES,
        )
        if detected is not None:
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
        warn(
            f"FreeRTOS version mismatch: requested {value}, "
            f"target exports {format_version(actual)}"
        )
        raise SystemExit(1)
    return expected
