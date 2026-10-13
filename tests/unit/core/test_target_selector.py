"""Unified `<name-or-address>` selector semantics shared by adapters."""

from __future__ import annotations

from gdr.gdb_bridge import parse_user_address


def test_parse_user_address_accepts_hex_and_decimal():
    assert parse_user_address("0x20000100") == 0x20000100
    assert parse_user_address("0X20000100") == 0x20000100
    assert parse_user_address("  536874000  ") == 536874000


def test_parse_user_address_rejects_names_zero_and_garbage():
    assert parse_user_address("work_sem") is None
    assert parse_user_address("Tmr Svc") is None
    assert parse_user_address("") is None
    assert parse_user_address("   ") is None
    assert parse_user_address("0x0") is None
    assert parse_user_address("0") is None
    assert parse_user_address("0xZZ") is None
    assert parse_user_address("12px") is None
