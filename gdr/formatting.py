"""Pure value and width-aware table formatting helpers."""

from __future__ import annotations

from collections.abc import Sequence
from io import StringIO

from .constants import (
    DEFAULT_TERMINAL_WIDTH,
    MIN_ELASTIC_COLUMN_WIDTH,
    TABLE_COLUMN_GAP,
    TABLE_TRUNCATION_SUFFIX,
)


def format_optional_int(value: int | None) -> str:
    """Render an optional integer without treating zero as unavailable."""
    return str(value) if value is not None else "N/A"


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
    return sum(widths) + TABLE_COLUMN_GAP * (len(widths) - 1)


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
        index: max(len(str(headers[index])), MIN_ELASTIC_COLUMN_WIDTH)
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


def _truncate_cell(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - len(TABLE_TRUNCATION_SUFFIX)] + TABLE_TRUNCATION_SUFFIX


def _render_table(
    rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    widths: Sequence[int],
    elastic_indexes: set[int],
) -> str:
    output = StringIO()
    row_format = "  ".join(f"{{:<{width}}}" for width in widths)
    output.write(f"{row_format.format(*headers)}\n")
    output.write(f"{'  '.join('-' * width for width in widths)}\n")
    for row in rows:
        cells = [str(cell) for cell in row]
        cells += [""] * (len(headers) - len(cells))
        for index in elastic_indexes:
            cells[index] = _truncate_cell(cells[index], widths[index])
        output.write(f"{row_format.format(*cells)}\n")
    return output.getvalue()


def format_table(
    rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    *,
    elastic: Sequence[str] = (),
    width: int = DEFAULT_TERMINAL_WIDTH,
) -> str:
    """Format rows as a fixed-column ASCII table within a target width."""
    if not rows:
        return "(empty)\n"
    widths = _natural_widths(rows, headers)
    if _total_width(widths) > width and elastic:
        fitted = _shrink_to_fit(widths, headers, elastic, width)
        if fitted is not None:
            widths = fitted
    return _render_table(
        rows,
        headers,
        widths,
        set(_elastic_indexes(headers, elastic)),
    )
