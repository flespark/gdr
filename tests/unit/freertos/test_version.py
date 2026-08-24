"""FreeRTOS version policy tests."""

import pytest

import freertos.version as version


@pytest.mark.parametrize(
    "value",
    ("10.3.0", "10.3.1", "10.4.0", "10.5.0", "11.1.0", "11.2.0"),
)
def test_validate_version_accepts_supported_ranges(value, monkeypatch):
    monkeypatch.setattr(version, "warn", lambda _message: None)
    assert version.validate_version(value) == tuple(map(int, value.split(".")))


def test_validate_version_rejects_invalid_and_unsupported_values(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)

    with pytest.raises(SystemExit):
        version.validate_version("10.3")
    with pytest.raises(SystemExit):
        version.validate_version("12.0.0")

    assert "invalid FreeRTOS version" in warnings[0]
    assert "unsupported FreeRTOS version" in warnings[1]


def test_detect_version_from_macro_triplet(monkeypatch):
    """The tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD int macros win when present."""
    values = {
        "tskKERNEL_VERSION_MAJOR": 10,
        "tskKERNEL_VERSION_MINOR": 3,
        "tskKERNEL_VERSION_BUILD": 1,
    }
    monkeypatch.setattr(version, "read_macro_int", lambda name: values.get(name))
    monkeypatch.setattr(version, "read_macro_text", lambda _name: None)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == (10, 3, 1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("V11.1.0+", (11, 1, 0)),
        ("V10.3.1", (10, 3, 1)),
        ("10.5.0", (10, 5, 0)),
    ],
)
def test_detect_version_from_version_number_string(monkeypatch, text, expected):
    """The tskKERNEL_VERSION_NUMBER string macro is parsed with the V/+ forms."""
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name: text)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == expected


def test_detect_version_rejects_invalid_string(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name: "unknown")
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() is None


def test_detect_version_from_gdr_symbol_decimal_fallback(monkeypatch):
    """gdr_freertos_version_num (decimal MMmmpp) is the last-resort escape hatch."""
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name: None)
    monkeypatch.setattr(
        version,
        "lookup_symbol",
        lambda name: 100301 if name == "gdr_freertos_version_num" else None,
    )

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_from_gdr_symbol_packed_hex_fallback(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name: None)
    monkeypatch.setattr(
        version,
        "lookup_symbol",
        lambda name: 0xA0301 if name == "gdr_freertos_version_num" else None,
    )

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_returns_none_when_nothing_exported(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name: None)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() is None


def test_check_version_rejects_a_target_mismatch(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "detect_target_version", lambda: (10, 5, 0))

    with pytest.raises(SystemExit):
        version.check_version("10.3.1")

    assert "version mismatch" in warnings[-1]
