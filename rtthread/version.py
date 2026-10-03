"""RT-Thread version policy and target-version probing."""

from __future__ import annotations

from gdr.gdb_bridge import (
    lookup_symbol,
    read_int,
    read_macro_int,
    read_macro_int_in_source,
    symbol_exists,
    warn,
)
from gdr.version import (
    TargetVerdict,
    Version,
    VersionEncoding,
    VersionRange,
    decode_version,
    format_version,
    parse_version,
    version_in_ranges,
)
from rtthread.navigation import PROBE_MARKERS

SUPPORTED_RANGES: tuple[VersionRange, ...] = (
    ((3, 1, 0), (3, 1, 5)),
    ((4, 0, 0), (4, 1, 1)),
)
# Reason: the annotation keeps the encoding literals typed as VersionEncoding;
# without it the tuple widens to plain str and decode_version's parameter no
# longer type-checks.
# Reason: the version constants are macros in a kernel header, so GDB only
# resolves them with a compilation unit selected (`info macro` otherwise
# answers "at <user-defined>:-1").  `rt_thread_init` lives in thread.c,
# which includes rtdef.h on every supported version.
_VERSION_MACRO_SOURCE = "rt_thread_init"

# The ordinal version macros every supported release defines.  They are plain
# single-line integers, unlike ``RTTHREAD_VERSION`` (a multi-line expression in
# 3.1.x/4.0.x and a macro call in 4.1.x), so they are the portable source.
_VERSION_TRIPLET = ("RT_VERSION", "RT_SUBVERSION", "RT_REVISION")

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


def _version_macro_int(name: str) -> int | None:
    """Read one version macro, preferring the compilation-unit-scoped read.

    Reason: the test must be ``is not None``, not truthiness.  A version
    ordinal is legitimately ``0`` -- ``RT_SUBVERSION`` is 0 in every release
    and ``RT_REVISION`` is 0 on a ``.0`` release -- so an ``or`` chain discards
    a successful read of 0 and reports the version as unreadable whenever only
    one of the two attempts happens to succeed in this session.
    """
    value = read_macro_int(name)
    if value is not None:
        return value
    return read_macro_int_in_source(name, _VERSION_MACRO_SOURCE)


def _version_triplet() -> Version | None:
    """The ``RT_VERSION``/``RT_SUBVERSION``/``RT_REVISION`` triplet, or ``None``.

    Reading three ordinals is what makes the 3.1.x range detectable at all:
    those releases export no packed ``RT_VER_NUM``, and their
    ``RTTHREAD_VERSION`` is a multi-line expression.
    """
    components: list[int] = []
    for name in _VERSION_TRIPLET:
        value = _version_macro_int(name)
        if value is None:
            return None
        components.append(value)
    candidate: Version = (components[0], components[1], components[2])
    return candidate if version_in_ranges(candidate, SUPPORTED_RANGES) else None


def detect_target_version() -> Version | None:
    """Best-effort RT-Thread version detection from exported constants."""
    triplet = _version_triplet()
    if triplet is not None:
        return triplet
    for expression, encodings in TARGET_VERSION_SYMBOLS:
        detected = decode_version(
            read_int(lookup_symbol(expression))
            or read_macro_int(expression)
            or read_macro_int_in_source(expression, _VERSION_MACRO_SOURCE)
            or 0,
            encodings,
            SUPPORTED_RANGES,
        )
        if detected is not None:
            return detected
    return None


def check_target(version: str, *, allow_unverified: bool = False) -> TargetVerdict:
    """Decide whether *version* may register this adapter on this target.

    The presence markers are ``navigation.PROBE_MARKERS``, the same table
    ``gdr probe`` matches, so the probe and this gate cannot disagree about
    what counts as evidence for "this target runs RT-Thread".

    Refused when the argument is invalid or unsupported, when the target
    exports a different version, or when no marker matched at all. That last
    refusal is what keeps a symbol-less or wrong-target session from
    registering an adapter whose every read would be empty; *allow_unverified*
    is the escape hatch for a deliberately modified kernel, and it records the
    override as the verdict's note rather than hiding it.

    A target that shows markers but exports no version constant is *accepted*
    with a note: the user asserted the version, which is exactly the flow the
    plugin's detection wizard needs.
    """
    expected = validate_version(version)
    if expected is None:
        return TargetVerdict(
            code="unsupported_version",
            message=f"invalid or unsupported RT-Thread version: {version!r}",
        )
    markers = tuple(name for name in PROBE_MARKERS if symbol_exists(name))
    detected = detect_target_version()
    if detected is not None and detected != expected:
        message = (
            f"RT-Thread version mismatch: requested {version}, "
            f"but target exports {format_version(detected)}"
        )
        warn(message)
        return TargetVerdict(code="version_mismatch", message=message)
    if not markers:
        if not allow_unverified:
            message = (
                "no RT-Thread symbol matched on this target; load the image "
                "first, or pass --allow-unverified to register anyway"
            )
            warn(message)
            return TargetVerdict(code="no_target_evidence", message=message)
        note = (
            "no RT-Thread symbol matched; registration was forced with "
            "--allow-unverified"
        )
        warn(note)
        return TargetVerdict(version=expected, note=note, markers=markers)
    if detected is None:
        note = (
            "target RT-Thread version not exported; the requested version is unverified"
        )
        warn(note)
        return TargetVerdict(version=expected, note=note, markers=markers)
    return TargetVerdict(version=expected, markers=markers)
