"""Unit tests for RT-Thread supported-version intervals."""

from __future__ import annotations

import pytest

import rtthread.version as version


@pytest.mark.parametrize("value", ("3.1.0", "3.1.5", "4.0.0", "4.1.1"))
def test_validate_version_accepts_supported_interval_endpoints(value, monkeypatch):
    """Both explicitly verified intervals accept their boundary releases."""
    monkeypatch.setattr(version, "warn", lambda _message: None)

    assert version.validate_version(value) == tuple(map(int, value.split(".")))


@pytest.mark.parametrize("value", ("3.0.9", "3.1.6", "3.2.0", "4.1.2"))
def test_validate_version_rejects_unverified_gaps(value, monkeypatch):
    """Adjacent and intermediate releases are not implied by range support."""
    monkeypatch.setattr(version, "warn", lambda _message: None)

    with pytest.raises(SystemExit):
        version.validate_version(value)


def test_check_version_returns_the_parsed_target_profile(monkeypatch):
    """Bootstrap receives the parsed version needed by the layout factory."""
    monkeypatch.setattr(version, "detect_target_version", lambda: None)
    monkeypatch.setattr(version, "warn", lambda _message: None)

    assert version.check_version("3.1.3") == (3, 1, 3)


def test_detect_target_version_uses_declared_packed_hex_encoding(monkeypatch):
    monkeypatch.setattr(
        version, "eval_safe", lambda name: 0x40005 if name == "RT_VER_NUM" else None
    )
    monkeypatch.setattr(version, "read_int", lambda value: value)

    assert version.detect_target_version() == (4, 0, 5)


def test_check_version_rejects_a_target_mismatch(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "detect_target_version", lambda: (4, 0, 5))

    with pytest.raises(SystemExit):
        version.check_version("3.1.3")

    assert "version mismatch" in warnings[-1]
