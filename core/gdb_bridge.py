"""GDB Python API wrappers.

Centralises all direct ``gdb.*`` calls so the rest of the codebase can be
written against stable, typed helpers with uniform error handling.

The ``gdb`` import is guarded so the module is importable outside GDB for
static analysis and unit-testing of non-GDB logic.  Calling any function
that touches ``gdb.*`` outside a GDB session raises ``RuntimeError``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import platform
import re
import shutil
import struct
import sys
import traceback as _traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Literal

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from core.constants import (
    GDR_DEBUG,
    GDR_DEFAULT_TERMINAL_WIDTH,
    GDR_MAX_CSTRING_LENGTH,
    GDR_PROPAGATE_EXCEPTION,
)
from core.elf import (
    macro_definitions_from_elf as macro_definitions_from_elf,
)
from core.elf import (
    parse_integer_literal as parse_integer_literal,
)
from core.elf import (
    scan_definitions as scan_definitions,
)
from core.elf import (
    section_payloads as section_payloads,
)
from core.formatting import ElasticMode, SourceLoc, format_table
from core.source_paths import (
    ElfSourceInfo,
    SourcePathResolver,
    collect_local_source_files,
    infer_elf_prefix_mappings,
    inspect_elf_sources,
    is_abs_path,
    normalize_path,
    relative_under_root,
)

try:
    _STARTUP_CWD = os.getcwd()
except OSError:
    _STARTUP_CWD = ""


@dataclass(frozen=True)
class ArchInfo:
    """Target architecture properties needed for raw memory decoding.

    Attributes:
        ptrsize: Pointer width in target bytes.
        endian: Target byte order, either ``"little"`` or ``"big"``.
    """

    ptrsize: int
    endian: str


def _ensure_gdb() -> None:
    """Raise RuntimeError if not running inside GDB."""
    if gdb is None:
        raise RuntimeError("not running inside GDB")


# The GDB CLI verbs GDR issues through the single execution helper below.
# NOTE: every command is internally constructed from one of these verbs
# plus identifiers, addresses or source paths GDB itself reported, so an
# allowlist keeps a malformed name from becoming an arbitrary command.
_ALLOWED_GDB_COMMANDS = frozenset({"info", "list", "set", "show"})


def _gdb_execute(command: str) -> str:
    """Run one GDB console command and return its captured output.

    All ``gdb.execute`` calls in the project funnel through this single
    name so command execution stays in the bridge (adapters never touch
    ``gdb.execute`` directly) and error handling is uniform: a failed
    command returns ``""`` and lets the caller decide, exactly like the
    old per-site ``except gdb.error: return None`` shapes.
    """
    if gdb is None:
        return ""
    verb = command.strip().split(" ", 1)[0]
    if verb not in _ALLOWED_GDB_COMMANDS:
        err(f"refusing a GDB command outside the allowlist: {verb!r}")
        return ""
    try:
        # NOTE: GDR's single GDB CLI choke point.  Every command is built from
        # an allowlist of verbs plus identifiers, addresses and source paths GDB
        # itself reported, so no query language is involved; the bare marker also
        # covers analysers that report this call under a different rule id.
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query
        return gdb.execute(command, to_string=True)  # nosemgrep
    except gdb.error:
        return ""


# Expected target/kernel-data access errors shared by every adapter.
# Probing kernel structures degrades on these and returns a safe default
# (None/0/[]); anything outside the set bubbles to a command guard for a
# full diagnostic.  Kept in exactly one place (with the gdb-None fallback
# spelled once) so adapters never re-declare near-identical tuples.
if gdb is not None:
    TARGET_ACCESS_ERRORS: tuple[type[BaseException], ...] = (
        gdb.error,
        gdb.MemoryError,
        IndexError,
        TypeError,
        ValueError,
        AttributeError,
    )
else:
    TARGET_ACCESS_ERRORS = (IndexError, TypeError, ValueError, AttributeError)


def safe_int(value) -> int | None:
    """Convert a GDB-like scalar to int, returning ``None`` on failure."""
    try:
        return int(value)
    except Exception:
        return None


def value_address(value) -> int:
    """Return a GDB-like value's address, or zero when it is unavailable."""
    try:
        return safe_int(value.address) or 0
    except Exception:
        return 0


def safe_dereference(pointer):
    """Dereference a non-null GDB-like pointer without propagating errors."""
    try:
        return pointer.dereference() if safe_int(pointer) else None
    except Exception:
        return None


def get_arch_info() -> ArchInfo | None:
    """Return a fresh pointer-width and byte-order snapshot for the target.

    Returns ``None`` when GDB cannot resolve either property. The result is
    intentionally not cached because reconnecting or changing GDB's target
    architecture or endianness can invalidate it.
    """
    _ensure_gdb()
    try:
        # type: ignore[attr-defined] - Architecture.void_type() exists at runtime
        # but is missing from the bundled gdb type stub; the AttributeError
        # branch below already covers GDB builds that lack it.
        ptrsize = gdb.selected_inferior().architecture().void_type().pointer().sizeof  # type: ignore[attr-defined]
    except (AttributeError, TypeError, gdb.error):
        try:
            ptrsize = gdb.lookup_type("void").pointer().sizeof
        except (AttributeError, TypeError, gdb.error):
            return None

    if not isinstance(ptrsize, int) or ptrsize <= 0:
        return None

    try:
        endian_output = _gdb_execute("show endian").lower()
    except gdb.error:
        return None

    is_little_endian = "little endian" in endian_output
    is_big_endian = "big endian" in endian_output
    if is_little_endian == is_big_endian:
        return None
    endian = "little" if is_little_endian else "big"
    return ArchInfo(ptrsize=ptrsize, endian=endian)


def arch_or_default() -> tuple[int, Literal["little", "big"]]:
    """Return ``(pointer width in bytes, target byte order)`` for raw reads.

    Degrades to ``(4, "little")`` when the architecture is unknown or the
    pointer width is not 4/8, so raw-memory decoders outside a GDB session or
    against an exotic target never crash on a ``None``.
    """
    info = get_arch_info()
    if info is None or info.ptrsize not in (4, 8):
        return (4, "little")
    endian: Literal["little", "big"] = "little" if info.endian == "little" else "big"
    return (info.ptrsize, endian)


