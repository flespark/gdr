"""GDB Python API wrappers.

Centralises all direct ``gdb.*`` calls so the rest of the codebase can be
written against stable, typed helpers with uniform error handling.

The ``gdb`` import is guarded so the module is importable outside GDB for
static analysis and unit-testing of non-GDB logic.  Calling any function
that touches ``gdb.*`` outside a GDB session raises ``RuntimeError``.
"""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

MAX_LIST_LEN = 4096


def _ensure_gdb() -> None:
    """Raise RuntimeError if not running inside GDB."""
    if gdb is None:
        raise RuntimeError("not running inside GDB")


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


def symbol_exists(name: str) -> bool:
    """Check whether a symbol is visible in the current target."""
    return lookup_symbol(name) is not None


def lookup_type(name: str) -> gdb.Type | None:
    """Look up a type by name (e.g. ``"struct rt_thread"``)."""
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
    """Convert a ``gdb.Value`` to ``int``, returning ``None`` on failure."""
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
    """Read raw memory from the target inferior."""
    _ensure_gdb()
    try:
        inferior = gdb.selected_inferior()
        return bytes(inferior.read_memory(addr, size))
    except (gdb.MemoryError, gdb.error):
        return None


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    """Print a formatted ASCII table to GDB stdout.

    Args:
        rows: List of row lists; each row should have ``len(headers)`` cells.
        headers: Column header strings.
    """
    if not rows:
        print("(empty)")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in col_widths))
    for row in rows:
        cells = [str(c) for c in row]
        cells += [""] * (len(headers) - len(cells))
        print(fmt.format(*cells))


def warn(msg: str) -> None:
    """Print a warning-prefixed message to GDB stderr."""
    _ensure_gdb()
    gdb.write(f"warning: {msg}\n", stream=gdb.STDERR)


def info(msg: str) -> None:
    """Print an info-prefixed message to GDB stdout."""
    _ensure_gdb()
    gdb.write(f"[gdr] {msg}\n")
