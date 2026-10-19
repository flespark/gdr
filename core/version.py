"""RTOS-neutral semantic-version parsing and numeric decoding."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
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


# ---------------------------------------------------------------------------
# Adapter self-description registry for ``gdr probe``.
# ---------------------------------------------------------------------------


ProbeConfidence = Literal["exact", "range", "undetected"]

_CONFIDENCE_ORDER: dict[str, int] = {"exact": 0, "range": 1, "undetected": 2}


@dataclass(frozen=True)
class ProbeCandidate:
    """One adapter's claim that it is present, with the evidence for it.

    Attributes:
        rtos: Adapter name accepted by ``gdr init``.
        confidence: ``exact`` when the build exports a supported version,
            ``range`` when only the layout era could be narrowed down, and
            ``undetected`` when the adapter is present but exports no version.
        version: The exported version (``exact`` only).
        evidence: Human-readable proof lines (matched symbols, version source,
            structural fingerprints).  A client shows these rather than
            trusting a bare verdict.
        detail: What to do about a non-``exact`` confidence.
    """

    rtos: str
    confidence: ProbeConfidence
    version: str | None = None
    evidence: tuple[str, ...] = ()
    detail: str = ""


ProbeFn = Callable[[], ProbeCandidate | None]

_probes: list[ProbeFn] = []


def register_probe(probe: ProbeFn) -> None:
    """Register one adapter probe; re-registration is idempotent."""
    if probe not in _probes:
        _probes.append(probe)


def registered_probes() -> tuple[ProbeFn, ...]:
    """The registered probes, in registration order."""
    return tuple(_probes)


def reset_probes() -> None:
    """Drop every registered probe (session reset and tests)."""
    _probes.clear()


def collect_candidates(probes: Iterable[ProbeFn] | None = None) -> list[ProbeCandidate]:
    """Run the probes and return the adapters that found themselves present.

    Candidates are ordered by confidence and then by name, so a client can
    take the first one without re-implementing the ranking.
    """
    results: list[ProbeCandidate] = []
    for probe in registered_probes() if probes is None else probes:
        candidate = probe()
        if candidate is not None:
            results.append(candidate)
    results.sort(
        key=lambda item: (_CONFIDENCE_ORDER.get(item.confidence, 9), item.rtos)
    )
    return results


def resolve_candidate(candidates: Iterable[ProbeCandidate]) -> ProbeCandidate | None:
    """The one candidate safe to initialize from, or ``None``.

    Only an unambiguous ``exact`` result qualifies: two adapters present, or a
    version that is merely a range, must reach the user as a question rather
    than a guess.
    """
    exact = [item for item in candidates if item.confidence == "exact"]
    return exact[0] if len(exact) == 1 else None