def read_uint_at(address: int, size: int) -> int | None:
    """Read one raw unsigned integer at ``address + size``, or ``None``.

    The target byte order is applied to the raw bytes read by
    :func:`read_bytes`, so callers never re-implement endianness handling.
    """
    _ptrsize, endian = arch_or_default()
    raw = read_bytes(address, size)
    if raw is None:
        return None
    return int.from_bytes(raw, byteorder=endian)


def is_debug() -> bool:
    """Whether verbose diagnostics are enabled.

    Reflects :data:`core.constants.GDR_DEBUG`.  When true, :func:`err`
    formatting and the guards use the full :func:`show_last_exception`
    diagnostic instead of a one-line message.
    """
    return GDR_DEBUG


def propagate_exception() -> bool:
    """Whether a debug-mode diagnostic should re-raise after printing.

    Reflects :data:`core.constants.GDR_PROPAGATE_EXCEPTION`.  Only meaningful
    together with :func:`is_debug`; lets a reported bug be re-raised for GDB
    to surface.
    """
    return GDR_PROPAGATE_EXCEPTION


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYMBOL_LOOKUP_ERRORS = (TypeError, ValueError, RuntimeError, AttributeError)


def is_plain_identifier(name: str) -> bool:
    """Return whether *name* is a single C identifier with no call or index."""
    return bool(_IDENTIFIER.fullmatch(name))


