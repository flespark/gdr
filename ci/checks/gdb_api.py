#!/usr/bin/env python3
"""Verify GDR's GDB Python API surface and module loading inside a real GDB.

Collects all unguarded ``gdb.*`` attribute chains and ``from gdb...`` imports
from the runtime packages using Python's ``ast``, pairs them with essential
GDB built-in class member probes, and resolves every chain segment-by-segment
via ``getattr`` inside the target GDB's embedded Python interpreter before
sourcing ``gdr.py`` and importing every runtime module.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOTS: tuple[str, ...] = ("gdr.py", "core", "rtthread", "freertos")
MIN_EMBEDDED_PYTHON: tuple[int, int] = (3, 10)

# Methods and descriptors GDR calls on objects returned by GDB APIs. Because
# AST chains rooted at ``gdb`` stop at call boundaries (e.g.
# ``gdb.lookup_type(...).strip_typedefs()``), probing these attributes on the
# GDB built-in class objects closes that gap without needing a live target.
REQUIRED_CLASS_MEMBERS: dict[str, tuple[str, ...]] = {
    "gdb.Value": ("address", "cast", "dereference", "string", "type"),
    "gdb.Type": (
        "array",
        "code",
        "fields",
        "name",
        "pointer",
        "range",
        "sizeof",
        "strip_typedefs",
        "tag",
        "target",
        "unqualified",
    ),
    "gdb.Symbol": ("name", "needs_frame", "symtab", "type", "value"),
    "gdb.Frame": (
        "architecture",
        "is_valid",
        "name",
        "newer",
        "older",
        "pc",
        "read_register",
    ),
    "gdb.Inferior": ("architecture", "is_valid", "read_memory"),
    "gdb.Architecture": ("name", "void_type"),
    "gdb.Progspace": ("filename", "objfiles"),
    "gdb.Objfile": ("filename",),
    "gdb.Symtab": ("filename", "fullname"),
    "gdb.Symtab_and_line": ("line", "pc", "symtab"),
}


@dataclass(frozen=True)
class ApiUsage:
    """One static reference to a ``gdb`` attribute chain."""

    chain: str
    path: str
    lineno: int


def _iter_python_files(repo_root: Path, roots: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for entry in roots:
        target = repo_root / entry
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            files.extend(
                path
                for path in sorted(target.rglob("*.py"))
                if "__pycache__" not in path.parts
            )
    return files


def _extract_hasattr_gdb_guard(test: ast.expr) -> set[str]:
    """Return attribute names guarded by ``hasattr(gdb, "<name>")``."""
    guarded: set[str] = set()
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        for value in test.values:
            guarded.update(_extract_hasattr_gdb_guard(value))
        return guarded
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "hasattr"
        and len(test.args) == 2
        and isinstance(test.args[0], ast.Name)
        and test.args[0].id == "gdb"
        and isinstance(test.args[1], ast.Constant)
        and isinstance(test.args[1].value, str)
    ):
        guarded.add(test.args[1].value)
    return guarded


class _GdbApiVisitor(ast.NodeVisitor):
    """Collect maximal unguarded ``gdb.*`` attribute chains and imports."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.usages: list[ApiUsage] = []
        self._guarded_attrs: list[set[str]] = []

    def _is_guarded(self, first_attr: str) -> bool:
        return any(first_attr in frame for frame in self._guarded_attrs)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        guarded = _extract_hasattr_gdb_guard(node.test)
        if guarded:
            self._guarded_attrs.append(guarded)
        try:
            for stmt in node.body:
                self.visit(stmt)
        finally:
            if guarded:
                self._guarded_attrs.pop()
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0 or not node.module:
            return
        if node.module == "gdb" or node.module.startswith("gdb."):
            for alias in node.names:
                if alias.name == "*":
                    continue
                chain = f"{node.module}.{alias.name}"
                first = chain.split(".", 2)[1]
                if not self._is_guarded(first):
                    self.usages.append(ApiUsage(chain, self.rel_path, node.lineno))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name) and current.id == "gdb":
            ordered = list(reversed(parts))
            if not self._is_guarded(ordered[0]):
                self.usages.append(
                    ApiUsage("gdb." + ".".join(ordered), self.rel_path, node.lineno)
                )
            return
        # Continue walking into call receivers or subscript bases, e.g.
        # ``gdb.lookup_type("void").pointer()`` still visits ``gdb.lookup_type``.
        self.visit(current)


