"""Unit tests for the probe registry and its neutral result contract."""

from __future__ import annotations

import pytest

from gdr.probe import (
    ProbeCandidate,
    collect_candidates,
    register_probe,
    registered_probes,
    reset_probes,
    resolve_candidate,
)
from gdr.serialize import probe_to_dict


def _dict_list(value: object) -> list[dict[str, object]]:
    """Narrow a serialized list of objects for typed assertions."""
    assert isinstance(value, list)
    result: list[dict[str, object]] = []
    for item in value:
        assert isinstance(item, dict)
        result.append(item)
    return result


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts and ends with an empty probe registry."""
    reset_probes()
    yield
    reset_probes()


def _strings(value: object) -> list[str]:
    """Narrow a serialized list of strings for typed assertions."""
    assert isinstance(value, list)
    result: list[str] = []
    for item in value:
        assert isinstance(item, str)
        result.append(item)
    return result


def test_registry_is_idempotent_and_resettable():
    """Re-sourcing gdr.py must not register the same probe twice."""

    def probe() -> ProbeCandidate | None:
        return None

    register_probe(probe)
    register_probe(probe)

    assert registered_probes() == (probe,)
    reset_probes()
    assert registered_probes() == ()


def test_candidates_are_ordered_by_confidence_then_name():
    """A client can take the first row without re-implementing the ranking."""
    register_probe(
        lambda: ProbeCandidate("zzz", "undetected", detail="only markers matched")
    )
    register_probe(lambda: ProbeCandidate("b-second", "exact", version="1.0.0"))
    register_probe(lambda: ProbeCandidate("a-first", "exact", version="2.0.0"))
    register_probe(lambda: None)

    ordered = collect_candidates()

    assert [(item.rtos, item.confidence) for item in ordered] == [
        ("a-first", "exact"),
        ("b-second", "exact"),
        ("zzz", "undetected"),
    ]


def test_resolution_requires_exactly_one_exact_candidate():
    """A range, an undetected version or two adapters must be a question."""
    exact = ProbeCandidate("rtthread", "exact", version="4.0.5")
    assert resolve_candidate([exact]).version == "4.0.5"

    assert resolve_candidate([]) is None
    assert resolve_candidate([ProbeCandidate("rtthread", "range")]) is None
    assert (
        resolve_candidate(
            [exact, ProbeCandidate("freertos", "exact", version="10.3.1")]
        )
        is None
    )


def test_probe_payload_carries_evidence_and_the_resolved_pair():
    """The machine reader sees the proof, not only a verdict."""
    payload = probe_to_dict(
        [
            ProbeCandidate(
                rtos="freertos",
                confidence="range",
                evidence=("symbol: pxCurrentTCB", "layout: xTaskRunState absent"),
                detail="confirm the version manually",
            )
        ]
    )

    assert payload["resolved"] is None
    candidate = _dict_list(payload["candidates"])[0]
    assert candidate["rtos"] == "freertos"
    assert candidate["version"] is None
    assert _strings(candidate["evidence"])[1] == "layout: xTaskRunState absent"
    assert candidate["detail"] == "confirm the version manually"


def test_a_raising_probe_propagates_instead_of_blanking_the_report():
    """A probe defect is a programming error, not a silent empty report.

    Expected target-access failures are already absorbed by the probe helpers
    (``symbol_exists`` and the read helpers degrade to safe defaults), so a
    raise that reaches here is a defect: it must surface through the command
    guard instead of being swallowed into "no RTOS detected".
    """

    def explode() -> ProbeCandidate | None:
        raise RuntimeError("probe defect")

    register_probe(explode)

    with pytest.raises(RuntimeError, match="probe defect"):
        collect_candidates()