def parse_user_address(text: str) -> int | None:
    """Parse an explicit ``0x``-hex or decimal address literal.

    Shared by the RTOS adapters so ``<name-or-address>`` means the same
    everywhere: a name wins over a decimal reading (a numeric object name
    is resolved as a symbol first), digits fall back to an address only
    when the symbol lookup fails. Returns ``None`` for anything that is
    not an address literal, including zero (a null selector resolves to
    nothing, never to the null object).
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        if cleaned.lower().startswith("0x"):
            address = int(cleaned, 16)
        elif cleaned.isdecimal():
            address = int(cleaned, 10)
        else:
            return None
    except ValueError:
        return None
    return address or None


_SYMBOL_AT_CACHE_LIMIT = 4096
_symbol_cache_owner: tuple[int, int] | None = None
_symbol_cache: dict[str, Any] = {}
_symbols_cache: dict[str, tuple[Any, ...]] = {}
_symbol_at_cache: dict[int, str | None] = {}


def reset_symbol_caches() -> None:
    """Clear session-scoped GDB symbol descriptor and address caches."""
    global _symbol_cache_owner
    _symbol_cache_owner = None
    _symbol_cache.clear()
    _symbols_cache.clear()
    _symbol_at_cache.clear()


def _sync_symbol_cache_owner() -> None:
    global _symbol_cache_owner
    owner = (id(gdb), id(_gdb_execute))
    if _symbol_cache_owner != owner:
        _symbol_cache.clear()
        _symbols_cache.clear()
        _symbol_at_cache.clear()
        _symbol_cache_owner = owner


def _lookup_gdb_symbol(name: str):
    """Return a ``gdb.Symbol`` for *name*, or ``None``.

    Searches the selected block, then globals, then file-static symbols.
    Global/static descriptors (and misses) are cached per session; frame-local
    symbols (``needs_frame``) bypass the cache so another stop's frame never
    reuses them. Does not evaluate expressions or call inferior functions.
    """
    _sync_symbol_cache_owner()
    if name in _symbol_cache:
        return _symbol_cache[name]
    symbol = None
    try:
        found, _is_field = gdb.lookup_symbol(name)
        symbol = found
    except (gdb.error, *_SYMBOL_LOOKUP_ERRORS):
        symbol = None
    if symbol is not None and getattr(symbol, "needs_frame", False):
        return symbol
    if symbol is None:
        try:
            symbol = gdb.lookup_global_symbol(name)
        except (gdb.error, *_SYMBOL_LOOKUP_ERRORS):
            symbol = None
    if symbol is None:
        try:
            symbol = gdb.lookup_static_symbol(name)
        except (gdb.error, *_SYMBOL_LOOKUP_ERRORS):
            symbol = None
    _symbol_cache[name] = symbol
    return symbol


def _read_symbol_value(symbol) -> gdb.Value | None:
    try:
        return symbol.value()
    except (gdb.error, *_SYMBOL_LOOKUP_ERRORS):
        return None


def lookup_symbol(name: str) -> gdb.Value | None:
    """Look up a global or file-static data symbol by identifier."""
    if not is_plain_identifier(name):
        return None
    _ensure_gdb()
    symbol = _lookup_gdb_symbol(name)
    return _read_symbol_value(symbol) if symbol is not None else None


def lookup_symbols(name: str, *, single_lookup=None) -> list[gdb.Value]:
    """Return every global and file-static data symbol matching *name*.

    Global and file-static ``gdb.Symbol`` descriptors are cached per session;
    each call still reads ``symbol.value()`` live so dynamic pointers stay
    current across target stops.
    """
    if single_lookup is not None and single_lookup is not lookup_symbol:
        value = single_lookup(name)
        return [value] if value is not None else []
    if not is_plain_identifier(name):
        return []
    _ensure_gdb()
    _sync_symbol_cache_owner()
    cached = _symbols_cache.get(name)
    if cached is None:
        found_syms: list[Any] = []
        try:
            global_sym = gdb.lookup_global_symbol(name)
        except (gdb.error, *_SYMBOL_LOOKUP_ERRORS):
            global_sym = None
        if global_sym is not None:
            found_syms.append(global_sym)
        static_all = getattr(gdb, "lookup_static_symbols", None)
        if callable(static_all):
            try:
                found = static_all(name)
            except (gdb.error, *_SYMBOL_LOOKUP_ERRORS):
                found = None
            if isinstance(found, Iterable):
                found_syms.extend(found)
        cached = tuple(found_syms)
        _symbols_cache[name] = cached
    values = [v for sym in cached if (v := _read_symbol_value(sym)) is not None]
    if not values:
        fallback = _lookup_gdb_symbol(name)
        val = _read_symbol_value(fallback) if fallback is not None else None
        if val is not None:
            values.append(val)
    return values


def lookup_symbol_at(addr: int) -> str | None:
    """Look up the symbol and offset at a target address.

    Returns a display-ready ``"symbol+offset"`` string without enclosing
    brackets, or ``None`` when no symbol covers ``addr``. Results are cached
    per session (bounded) because symbol addresses are static for a loaded
    objfile; transient GDB errors are never cached.
    """
    _ensure_gdb()
    _sync_symbol_cache_owner()
    if addr in _symbol_at_cache:
        return _symbol_at_cache[addr]
    try:
        symbol = _gdb_execute(f"info symbol {addr:#x}").strip()
    except gdb.error:
        return None
    if not symbol:
        return None
    if symbol.startswith("No symbol matches"):
        result: str | None = None
    else:
        cleaned = symbol.partition(" in section ")[0]
        result = cleaned.replace(" + ", "+").replace(" - ", "-")
    if len(_symbol_at_cache) >= _SYMBOL_AT_CACHE_LIMIT:
        _symbol_at_cache.pop(next(iter(_symbol_at_cache)))
    _symbol_at_cache[addr] = result
    return result


def source_location_at(addr: int) -> SourceLoc | None:
    """Return the source line covering ``addr`` as a :class:`SourceLoc`.

    Reads only the debug line table through ``gdb.find_pc_line``, never the
    target memory, so it is safe on a halted target. ``None`` when the address
    carries no line info. An ARM Thumb function pointer has bit 0 set, but the
    line table is keyed on the even instruction address, so that bit is
    cleared first.
    """
    _ensure_gdb()
    try:
        sal = gdb.find_pc_line(addr & ~1)
    except gdb.error:
        return None
    if sal.symtab is None or sal.line <= 0:
        return None
    return SourceLoc(sal.symtab.fullname(), int(sal.line), sal.symtab.filename)


def source_definition_at(addr: int) -> SourceLoc | None:
    """Resolve a data symbol's declaration, falling back to its code line.

    Symbol lookup is identifier-only; offsets never become GDB expressions.
    """
    _ensure_gdb()
    name = lookup_symbol_at(addr)
    if name:
        name = re.split(r"[+-]", name, maxsplit=1)[0]
        if is_plain_identifier(name):
            symbol = _lookup_gdb_symbol(name)
            if symbol is not None:
                try:
                    if symbol.symtab is not None and symbol.line > 0:
                        return SourceLoc(
                            symbol.symtab.fullname(),
                            int(symbol.line),
                            symbol.symtab.filename,
                        )
                except (gdb.error, AttributeError, TypeError, ValueError):
                    pass
    return source_location_at(addr)


def format_code_address(address: int | None) -> str:
    """Render a target code address as ``<symbol+offset>`` or hex.

    Combines :func:`lookup_symbol_at` with the pure address formatter, so
    callers that touch GDB (symbol resolution) never inline the two-step
    dance.  ``None`` / zero addresses render as plain hex.
    """
    symbol = lookup_symbol_at(address) if address else None
    from core.formatting import format_symbol_or_address

    return format_symbol_or_address(address, symbol)


def macro_defined(name: str) -> bool:
    """Return whether GDB debug information defines a C/C++ macro."""
    _ensure_gdb()
    try:
        output = _gdb_execute(f"info macro {name}")
    except gdb.error:
        return False
    return "#define" in output


def read_macro_text(name: str) -> str | None:
    """Read a preprocessor macro's expansion text, or ``None``.

    Parses GDB's ``info macro NAME`` output. Returns the raw expansion with
    surrounding double quotes stripped, so a string-valued macro (which
    ``info macro`` prints as a quoted literal, ``#define NAME "text"``) yields
    the bare text a caller can parse. ``None`` when the macro is not defined
    in the current source context or *name* is not a plain identifier.

    Args:
        name: A single C identifier naming the macro.
        The macro is read from the current source context. Use
            :func:`read_macro_text_in_source` when a specific compilation unit
            must be selected first.
    """
    if not is_plain_identifier(name):
        return None
    _ensure_gdb()
    command = f"info macro {name}"
    try:
        output = _gdb_execute(command)
    except gdb.error:
        return None
    marker = f"#define {name}"
    for line in output.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(marker):
            continue
        expansion = stripped[len(marker) :].strip()
        # NOTE: string-valued macros print as a quoted literal; strip the
        # outer quotes so callers get bare text to parse.
        if len(expansion) >= 2 and expansion[0] == '"' and expansion[-1] == '"':
            return expansion[1:-1]
        return expansion
    return None


_elf_macro_cache: dict[str, dict[str, str]] = {}


def loaded_object_path() -> str | None:
    """Path of the loaded object file, or ``None`` when GDB has none."""
    if gdb is None:
        return None
    gdb_error = getattr(gdb, "error", None)
    caught: tuple[type[BaseException], ...] = (
        (gdb_error, AttributeError, RuntimeError)
        if isinstance(gdb_error, type) and issubclass(gdb_error, BaseException)
        else (AttributeError, RuntimeError)
    )
    try:
        filename = getattr(gdb.current_progspace(), "filename", None)
    except caught:
        return None
    return filename if isinstance(filename, str) and filename else None


def read_macro_int_from_elf(name: str) -> int | None:
    """Read an integer macro from the loaded ELF's macro table, or ``None``.

    The table is parsed once per session: the macro definitions of one image
    cannot change while it stays loaded, and ``reset_loadable_ranges`` drops the
    cache on a reconnect or a reflash.
    """
    path = loaded_object_path()
    if path is None:
        return None
    table = _elf_macro_cache.get(path)
    if table is None:
        table = macro_definitions_from_elf(path)
        _elf_macro_cache[path] = table
    text = table.get(name)
    if text is None:
        return None
    return parse_integer_literal(text)


def read_macro_text_in_source(name: str, source: str) -> str | None:
    """Read a macro after temporarily selecting a GDB source location.

    GDB's macro lookup is scoped to the current source location. Selecting a
    known kernel function is therefore the portable way to read a macro from
    another compilation unit without driving the inferior.
    """
    if not is_plain_identifier(name) or not source.strip():
        return None
    _ensure_gdb()
    saved_source: str | None = None
    saved_listsize: str | None = None
    try:
        source_info = _gdb_execute("info source")
        # NOTE: GDB prints ``Current source file is <path>.`` with a
        # trailing sentence period; the path itself usually contains dots
        # (``main.c``), so a non-greedy stop-at-dot capture would truncate it.
        match = re.search(r"Current source file is (.+)$", source_info, re.MULTILINE)
        if match:
            saved_source = match.group(1).strip().removesuffix(".")
        saved_listsize = _gdb_execute("show listsize")
        _gdb_execute("set listsize 1")
        _gdb_execute(f"list {source}")
        return read_macro_text(name)
    except (gdb.error, ValueError, AttributeError):
        return None
    finally:
        if saved_listsize:
            try:
                match = re.search(r"(?:listsize is|default is) (.+?)\.", saved_listsize)
                if match:
                    _gdb_execute(f"set listsize {match.group(1)}")
            except gdb.error:
                pass
        if saved_source:
            with contextlib.suppress(gdb.error):
                _gdb_execute(f"list {saved_source}:1")


def symbol_exists(name: str) -> bool:
    """Return whether a DWARF/ELF symbol named *name* is visible.

    Function symbols used as config probes are detected without reading
    ``symbol.value()``, so a missing selected frame cannot hide them.
    """
    if not is_plain_identifier(name):
        return False
    _ensure_gdb()
    return _lookup_gdb_symbol(name) is not None


def lookup_type(name: str) -> gdb.Type | None:
    """Look up a type by its GDB name."""
    _ensure_gdb()
    try:
        return gdb.lookup_type(name)
    except gdb.error:
        return None


def type_size(type_or_name) -> int | None:
    """Return ``sizeof`` of a GDB type (or type name) in bytes, or ``None``.

    The width is used for raw-memory reads and width-adaptive decode; an
    unresolvable type (missing DWARF, ``gdb.Type`` without ``sizeof``)
    degrades to ``None`` so callers render ``N/A`` instead of crashing.
    ``int()`` around the size normalises GDB pointer sizes on some ports.
    Duck-typed on ``sizeof`` so it also works on stand-in fake types in
    unit tests (no ``gdb.Type`` in scope).
    """
    try:
        if isinstance(type_or_name, str):
            typ = lookup_type(type_or_name)
            if typ is None:
                return None
            return int(typ.sizeof)
        return int(type_or_name.sizeof)
    except TARGET_ACCESS_ERRORS:
        return None


def array_bound(value_or_type) -> int | None:
    """Return the element count of an array type/value, or ``None``.

    ``gdb.Type`` reports the bound as a ``range``; a declaration without a
    bound (``extern T sym[]``) reports ``(0, -1)`` i.e. count 0, which is
    unknown, not a real zero-length array -- ``None`` says so.  Accepts a
    ``gdb.Type``, a ``gdb.Value`` (via its ``.type``) or a type name.
    """
    try:
        typ = (
            lookup_type(value_or_type)
            if isinstance(value_or_type, str)
            else getattr(value_or_type, "type", value_or_type)
        )
        if typ is None:
            return None
        count = typ.strip_typedefs().range()[1] + 1
        return count if count > 0 else None
    except TARGET_ACCESS_ERRORS:
        return None


def type_field_names(type_or_name) -> frozenset[str]:
    """Return the DWARF member names of a struct type, or an empty set.

    ``strip_typedefs`` is applied so a typedef spelling of a struct exposes
    the same members as the tagged spelling.  Unreadable or absent types
    degrade to an empty set (``None`` would force every caller into a
    ``or set()`` dance).
    """
    try:
        if isinstance(type_or_name, str):
            typ = lookup_type(type_or_name)
            if typ is None:
                return frozenset()
        else:
            typ = type_or_name
        return frozenset(f.name for f in typ.strip_typedefs().fields() if f.name)
    except TARGET_ACCESS_ERRORS:
        return frozenset()


def eval_identifier(name: str) -> gdb.Value | None:
    """Evaluate one C identifier on the host after rejecting call syntax.

    GDB expands preprocessor macros and constant expressions without
    resuming the inferior. ``foo()``, ``_timer_list[0]``, and convenience
    variables are rejected so this cannot become an inferior function call.
    """
    if not is_plain_identifier(name):
        return None
    _ensure_gdb()
    try:
        return gdb.parse_and_eval(name)
    except (gdb.error, *_SYMBOL_LOOKUP_ERRORS):
        return None


def linker_symbol_address(name: str) -> int | None:
    """Return one linker-defined symbol's address without resuming the target.

    Absolute labels such as ``_estack`` have no DWARF type, so a bare
    ``parse_and_eval`` rejects them.  Taking the address is an address
    computation only; the name is still restricted to one plain identifier.
    """
    if not is_plain_identifier(name):
        return None
    _ensure_gdb()
    try:
        return int(gdb.parse_and_eval(f"&{name}"))
    except (gdb.error, *_SYMBOL_LOOKUP_ERRORS, TypeError, ValueError):
        return None


def read_macro_int(name: str) -> int | None:
    """Read a preprocessor macro or integer constant as ``int``.

    Uses :func:`eval_identifier` so GDB performs the expansion, including
    nested arithmetic such as packed version macros.
    """
    return read_int(eval_identifier(name))


def read_macro_int_in_source(name: str, source: str) -> int | None:
    """Read an integer macro after selecting a known GDB source location.

    GDB's macro table is scoped to the current source location, so a bare
    :func:`read_macro_int` misses a macro unless the user happens to be
    stopped in a CU that saw the defining header.  Selecting a function that
    every candidate implementation defines (``pvPortMalloc`` for heap_*,
    ``vTaskStartScheduler`` for the kernel version triplet) is the portable
    way to evaluate it there; the caller's source view is restored.

    Returns ``None`` when the macro is unreadable or not an integer literal.
    """
    value = read_macro_int(name)
    if value is not None:
        return value
    # NOTE: the loaded ELF's own macro table does not depend on the selected
    # compilation unit nor on the build tree still existing at the path recorded
    # in the DWARF, so it is tried before GDB's `list`-based CU switch -- which
    # silently fails on an already-flashed image or a discarded build tree (see
    # core.gdb_bridge).
    from_elf = read_macro_int_from_elf(name)
    if from_elf is not None:
        return from_elf
    text = read_macro_text_in_source(name, source)
    if text is None:
        return None
    # NOTE: a C integer literal may carry a suffix (``3L``, ``8UL``), which
    # ``int(text, 0)`` rejects; the macro text is a literal, not an expression.
    match = _C_INTEGER_LITERAL_RE.match(text.strip())
    if match is None:
        return None
    try:
        return int(match.group("body"), 0)
    except ValueError:
        return None


# C integer literal with an optional unsigned/long suffix (``3L``, ``8UL``).
_C_INTEGER_LITERAL_RE = re.compile(
    r"^(?P<body>0[xX][0-9a-fA-F]+|[0-9]+)(?P<suffix>[uUlL]*)$"
)
_SECTION_RANGE_RE = re.compile(r"0x([0-9a-fA-F]+)\s*-\s*0x([0-9a-fA-F]+)")
_loadable_ranges_cache: tuple[tuple[int, int], ...] | None = None


def loadable_ranges() -> tuple[tuple[int, int], ...]:
    """Return loadable ELF section ranges from ``info files``, or empty.

    An empty result means the map is unknown (unit tests, or GDB not ready),
    so callers skip the range check rather than inventing a RAM window.  The
    section map is static once a target is loaded, so the result is cached
    per session; :func:`reset_loadable_ranges` forces a fresh read (e.g.
    after reconnecting GDB to a different image).
    """
    global _loadable_ranges_cache
    if _loadable_ranges_cache is not None:
        return _loadable_ranges_cache
    if gdb is None:
        return ()
    try:
        output = _gdb_execute("info files")
    except (gdb.error, AttributeError):
        return ()
    ranges: list[tuple[int, int]] = []
    for match in _SECTION_RANGE_RE.finditer(output or ""):
        low = int(match.group(1), 16)
        high = int(match.group(2), 16)
        if high > low:
            ranges.append((low, high))
    _loadable_ranges_cache = tuple(ranges)
    return _loadable_ranges_cache


_source_roots_cache: dict[tuple[str | None, str], tuple[str, ...]] = {}
_source_resolver_cache: dict[
    tuple[str | None, str, tuple[str, ...]], SourcePathResolver
] = {}


def reset_loadable_ranges() -> None:
    """Clear session-scoped bridge caches (sections, macros, paths, symbols)."""
    global _loadable_ranges_cache
    _loadable_ranges_cache = None
    # NOTE: a new image means new macro definitions; the table is keyed by the
    # objfile path, which a reflash may reuse.
    _elf_macro_cache.clear()
    _source_roots_cache.clear()
    _source_resolver_cache.clear()
    reset_symbol_caches()


def array_item(value, index):
    """Return ``value[index]`` safely, or ``None`` on any access error."""
    try:
        return value[index]
    except (IndexError, TypeError, ValueError, AttributeError):
        return None


def read_int(value: gdb.Value | None) -> int | None:
    """Convert a target-decoded ``gdb.Value`` to ``int`` safely.

    GDB already applies the target byte order during ``gdb.Value`` conversion.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError, AttributeError):
        # NOTE: AttributeError covers the case where gdb is None and
        # the except clause cannot evaluate gdb.error; it also covers
        # attribute access failures on invalid gdb.Value objects.
        return None


