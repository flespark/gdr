"""RTOS-neutral semantic-version parsing and numeric decoding."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

Version = tuple[int, int, int]
VersionRange = tuple[Version, Version]
VersionEncoding = Literal["decimal", "packed-hex"]


@dataclass(frozen=True)
class TargetVerdict:
    """What a target itself could confirm about a requested adapter version.

    An accepted request carries ``version`` (the parsed profile a layout
    factory needs) and, when the target could not confirm the request, a
    ``note``. The note is recorded on the session and repeated in every later
    envelope, because a plugin cannot read this command's console output.

    A refusal carries ``code``/``message`` and no version: registration must
    not happen. Both paths describe the *same* evidence ladder ``gdr probe``
    reports, so the two entry points cannot disagree about a target.
    """

    version: Version | None = None
    note: str | None = None
    markers: tuple[str, ...] = ()
    code: str | None = None
    message: str | None = None

    @property
    def accepted(self) -> bool:
        """Whether the request may register an adapter."""
        return self.version is not None


def parse_version(value: str) -> Version | None:
    """Parse exactly three dot-separated decimal components."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        return None
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def format_version(value: Version) -> str:
    """Format a semantic version tuple as ``X.Y.Z``."""
    return ".".join(str(part) for part in value)


def version_in_ranges(value: Version, ranges: Iterable[VersionRange]) -> bool:
    """Return whether a version belongs to any inclusive range."""
    return any(lower <= value <= upper for lower, upper in ranges)


def decode_decimal_version(value: int) -> Version | None:
    """Decode ``MMmmpp``-style decimal versions, such as ``40005``."""
    if value < 10000:
        return None
    return value // 10000, (value % 10000) // 100, value % 100


def decode_packed_hex_version(value: int) -> Version | None:
    """Decode byte-packed hexadecimal versions, such as ``0x40005``."""
    if value <= 0 or value > 0xFFFFFF:
        return None
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def decode_version(
    value: int,
    encodings: Sequence[VersionEncoding],
    supported_ranges: Iterable[VersionRange],
) -> Version | None:
    """Decode using declared encodings and accept the first supported result."""
    ranges = tuple(supported_ranges)
    decoders = {
        "decimal": decode_decimal_version,
        "packed-hex": decode_packed_hex_version,
    }
    seen: set[Version] = set()
    for encoding in encodings:
        candidate = decoders[encoding](value)
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        if version_in_ranges(candidate, ranges):
            return candidate
    return None
