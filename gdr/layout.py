"""Generic kernel struct layout descriptions and accessors.

Defines dataclasses for describing kernel struct fields, linked-list hooks,
and object-container mappings in an RTOS-agnostic way. Concrete platform
adapters use these to build a ``KernelLayout`` at startup.

The dataclasses are pure Python and importable without GDB.  The accessor
functions (``read_field``, ``iter_list``, ``container_of``) require GDB and
are only called inside a GDB session.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from .constants import GDR_MAX_TRAVERSAL_COUNT
from .gdb_bridge import lookup_symbol, warn

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

# Field/access errors we degrade from.  Stored at module scope so the tuple
# stays evaluable when imported outside GDB (where ``gdb`` is ``None`` would
# otherwise turn a running handler into an AttributeError).  Unexpected
# exceptions bubble to a command/function guard for a full diagnostic.
if gdb is not None:
    _ACCESS_ERRORS = (
        gdb.error,
        gdb.MemoryError,
        IndexError,
        TypeError,
        ValueError,
    )
else:
    _ACCESS_ERRORS = (IndexError, TypeError, ValueError)


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
        kind: Hint for the adapter layer: ``"string"``, ``"ptr"``, ``"list"``,
            ``"enum"``, ``"flags"``, or ``""`` (default, treated as int).
        summary: ``True`` if this field should appear in the one-line
            pretty-printer fold.  Selecting 2-4 key fields per struct keeps
            ``p``/``bt full``/``info locals`` output readable.
        enum_map: Optional mapping from raw integer value to symbolic name,
            used by the pretty-printer when ``kind == "enum"``.  Renders
            ``stat=READY`` instead of ``stat=3``.  ``None`` falls back to
            the raw integer display.
        pointee_string_path: Optional path to a C string in the dereferenced
            value. This lets an adapter opt into useful pointer summaries
            without teaching the generic printer a target's struct layout.
        formatter: Optional callable that maps the field's raw integer value
            to a display string.  Adapters attach their own decoding functions
            here (for example an IPC policy decode) so the RTOS-neutral
            printer can reuse existing adapter logic instead of duplicating it
            as an ``enum_map``.
    """

    # TODO: StructField attributes simplify
    name: str
    path: tuple[str | int, ...]
    kind: str = ""
    summary: bool = False
    enum_map: dict[int, str] | None = None
    pointee_string_path: tuple[str | int, ...] | None = None
    formatter: Callable[[int | None], str] | None = None


@dataclass
class StructLayout:
    """Describes the layout of a kernel struct for GDB access.

    Attributes:
        struct_name: GDB type name (e.g. ``"struct kernel_thread"``).
        fields: Mapping of logical field name to ``StructField``.
        display_name: Optional short label for folded output. The target
            adapter supplies this label when the GDB type name is unsuitable.
    """

    struct_name: str
    fields: dict[str, StructField] = field(default_factory=dict)
    display_name: str | None = None


@dataclass
class ListHook:
    """Describes how to iterate an intrusive doubly-linked list.

    Attributes:
        head_symbol: Identifier of the list-head symbol. Empty when the
            caller already holds the head ``gdb.Value``.
        node_path: Path from the container struct to its embedded list node
            (used with ``container_of``).
        container_type: GDB type name to cast the container to.
        next_path: Path from a list node to its next node. This is layout
            metadata because intrusive-list implementations vary by target.
        head_index: Optional array index applied after looking up
            ``head_symbol`` (for example skip-list slot 0). ``None`` means
            the symbol itself is the head.
    """

    head_symbol: str
    node_path: tuple[str | int, ...]
    container_type: str
    next_path: tuple[str | int, ...]
    head_index: int | None = None


@dataclass
class ObjectTypeInfo:
    """Maps a kernel object type code to its struct and list member path.

    Attributes:
        type_code: Numeric object type (e.g. ``0x01`` for Thread).
        struct_name: GDB type name of the container struct.
        list_path: Path from the container to its object-registry list node.
        next_path: Path from that list node to its next node.
        enabled: Whether this object type is present in the current config.
        name: Adapter-supplied human-readable object type name.
    """

    type_code: int
    struct_name: str
    list_path: tuple[str | int, ...]
    next_path: tuple[str | int, ...]
    enabled: bool = True
    name: str = ""