def read_cstring(
    value: gdb.Value | None, max_len: int = GDR_MAX_CSTRING_LENGTH
) -> str | None:
    """Read a C string (``char*`` or ``char[]``) from a ``gdb.Value``.

    For ``char[]`` arrays, GDB auto-detects the null terminator; for
    ``char*`` pointers the read is bounded to ``max_len`` because GDB cannot
    size the buffer.  A bounded pointer read may carry embedded NULs and
    arbitrary following bytes, so the result is truncated at the first NUL;
    array reads are truncated the same way so the caller never sees NUL
    filler from either path.
    """
    if value is None:
        return None
    _ensure_gdb()
    try:
        is_ptr = value.type.code == gdb.TYPE_CODE_PTR
        if is_ptr:
            if int(value) == 0:
                return None
            # NOTE: Value.string(length=N) fetches a full N-byte window
            # (GDB manual) that includes embedded NULs; ``errors="replace"``
            # keeps a stray non-UTF-8 byte in the window from aborting the
            # whole read.  Dereferencing the pointer first would yield a
            # scalar ``char`` whose .string() raises instead of reading the
            # pointed-to string, so the pointer is passed to .string() as-is.
            return value.string(length=max_len, errors="replace").split("\x00", 1)[0]
        return value.string().split("\x00", 1)[0]
    except (gdb.error, gdb.MemoryError, ValueError):
        return None


