"""Adapter self-description for RTOS and version probing.

``gdr probe`` asks every registered adapter whether it is present on the
halted target and, when the build exports one, which version it is.  The core
owns only the registry and the result shape: an adapter supplies its own
marker symbols and evidence, so no RTOS name or target symbol appears here.

This is deliberately *reporting*, not selection: GDR still never picks an
adapter on the user's behalf (see the architecture decision on auto-detection).
The command exists so a client can present evidence and a candidate, and can
say precisely why a candidate is only a range or has no version at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

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
