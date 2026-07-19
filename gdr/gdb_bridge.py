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
import traceback as _traceback
from dataclasses import dataclass
from io import StringIO

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

MAX_LIST_LEN = 4096


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


def read_cstring(value: gdb.Value | None, max_len: int = 256) -> str | None:
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


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    """Print a formatted ASCII table to GDB stdout in one write.

    Args:
        rows: List of row lists; each row should have ``len(headers)`` cells.
        headers: Column header strings.
    """
    _ensure_gdb()
    output = StringIO()
    if not rows:
        output.write("(empty)\n")
    else:
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                if i < len(col_widths):
                    col_widths[i] = max(col_widths[i], len(str(cell)))

        fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
        output.write(f"{fmt.format(*headers)}\n")
        output.write(f"{'  '.join('-' * w for w in col_widths)}\n")
        for row in rows:
            cells = [str(c) for c in row]
            cells += [""] * (len(headers) - len(cells))
            output.write(f"{fmt.format(*cells)}\n")

    # Reason: one write prevents asynchronous GDB output from splitting rows.
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