def read_bytes(addr: int, size: int) -> bytes | None:
    """Read raw memory from the target inferior without byte reordering.

    Callers decoding the returned bytes as an integer must use
    :func:`get_arch_info` to select the target byte order.
    """
    _ensure_gdb()
    try:
        inferior = gdb.selected_inferior()
        return bytes(inferior.read_memory(addr, size))
    except (gdb.MemoryError, gdb.error):
        return None


def make_pointer_array(values: list[gdb.Value]) -> gdb.Value:
    """Build a host-side ``gdb.Value`` array of pointers to *values*.

    Convenience functions can only return a ``gdb.Value``. Packing target
    addresses into a typed pointer array lets ``$gdr_tasks()`` expose every
    thread while remaining usable from native GDB expressions
    (``$gdr_tasks()[i]``, ``*$gdr_tasks()[i]``).

    Args:
        values: Struct values or pointers from the inferior.

    Returns:
        A ``T *[]`` array value, or ``0`` when *values* is empty / unusable.
    """
    _ensure_gdb()
    if not values:
        return gdb.Value(0)

    arch = get_arch_info()
    if arch is None or arch.ptrsize not in (4, 8):
        return gdb.Value(0)

    pointers: list[int] = []
    elem_type: gdb.Type | None = None
    for val in values:
        vtype = val.type.strip_typedefs()
        try:
            if vtype.code == gdb.TYPE_CODE_PTR:
                pointers.append(int(val))
                if elem_type is None:
                    elem_type = vtype
            else:
                addr = val.address
                if addr is None:
                    continue
                pointers.append(int(addr))
                if elem_type is None:
                    elem_type = vtype.pointer()
        except (TypeError, ValueError, gdb.error):
            continue

    if not pointers or elem_type is None:
        return gdb.Value(0)

    # NOTE: gdb.Value(buffer, type) builds a host-side value without writing
    # inferior memory — required for read-only embedded targets.
    endian = "<" if arch.endian == "little" else ">"
    fmt = "I" if arch.ptrsize == 4 else "Q"
    mask = (1 << (arch.ptrsize * 8)) - 1
    buf = b"".join(struct.pack(f"{endian}{fmt}", ptr & mask) for ptr in pointers)
    # type: ignore[arg-type] - gdb.Value accepts a buffer plus an array type;
    # the stub only declares the scalar overloads.
    return gdb.Value(buf, elem_type.array(len(pointers) - 1))  # type: ignore[arg-type]


