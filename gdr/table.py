"""Width-aware ASCII table formatting.

Pure formatting logic with no GDB dependency so it can be unit-tested across
fixed terminal widths. Terminal probing lives in ``gdr.gdb_bridge`` and stays
separate from this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from io import StringIO

COLUMN_GAP = 2
DEFAULT_WIDTH = 120
MIN_ELASTIC_WIDTH = 5
TRUNCATION_SUFFIX = ".."

# Elastic column shrink priority: lower value shrinks first. "Callback" and
# "Entry" share one rank; ties shrink the currently-widest column first.
_ELASTIC_RANK: dict[str, int] = {
    "Waiters": 0,
    "RecvWait": 0,
    "SendWait": 0,
    "Callback": 1,
    "Entry": 1,
    "Owner": 2,
    "Name": 3,
}


def _natural_widths(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> list[int]:
    """Return each column's natural width (header and cell lengths)."""
    widths = [len(str(header)) for header in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    return widths


def _total_width(widths: Sequence[int]) -> int:
    """Return a table's full width for the given column widths."""
    return sum(widths) + COLUMN_GAP * (len(widths) - 1)


def _elastic_indexes(headers: Sequence[str], elastic: Sequence[str]) -> list[int]:
    """Map elastic header names to their column indexes."""
    return [i for i, header in enumerate(headers) if header in elastic]


def _shrink_to_fit(
    natural: Sequence[int],
    headers: Sequence[str],
    elastic: Sequence[str],
    width: int,
) -> list[int] | None:
    """Shrink elastic columns so the table fits ``width``.

    Returns the fitted widths, or ``None`` when even minimum elastic widths
    overflow the target (callers must fall back to the natural table).
    """
    indexes = _elastic_indexes(headers, elastic)
    min_widths = {i: max(len(str(headers[i])), MIN_ELASTIC_WIDTH) for i in indexes}
    widths = list(natural)
    if (
        _total_width(
            [
                min(
                    natural_width,
                    min_widths.get(i, natural_width),
                )
                for i, natural_width in enumerate(widths)
            ]
        )
        > width
    ):
        return None

    while _total_width(widths) > width:
        next_index = _next_shrink(widths, headers, indexes, min_widths)
        if next_index is None:
            return None
        widths[next_index] -= 1
    return widths


def _next_shrink(
    widths: Sequence[int],
    headers: Sequence[str],
    indexes: Sequence[int],
    min_widths: dict[int, int],
) -> int | None:
    """Return the elastic column to shrink next, or ``None`` when at minimum."""
    eligible = [i for i in indexes if widths[i] > min_widths[i]]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda i: (
            _ELASTIC_RANK.get(headers[i], len(_ELASTIC_RANK)),
            -widths[i],
            i,
        ),
    )


def _truncate_cell(text: str, width: int) -> str:
    """Two-dot right truncation: ``text[:width-2] + ".."``."""
    if len(text) <= width:
        return text
    return text[: width - 2] + TRUNCATION_SUFFIX


def _render(
    rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    widths: Sequence[int],
    elastic_indexes: set[int],
) -> str:
    """Render rows with the given column widths and elastic truncation."""
    out = StringIO()
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    out.write(f"{fmt.format(*headers)}\n")
    out.write(f"{'  '.join('-' * w for w in widths)}\n")
    for row in rows:
        cells = [str(cell) for cell in row]
        cells += [""] * (len(headers) - len(cells))
        for i in elastic_indexes:
            cells[i] = _truncate_cell(cells[i], widths[i])
        out.write(f"{fmt.format(*cells)}\n")
    return out.getvalue()


def format_table(
    rows: Sequence[Sequence[str]],
    headers: Sequence[str],
    *,
    elastic: Sequence[str] = (),
    width: int = DEFAULT_WIDTH,
) -> str:
    """Format *rows* as a width-aware ASCII table string.

    The column set is fixed by *headers*; only headers listed in *elastic*
    may shrink when the natural width exceeds *width*. When even minimum
    elastic widths overflow, the natural table is returned unchanged so the
    terminal may wrap it.

    Empty input produces ``"(empty)\\n"`` and a full table is returned as a
    single string so callers can write it with one ``gdb.write``.
    """
    if not rows:
        return "(empty)\n"
    widths = _natural_widths(rows, headers)
    if _total_width(widths) > width and elastic:
        fitted = _shrink_to_fit(widths, headers, elastic, width)
        if fitted is not None:
            widths = fitted
    return _render(rows, headers, widths, set(_elastic_indexes(headers, elastic)))