@dataclass
class KernelLayout:
    """Complete layout description for a kernel.

    Attributes:
        structs: Mapping of struct name to ``StructLayout``.
        list_hooks: Mapping of hook name to ``ListHook``.
        object_types: Mapping of type code to ``ObjectTypeInfo``.
        object_codes: Adapter-supplied mapping from stable semantic object
            names to target-specific numeric type codes.
        stack_grows_up: Whether thread stacks grow toward higher addresses, or
            ``None`` when the target direction cannot be determined.
        cpu_count: Number of configured CPUs for SMP kernels, or ``None`` for
            UP targets or when the count cannot be probed. SMP capability
            fields (e.g. ``oncpu``/``bind_cpu``) are only meaningful relative
            to this bound.
    """

    structs: dict[str, StructLayout] = field(default_factory=dict)
    list_hooks: dict[str, ListHook] = field(default_factory=dict)
    object_types: dict[int, ObjectTypeInfo] = field(default_factory=dict)
    object_codes: dict[str, int] = field(default_factory=dict)
    stack_grows_up: bool | None = None
    cpu_count: int | None = None


# ---------------------------------------------------------------------------
# Accessors (require GDB)
# ---------------------------------------------------------------------------


def member_offset(type_name: str, path: tuple[str | int, ...]) -> int | None:
    """Calculate the byte offset of a (possibly nested) struct member.

    Args:
        type_name: GDB type name.
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

    Mirrors the common C ``container_of(ptr, type, member)`` idiom.

    Args:
        node_ptr: ``gdb.Value`` pointing to the embedded list node.
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


def read_path(value: gdb.Value, path: tuple[str | int, ...]) -> gdb.Value | None:
    """Read a nested field path from a ``gdb.Value`` safely."""
    try:
        current = value
        for part in path:
            current = current[part]
        return current
    except _ACCESS_ERRORS:
        return None


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
    return read_path(value, f.path)


def resolve_list_head(hook: ListHook):
    """Return the list-head ``gdb.Value`` for *hook*, or ``None``.

    Looks up ``head_symbol`` as a DWARF/ELF identifier and applies
    ``head_index`` with ``gdb.Value`` indexing. Never evaluates a GDB
    expression, so the inferior stays halted.
    """
    if not hook.head_symbol:
        return None
    value = lookup_symbol(hook.head_symbol)
    if value is None:
        return None
    if hook.head_index is None:
        return value
    try:
        return value[hook.head_index]
    except _ACCESS_ERRORS:
        return None


def iter_list(
    head_value: gdb.Value,
    hook: ListHook,
    max_count: int = GDR_MAX_TRAVERSAL_COUNT,
) -> Iterator[gdb.Value]:
    """Iterate a doubly-linked list, yielding container ``gdb.Value`` objects.

    Args:
        head_value: ``gdb.Value`` of the list head.
        hook: ``ListHook`` describing the container type and node path.
        max_count: Safety limit to prevent infinite loops on corrupted lists.
            Emits a warning if traversal reaches the limit before the list head.

    Yields:
        Dereferenced ``gdb.Value`` of each container struct.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    try:
        head_int = int(head_value.address)
        node = read_path(head_value, hook.next_path)
        count = 0
        seen_addrs: set[int] = set()
        while node is not None:
            node_int = int(node)
            if node_int == head_int:
                return
            if node_int in seen_addrs:
                warn(
                    f"list traversal stopped at repeated node {node_int:#x} "
                    "(corrupted cycle)"
                )
                return
            if count >= max_count:
                warn(f"list traversal truncated after {max_count} nodes")
                return
            seen_addrs.add(node_int)
            yield container_of(node, hook.container_type, hook.node_path)
            node = read_path(node, hook.next_path)
            count += 1
    except _ACCESS_ERRORS:
        return
