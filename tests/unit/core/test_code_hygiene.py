"""Code-hygiene and module-structure checks across production packages."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_FREERTOS_DIR = _ROOT / "freertos"
_RTTHREAD_DIR = _ROOT / "rtthread"
_PRODUCTION_ROOTS = (_ROOT / "core", _FREERTOS_DIR, _RTTHREAD_DIR)

# Module-level error tuples that keep a locally-narrowed definition on
# purpose (all other _*_ERRORS must resolve to core.gdb_bridge.TARGET_ACCESS_ERRORS):
#   freertos.layout._PROBE_ERRORS           config probing (gdb.error + attr errors)
#   freertos.navigation._TRAVERSAL_ERRORS   list walks exclude AttributeError
#   freertos.navigation._COMMAND_ERRORS     gdb.execute probes (narrow)
#   core.layout._TYPE_ERRORS                printer type probing (narrow)
_KEEP_ERRORS_TUPLES = {
    "freertos/layout.py",
    "freertos/navigation.py",
    "core/layout.py",
}


def test_production_files_stay_within_the_size_limit():
    """The repository keeps production modules below the 1500-line limit."""
    oversized = [
        str(path.relative_to(_ROOT))
        for root in _PRODUCTION_ROOTS
        for path in root.rglob("*.py")
        if len(path.read_text().splitlines()) > 1500
    ]
    assert not oversized


# ---------------------------------------------------------------------------
# Sentinel hygiene: cells use N/A; "unavailable" only survives as the
# "unavailable: <reason>" verdict prefix (e.g. CrossCheck).
# ---------------------------------------------------------------------------


def test_unavailable_sentinel_only_as_a_verdict_prefix():
    """Bare ``"unavailable"`` cells must use ``N/A`` instead."""
    bare = re.compile(r'(?:= |return |else )"unavailable"(?:[^:.]|$)')
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.rglob("*.py"):
            for lineno, line in enumerate(source.read_text().splitlines(), 1):
                if bare.search(line):
                    raise AssertionError(
                        f"{source.relative_to(_ROOT)}:{lineno}: bare 'unavailable' sentinel; "
                        "use N/A for cells or 'unavailable: <reason>' for a verdict"
                    )


# ---------------------------------------------------------------------------
# Exception-delegation hygiene: expected target errors come from ONE shared
# tuple (TARGET_ACCESS_ERRORS), and bare ``except Exception:`` is reserved for
# the GDB tab-completion boundary.
# ---------------------------------------------------------------------------


def test_adapter_error_tuples_resolve_to_the_shared_set():
    """Adapters must not declare fresh module-level ``_*_ERRORS`` tuples."""
    pattern = re.compile(
        r"^_\w+ERRORS(?:: tuple\[type\[BaseException\], \.\.\.\])? = \("
    )
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.rglob("*.py"):
            rel = source.relative_to(_ROOT).as_posix()
            if rel in _KEEP_ERRORS_TUPLES:
                continue
            if pattern.search(source.read_text()):
                raise AssertionError(
                    f"{rel}: new module-level _*_ERRORS tuple; "
                    "resolve to core.gdb_bridge.TARGET_ACCESS_ERRORS instead"
                )


def test_no_bare_except_exception_outside_completion():
    """Bare ``except Exception`` is reserved for the GDB-completion boundary."""
    pattern = re.compile(r"except Exception:")
    completion_files = {"freertos/commands.py", "rtthread/commands.py"}
    for directory in (_FREERTOS_DIR, _RTTHREAD_DIR):
        for source in directory.rglob("*.py"):
            rel = source.relative_to(_ROOT).as_posix()
            if rel not in completion_files and pattern.search(source.read_text()):
                raise AssertionError(
                    f"{rel}: bare 'except Exception:'; use "
                    "TARGET_ACCESS_ERRORS or let the command guard report it"
                )


# ---------------------------------------------------------------------------
# Module declaration layout: data models belong before their consumers, and
# non-dunder constants must not trail after the last function or class.
# ---------------------------------------------------------------------------


def _check_module_layout(tree: ast.Module) -> list[str]:
    """Flag trailing data-only classes or non-dunder constants appended at EOF.

    Local lookup tables (dicts, enums, regexes) placed immediately above the
    function that uses them are allowed; constants or data-only classes
    appended after the last function/class in a module are smells.
    """
    issues: list[str] = []
    top = tree.body
    last_def = max(
        (
            i
            for i, node in enumerate(top)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
        default=-1,
    )
    last_decl = max(
        (
            i
            for i, node in enumerate(top)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        ),
        default=-1,
    )
    if last_decl >= 0:
        for node in top[last_decl + 1 :]:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            bad = [n for n in names if not (n.startswith("__") and n.endswith("__"))]
            if bad:
                issues.append(f"L{node.lineno}: trailing assignment {', '.join(bad)}")
    if last_def >= 0:
        issues.extend(
            f"L{node.lineno}: trailing data class {node.name}"
            for node in top[last_def + 1 :]
            if isinstance(node, ast.ClassDef)
            and not any(
                isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                for item in node.body
            )
        )
    return issues


def test_production_module_layout_avoids_trailing_models_and_constants():
    """Data models belong before their consumers; constants do not trail at EOF."""
    violations = [
        f"{path.relative_to(_ROOT)}:{issue}"
        for root in _PRODUCTION_ROOTS
        for path in sorted(root.rglob("*.py"))
        for issue in _check_module_layout(ast.parse(path.read_text(), str(path)))
    ]
    assert not violations, "module layout smell: " + "; ".join(violations)


def test_module_layout_checker_catches_trailing_class_and_constant_only():
    smelly = ast.parse(
        "def f():\n    return 1\n\nclass Model:\n    x: int = 1\n\n_CONST = 1\n"
    )
    issues = _check_module_layout(smelly)
    assert len(issues) == 2
    assert any("trailing data class Model" in item for item in issues)
    assert any("trailing assignment _CONST" in item for item in issues)

    clean = ast.parse(
        "class Model:\n    x: int = 1\n\n_LOCAL_MAP = {'a': 1}\n\n"
        "def f():\n    return _LOCAL_MAP\n\n__all__ = ['Model', 'f']\n"
    )
    assert _check_module_layout(clean) == []
