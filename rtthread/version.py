"""RT-Thread version policy and target-version probing."""

from __future__ import annotations

from gdr.gdb_bridge import lookup_symbol, read_int, read_macro_int, warn
from gdr.version import (
    Version,
    VersionEncoding,
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
# Reason: the annotation keeps the encoding literals typed as VersionEncoding;
# without it the tuple widens to plain str and decode_version's parameter no
# longer type-checks.
TARGET_VERSION_SYMBOLS: tuple[tuple[str, tuple[VersionEncoding, ...]], ...] = (
    ("RT_VER_NUM", ("packed-hex", "decimal")),
    ("RTTHREAD_VERSION", ("decimal", "packed-hex")),
    ("gdr_rtthread_version_num", ("decimal", "packed-hex")),
)


def validate_version(version: str) -> Version | None:
    """Validate the RT-Thread version argument accepted by this adapter.

    ``None`` (already warned) on failure.  Reason: policy failures must not
    raise ``SystemExit`` -- a GDB command that raises it kills the whole
    GDB session; callers abort the init on ``None`` instead.
    """
    parsed = parse_version(version)
    if parsed is None:
        warn(f"invalid RT-Thread version: {version!r}")
        warn("expected full RT-Thread version form, e.g. 4.0.5")
        return None
    if not version_in_ranges(parsed, SUPPORTED_RANGES):
        warn(f"unsupported RT-Thread version: {version!r}")
        warn("currently verified: 3.1.0 through 3.1.5, and 4.0.0 through 4.1.1")
        return None
    return parsed


def detect_target_version() -> Version | None:
    """Best-effort RT-Thread version detection from exported constants."""
    for expression, encodings in TARGET_VERSION_SYMBOLS:
        detected = decode_version(
            read_int(lookup_symbol(expression)) or read_macro_int(expression) or 0,
            encodings,
            SUPPORTED_RANGES,
        )
        if detected is not None:
            return detected
    return None


def check_version(version: str) -> Version | None:
    """Validate the requested version and compare with the target when known.

    ``None`` (already warned) when the argument is invalid/unsupported or
    disagrees with the target; the bootstrap aborts the init on ``None``
    instead of raising (a GDB command that raises ``SystemExit`` kills the
    whole session).
    """
    expected = validate_version(version)
    if expected is None:
        return None
    detected = detect_target_version()
    if detected is None:
        warn("target RT-Thread version not exported; cannot verify version")
        return expected
    if detected != expected:
        warn(
            f"RT-Thread version mismatch: expected {version}, "
            f"target is {format_version(detected)}"
        )
        return None
    return expected
