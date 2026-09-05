"""Unit tests for generic intrusive-list traversal."""

from __future__ import annotations

import gdr.layout as layout


class _FakeGdb:
    """GDB exceptions used by ``iter_list``."""

    class error(Exception):
        pass

    class MemoryError(Exception):
        pass


class _FakeNode:
    """Minimal addressable list node."""

    def __init__(self, address: int):
        self.address = address
        self.next: _FakeNode | None = None

    def __int__(self) -> int:
        return self.address


_HOOK = layout.ListHook(
    head_symbol="head",
    node_path=("node",),
    container_type="struct item",
    next_path=("next",),
)


def _configure_iter_list(monkeypatch) -> list[str]:
    """Replace GDB accessors with linked fake nodes."""
    warnings: list[str] = []
    monkeypatch.setattr(layout, "gdb", _FakeGdb)
    monkeypatch.setattr(layout, "read_path", lambda value, _path: value.next)
    monkeypatch.setattr(
        layout,
        "container_of",
        lambda node, _container_type, _member_path: node,
    )
    monkeypatch.setattr(layout, "warn", warnings.append)
    return warnings


def test_iter_list_terminates_at_the_list_head(monkeypatch):
    """A well-formed sentinel list completes without a corruption warning."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    first = _FakeNode(0x200)
    second = _FakeNode(0x300)
    head.next = first
    first.next = second
    second.next = head

    assert list(layout.iter_list(head, _HOOK)) == [first, second]
    assert warnings == []


def test_iter_list_warns_and_stops_at_a_corrupted_cycle(monkeypatch):
    """A cycle that excludes the list head must not yield duplicate nodes."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    first = _FakeNode(0x200)
    second = _FakeNode(0x300)
    head.next = first
    first.next = second
    second.next = first

    assert list(layout.iter_list(head, _HOOK)) == [first, second]
    assert len(warnings) == 1
    assert "repeated node" in warnings[0]
    assert "corrupted cycle" in warnings[0]


def test_iter_list_warns_when_the_safety_limit_truncates_a_list(monkeypatch):
    """A nonterminated list after the configured limit is reported."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    first = _FakeNode(0x200)
    second = _FakeNode(0x300)
    head.next = first
    first.next = second
    second.next = head

    assert list(layout.iter_list(head, _HOOK, max_count=1)) == [first]
    assert warnings == ["list traversal truncated after 1 nodes"]


def test_iter_list_does_not_warn_at_an_exact_safety_limit_boundary(monkeypatch):
    """Reaching the head at the limit is normal completion, not truncation."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    node = _FakeNode(0x200)
    head.next = node
    node.next = head

    assert list(layout.iter_list(head, _HOOK, max_count=1)) == [node]
    assert warnings == []


def test_resolve_list_head_indexes_an_array_symbol(monkeypatch):
    """Timer skip-list heads are DWARF arrays indexed in Python, not expressions."""

    class _Array:
        def __getitem__(self, index: int) -> str:
            return f"slot-{index}"

    monkeypatch.setattr(
        layout,
        "lookup_symbol",
        lambda name: _Array() if name == "_timer_list" else None,
    )
    hook = layout.ListHook(
        head_symbol="_timer_list",
        node_path=("row", 0),
        container_type="struct rt_timer",
        next_path=("next",),
        head_index=0,
    )

    assert layout.resolve_list_head(hook) == "slot-0"


def test_resolve_list_head_rejects_an_empty_symbol():
    """Inline wait-list walks already hold the head value and skip lookup."""
    hook = layout.ListHook(
        head_symbol="",
        node_path=("tlist",),
        container_type="struct rt_thread",
        next_path=("next",),
    )

    assert layout.resolve_list_head(hook) is None


# ---------------------------------------------------------------------------
# value_at / read_field_at
# ---------------------------------------------------------------------------


class _TypeLookupGdb(_FakeGdb):
    """GDB stand-in modelling ``Type.pointer()`` + ``Value.cast().dereference()``."""

    def __init__(self):
        super().__init__()
        self._last_address: int | None = None

    def lookup_type(self, name: str):
        if name == "struct item":
            return _StructType()
        raise TypeError(f"no type {name}")

    def Value(self, address: int):
        self._last_address = int(address)
        return _CastableValue(address)


