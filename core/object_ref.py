"""RTOS-agnostic kernel object reference resolution (name, symbol, address).

Implements the three-tier reference contract shared by commands, watches,
convenience functions, and API verbs:

1. Exact object name (e.g. registry name, task name, timer name).
2. C symbol name (statically defined struct or dynamic pointer/handle).
3. Explicit target address (``0x``-hex or decimal).

Multiple distinct objects matching a name or symbol are reported as ambiguous
with the conflicting addresses so callers prompt for a more specific reference
instead of silently picking one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from .gdb_bridge import (
    is_plain_identifier,
    lookup_symbols,
    parse_user_address,
    safe_int,
    value_address,
)

T = TypeVar("T")

_MAX_LISTED_MATCHES = 6


@dataclass(frozen=True)
class ObjectCandidate(Generic[T]):
    """One live kernel object eligible for name, symbol, or address lookup."""

    address: int
    value: T
    name: str | None = None


@dataclass(frozen=True)
class ObjectResolution(Generic[T]):
    """Structured result of resolving one kernel object reference."""

    status: Literal["found", "not_found", "ambiguous"]
    value: T | None = None
    address: int | None = None
    matched_by: Literal["name", "symbol", "address", ""] = ""
    matches: tuple[tuple[int, str | None], ...] = ()
    message: str | None = None

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"


def resolve_or_legacy(
    resolve_fn: Callable[[], ObjectResolution[T]],
    legacy_fn: Callable[[], T | None],
    is_patched: bool,
) -> ObjectResolution[T]:
    """Use *resolve_fn*, honoring a test-patched *legacy_fn* when overridden."""
    if is_patched:
        val = legacy_fn()
        return (
            ObjectResolution("found", val)
            if val is not None
            else ObjectResolution("not_found")
        )
    return resolve_fn()


def symbol_addresses_for(
    name: str,
    *,
    address_of: Callable[[object], int | None] | None = None,
    single_lookup=None,
) -> list[int]:
    """Return every non-zero object address resolved from symbol *name*."""
    extract = address_of or symbol_value_address
    addrs: list[int] = []
    for value in lookup_symbols(name, single_lookup=single_lookup):
        address = extract(value)
        if address:
            addrs.append(address)
    return addrs


def format_ambiguity_message(
    kind: str,
    target: str,
    matched_by: Literal["name", "symbol"],
    matches: Sequence[tuple[int, str | None]],
) -> str:
    """Format a user-facing prompt to replace a conflicting name or symbol."""
    shown = [f"{addr:#x}" for addr, _label in matches[:_MAX_LISTED_MATCHES]]
    extra = len(matches) - len(shown)
    listing = ", ".join(shown) + (f", +{extra} more" if extra > 0 else "")
    guidance = (
        "specify a unique symbol or address"
        if matched_by == "name"
        else "specify an explicit address"
    )
    return (
        f"ambiguous {kind} {matched_by} {target!r} matches "
        f"{len(matches)} objects ({listing}); {guidance}"
    )


def symbol_value_address(
    value,
    *,
    safe_int_fn: Callable[[object], int | None] = safe_int,
    value_address_fn: Callable[[object], int] = value_address,
) -> int | None:
    """Return the object address referenced by a data symbol value, or ``None``.

    A pointer/handle symbol yields the non-zero address it points to (a NULL
    pointer returns ``None``, never the pointer variable's own address). A
    struct/union instance symbol yields its own address. Scalar non-pointer
    values return ``None`` when GDB type metadata is present.
    """
    if value is None:
        return None
    vtype = getattr(value, "type", None)
    if gdb is not None and vtype is not None:
        try:
            stripped = vtype.strip_typedefs()
            code = stripped.code
        except (gdb.error, AttributeError, TypeError, ValueError):
            code = None
        if code == gdb.TYPE_CODE_PTR:
            pointed = safe_int_fn(value)
            return pointed if pointed else None
        if code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            return value_address_fn(value) or None
        if code is not None:
            return None
    pointed = safe_int_fn(value)
    if pointed is not None:
        return pointed or None
    return value_address_fn(value) or None


def resolve_object_ref(
    kind: str,
    target: str,
    candidates: Sequence[ObjectCandidate[T]],
    *,
    symbol_addresses: Callable[[str], Sequence[int]] | None = None,
    synthesize_address: Callable[[int, str | None], T | None] | None = None,
    allow_numeric_symbol: bool = False,
) -> ObjectResolution[T]:
    """Resolve *target* against *candidates* by name, symbol, then address.

    Args:
        kind: Human-readable object kind used in ambiguity messages.
        target: User-supplied name, C symbol, ``0x`` hex, or decimal address.
        candidates: Live objects discovered for this kind.
        symbol_addresses: Callback returning non-zero target object addresses
            for a C identifier (covering static structs and dynamic handles).
        synthesize_address: Optional fallback building a value from an explicit
            or symbol-resolved address absent from *candidates*.
        allow_numeric_symbol: When true, all-digit tokens probe
            *symbol_addresses* before falling back to a decimal address.

    Returns:
        :class:`ObjectResolution` with ``status`` in ``"found"``,
        ``"not_found"``, or ``"ambiguous"``.
    """
    text = target.strip()
    if not text:
        return ObjectResolution(status="not_found")

    by_addr: dict[int, ObjectCandidate[T]] = {}
    unique: list[ObjectCandidate[T]] = []
    for candidate in candidates:
        if not candidate.address or candidate.address in by_addr:
            continue
        by_addr[candidate.address] = candidate
        unique.append(candidate)

    name_matches = [
        candidate for candidate in unique if candidate.name and candidate.name == text
    ]
    if len(name_matches) > 1:
        pairs = tuple((c.address, c.name) for c in name_matches)
        return ObjectResolution(
            status="ambiguous",
            matched_by="name",
            matches=pairs,
            message=format_ambiguity_message(kind, text, "name", pairs),
        )
    if len(name_matches) == 1:
        hit = name_matches[0]
        return ObjectResolution(
            status="found",
            value=hit.value,
            address=hit.address,
            matched_by="name",
        )

    if symbol_addresses is not None and (
        is_plain_identifier(text) or (allow_numeric_symbol and text.isdecimal())
    ):
        seen_addrs: set[int] = set()
        sym_matches: list[ObjectCandidate[T]] = []
        for addr in symbol_addresses(text):
            if not addr or addr in seen_addrs:
                continue
            seen_addrs.add(addr)
            existing = by_addr.get(addr)
            if existing is not None:
                sym_matches.append(existing)
            elif synthesize_address is not None:
                synthesized = synthesize_address(addr, text)
                if synthesized is not None:
                    sym_matches.append(
                        ObjectCandidate(address=addr, value=synthesized, name=text)
                    )
        if len(sym_matches) > 1:
            pairs = tuple((c.address, c.name) for c in sym_matches)
            return ObjectResolution(
                status="ambiguous",
                matched_by="symbol",
                matches=pairs,
                message=format_ambiguity_message(kind, text, "symbol", pairs),
            )
        if len(sym_matches) == 1:
            hit = sym_matches[0]
            return ObjectResolution(
                status="found",
                value=hit.value,
                address=hit.address,
                matched_by="symbol",
            )

    address = parse_user_address(text)
    if address is not None:
        existing = by_addr.get(address)
        if existing is not None:
            return ObjectResolution(
                status="found",
                value=existing.value,
                address=existing.address,
                matched_by="address",
            )
        if synthesize_address is not None:
            synthesized = synthesize_address(address, None)
            if synthesized is not None:
                return ObjectResolution(
                    status="found",
                    value=synthesized,
                    address=address,
                    matched_by="address",
                )

    return ObjectResolution(status="not_found")
