"""Pure value and width-aware table formatting helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from io import StringIO
from typing import Literal, NamedTuple

from .constants import (
    GDR_DEFAULT_TERMINAL_WIDTH,
    GDR_MIN_ELASTIC_COLUMN_WIDTH,
    GDR_TABLE_COLUMN_GAP,
    GDR_TABLE_TRUNCATION_SUFFIX,
)

ElasticMode = Literal["full", "left", "right"]


class SourceLoc(NamedTuple):
    """Where a code address lives in the source tree.

    ``fullname`` is GDB's resolved path, which may name a file that does not
    exist on this machine (a build directory); ``name`` is the file as the
    debug information recorded it, possibly relative to the compile directory.
    The extension resolves the pair against the workspace (see its
    ``resolveSourceFile``), so both must travel.
    """

    fullname: str
    line: int
    name: str


def format_optional_int(value: int | None) -> str:
    """Render an optional integer without treating zero as unavailable."""
    return str(value) if value is not None else "N/A"


def format_optional_bytes(value: int | None) -> str:
    """Render an optional memory amount with its unit: ``<n> B`` or ``N/A``.

    Every memory size GDR prints goes through this, so the unit is never
    implied by the label alone.
    """
    return f"{value} B" if value is not None else "N/A"


def format_address(address: int | None) -> str:
    """Render a non-zero address in hexadecimal, or ``N/A``."""
    return hex(address) if address else "N/A"


def format_symbol_or_address(address: int | None, symbol: str | None = None) -> str:
    """Render a resolved symbol when available, otherwise an address."""
    return f"<{symbol}>" if symbol else format_address(address)


def _natural_widths(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> list[int]:
    widths = [len(str(header)) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(str(cell)))
    return widths


def _total_width(widths: Sequence[int]) -> int:
    return sum(widths) + GDR_TABLE_COLUMN_GAP * (len(widths) - 1)


def _elastic_indexes(headers: Sequence[str], elastic: Sequence[str]) -> list[int]:
    return [index for index, header in enumerate(headers) if header in elastic]


def _next_shrink(
    widths: Sequence[int],
    indexes: Sequence[int],
    minimums: dict[int, int],
    priorities: dict[int, int],
) -> int | None:
    eligible = [index for index in indexes if widths[index] > minimums[index]]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda index: (
            priorities[index],
            -widths[index],
            index,
        ),
    )


def _shrink_to_fit(
    natural: Sequence[int],
    headers: Sequence[str],
    elastic: Sequence[str],
    width: int,
) -> list[int] | None:
    indexes = _elastic_indexes(headers, elastic)
    priorities = {index: elastic.index(headers[index]) for index in indexes}
    minimums = {
        index: max(len(str(headers[index])), GDR_MIN_ELASTIC_COLUMN_WIDTH)
        for index in indexes
    }
    widths = list(natural)
    minimum_widths = [
        min(natural_width, minimums.get(index, natural_width))
        for index, natural_width in enumerate(widths)
    ]
    if _total_width(minimum_widths) > width:
        return None

    while _total_width(widths) > width:
        index = _next_shrink(widths, indexes, minimums, priorities)
        if index is None:
            return None
        widths[index] -= 1
    return widths


def _truncate_cell(text: str, width: int, mode: ElasticMode = "left") -> str:
    if len(text) <= width or mode == "full":
        return text
    suffix = GDR_TABLE_TRUNCATION_SUFFIX
    keep = max(0, width - len(suffix))
    if mode == "right":
        return suffix + text[len(text) - keep :]
    return text[:keep] + suffix


def _render_table(
    rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    widths: Sequence[int],
    elastic_modes: Mapping[int, ElasticMode],
) -> str:
    output = StringIO()
    row_format = "  ".join(f"{{:<{width}}}" for width in widths)
    output.write(f"{row_format.format(*headers)}\n")
    output.write(f"{'  '.join('-' * width for width in widths)}\n")
    for row in rows:
        cells = [str(cell) for cell in row]
        cells += [""] * (len(headers) - len(cells))
        for index, mode in elastic_modes.items():
            cells[index] = _truncate_cell(cells[index], widths[index], mode)
        output.write(f"{row_format.format(*cells)}\n")
    return output.getvalue()


def _resolve_elastic(
    headers: Sequence[str],
    elastic: Sequence[str] | Mapping[str, ElasticMode],
    elastic_modes: Mapping[str, ElasticMode] | None,
) -> tuple[tuple[str, ...], dict[int, ElasticMode]]:
    if isinstance(elastic, Mapping):
        mode_map: dict[str, ElasticMode] = {
            key: val for key, val in elastic.items() if val != "full"
        }
    else:
        mode_map = {}
        for key in elastic:
            selected: ElasticMode = (
                elastic_modes.get(key, "left") if elastic_modes else "left"
            )
            if selected != "full":
                mode_map[key] = selected
    order = tuple(mode_map)
    if elastic_modes:
        for key, mode in elastic_modes.items():
            if mode != "full" and key in mode_map:
                mode_map[key] = mode
    index_modes: dict[int, ElasticMode] = {
        index: mode_map[header]
        for index, header in enumerate(headers)
        if header in mode_map
    }
    return order, index_modes


def format_table(
    rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    *,
    elastic: Sequence[str] | Mapping[str, ElasticMode] = (),
    elastic_modes: Mapping[str, ElasticMode] | None = None,
    width: int = GDR_DEFAULT_TERMINAL_WIDTH,
) -> str:
    """Format rows as a fixed-column ASCII table within a target width."""
    if not rows:
        return "(empty)\n"
    widths = _natural_widths(rows, headers)
    order, index_modes = _resolve_elastic(headers, elastic, elastic_modes)
    if _total_width(widths) > width and order:
        fitted = _shrink_to_fit(widths, headers, order, width)
        if fitted is not None:
            widths = fitted
    return _render_table(rows, headers, widths, index_modes)


# ---------------------------------------------------------------------------
# RTOS-neutral derived-value helpers.
# ---------------------------------------------------------------------------


def timer_expires_in(
    expiry: int | None,
    tick: int | None,
    tick_mask: int,
    *,
    in_overflow: bool = False,
    overdue: bool = True,
) -> str:
    """Render the wrap-safe remaining ticks until a timer expires.

    Current-list items carry the absolute expiry tick in the same epoch as
    the tick counter.  With ``overdue`` (the timer-list adapter) an item
    whose expiry already passed but is still linked means the daemon has not
    processed it yet, rendered as ``overdue`` instead of a huge unsigned wrap
    value; with ``overdue=False`` the wrapped unsigned subtraction stays the
    answer, matching ``(timeout - tick) & mask``.  Overflow-list items belong
    to the next tick epoch: their ``xItemValue`` wrapped, so the real expiry
    is ``2**bits + expiry``.
    """
    if expiry is None or tick is None:
        return "N/A"
    if in_overflow:
        return str(((tick_mask + 1) - tick) + expiry)
    if overdue and expiry < tick:
        return "overdue"
    return str((expiry - tick) & tick_mask)


def fill_watermark(
    stack: bytes | None,
    *,
    from_low: bool,
    word_bytes: int = 1,
    fill_byte: int = 0xA5,
) -> int | None:
    """Count untouched fill bytes at the low/high end of a stack window.

    Stacks are prefilled with a fill byte (0xa5 by default, configurable
    per adapter) under the watermarking macros;
    the high-water mark is the number of words never overwritten.  ``None``
    when the window is empty or its first byte is not the fill byte (the
    build never prefills, or the stack grew past its watermark).  The caller
    decides growth direction (``from_low``) and fill byte; only the count is
    shared.
    """
    if stack is None or not stack:
        return None
    marker = bytes([fill_byte])
    if from_low:
        if stack[0] != fill_byte:
            return None
        raw = len(stack.lstrip(marker))
    else:
        if stack[-1] != fill_byte:
            return None
        raw = len(stack.rstrip(marker))
    return (len(stack) - raw) // word_bytes


def waiter_cell(
    names: Sequence[str] | None,
    *,
    available: bool = True,
) -> str:
    """Render one waiter cell as ``count@name1,name2`` or ``N/A``.

    ``count`` always leads so truncation of long name lists drops *names*,
    never the diagnostic count; ``None`` or ``available=False`` renders the
    neutral ``N/A`` (a version without the wait list must not fabricate 0).
    """
    if not available or names is None:
        return "N/A"
    if not names:
        return "0"
    return f"{len(names)}@{','.join(names)}"
