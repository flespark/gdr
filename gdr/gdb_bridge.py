"""GDB Python API wrappers.

Centralises all direct ``gdb.*`` calls so the rest of the codebase can be
written against stable, typed helpers with uniform error handling.

The ``gdb`` import is guarded so the module is importable outside GDB for
static analysis and unit-testing of non-GDB logic.  Calling any function
that touches ``gdb.*`` outside a GDB session raises ``RuntimeError``.
"""

from __future__ import annotations

import functools
import os
import shutil
import struct
import traceback as _traceback
from collections.abc import Sequence
from dataclasses import dataclass
from io import StringIO

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.constants import DEFAULT_TERMINAL_WIDTH, MAX_CSTRING_LENGTH
from gdr.formatting import format_table


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
        ptrsize = gdb.selected_inferior().architecture().void_type().pointer().sizeof
    except (AttributeError, TypeError, gdb.error):
        try:
            ptrsize = gdb.lookup_type("void").pointer().sizeof
        except (AttributeError, TypeError, gdb.error):
            return None

    if not isinstance(ptrsize, int) or ptrsize <= 0:
        return None

    try:
        endian_output = gdb.execute("show endian", to_string=True).lower()
    except gdb.error:
        return None

    is_little_endian = "little endian" in endian_output
    is_big_endian = "big endian" in endian_output
    if is_little_endian == is_big_endian:
        return None
    endian = "little" if is_little_endian else "big"
    return ArchInfo(ptrsize=ptrsize, endian=endian)


def is_debug() -> bool:
    """True if ``GDR_DEBUG`` env var is set (enables verbose diagnostics).

    Enables full Python tracebacks in :func:`format_exception` and surfaces
    them through :func:`err` instead of a one-line message.  Mirrors GEF's
    ``gef.debug`` setting but opt-in via environment so it works before GDB
    has finished initialising.
    """
    return os.environ.get("GDR_DEBUG", "").lower() in ("1", "true", "yes")


def lookup_symbol(name: str) -> gdb.Value | None:
    """Look up a global/static symbol by name.

    Args:
        name: Symbol or expression understood by ``gdb.parse_and_eval``.

    Returns:
        The ``gdb.Value`` or ``None`` if not found / not readable.
    """
    _ensure_gdb()
    try:
        return gdb.parse_and_eval(name)
    except gdb.error:
        return None


def lookup_symbol_at(addr: int) -> str | None:
    """Look up the symbol and offset at a target address.

    Returns a display-ready ``"symbol+offset"`` string without enclosing
    brackets, or ``None`` when no symbol covers ``addr``.
    """
    _ensure_gdb()
    try:
        symbol = gdb.execute(f"info symbol {addr:#x}", to_string=True).strip()
    except gdb.error:
        return None
    if symbol.startswith("No symbol matches"):
        return None
    symbol = symbol.partition(" in section ")[0]
    return symbol.replace(" + ", "+").replace(" - ", "-")


def macro_defined(name: str) -> bool:
    """Return whether GDB debug information defines a C/C++ macro."""
    _ensure_gdb()
    try:
        output = gdb.execute(f"info macro {name}", to_string=True)
    except gdb.error:
        return False
    return "#define" in output


def symbol_exists(name: str) -> bool:
    """Check whether a symbol is visible in the current target."""
    return lookup_symbol(name) is not None


def lookup_type(name: str) -> gdb.Type | None:
    """Look up a type by its GDB name."""
    _ensure_gdb()
    try:
        return gdb.lookup_type(name)
    except gdb.error:
        return None


def eval_safe(expr: str) -> gdb.Value | None:
    """Evaluate a GDB expression, returning ``None`` on error."""
    _ensure_gdb()
    try:
        return gdb.parse_and_eval(expr)
    except (gdb.error, gdb.MemoryError):
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
        # Reason: AttributeError covers the case where gdb is None and
        # the except clause cannot evaluate gdb.error; it also covers
        # attribute access failures on invalid gdb.Value objects.
        return None


