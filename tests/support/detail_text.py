"""Reassemble ``<rtos> <kind> <name>`` CLI detail text for assertions.

``print_detail`` writes one ``Key: Value`` line per pair with the keys
right-aligned, but a value may itself contain newlines (the FreeRTOS timer
command table, for example).  A test that compares the terminal output with
the lossless ``gdr api detail`` pairs therefore has to rebuild those values
from the API's own key list instead of splitting on the first ``": "``, which
would silently compare only a value's first line.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def parse_detail(text: str, keys: Sequence[str]) -> dict[str, str]:
    """Return the detail values of *text*, keyed by the API's pair keys.

    A line starts a new value when it begins with one of *keys*; every other
    line (including an empty one) extends the value that is currently open.
    Longest-first matching keeps a key that prefixes another key from winning.
    """
    anchors = sorted(keys, key=len, reverse=True)
    parsed: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        for anchor in anchors:
            prefix = f"{anchor}: "
            if stripped.startswith(prefix):
                current = anchor
                parsed[anchor] = stripped[len(prefix) :]
                break
        else:
            if current is not None:
                parsed[current] += "\n" + line
    return parsed


def _normalize(value: str) -> str:
    """A value with per-line trailing padding removed.

    Reason: a terminal is not a lossless carrier for right-padded columns --
    GDB drops the padding that follows the last column of the first line --
    while the API keeps it.  Every content-bearing difference (order, text,
    line breaks) survives the comparison.
    """
    return "\n".join(line.rstrip() for line in value.split("\n"))


def assert_detail_matches(text: str, pairs: Mapping[str, str]) -> None:
    """Assert the CLI text reproduces the API's pairs."""
    parsed = parse_detail(text, list(pairs))
    expected = dict(pairs)
    differing = {
        key: (_normalize(parsed[key]), _normalize(expected[key]))
        for key in dict.fromkeys([*expected, *parsed])
        if key in parsed
        and key in expected
        and _normalize(parsed[key]) != _normalize(expected[key])
    }
    missing = {key for key in expected if key not in parsed} | {
        key for key in parsed if key not in expected
    }
    if differing or missing:
        raise AssertionError(
            f"CLI detail differs from the API pairs:\n{differing!r}\n"
            f"missing/extra keys: {sorted(missing)!r}"
        )