def collect_gdb_api_usage(
    repo_root: Path, roots: Iterable[str] = DEFAULT_ROOTS
) -> list[ApiUsage]:
    """Scan *roots* under *repo_root* and return all static ``gdb.*`` usages."""
    usages: list[ApiUsage] = []
    for path in _iter_python_files(repo_root, roots):
        rel_path = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
        visitor = _GdbApiVisitor(rel_path)
        visitor.visit(tree)
        usages.extend(visitor.usages)
    usages.sort(key=lambda item: (item.chain, item.path, item.lineno))
    return usages


def collect_runtime_modules(
    repo_root: Path, roots: Iterable[str] = DEFAULT_ROOTS
) -> list[str]:
    """Return importable package/module names under the runtime package roots."""
    modules: list[str] = []
    for entry in roots:
        target = repo_root / entry
        if not target.is_dir():
            continue
        for path in sorted(target.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo_root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                modules.append(".".join(parts))
    return sorted(set(modules))


def parse_gdb_major(version_text: str) -> int | None:
    """Extract the major GDB version from ``gdb.VERSION`` or ``gdb --version``."""
    match = re.search(r"\b(\d+)\.\d+", version_text)
    if match is None:
        return None
    return int(match.group(1))


def build_probe_payload(
    repo_root: Path,
    *,
    roots: Iterable[str] = DEFAULT_ROOTS,
    expected_major: int | None = None,
    min_major: int | None = None,
    verify_imports: bool = True,
) -> dict[str, object]:
    """Assemble the JSON payload consumed by the in-GDB probe script."""
    usages = collect_gdb_api_usage(repo_root, roots)
    grouped: dict[str, list[str]] = defaultdict(list)
    for usage in usages:
        grouped[usage.chain].append(f"{usage.path}:{usage.lineno}")
    class_chains = [
        f"{cls}.{member}"
        for cls, members in sorted(REQUIRED_CLASS_MEMBERS.items())
        for member in members
    ]
    return {
        "repo_root": str(repo_root.resolve()),
        "chains": dict(sorted(grouped.items())),
        "class_chains": class_chains,
        "modules": collect_runtime_modules(repo_root, roots) if verify_imports else [],
        "expected_major": expected_major,
        "min_major": min_major,
        "min_python": list(MIN_EMBEDDED_PYTHON),
    }


_IN_GDB_PROBE = r"""
import importlib
import json
import os
import re
import sys
import traceback
import gdb

payload_path = os.environ["GDR_GDB_API_PAYLOAD"]
result_path = os.environ["GDR_GDB_API_RESULT"]

with open(payload_path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)

def resolve_chain(chain):
    parts = chain.split(".")
    if not parts or parts[0] != "gdb":
        return False, parts[0] if parts else ""
    obj = gdb
    for attr in parts[1:]:
        if not hasattr(obj, attr):
            return False, attr
        obj = getattr(obj, attr)
    return True, None

missing = []
errors = []

gdb_version = str(getattr(gdb, "VERSION", "unknown"))
py_version = ".".join(map(str, sys.version_info[:3]))

min_py = tuple(payload["min_python"])
if sys.version_info[:2] < min_py:
    errors.append(
        f"embedded Python {py_version} is older than required {min_py[0]}.{min_py[1]}"
    )

match = re.search(r"\b(\d+)\.\d+", gdb_version)
gdb_major = int(match.group(1)) if match else None
expected_major = payload["expected_major"]
min_major = payload["min_major"]
if expected_major is not None and gdb_major != expected_major:
    errors.append(f"expected GDB major {expected_major}, found {gdb_version!r}")
if min_major is not None and (gdb_major is None or gdb_major < min_major):
    errors.append(f"expected GDB major >= {min_major}, found {gdb_version!r}")

for chain, sites in payload["chains"].items():
    ok, _failed_attr = resolve_chain(chain)
    if not ok:
        for site in sites:
            missing.append({"chain": chain, "site": site})

for chain in payload["class_chains"]:
    ok, _failed_attr = resolve_chain(chain)
    if not ok:
        missing.append({"chain": chain, "site": "gdb class probe"})

modules = payload["modules"]
if modules:
    repo_root = payload["repo_root"]
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        gdb.execute(f"source {os.path.join(repo_root, 'gdr.py')}")
        help_text = gdb.execute("gdr help", to_string=True)
        if "gdr init" not in help_text:
            errors.append("`gdr help` did not advertise `gdr init` after sourcing gdr.py")
    except Exception:
        errors.append("sourcing gdr.py failed:\n" + traceback.format_exc().strip())
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception:
            errors.append(f"import {mod} failed:\n" + traceback.format_exc().strip())

result = {
    "ok": not missing and not errors,
    "gdb_version": gdb_version,
    "python_version": py_version,
    "chain_count": len(payload["chains"]),
    "class_chain_count": len(payload["class_chains"]),
    "module_count": len(modules),
    "missing": missing,
    "errors": errors,
}
with open(result_path, "w", encoding="utf-8") as fh:
    json.dump(result, fh)
"""


def run_gdb_api_check(
    gdb_bin: str,
    repo_root: Path,
    *,
    roots: Iterable[str] = DEFAULT_ROOTS,
    expected_major: int | None = None,
    min_major: int | None = None,
    verify_imports: bool = True,
) -> tuple[bool, list[str]]:
    """Run the GDB API surface probe against *gdb_bin* and return ``(ok, lines)``."""
    payload = build_probe_payload(
        repo_root,
        roots=roots,
        expected_major=expected_major,
        min_major=min_major,
        verify_imports=verify_imports,
    )
    with tempfile.TemporaryDirectory(prefix="gdr-gdb-api-") as tmpdir:
        tmp = Path(tmpdir)
        payload_path = tmp / "payload.json"
        result_path = tmp / "result.json"
        probe_path = tmp / "probe.py"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        probe_path.write_text(_IN_GDB_PROBE, encoding="utf-8")

        env = dict(os.environ)
        env["GDR_GDB_API_PAYLOAD"] = str(payload_path)
        env["GDR_GDB_API_RESULT"] = str(result_path)
        env["LC_ALL"] = "C"

        proc = subprocess.run(
            [
                gdb_bin,
                "--nx",
                "--quiet",
                "--batch",
                "-ex",
                f"source {probe_path}",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        # NOTE: GDB `--batch` can exit 0 even when an embedded Python script
        # raises or Python support is absent. Require the explicit result file
        # written at the end of the probe instead of trusting returncode alone.
        if not result_path.exists():
            lines = [f"[gdr-ci] FAILED: GDB probe did not complete using {gdb_bin}"]
            if proc.stdout.strip():
                lines.append(proc.stdout.strip())
            if proc.stderr.strip():
                lines.append(proc.stderr.strip())
            return False, lines

        result = json.loads(result_path.read_text(encoding="utf-8"))
        lines: list[str] = []
        for item in result.get("missing", []):
            lines.append(f"MISSING {item['chain']} ({item['site']})")
        for err in result.get("errors", []):
            lines.append(f"[gdr-ci] ERROR: {err}")
        if result.get("ok") is True and proc.returncode == 0:
            lines.append(
                "[gdr-ci] GDB API surface OK "
                f"(gdb {result['gdb_version']}, python {result['python_version']}, "
                f"{result['chain_count']} chains, {result['class_chain_count']} class probes, "
                f"{result['module_count']} modules)"
            )
            return True, lines
        if not lines:
            lines.append(
                f"[gdr-ci] FAILED: {gdb_bin} exited with status {proc.returncode}"
            )
        return False, lines


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for standalone or CI execution."""
    parser = argparse.ArgumentParser(
        description="Verify GDR's static gdb.* API surface inside a real GDB."
    )
    parser.add_argument(
        "--gdb",
        default="gdb-multiarch",
        help="GDB executable to probe (default: gdb-multiarch)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root containing gdr.py, core/, rtthread/, freertos/",
    )
    parser.add_argument(
        "--expected-major",
        type=int,
        default=None,
        help="Require an exact GDB major version (e.g. 12)",
    )
    parser.add_argument(
        "--min-major",
        type=int,
        default=None,
        help="Require a minimum GDB major version (e.g. 16)",
    )
    parser.add_argument(
        "--no-imports",
        action="store_true",
        help="Skip sourcing gdr.py and importing runtime modules",
    )
    args = parser.parse_args(argv)
    ok, lines = run_gdb_api_check(
        args.gdb,
        args.repo_root,
        expected_major=args.expected_major,
        min_major=args.min_major,
        verify_imports=not args.no_imports,
    )
    stream = sys.stdout if ok else sys.stderr
    for line in lines:
        print(line, file=stream)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
