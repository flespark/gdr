#!/usr/bin/env bash
# Bundle the GDR runtime into this extension.
#
# The plugin parses GDR's envelope, so the two must travel together: the copy
# under ``resources/gdr`` is generated from the repository's own runtime (never
# edited in place) and stamped with the version and envelope schema the client
# was built for.  ``src/runtime.ts`` refuses to run against a different schema.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
vscode_dir="$(cd "$script_dir/.." && pwd)"
repo_root="$(cd "$vscode_dir/.." && pwd)"
destination="${1:-$vscode_dir/resources/gdr}"

if [[ ! -f "$repo_root/gdr.py" ]]; then
    echo "vendor-gdr: $repo_root is not a GDR checkout (gdr.py is missing)" >&2
    exit 1
fi

runtime_paths=(gdr.py gdr rtthread freertos LICENSE)
for path in "${runtime_paths[@]}"; do
    if [[ ! -e "$repo_root/$path" ]]; then
        echo "vendor-gdr: required runtime path is missing: $path" >&2
        exit 1
    fi
done

version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$repo_root/gdr/__init__.py")"
schema="$(sed -n 's/^GDR_API_SCHEMA_VERSION = \([0-9][0-9]*\)$/\1/p' "$repo_root/gdr/constants.py")"
if [[ -z "$version" || -z "$schema" ]]; then
    echo "vendor-gdr: could not read the runtime version or the envelope schema" >&2
    exit 1
fi

rm -rf "$destination"
mkdir -p "$destination"
for path in "${runtime_paths[@]}"; do
    cp -R "$repo_root/$path" "$destination/"
done
# Reason: byte-compiled caches are host- and interpreter-specific; shipping one
# could shadow a stale module inside GDB's own interpreter.
find "$destination" -name '__pycache__' -type d -prune -exec rm -rf {} +

printf '{"version": "%s", "schema_version": %s}\n' "$version" "$schema" >"$destination/VERSION"
echo "vendor-gdr: bundled GDR $version (envelope schema $schema) into $destination"
