"""FreeRTOS version policy tests."""

import pytest

import freertos.version as version


@pytest.mark.parametrize(
    "value",
    ("10.3.0", "10.3.1", "10.4.0", "10.5.0", "11.1.0", "11.2.0", "11.3.0", "11.3.1"),
)
def test_validate_version_accepts_supported_ranges(value, monkeypatch):
    monkeypatch.setattr(version, "warn", lambda _message: None)
    assert version.validate_version(value) == tuple(map(int, value.split(".")))


def test_validate_version_rejects_invalid_and_unsupported_values(monkeypatch):
    """Policy failures warn and return None; SystemExit would kill GDB."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)

    assert version.validate_version("10.3") is None
    assert version.validate_version("11.4.0") is None
    assert version.validate_version("12.0.0") is None

    assert "invalid FreeRTOS version" in warnings[0]
    assert (
        sum(1 for message in warnings if "unsupported FreeRTOS version" in message) == 2
    )


def _stub_cu_fallback(monkeypatch) -> None:
    """Keep CU-scoped fallback off unless a test is exercising it."""
    monkeypatch.setattr(version, "read_macro_text_in_source", lambda *_a, **_k: None)


def test_detect_version_from_macro_triplet(monkeypatch):
    """The tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD int macros win when present."""
    values = {
        "tskKERNEL_VERSION_MAJOR": 10,
        "tskKERNEL_VERSION_MINOR": 3,
        "tskKERNEL_VERSION_BUILD": 1,
    }
    monkeypatch.setattr(version, "read_macro_int", lambda name: values.get(name))
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_switches_to_kernel_source_context(monkeypatch):
    """A non-kernel CU is repaired by selecting the kernel source context."""
    values = {
        "tskKERNEL_VERSION_MAJOR": "11",
        "tskKERNEL_VERSION_MINOR": "1",
        "tskKERNEL_VERSION_BUILD": "0",
    }
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(
        version,
        "read_macro_text_in_source",
        lambda name, source: values[name] if source == "vTaskStartScheduler" else None,
    )
    monkeypatch.setattr(version, "read_macro_text", lambda _name: None)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == (11, 1, 0)


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
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: text)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() == expected


def test_detect_version_rejects_invalid_string(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: "unknown")
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() is None


def test_detect_version_from_gdr_symbol_decimal_fallback(monkeypatch):
    """gdr_freertos_version_num (decimal MMmmpp) is the last-resort escape hatch."""
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(
        version,
        "lookup_symbol",
        lambda name: 100301 if name == "gdr_freertos_version_num" else None,
    )

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_from_gdr_symbol_packed_hex_fallback(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(
        version,
        "lookup_symbol",
        lambda name: 0xA0301 if name == "gdr_freertos_version_num" else None,
    )

    assert version.detect_target_version() == (10, 3, 1)


def test_detect_version_returns_none_when_nothing_exported(monkeypatch):
    monkeypatch.setattr(version, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(version, "read_macro_text", lambda _name, **_kwargs: None)
    _stub_cu_fallback(monkeypatch)
    monkeypatch.setattr(version, "lookup_symbol", lambda _name: None)

    assert version.detect_target_version() is None


def test_check_target_rejects_a_target_mismatch(monkeypatch):
    """A mismatch refuses registration; the GDB session keeps running."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "detect_target_version", lambda: (10, 5, 0))
    monkeypatch.setattr(version, "symbol_exists", lambda _name: True)

    verdict = version.check_target("10.3.1")

    assert not verdict.accepted
    assert verdict.code == "version_mismatch"
    assert "version mismatch" in warnings[-1]


def test_check_target_refuses_a_target_without_any_marker(monkeypatch):
    """No FreeRTOS symbol means no adapter: every read would come back empty."""
    warnings: list[str] = []
    monkeypatch.setattr(version, "warn", warnings.append)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)

    verdict = version.check_target("10.3.1")

    assert not verdict.accepted
    assert verdict.code == "no_target_evidence"
    assert "--allow-unverified" in warnings[-1]


def test_allow_unverified_keeps_the_escape_hatch_open(monkeypatch):
    """A modified or stripped kernel must still be debuggable on request."""
    monkeypatch.setattr(version, "warn", lambda _message: None)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: False)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)

    verdict = version.check_target("10.3.1", allow_unverified=True)

    assert verdict.accepted
    assert verdict.version == (10, 3, 1)
    assert verdict.note is not None and "--allow-unverified" in verdict.note


def test_check_target_accepts_markers_without_an_exported_version(monkeypatch):
    """The user asserts the version here; that is the plugin's confirm flow."""
    monkeypatch.setattr(version, "warn", lambda _message: None)
    monkeypatch.setattr(version, "symbol_exists", lambda _name: True)
    monkeypatch.setattr(version, "detect_target_version", lambda: None)

    verdict = version.check_target("10.3.1")

    assert verdict.accepted
    assert verdict.note is not None and "unverified" in verdict.note
    assert verdict.markers == version.PROBE_MARKERS
