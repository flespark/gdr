"""Unit tests for the RT-Thread presence/version probe."""

from __future__ import annotations

import rtthread.probe as probe_module
from rtthread.navigation import PROBE_MARKERS


def _stub(monkeypatch, *, present: tuple[str, ...], version):
    """Make only *present* markers resolve, and pin the version detection."""
    monkeypatch.setattr(probe_module, "symbol_exists", lambda name: name in present)
    monkeypatch.setattr(probe_module, "detect_target_version", lambda: version)


def test_probe_returns_none_when_no_marker_matches(monkeypatch):
    """An adapter with nothing to claim must not appear in the report."""
    _stub(monkeypatch, present=(), version=None)

    assert probe_module.probe() is None


def test_probe_reports_an_exact_version_with_its_evidence(monkeypatch):
    """A resolvable version is ``exact``, and the evidence names its source."""
    _stub(monkeypatch, present=PROBE_MARKERS[:2], version=(4, 0, 5))

    candidate = probe_module.probe()

    assert candidate is not None
    assert candidate.confidence == "exact"
    assert candidate.version == "4.0.5"
    assert candidate.evidence[0] == f"symbol: {PROBE_MARKERS[0]}"
    assert "RT_VER_NUM" in candidate.evidence[-1]


def test_probe_reports_an_unreadable_version_as_undetected(monkeypatch):
    """Detected, but versionless: the caller must ask, and is told how to fix it."""
    _stub(monkeypatch, present=PROBE_MARKERS[:1], version=None)

    candidate = probe_module.probe()

    assert candidate is not None
    assert candidate.confidence == "undetected"
    assert candidate.version is None
    assert "readable" in candidate.evidence[-1]
    assert "-g3" in candidate.detail