class _StructType:
    """Struct type whose ``.pointer()`` yields a pointer type that can cast."""

    def pointer(self):
        return _PointerType()


class _PointerType:
    """Pointer type whose ``cast()`` result dereferences to a sentinel."""

    @staticmethod
    def cast_target():
        return _DerefTarget()


class _CastableValue:
    """``gdb.Value`` stand-in that casts by delegating to the pointer type."""

    def __init__(self, address: int):
        self.address = address

    def __int__(self) -> int:
        return self.address

    def cast(self, ptr_type):
        return ptr_type.cast_target()


class _DerefTarget:
    """Cast result: dereferencing yields a non-null sentinel value."""

    def dereference(self):
        return self


def test_value_at_casts_and_dereferences_through_the_layout_type(monkeypatch):
    sl = layout.StructLayout("struct item")
    fake = _TypeLookupGdb()
    monkeypatch.setattr(layout, "gdb", fake)
    assert layout.value_at(0x1000, sl) is not None
    assert fake._last_address == 0x1000


def test_value_at_degrades_on_zero_or_missing_type(monkeypatch):
    sl = layout.StructLayout("struct item")

    class _MissingTypeGdb(_FakeGdb):
        def lookup_type(self, name: str):
            raise TypeError(f"no type {name}")

    monkeypatch.setattr(layout, "gdb", _MissingTypeGdb())
    assert layout.value_at(0, sl) is None  # zero address short-circuits
    assert layout.value_at(0x1000, sl) is None  # missing type degrades


def _sl_with_field(name: str, path) -> layout.StructLayout:
    return layout.StructLayout(
        "struct item", fields={name: layout.StructField(name, path)}
    )


class _OffsetGdb(_FakeGdb):
    """GDB stand-in computing nested member offsets from DWARF fields."""

    def __init__(self):
        super().__init__()

    def lookup_type(self, name: str):
        if name == "struct item":
            return _FieldType({"magic": (0, "unsigned long"), "tail": (8, "char")})
        raise _FakeGdb.error("no type")


class _FieldType:
    def __init__(self, members: dict[str, tuple[int, str]]):
        self._members = members

    def fields(self):
        out = []
        for name, (bitpos, _tname) in self._members.items():
            out.append(_Member(name, bitpos, _PlainType(_tname)))
        return out


class _Member:
    def __init__(self, name: str, bitpos: int, type_):
        self.name = name
        self.bitpos = bitpos
        self.type = type_


class _PlainType:
    def __init__(self, name: str):
        self.name = name


def test_read_field_at_reads_a_dwarf_member_at_its_offset(monkeypatch):
    sl = _sl_with_field("magic", ("magic",))
    monkeypatch.setattr(layout, "gdb", _OffsetGdb())
    monkeypatch.setattr(
        layout, "read_bytes", lambda _addr, _size: bytes([0x78, 0x56])[:2]
    )
    assert (
        layout.read_field_at(0x1000, "struct item", sl, "magic", 2, "little") == 0x5678
    )


def test_read_field_at_degrades_for_missing_field_type_or_memory(monkeypatch):
    monkeypatch.setattr(layout, "gdb", _OffsetGdb())
    sl = _sl_with_field("magic", ("magic",))

    monkeypatch.setattr(layout, "read_bytes", lambda _addr, _size: None)
    assert layout.read_field_at(0x1000, "struct item", sl, "magic", 2, "little") is None
    # Unknown field: no StructField -> None before any memory read.
    monkeypatch.setattr(layout, "read_bytes", lambda _addr, _size: b"\x00\x00")
    assert (
        layout.read_field_at(0x1000, "struct item", sl, "absent", 2, "little") is None
    )
    # Unknown type: member_offset returns None.
    assert (
        layout.read_field_at(0x1000, "struct other", sl, "magic", 2, "little") is None
    )
