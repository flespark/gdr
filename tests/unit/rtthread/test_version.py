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

    assert version.validate_version(value) is None


def test_check_target_returns_the_parsed_target_profile(monkeypatch):
    """Bootstrap receives the parsed version the layout factory needs."""
    monkeypatch.setattr(version, "detect_target_version", lambda: None)
    monkeypatch.setattr(version, "warn", lambda _message: None)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: True)

    verdict = version.check_target("3.1.3")

    assert verdict.accepted
    assert verdict.version == (3, 1, 3)
    assert verdict.note is not None, "an unexported version is recorded, not hidden"
    assert verdict.markers == version.PROBE_MARKERS


def test_detect_target_version_uses_declared_packed_hex_encoding(monkeypatch):
    monkeypatch.setattr(
        version,
        "lookup_symbol",
        lambda name: 0x40005 if name == "RT_VER_NUM" else None,
    )
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(
        version, "read_macro_int_in_source", lambda _name, _source: None
    )
    monkeypatch.setattr(version, "read_int", lambda value: value)

    assert version.detect_target_version() == (4, 0, 5)


def test_detect_target_version_uses_macros_when_symbols_are_absent(monkeypatch):
    """Version macros are read through ``info macro``, not inferior evaluation."""
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(
        version,
        "read_macro_int",
        lambda name: 0x40005 if name == "RT_VER_NUM" else None,
    )
    monkeypatch.setattr(
        version, "read_macro_int_in_source", lambda _name, _source: None
    )
    monkeypatch.setattr(version, "read_int", lambda value: value)

    assert version.detect_target_version() == (4, 0, 5)


def test_check_target_rejects_a_target_mismatch(monkeypatch):
    """A mismatch refuses registration; the GDB session keeps running."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "detect_target_version", lambda: (4, 0, 5))
    monkeypatch.setattr(version, "symbol_exists", lambda _name: True)

    verdict = version.check_target("3.1.3")

    assert not verdict.accepted
    assert verdict.code == "version_mismatch"
    assert "version mismatch" in warnings[-1]


def test_check_target_refuses_a_target_without_any_marker(monkeypatch):
    """No RT-Thread symbol means no adapter: every read would come back empty."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)
    monkeypatch.setattr(version, "loaded_object_path", lambda: "/fw/rtthread.elf")

    verdict = version.check_target("4.0.5")

    assert not verdict.accepted
    assert verdict.code == "no_target_evidence"
    assert "--allow-unverified" in warnings[-1]


def test_check_target_without_a_symbol_file_names_file(monkeypatch):
    """`load` without `file` gets the actionable hint, not the load hint."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)
    monkeypatch.setattr(version, "loaded_object_path", lambda: None)

    verdict = version.check_target("4.0.5")

    assert not verdict.accepted
    assert verdict.code == "no_target_evidence"
    assert "`file <elf>`" in warnings[-1]
    assert "`load` only writes the target memory" in warnings[-1]


def test_allow_unverified_keeps_the_escape_hatch_open(monkeypatch):
    """A modified or stripped kernel must still be debuggable on request."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)

    verdict = version.check_target("4.0.5", allow_unverified=True)

    assert verdict.accepted
    assert verdict.version == (4, 0, 5)
    assert verdict.markers == ()
    assert verdict.note is not None and "--allow-unverified" in verdict.note


def test_check_target_rejects_an_unsupported_version_argument(monkeypatch):
    monkeypatch.setattr(version, "warn", lambda _message: None)

    verdict = version.check_target("9.9.9")

    assert not verdict.accepted
    assert verdict.code == "unsupported_version"
