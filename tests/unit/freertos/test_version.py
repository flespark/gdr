"""FreeRTOS version policy tests."""

import pytest

import freertos.version as version


@pytest.mark.parametrize("value", ("10.3.0", "10.3.1", "10.5.0", "11.1.0"))
def test_validate_version_accepts_supported_ranges(value, monkeypatch):
    monkeypatch.setattr(version, "warn", lambda _message: None)
    assert version.validate_version(value) == tuple(map(int, value.split(".")))


def test_target_version_uses_declared_decimal_encoding(monkeypatch):
    monkeypatch.setattr(
        version, "eval_safe", lambda name: 100301 if name.startswith("gdr_") else None
    )
    monkeypatch.setattr(version, "read_int", lambda value: value)
    assert version.detect_target_version() == (10, 3, 1)


def test_validate_version_rejects_invalid_and_unsupported_values(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)

    with pytest.raises(SystemExit):
        version.validate_version("10.3")
    with pytest.raises(SystemExit):
        version.validate_version("10.4.0")

    assert "invalid FreeRTOS version" in warnings[0]
    assert "unsupported FreeRTOS version" in warnings[1]


def test_target_version_uses_declared_packed_hex_encoding(monkeypatch):
    monkeypatch.setattr(
        version,
        "eval_safe",
        lambda name: 0xA0301 if name == "tskKERNEL_VERSION" else None,
    )
    monkeypatch.setattr(version, "read_int", lambda value: value)

    assert version.detect_target_version() == (10, 3, 1)


def test_check_version_rejects_a_target_mismatch(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "detect_target_version", lambda: (10, 5, 0))

    with pytest.raises(SystemExit):
        version.check_version("10.3.1")

    assert "version mismatch" in warnings[-1]