def read_cstring(
    value: gdb.Value | None, max_len: int = MAX_CSTRING_LENGTH
) -> str | None:
    """Read a C string (``char*`` or ``char[]``) from a ``gdb.Value``.

    For ``char[]`` arrays, GDB auto-detects the null terminator; for
    ``char*`` pointers we pass ``length`` as a safety bound.
    """
    if value is None:
        return None
    _ensure_gdb()
    try:
        is_ptr = value.type.code == gdb.TYPE_CODE_PTR
        if is_ptr:
            if int(value) == 0:
                return None
            value = value.dereference()
            # Reason: for char*, GDB doesn't know the buffer size, so we
            # bound the read.  For char[], GDB reads to null terminator.
            return value.string(length=max_len)
        return value.string()
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

    # Reason: gdb.Value(buffer, type) builds a host-side value without writing
    # inferior memory — required for read-only embedded targets.
    endian = "<" if arch.endian == "little" else ">"
    fmt = "I" if arch.ptrsize == 4 else "Q"
    mask = (1 << (arch.ptrsize * 8)) - 1
    buf = b"".join(struct.pack(f"{endian}{fmt}", ptr & mask) for ptr in pointers)
    return gdb.Value(buf, elem_type.array(len(pointers) - 1))


def _gdb_width() -> int | None:
    """Return GDB's ``width`` parameter when it is a positive int.

    ``gdb.parameter("width")`` reports ``0`` for ``unlimited``; ``None`` or a
    non-positive value means "no explicit width", so callers fall through to
    terminal probing.
    """
    if gdb is None:
        return None
    try:
        value = gdb.parameter("width")
    except Exception:
        return None
    return value if isinstance(value, int) and value > 0 else None


def _system_columns() -> int | None:
    """Return the detected terminal column count, or ``None`` when unknown."""
    try:
        size = shutil.get_terminal_size(fallback=(DEFAULT_TERMINAL_WIDTH, 24))
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
    return _system_columns() or DEFAULT_TERMINAL_WIDTH


def print_table(
    rows: list[list[str]],
    headers: list[str],
    *,
    elastic: Sequence[str] = (),
    width: int | None = None,
) -> None:
    """Print a formatted ASCII table to GDB stdout in one write.

    Args:
        rows: List of row lists; each row should have ``len(headers)`` cells.
        headers: Column header strings.
        elastic: Headers of elastic text columns that may shrink when the
            natural table width exceeds the terminal ``width``.
        width: Explicit terminal width; ``None`` probes the active width.
    """
    _ensure_gdb()
    if width is None:
        width = terminal_width()
    # Reason: one write prevents asynchronous GDB output from splitting rows.
    gdb.write(format_table(rows, headers, elastic=elastic, width=width))


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
    gdb.write(f"warning: {msg}\n", stream=gdb.STDERR)


def err(msg: str) -> None:
    """Print an error-prefixed message to GDB stderr.

    Distinct from :func:`warn` in severity: ``warn`` is for recoverable
    degradation (e.g. symbol not found), ``err`` is for a command that
    failed outright.  Mirrors GEF's ``err()`` vs ``warn()`` distinction.
    """
    _ensure_gdb()
    gdb.write(f"[gdr] error: {msg}\n", stream=gdb.STDERR)


def info(msg: str) -> None:
    """Print an info-prefixed message to GDB stdout."""
    _ensure_gdb()
    gdb.write(f"[gdr] {msg}\n")


def format_exception(e: BaseException) -> str:
    """Format an exception with optional traceback for diagnostics.

    Returns a one-line ``"Type: message"`` normally, or appends the full
    Python traceback when :func:`is_debug` is true.  Inspired by GEF's
    ``show_last_exception`` but trimmed for the RTOS use case (no GDB
    command history, which is noisy over remote sessions).
    """
    lines = [f"{type(e).__name__}: {e}"]
    if is_debug():
        lines.append(_traceback.format_exc().rstrip())
    return "\n".join(lines)


def gdb_command_guard(func):
    """Decorator for GDB command bodies: catch target/runtime errors.

    RTOS debugging routinely hits ``gdb.error`` / ``gdb.MemoryError``
    (target halted, unmapped memory, remote link dropped).  Without this
    guard such errors bubble up as GDB "Python Exception" noise and abort
    the rest of the command.  With it:

    * ``gdb.error`` / ``gdb.MemoryError`` → :func:`warn` (recoverable).
    * any other ``Exception`` → :func:`err`, with a full traceback only
      when ``GDR_DEBUG`` is set (see :func:`is_debug`).

    The wrapped function's return value is preserved on success and
    discarded on error (commands are void-returning by convention).
    """
    target_errors: tuple[type[BaseException], ...] = ()
    if gdb is not None:
        target_errors = (gdb.error, gdb.MemoryError)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except target_errors as e:
            warn(f"{func.__name__}: {format_exception(e)}")
            return None
        except Exception as e:
            err(f"{func.__name__}: {format_exception(e)}")
            return None

    return wrapper
