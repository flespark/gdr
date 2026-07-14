"""Generic kernel struct layout descriptions and accessors.

Defines dataclasses for describing kernel struct fields, linked-list hooks,
and object-container mappings in a RTOS-agnostic way.  Concrete RTOS support
packages (e.g. ``rtthread``) use these to build a ``KernelLayout`` at startup.

The dataclasses are pure Python and importable without GDB.  The accessor
functions (``read_field``, ``iter_list``, ``container_of``) require GDB and
are only called inside a GDB session.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Dataclasses (pure Python, no GDB needed)
# ---------------------------------------------------------------------------


@dataclass
class StructField:
    """A logical field within a kernel struct, with its GDB access path.

    Attributes:
        name: Logical field name (e.g. ``"name"``, ``"stat"``).
        path: Tuple of field names and/or array indices that GDB traverses
            to reach the value.  E.g. ``("parent", "name")`` for a field
            inherited via an embedded ``parent`` struct, or ``("row", 0)``
            for the first element of an array member.
        optional: ``True`` if the field may not exist (config-conditional).
        kind: Hint for the adapter layer: ``"string"``, ``"ptr"``, ``"list"``,
            ``"enum"``, ``"flags"``, or ``""`` (default, treated as int).
        summary: ``True`` if this field should appear in the one-line
            pretty-printer fold.  Selecting 2-4 key fields per struct keeps
            ``p``/``bt full``/``info locals`` output readable.
    """

    name: str
    path: tuple[str | int, ...]
    optional: bool = False
    kind: str = ""
    summary: bool = False


@dataclass
class StructLayout:
    """Describes the layout of a kernel struct for GDB access.

    Attributes:
        struct_name: GDB type name (e.g. ``"struct rt_thread"``).
        fields: Mapping of logical field name to ``StructField``.
    """

    struct_name: str
    fields: dict[str, StructField] = field(default_factory=dict)

    def add(self, f_name: str, path: tuple[str | int, ...], **kw) -> None:
        """Add a field with the given logical name and access path."""
        self.fields[f_name] = StructField(f_name, path, **kw)


@dataclass
class ListHook:
    """Describes how to iterate a doubly-linked list (``rt_list_t`` style).

    Attributes:
        head_expr: GDB expression evaluating to the list head ``rt_list_t``.
        node_path: Path from the container struct to its ``rt_list_t`` member
            (used with ``container_of``).
        container_type: GDB type name to cast the container to.
    """

    head_expr: str
    node_path: tuple[str | int, ...]
    container_type: str


@dataclass
class ObjectTypeInfo:
    """Maps a kernel object type code to its struct and list member path.

    Attributes:
        type_code: Numeric object type (e.g. ``0x01`` for Thread).
        struct_name: GDB type name of the container struct.
        list_path: Path from the container to its object-list ``rt_list_t``.
        enabled: Whether this object type is present in the current config.
    """

    type_code: int
    struct_name: str
    list_path: tuple[str | int, ...]
    enabled: bool = True


@dataclass
class KernelLayout:
    """Complete layout description for a kernel.

    Attributes:
        structs: Mapping of struct name to ``StructLayout``.
        list_hooks: Mapping of hook name to ``ListHook``.
        object_types: Mapping of type code to ``ObjectTypeInfo``.
    """

    structs: dict[str, StructLayout] = field(default_factory=dict)
    list_hooks: dict[str, ListHook] = field(default_factory=dict)
    object_types: dict[int, ObjectTypeInfo] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Accessors (require GDB)
# ---------------------------------------------------------------------------


def member_offset(type_name: str, path: tuple[str | int, ...]) -> int | None:
    """Calculate the byte offset of a (possibly nested) struct member.

    Args:
        type_name: GDB type name (e.g. ``"struct rt_thread"``).
        path: Member access path; string elements are field names,
            int elements are array indices.

    Returns:
        Byte offset, or ``None`` if the type or path is not found.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    try:
        t = gdb.lookup_type(type_name)
    except gdb.error:
        return None

    offset_bits = 0
    for part in path:
        if isinstance(part, int):
            if t.code != gdb.TYPE_CODE_ARRAY:
                return None
            offset_bits += part * t.target().sizeof * 8
            t = t.target()
        else:
            found = False
            for f in t.fields():
                if f.name == part:
                    offset_bits += f.bitpos
                    t = f.type
                    found = True
                    break
            if not found:
                return None
    return offset_bits // 8


def container_of(
    node_ptr: gdb.Value, container_type: str, member_path: tuple[str | int, ...]
) -> gdb.Value:
    """Recover the containing struct from an embedded list-node pointer.

    Mirrors the C macro ``rt_container_of(ptr, type, member)``.

    Args:
        node_ptr: ``gdb.Value`` pointing to the ``rt_list_t`` node.
        container_type: GDB type name of the containing struct.
        member_path: Path from the container to the list-node member.

    Returns:
        A dereferenced ``gdb.Value`` of the containing struct.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    offset = member_offset(container_type, member_path)
    if offset is None:
        raise ValueError(f"cannot compute offset of {member_path} in {container_type}")
    addr = int(node_ptr) - offset
    ptr_type = gdb.lookup_type(container_type).pointer()
    return gdb.Value(addr).cast(ptr_type).dereference()


def read_field(
    value: gdb.Value, layout: StructLayout, field_name: str
) -> gdb.Value | None:
    """Read a field from a ``gdb.Value`` using a ``StructLayout`` description.

    Returns ``None`` if the field is not in the layout, not present in the
    value (config-conditional), or if GDB raises an error during access.
    """
    f = layout.fields.get(field_name)
    if f is None:
        return None
    try:
        current = value
        for part in f.path:
            current = current[part]
        return current
    except (gdb.error, gdb.MemoryError, IndexError):
        return None


def iter_list(
    head_value: gdb.Value, hook: ListHook, max_count: int = 4096
) -> Iterator[gdb.Value]:
    """Iterate a doubly-linked list, yielding container ``gdb.Value`` objects.

    Args:
        head_value: ``gdb.Value`` of the list head ``rt_list_t``.
        hook: ``ListHook`` describing the container type and node path.
        max_count: Safety limit to prevent infinite loops on corrupted lists.

    Yields:
        Dereferenced ``gdb.Value`` of each container struct.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    try:
        head_int = int(head_value.address)
        node = head_value["next"]
        count = 0
        while int(node) != head_int and count < max_count:
            yield container_of(node, hook.container_type, hook.node_path)
            node = node["next"]
            count += 1
    except (gdb.error, gdb.MemoryError):
        return