def _has_tty() -> bool:
    """Return whether standard output is attached to a controlling TTY."""
    with contextlib.suppress(OSError):
        return os.isatty(1)
    return False


def configure_non_tty_width() -> None:
    """Clear GDB's 80-column non-TTY fallback on MI/pipe sessions."""
    if gdb is None or _has_tty():
        return
    if os.environ.get("COLUMNS") == "80":
        os.environ.pop("COLUMNS", None)
    with contextlib.suppress(Exception):
        if gdb.parameter("width") == 80:
            _gdb_execute("set width 0")


def _gdb_width() -> int | None:
    """Return GDB's ``width`` parameter when it is a positive int.

    ``gdb.parameter("width")`` reports ``0`` for ``unlimited``; ``None`` or a
    non-positive value means "no explicit width", so callers fall through to
    terminal probing.  On non-TTY pipes (such as VSCode MI sessions), GDB
    pins ``width`` to ``80`` even after ``set width 0``; ignore that pipe
    default so :func:`terminal_width` uses ``COLUMNS`` or the 120-column
    default instead.
    """
    if gdb is None:
        return None
    try:
        value = gdb.parameter("width")
    except Exception:
        return None
    if not isinstance(value, int) or value <= 0:
        return None
    if value == 80 and not _has_tty():
        return None
    return value


def _system_columns() -> int | None:
    """Return the detected terminal column count, or ``None`` when unknown."""
    try:
        size = shutil.get_terminal_size(fallback=(GDR_DEFAULT_TERMINAL_WIDTH, 24))
    except (OSError, ValueError):
        return None
    columns = size.columns
    return columns if isinstance(columns, int) and columns > 0 else None


