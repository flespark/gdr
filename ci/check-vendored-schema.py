#!/usr/bin/env python3
"""Verify the extension's vendored GDR copy matches this runtime.

``vscode/scripts/vendor-gdr.sh`` copies the runtime into
``vscode/resources/gdr`` and stamps it with the GDR version and the envelope
schema.  The extension parses that envelope, so both numbers must match the
sources they came from: a bundle built from another revision would make the
client mis-render data instead of failing loudly.

Usage::

    ci/check-vendored-schema.py [<vscode-directory>]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_VERSION_RE = re.compile(r'^__version__ = "(.*)"$', re.MULTILINE)
_SCHEMA_RE = re.compile(r"^GDR_API_SCHEMA_VERSION = ([0-9]+)$", re.MULTILINE)


def _read_version(init_source: str) -> str | None:
    match = _VERSION_RE.search(init_source)
    return match.group(1) if match is not None else None


def _read_schema(constants_source: str) -> int | None:
    match = _SCHEMA_RE.search(constants_source)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def main(argv: list[str]) -> int:
    """Compare the vendored manifest against the runtime it was copied from."""
    vscode_dir = Path(argv[1] if len(argv) > 1 else "vscode").resolve()
    repo_root = vscode_dir.parent

    manifest_path = vscode_dir / "resources" / "gdr" / "VERSION"
    if not manifest_path.is_file():
        print(
            f"check-vendored-schema: no manifest at {manifest_path}; "
            "run `npm run vendor` in the extension directory",
            file=sys.stderr,
        )
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"check-vendored-schema: unreadable manifest: {exc}", file=sys.stderr)
        return 1

    version = _read_version((repo_root / "gdr" / "__init__.py").read_text("utf-8"))
    schema = _read_schema((repo_root / "gdr" / "constants.py").read_text("utf-8"))
    if version is None or schema is None:
        print(
            "check-vendored-schema: could not read the runtime version or schema",
            file=sys.stderr,
        )
        return 1

    mismatches: list[str] = []
    if manifest.get("version") != version:
        mismatches.append(f"version {manifest.get('version')} != runtime {version}")
    if manifest.get("schema_version") != schema:
        mismatches.append(
            f"schema {manifest.get('schema_version')} != runtime {schema}"
        )
    if mismatches:
        for mismatch in mismatches:
            print(f"check-vendored-schema: {mismatch}", file=sys.stderr)
        return 1

    print(f"check-vendored-schema: ok (GDR {version}, schema {schema})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
