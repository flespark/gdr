"""RT-Thread `<name-or-address>` resolution: name wins, then address/symbol."""

from __future__ import annotations

import rtthread.navigation as navigation
from gdr.layout import KernelLayout, ObjectTypeInfo, StructLayout


def _registry_layout() -> KernelLayout:
    return KernelLayout(
        structs={"struct rt_semaphore": StructLayout("struct rt_semaphore")},
        object_types={
            1: ObjectTypeInfo(
                type_code=1,
                struct_name="struct rt_semaphore",
                list_path=("parent", "list"),
                next_path=("next",),
                name="semaphore",
            )
        },
        object_codes={"semaphore": 1, "thread": 1},
    )


class _Obj:
    def __init__(self, address: int):
        self._address = address


def _install_registry(monkeypatch, names: dict[int, str]):
    objects = [_Obj(address) for address in names]

    def fake_iter_objects(_type_code, _kl):
        return iter(objects)

    def fake_read_field(value, _layout, field_name):
        assert field_name == "name"
        return names[value._address]

    def fake_read_cstring(value):
        return value

    monkeypatch.setattr(navigation, "iter_objects", fake_iter_objects)
    monkeypatch.setattr(navigation, "read_field", fake_read_field)
    monkeypatch.setattr(navigation, "read_cstring", fake_read_cstring)
    monkeypatch.setattr(navigation, "value_address", lambda value: value._address)
    return objects


def test_find_object_prefers_registry_name(monkeypatch):
    layout = _registry_layout()
    _install_registry(monkeypatch, {0x1000: "work_sem", 0x2000: "other"})
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: None)

    found = navigation.find_object(1, "work_sem", layout)

    assert found is not None and found._address == 0x1000


def test_find_object_matches_explicit_hex_address(monkeypatch):
    layout = _registry_layout()
    _install_registry(monkeypatch, {0x1000: "work_sem", 0x2000: "other"})
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: None)

    found = navigation.find_object(1, "0x2000", layout)

    assert found is not None and found._address == 0x2000


def test_find_object_matches_symbol_address(monkeypatch):
    layout = _registry_layout()
    objects = _install_registry(monkeypatch, {0x1000: "work_sem"})

    class _Sym:
        pass

    monkeypatch.setattr(
        navigation,
        "lookup_symbol",
        lambda name: _Sym() if name == "work_sem_sym" else None,
    )
    monkeypatch.setattr(navigation, "safe_int", lambda _value: None)
    monkeypatch.setattr(
        navigation,
        "value_address",
        lambda value: 0x1000 if isinstance(value, _Sym) else value._address,
    )

    found = navigation.find_object(1, "work_sem_sym", layout)

    assert found is objects[0]


def test_find_object_rejects_unknown_name_and_null_address(monkeypatch):
    layout = _registry_layout()
    _install_registry(monkeypatch, {0x1000: "work_sem"})
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: None)

    assert navigation.find_object(1, "missing", layout) is None
    assert navigation.find_object(1, "0x0", layout) is None
    assert navigation.find_object(1, "   ", layout) is None


def test_find_timer_by_target_matches_name_then_address(monkeypatch):
    layout = KernelLayout(structs={"struct rt_timer": StructLayout("struct rt_timer")})
    first, second = _Obj(0x1000), _Obj(0x2000)

    monkeypatch.setattr(navigation, "iter_timers", lambda _kl: iter([first, second]))
    monkeypatch.setattr(navigation, "read_field", lambda value, _l, _f: value)
    names = {id(first): "heartbeat", id(second): "other"}
    monkeypatch.setattr(
        navigation,
        "read_cstring",
        lambda value: names.get(id(value)),
    )
    monkeypatch.setattr(navigation, "value_address", lambda value: value._address)
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: None)

    assert navigation.find_timer_by_target("heartbeat", layout) is first
    assert navigation.find_timer_by_target("0x2000", layout) is second
    assert navigation.find_timer_by_target("missing", layout) is None
