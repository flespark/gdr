"""Tests for shared version parsing and target-number decoding."""

from gdr.version import (
    decode_version,
    format_version,
    parse_version,
    version_in_ranges,
)


def test_shared_version_parser_and_formatter():
    value = parse_version("4.0.5")
    assert value == (4, 0, 5)
    assert format_version(value) == "4.0.5"
    assert parse_version("4.0") is None


def test_decode_version_respects_declared_encoding_and_ranges():
    ranges = (((4, 0, 0), (4, 1, 1)),)
    assert decode_version(40005, ("decimal",), ranges) == (4, 0, 5)
    assert decode_version(0x40005, ("packed-hex",), ranges) == (4, 0, 5)
    assert decode_version(40005, ("packed-hex",), ranges) is None
    assert version_in_ranges((4, 1, 1), ranges)
    assert not version_in_ranges((4, 1, 2), ranges)
