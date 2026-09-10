"""FreeRTOS version policy and target-version probing."""

from __future__ import annotations

import re

from gdr.gdb_bridge import (
    lookup_symbol,
    read_int,
    read_macro_int,
    read_macro_text,
    read_macro_text_in_source,
    warn,
)
from gdr.version import (
    Version,
    VersionRange,
    decode_version,
    format_version,
    parse_version,
    version_in_ranges,
)

# Reason: V11.3.1 introduced ARMv8-M SMP support (History.txt, V11.3.1), and
# the six kernel header structs are identical between V11.2.0 and V11.3.1, so
# the public range extends through 11.3.x without new layout branches.
SUPPORTED_RANGES: tuple[VersionRange, ...] = (((10, 3, 0), (11, 3, 99)),)

# Matches the tskKERNEL_VERSION_NUMBER string macro, e.g. "V10.3.1" or
# "V11.1.0+": an optional leading V, three decimal components, and an
# optional trailing '+' (dirty builds).
_VERSION_STRING_RE = re.compile(r"^V?(\d+)\.(\d+)\.(\d+)\+?$")

# The integer macro triplet that FreeRTOS-Kernel exports (task.h).
_VERSION_TRIPLET = (
    "tskKERNEL_VERSION_MAJOR",
    "tskKERNEL_VERSION_MINOR",
    "tskKERNEL_VERSION_BUILD",
)


def _range_text() -> str:
    return ", ".join(
        f"{format_version(lower)}-{format_version(upper)}"
        for lower, upper in SUPPORTED_RANGES
    )


def validate_version(value: str) -> Version | None:
    """Validate the version argument; ``None`` (already warned) on failure.

    Reason: policy failures must not raise ``SystemExit`` -- a GDB command
    that raises it kills the whole GDB session, so a typo'd version would
    cost the user their target. Callers abort the init on ``None`` instead.
    """
    parsed = parse_version(value)
    if parsed is None:
        warn(f"invalid FreeRTOS version: {value!r}; expected X.Y.Z")
        return None
    if not version_in_ranges(parsed, SUPPORTED_RANGES):
        warn(f"unsupported FreeRTOS version: {value!r}")
        warn(f"supported public ranges: {_range_text()}")
        return None
    return parsed


def _macro_int_in_kernel_cu(name: str) -> int | None:
    """Read an integer macro from the FreeRTOS kernel compilation unit."""
    value = read_macro_int(name)
    if value is not None:
        return value
    text = read_macro_text_in_source(name, "vTaskStartScheduler")
    if text is None:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def _macro_triplet() -> Version | None:
    """Read the tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD integer macros.

    The first attempt uses the current CU. If the selected source is outside
    the kernel, the fallback temporarily lists ``vTaskStartScheduler`` to
    select a kernel CU, reads the macro, and restores the user's source view.
    """
    components: list[int] = []
    for name in _VERSION_TRIPLET:
        value = _macro_int_in_kernel_cu(name)
        if value is None:
            return None
        components.append(value)
    return (components[0], components[1], components[2])


def _version_number_string() -> Version | None:
    """Parse the tskKERNEL_VERSION_NUMBER string macro, or ``None``."""
    text = read_macro_text("tskKERNEL_VERSION_NUMBER")
    if text is None:
        text = read_macro_text_in_source(
            "tskKERNEL_VERSION_NUMBER", "vTaskStartScheduler"
        )
    if text is None:
        return None
    match = _VERSION_STRING_RE.match(text.strip())
    if match is None:
        return None
    # Reason: the regex normally guarantees decimal groups, but keep target
    # macro text untrusted at this GDB boundary.
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def detect_target_version() -> Version | None:
    """Best-effort FreeRTOS version detection from exported constants.

    Priority:
    1. the ``tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD`` integer-macro triplet;
    2. the ``tskKERNEL_VERSION_NUMBER`` string macro (``"V10.3.1"`` / ``+``);
    3. the project-injected ``gdr_freertos_version_num`` symbol
       (decimal or packed-hex encoding).

    The upstream kernel exports no single numeric version symbol, so the
    ``gdr_freertos_version_num`` fallback exists for builds that link a
    helper symbol instead of relying on macro debug info.
    """
    triplet = _macro_triplet()
    if triplet is not None:
        return triplet
    string_version = _version_number_string()
    if string_version is not None:
        return string_version
    numeric = read_int(lookup_symbol("gdr_freertos_version_num"))
    if numeric is None:
        return None
    return decode_version(numeric, ("decimal", "packed-hex"), SUPPORTED_RANGES)


def check_version(value: str) -> Version | None:
    """Validate the requested version against the target's exported one.

    Returns the parsed version, or ``None`` (already warned) when the
    argument is invalid/unsupported or disagrees with the target -- the
    bootstrap aborts the init on ``None`` instead of raising (a GDB command
    that raises ``SystemExit`` kills the whole session).
    """
    expected = validate_version(value)
    if expected is None:
        return None
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
        return None
    return expected