def terminal_width() -> int:
    """Return the effective terminal width for list rendering.

    Priority:
    1. ``gdb.parameter("width")`` when it is a positive int (``set width N``);
    2. ``shutil.get_terminal_size`` columns when GDB width is unlimited/None;
    3. fallback to 120.

    Terminal probing is separate from pure formatting (:func:`format_table`)
    so unit tests can pin 80/100/120/160-character behavior.
    """
    gdb_width = _gdb_width()
    if gdb_width is not None:
        return gdb_width
    return _system_columns() or GDR_DEFAULT_TERMINAL_WIDTH


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` comments, ``/* */`` comments and trailing commas from JSONC."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        ch = text[index]
        if in_string:
            out.append(ch)
            if ch == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if ch == '"':
                in_string = False
            index += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            index += 1
        elif ch == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
        elif ch == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and text[index : index + 2] != "*/":
                index += 1
            index += 2
        else:
            out.append(ch)
            index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _resolve_workspace_expr(value: object, workspace: Path) -> str | None:
    """Expand ``${workspaceFolder}`` in a launch attribute when fully concrete."""
    if not isinstance(value, str) or not value.strip():
        return None
    expanded = value.replace("${workspaceFolder}", str(workspace)).replace(
        "${workspaceRoot}", str(workspace)
    )
    if "${" in expanded:
        return None
    return os.path.normpath(expanded)


def _launch_json_roots(
    start_dirs: Sequence[Path], *, elf_path: str | None
) -> tuple[str, ...]:
    """Extract resolved ``cwd`` and workspace roots from ancestor ``launch.json``."""
    elf_real = os.path.realpath(elf_path) if elf_path else None
    seen_files: set[Path] = set()
    matched: list[str] = []
    fallback: list[str] = []
    workspaces: list[str] = []
    for base in start_dirs:
        for candidate in (base, *base.parents):
            launch = candidate / ".vscode" / "launch.json"
            if not launch.is_file():
                continue
            resolved = launch.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            workspaces.append(str(candidate))
            try:
                raw = json.loads(_strip_jsonc(launch.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            configs = raw.get("configurations") if isinstance(raw, dict) else None
            if not isinstance(configs, list):
                continue
            for cfg in configs:
                if not isinstance(cfg, dict):
                    continue
                cwd = _resolve_workspace_expr(cfg.get("cwd"), candidate)
                if cwd is None:
                    continue
                exe = _resolve_workspace_expr(
                    cfg.get("executable") or cfg.get("program"), candidate
                )
                is_match = (
                    elf_real is not None
                    and exe is not None
                    and os.path.realpath(exe) == elf_real
                )
                (matched if is_match else fallback).append(cwd)
    ordered = [*matched, *fallback, *workspaces]
    return tuple(dict.fromkeys(ordered))


def _current_cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return _STARTUP_CWD


def source_roots() -> tuple[str, ...]:
    """Ordered workspace/cwd roots used to turn DWARF paths into relative paths."""
    elf_path = loaded_object_path()
    cwd = _current_cwd()
    key = (elf_path, cwd)
    cached = _source_roots_cache.get(key)
    if cached is not None:
        return cached
    starts: list[Path] = []
    if elf_path:
        starts.append(Path(os.path.abspath(elf_path)).parent)
    if cwd:
        starts.append(Path(cwd))
    if _STARTUP_CWD and cwd != _STARTUP_CWD:
        starts.append(Path(_STARTUP_CWD))
    roots: list[str] = list(_launch_json_roots(starts, elf_path=elf_path))
    roots.extend(dir_path for dir_path in (cwd, _STARTUP_CWD) if dir_path)
    deduped: list[str] = []
    seen_real: set[str] = set()
    for root in roots:
        real = os.path.realpath(root)
        if real not in seen_real:
            seen_real.add(real)
            deduped.append(os.path.normpath(root))
    result = tuple(deduped)
    _source_roots_cache[key] = result
    return result


def _build_search_dirs(
    roots: tuple[str, ...], elf_path: str | None, cwd: str
) -> tuple[str, ...]:
    """Collect candidate local base directories for expanding ``../`` paths."""
    dirs: list[str] = []
    if elf_path:
        dirs.append(str(Path(os.path.abspath(elf_path)).parent))
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        dirs.append(root)
        try:
            with os.scandir(root) as entries:
                subdirs = sorted(
                    entry.path
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                    and not entry.name.startswith(".")
                )
        except OSError:
            subdirs = []
        dirs.extend(subdirs[:32])
    if cwd:
        dirs.append(cwd)
    if _STARTUP_CWD and cwd != _STARTUP_CWD:
        dirs.append(_STARTUP_CWD)
    return tuple(dict.fromkeys(dirs))


def source_path_resolver() -> SourcePathResolver:
    """Return a cached :class:`SourcePathResolver` for the active ELF and workspace."""
    elf_path = loaded_object_path()
    cwd = _current_cwd()
    roots = source_roots()
    key = (elf_path, cwd, roots)
    cached = _source_resolver_cache.get(key)
    if cached is not None:
        return cached
    elf_info = inspect_elf_sources(elf_path) if elf_path else ElfSourceInfo()
    mappings: tuple[tuple[str, str], ...] = ()
    if not elf_info.file_prefix_map and elf_info.paths:
        unmatched = [
            path
            for path in elf_info.paths
            if is_abs_path(normalize_path(path))
            and not any(relative_under_root(path, root) is not None for root in roots)
        ]
        if unmatched:
            basenames = frozenset(
                Path(path.replace("\\", "/")).name for path in unmatched if path
            )
            local_files = collect_local_source_files(roots, target_basenames=basenames)
            mappings = infer_elf_prefix_mappings(unmatched, local_files, roots)
    resolver = SourcePathResolver(
        roots,
        mappings,
        search_dirs=_build_search_dirs(roots, elf_path, cwd),
        file_prefix_map=elf_info.file_prefix_map,
    )
    _source_resolver_cache[key] = resolver
    return resolver


def display_path(path: str) -> str:
    """Shorten *path* via workspace roots, remote SDK matching, and ``../`` expansion."""
    return source_path_resolver().resolve_path(path)


def format_source_loc(source: SourceLoc | None) -> str:
    """Render *source* concisely using relative workspace paths when available."""
    if source is None:
        return ""
    return source_path_resolver().resolve_source_location(source)


def print_table(
    rows: list[list[str]],
    headers: list[str],
    *,
    elastic: Sequence[str] | Mapping[str, ElasticMode] = (),
    elastic_modes: Mapping[str, ElasticMode] | None = None,
    width: int | None = None,
) -> None:
    """Print a formatted ASCII table to GDB stdout in one write.

    Args:
        rows: List of row lists; each row should have ``len(headers)`` cells.
        headers: Column header strings.
        elastic: Elastic headers or priority mapping from header to mode
            (``full`` / ``left`` / ``right``).
        elastic_modes: Optional per-header modes overriding ``elastic``'s
            default ``left`` mode when ``elastic`` is a sequence.
        width: Explicit terminal width; ``None`` probes the active width.
    """
    _ensure_gdb()
    if width is None:
        width = terminal_width()
    # NOTE: one write prevents asynchronous GDB output from splitting rows.
    gdb.write(
        format_table(
            rows,
            headers,
            elastic=elastic,
            elastic_modes=elastic_modes,
            width=width,
        )
    )


def print_detail(pairs: Sequence[tuple[str, str]]) -> None:
    """Print a vertical ``Key: Value`` detail block to GDB stdout in one write.

    Keys are right-aligned to a shared column so the colons line up vertically,
    which makes values easier to scan.  Args:
        pairs: Ordered ``(key, value)`` pairs describing one object.
    """
    _ensure_gdb()
    output = StringIO()
    if pairs:
        width = max(len(key) for key, _value in pairs)
        for key, value in pairs:
            output.write(f"{key:>{width}}: {value}\n")
    gdb.write(output.getvalue())


def warn(msg: str) -> None:
    """Print a warning-prefixed message to GDB stderr."""
    _ensure_gdb()
    gdb.write(f"[gdr] warning: {msg}\n", stream=gdb.STDERR)


def err(msg: str) -> None:
    """Print an error-prefixed message to GDB stderr.

    Distinct from :func:`warn` in severity: ``warn`` is for recoverable
    degradation (e.g. symbol not found), ``err`` is for a command that
    failed outright.
    """
    _ensure_gdb()
    gdb.write(f"[gdr] error: {msg}\n", stream=gdb.STDERR)


def info(msg: str) -> None:
    """Print an info-prefixed message to GDB stdout."""
    _ensure_gdb()
    gdb.write(f"[gdr] {msg}\n")


def write_stdout(text: str) -> None:
    """Write raw text to GDB stdout without any prefix or wrapping.

    Used for terminal escape sequences (inline images). ``gdb.write`` passes the
    bytes through unchanged, which the pty check in the heap-dump spike confirmed
    for a 20 KB base64 payload at 80 columns.
    """
    _ensure_gdb()
    gdb.write(text)


def format_exception(e: BaseException) -> str:
    """Format an exception for a one-line diagnostic.

    Returns a ``"Type: message"`` string.  Debug-mode stacktraces are left to
    :func:`show_last_exception`; keeping this a one-liner means command and
    function guards can embed it directly even when :data:`core.constants.GDR_DEBUG`
    is disabled.
    """
    return f"{type(e).__name__}: {e}"


def _source_line(filename: str, lineno: int) -> str:
    """Return the source line at *lineno* in *filename*, or ``""``.

    Best-effort and never allowed to raise: source trees may be missing,
    moved, or exceed the pyc cache after a binary is flashed.
    """
    try:
        with open(filename, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
    except OSError:
        return ""


def show_last_exception() -> None:
    """Print a full diagnostic for the exception currently being handled.

    Print a full diagnostic for the exception currently being handled: an
    exception banner, the reversed stacktrace with one source line per frame,
    and the runtime environment (GDB and Python versions, platform).  Call
    only from inside an ``except`` block (it reads ``sys.exc_info()``).

    Deliberately does not print GDB command history via ``_gdb_execute("show
    commands")``: history is noisy over remote sessions (see
    :func:`format_exception`).
    """
    _ensure_gdb()
    _exc_type, exc_value, exc_traceback = sys.exc_info()
    # NOTE: test exc_value (not the type) so the value is narrowed to a real
    # exception for format_exception below; the triple is all-or-nothing.
    if exc_value is None:
        err("show_last_exception() called outside of an exception handler")
        return

    lines: list[str] = [""]
    lines.append(" Exception raised ".center(72, "="))
    lines.append(format_exception(exc_value))
    lines.append(" Detailed stacktrace ".center(72, "="))
    for frame in _traceback.extract_tb(exc_traceback)[::-1]:
        filename, lineno, method, code = frame
        source = code if code and code.strip() else _source_line(filename, lineno)
        lines.append(f'  File "{filename}", line {lineno:d}, in {method}()')
        if source:
            lines.append(f"    {source}")
    lines.append(" Runtime environment ".center(72, "="))
    gdb_version = getattr(gdb, "VERSION", "unknown")
    lines.append(f"GDB: {gdb_version}")
    lines.append(
        f"Python: {sys.version_info.major:d}.{sys.version_info.minor:d}."
        f"{sys.version_info.micro:d}"
    )
    lines.append(f"OS: {platform.system()} {platform.release()} ({platform.machine()})")
    gdb.write("\n".join(lines) + "\n")


def gdb_command_guard(func):
    """Decorator for GDB command bodies: catch target/runtime errors.

    RTOS debugging routinely hits ``gdb.error`` / ``gdb.MemoryError``
    (target halted, unmapped memory, remote link dropped).  Without this
    guard such errors bubble up as GDB "Python Exception" noise and abort
    the rest of the command.  With it:

    * ``gdb.error`` / ``gdb.MemoryError`` → :func:`warn` (recoverable).
    * any other ``Exception`` → :func:`err` one-liner, or
      :func:`show_last_exception` plus an optional re-raise when
      :data:`core.constants.GDR_DEBUG` is set (see :func:`is_debug` /
      :func:`propagate_exception`).

    The wrapped function's return value is preserved on success and
    discarded on error (commands are void-returning by convention).
    """
    target_errors: tuple[type[BaseException], ...] = tuple(
        exc
        for name in ("error", "MemoryError")
        if isinstance((exc := getattr(gdb, name, None)), type)
        and issubclass(exc, BaseException)
    )

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except target_errors as e:
            warn(f"{func.__name__}: {format_exception(e)}")
            return None
        except Exception as e:
            if is_debug():
                show_last_exception()
                if propagate_exception():
                    raise
            else:
                err(f"{func.__name__}: {format_exception(e)}")
            return None

    return wrapper


def gdb_function_guard(func):
    """Decorator for GDB convenience-function bodies (return ``gdb.Value``).

    GDB requires ``gdb.Function.invoke`` to return a ``gdb.Value``; the
    command guard's ``None`` fallback would itself raise ``TypeError``.  This
    guard therefore converts errors into ``gdb.GdbError``, whose message GDB
    prints cleanly without raw "Python Exception" noise.

    * ``gdb.GdbError`` → re-raised unchanged (explicit user-facing messages).
    * ``gdb.error`` / ``gdb.MemoryError`` → ``gdb.GdbError`` (recoverable).
    * any other ``Exception`` → debug: :func:`show_last_exception` (+ optional
      re-raise); otherwise ``gdb.GdbError``.
    Not finding a requested object is *not* an error: adapters return a null
    ``gdb.Value`` for that, and this guard leaves successful results intact.
    """
    target_errors: tuple[type[BaseException], ...] = ()
    user_error: type[BaseException] = Exception
    if gdb is not None:
        target_errors = (gdb.error, gdb.MemoryError)
        user_error = gdb.GdbError

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except user_error:
            raise
        except target_errors as e:
            raise user_error(f"{func.__name__}: {format_exception(e)}") from e
        except Exception as e:
            if is_debug():
                show_last_exception()
                if propagate_exception():
                    raise
            raise user_error(f"{func.__name__}: {format_exception(e)}") from e

    return wrapper
