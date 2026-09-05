"""Regression tests for the RTOS-agnostic core boundary."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[3] / "gdr"
_RTTHREAD_TERMS = re.compile(
    r"\b(?:rt-?thread|rtthread|rt_[a-z0-9_]+)\b", re.IGNORECASE
)
# FreeRTOS-specific identifiers and symbols must also stay out of the core.
# ``tcb`` alone is excluded: it is too generic to prove coupling, and the
# FreeRTOS adapter already owns the struct by its ``struct tskTaskControlBlock``
# name (matched below via the ``tsk`` prefix term).
# Reason: the ``tsk`` prefix and the kernel ``*_t`` typedefs are matched with an
# identifier tail rather than a bare ``\b``, so long names such as
# ``tskKERNEL_VERSION_NUMBER`` are caught and not only 4-character words.
_FREERTOS_TERMS = re.compile(
    r"\b(?:free-?rtos|freertos|frt_[a-z0-9_]+|tsk[A-Za-z0-9_]+"
    r"|Queue_t|TCB_t|List_t|ListItem_t|MiniListItem_t|StreamBuffer_t)\b",
    re.IGNORECASE,
)


def test_core_source_contains_no_rtthread_coupling():
    """Target names, symbols, and types must remain in the adapter package."""
    violations = []
    for source in _CORE_DIR.glob("*.py"):
        match = _RTTHREAD_TERMS.search(source.read_text())
        if match is not None:
            violations.append(f"{source.name}: {match.group(0)}")

    assert not violations, "RT-Thread coupling leaked into gdr/: " + ", ".join(
        violations
    )


def test_core_source_contains_no_freertos_coupling():
    """FreeRTOS symbol/type names must remain in the adapter package."""
    violations = []
    for source in _CORE_DIR.glob("*.py"):
        match = _FREERTOS_TERMS.search(source.read_text())
        if match is not None:
            violations.append(f"{source.name}: {match.group(0)}")

    assert not violations, "FreeRTOS coupling leaked into gdr/: " + ", ".join(
        violations
    )


def test_freertos_term_pattern_catches_long_identifiers():
    """The FreeRTOS term pattern must not silently match nothing.

    A trailing ``\\b`` after a fixed-width class (``tsk[A-Z]``) only matches
    4-character words, which would let ``tskKERNEL_VERSION_NUMBER`` or
    ``tskTaskControlBlock`` leak into the core unnoticed.
    """
    for term in (
        "tskTaskControlBlock",
        "tskKERNEL_VERSION_NUMBER",
        "FreeRTOS",
        "Queue_t",
        "TCB_t",
    ):
        assert _FREERTOS_TERMS.search(f"a {term} b") is not None, term
    for benign in ("task_count", "structs", "list_items"):
        assert _FREERTOS_TERMS.search(benign) is None, benign


def test_core_never_imports_an_rtos_adapter_package():
    """Only the composition root may select RT-Thread or FreeRTOS."""
    violations = []
    for source in _CORE_DIR.glob("*.py"):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.Import or ast.ImportFrom)):
                lineno = node.lineno
            else:
                continue
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif node.module:
                names = [node.module]
            violations.extend(
                f"{source.name}:{lineno}: {name}"
                for name in names
                if name.split(".", 1)[0] in {"rtthread", "freertos"}
            )
    assert not violations, "RTOS adapter imported by gdr/: " + ", ".join(violations)


def test_production_files_stay_within_the_size_limit():
    """The repository keeps production modules below the 1500-line limit."""
    roots = [
        Path(__file__).resolve().parents[3] / "gdr",
        Path(__file__).resolve().parents[3] / "rtthread",
        Path(__file__).resolve().parents[3] / "freertos",
    ]
    oversized = [
        str(path.relative_to(roots[0].parent))
        for root in roots
        for path in root.glob("*.py")
        if len(path.read_text().splitlines()) > 1500
    ]
    assert not oversized


# ---------------------------------------------------------------------------
# Adapter-internal coupling: kernel ABI names live in the layout, and raw
# member traversal is layout-driven (read_field), never inline read_path.
# ---------------------------------------------------------------------------

_FREERTOS_DIR = Path(__file__).resolve().parents[3] / "freertos"
_RTTHREAD_DIR = Path(__file__).resolve().parents[3] / "rtthread"
# Domain modules that legitimately own their kernel subset's symbol/type
# names (mirroring rtthread.layout's heap probes and freertos.layout's
# object ABI): heap keeps the allocator ABI, timers/streams keep their own
# globals and type probes, version keeps the version policy symbols.
_FREERTOS_DOMAIN_FILES = {
    "layout.py",
    "heap.py",
    "timers.py",
    "streams.py",
    "version.py",
}
# rtthread has no separate heap module: the system-heap symbol subset
# (system_heap/heap_ptr/...) lives in diagnostics.py, which owns its heap
# domain like freertos/heap.py owns the allocator ABI.
_RTTHREAD_DOMAIN_FILES = {
    "layout.py",
    "navigation.py",
    "version.py",
    "diagnostics.py",
}
_RAW_SYMBOL_TERMS = re.compile(r'(?:lookup_symbol|lookup_type|symbol_exists)\("')


def _collect_violations(directory: Path, skip: set[str], pattern) -> list[str]:
    violations = []
    for source in directory.glob("*.py"):
        if source.name in skip:
            continue
        match = pattern.search(source.read_text())
        if match is not None:
            violations.append(f"{source.name}: {match.group(0)}")
    return violations


def test_freertos_consumers_never_use_inline_read_path():
    """Member traversal goes through the layout (read_field), never read_path.

    The 46 inline ``read_path(value, ("uxLength",))`` tuples that duplicated
    layout-declared paths are gone; a regression here would re-introduce raw
    kernel member spellings in consumers.
    """
    violations = _collect_violations(
        _FREERTOS_DIR, {"layout.py"}, re.compile(r"read_path\(")
    )
    assert not violations, "read_path leaked into freertos consumers: " + ", ".join(
        violations
    )


def test_rtthread_consumers_never_use_raw_traversal():
    """RT-Thread adapter code never spells raw member paths either."""
    violations = _collect_violations(_RTTHREAD_DIR, set(), re.compile(r"read_path\("))
    assert not violations, "read_path leaked into rtthread: " + ", ".join(violations)


def test_freertos_domain_modules_own_their_symbols():
    """Cross-domain consumers resolve symbols/types through the layout.

    adapter/details/events/navigation/diagnostics/commands are consumers:
    they must not hard-code ``lookup_symbol("xQueue")``-style spellings (the
    layout owns the object ABI; heap/timers/streams/version own their own
    subset).  This keeps one lookup table per domain instead of a copy per
    consumer.
    """
    violations = _collect_violations(
        _FREERTOS_DIR, _FREERTOS_DOMAIN_FILES, _RAW_SYMBOL_TERMS
    )
    assert not violations, (
        "raw symbol/type literals in cross-domain consumers: " + ", ".join(violations)
    )


def test_rtthread_consumers_own_only_domain_symbols():
    """rtthread layout/navigation/version own their subset; adapter must not."""
    violations = _collect_violations(
        _RTTHREAD_DIR, _RTTHREAD_DOMAIN_FILES, _RAW_SYMBOL_TERMS
    )
    assert not violations, (
        "raw symbol/type literals in rtthread consumers: " + ", ".join(violations)
    )


def test_no_raw_gdb_type_lookup_outside_the_core():
    """Adapter casts go through gdr.layout.value_at, never gdb.lookup_type.

    The 7 hand-rolled ``gdb.lookup_type(...).pointer()`` casts were
    consolidated into :func:`gdr.layout.value_at`; a raw cast in an adapter
    would bypass the layout-described type name and its error handling.
    """
    violations = []
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.glob("*.py"):
            match = re.search(r"gdb\.lookup_type\(", source.read_text())
            if match is not None:
                violations.append(f"{source.name}: {match.group(0)}")
    assert not violations, "raw gdb.lookup_type outside gdr/: " + ", ".join(violations)


# ---------------------------------------------------------------------------
# Sentinel hygiene: cells use N/A; "unavailable" only survives as the
# "unavailable: <reason>" verdict prefix (e.g. CrossCheck).
# ---------------------------------------------------------------------------


def test_unavailable_sentinel_only_as_a_verdict_prefix():
    """Bare ``"unavailable"`` cells were unified to ``N/A``.

    A bare ``"unavailable"`` (an empty reason) is a second sentinel for the
    same "no value" fact and kept drifting from ``format_optional_int``'s
    ``N/A``; only the verdict prefix ``"unavailable: <reason>"`` remains,
    where the word is a sentence fragment, not a missing-value cell.
    """
    # Match the *sentinel literal* only: a bare "unavailable" used as an
    # output value (assignment, return, ternary).  ``.startswith(...)`` and
    # the ``"unavailable:"`` prefix remain verdict logic, not sentinels.
    bare = re.compile(r'(?:= |return |else )"unavailable"(?:[^:.]|$)')
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.glob("*.py"):
            text = source.read_text()
            for lineno, line in enumerate(text.splitlines(), 1):
                if bare.search(line):
                    raise AssertionError(
                        f"{source.name}:{lineno}: bare 'unavailable' sentinel; "
                        "use N/A for cells or 'unavailable: <reason>' for a verdict"
                    )


# ---------------------------------------------------------------------------
# Exception-delegation hygiene: expected target errors come from ONE shared
# tuple (TARGET_ACCESS_ERRORS); adapters never re-declare near-identical
# module-level tuples, never catch bare Exception (except the two sanctioned
# GDB-completion boundaries), and never hand-roll raw DWARF attribute reads.
# ---------------------------------------------------------------------------

# Module-level error tuples that keep a locally-narrowed definition on
# purpose (all other _*_ERRORS must resolve to gdr.gdb_bridge.TARGET_ACCESS_ERRORS):
#   freertos.layout._PROBE_ERRORS    config probing (gdb.error + attr errors)
#   freertos.navigation._TRAVERSAL_ERRORS   list walks deliberately exclude
#                                           AttributeError so a programming
#                                           error bubbles to the guard
#   freertos.navigation._COMMAND_ERRORS     gdb.execute probes (narrow)
#   gdr.printers._TYPE_ERRORS               printer type probing (narrow)
_KEEP_ERRORS_TUPLES = {
    "freertos/layout.py",
    "freertos/navigation.py",
    "gdr/printers.py",
}
# Member-name-lookup needs DWARF type / field objects (not just names), so
# freertos.layout.py does its own .fields() traversal inside detect_config /
# _mini_list_detected, and gdr.layout.py computes member offsets.
_KEEP_DWARF_ACCESS = {
    "freertos/layout.py",
    "gdr/layout.py",
}


def test_adapter_error_tuples_resolve_to_the_shared_set():
    """Adapters must not declare fresh module-level ``_*_ERRORS`` tuples.

    The four modules in ``_KEEP_ERRORS_TUPLES`` narrow deliberately; every
    other freertos/rtthread module resolves expected target errors through
    ``gdr.gdb_bridge.TARGET_ACCESS_ERRORS`` (one definition, one gdb-None
    fallback) instead of copying the near-identical tuple into yet another
    file as each patch historically did.
    """
    pattern = re.compile(
        r"^_\w+ERRORS(?:: tuple\[type\[BaseException\], \.\.\.\])? = \("
    )
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.glob("*.py"):
            rel = source.as_posix().removeprefix(
                str(_FREERTOS_DIR.parent).rstrip("/") + "/"
            )
            if rel in _KEEP_ERRORS_TUPLES:
                continue
            text = source.read_text()
            if pattern.search(text):
                raise AssertionError(
                    f"{source.name}: new module-level _*_ERRORS tuple; "
                    "resolve to gdr.gdb_bridge.TARGET_ACCESS_ERRORS instead"
                )


def test_no_bare_except_exception_outside_completion():
    """Bare ``except Exception`` is reserved for the GDB-completion boundary.

    ``_object_names`` in both command trees must never print or raise while
    completing inside GDB, so it degrades to no candidates on anything.
    Everywhere else an expected target/runtime failure either resolves to
    ``TARGET_ACCESS_ERRORS`` (probe helpers, best-effort sections) or
    bubbles to a ``gdb_command_guard`` for a full diagnostic.
    """
    pattern = re.compile(r"except Exception:")
    completion_files = {"freertos/commands.py", "rtthread/commands.py"}
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.glob("*.py"):
            rel = source.as_posix().removeprefix(
                str(_FREERTOS_DIR.parent).rstrip("/") + "/"
            )
            if rel not in completion_files and pattern.search(source.read_text()):
                raise AssertionError(
                    f"{source.name}: bare 'except Exception:'; use "
                    "TARGET_ACCESS_ERRORS or let the command guard report it"
                )


def test_no_raw_dwarf_attribute_access_outside_whitelist():
    """Raw ``.sizeof`` / ``.range()[1]`` / ``.fields()`` live in the bridge.

    freertos.layout.py's own ``.fields()`` traversal (member-name lookup in
    detect_config / _mini_list_detected) and gdr.layout.py's offset math need
    the DWARF field objects, so they are whitelisted; every other adapter
    reads type size / array bounds / member names through the bridge
    primitives ``type_size`` / ``array_bound`` / ``type_field_names``.
    """
    pattern = re.compile(r"\.sizeof\b|\.range\(\)\[1\]|\.fields\(\)")
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.glob("*.py"):
            rel = source.as_posix().removeprefix(
                str(_FREERTOS_DIR.parent).rstrip("/") + "/"
            )
            if rel in _KEEP_DWARF_ACCESS:
                continue
            if pattern.search(source.read_text()):
                raise AssertionError(
                    f"{source.name}: raw DWARF attribute access; use "
                    "gdr.gdb_bridge.type_size / array_bound / type_field_names"
                )
