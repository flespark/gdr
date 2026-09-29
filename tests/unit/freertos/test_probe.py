"""Unit tests for the FreeRTOS presence/version probe and its era fallback."""

from __future__ import annotations

import freertos.probe as probe_module
from freertos.layout import PROBE_MARKERS, SMP_ERA_FIELD


def _stub(monkeypatch, *, present: tuple[str, ...], version, tcb_fields=()):
    """Resolve only *present* markers, pin the version, pin the TCB fields."""
    monkeypatch.setattr(probe_module, "symbol_exists", lambda name: name in present)
    monkeypatch.setattr(probe_module, "detect_target_version", lambda: version)
    monkeypatch.setattr(
        probe_module, "type_field_names", lambda _type_name: frozenset(tcb_fields)
    )


def test_probe_returns_none_when_no_marker_matches(monkeypatch):
    """An adapter with nothing to claim must not appear in the report."""
    _stub(monkeypatch, present=(), version=None)

    assert probe_module.probe() is None


def test_probe_reports_an_exact_version_with_its_evidence(monkeypatch):
    """A resolvable version is ``exact`` and names the macros behind it."""
    _stub(monkeypatch, present=PROBE_MARKERS[:2], version=(10, 3, 1))

    candidate = probe_module.probe()

    assert candidate is not None
    assert candidate.confidence == "exact"
    assert candidate.version == "10.3.1"
    assert "tskKERNEL_VERSION_MAJOR" in candidate.evidence[-1]


def test_probe_narrows_the_era_when_the_version_macros_are_absent(monkeypatch):
    """A -g2 build still yields a range, with the fingerprint as evidence."""
    _stub(
        monkeypatch,
        present=PROBE_MARKERS[:1],
        version=None,
        tcb_fields=(SMP_ERA_FIELD,),
    )

    candidate = probe_module.probe()

    assert candidate is not None
    assert candidate.confidence == "range"
    assert candidate.version is None
    assert any(SMP_ERA_FIELD in line for line in candidate.evidence)
    assert "confirmed by the user" in candidate.detail

    _stub(monkeypatch, present=PROBE_MARKERS[:1], version=None)

    pre_smp = probe_module.probe()

    assert pre_smp is not None
    assert pre_smp.confidence == "range"
    assert any("absent" in line for line in pre_smp.evidence)
