#!/usr/bin/env bash
# print the exact release VSIX path for this checkout.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
output_dir="dist"
if [[ $# -gt 1 ]]; then
    echo "usage: $0 [output-directory]" >&2
    exit 2
fi
if [[ $# -eq 1 ]]; then
    output_dir="$1"
fi
if [[ "$output_dir" != /* ]]; then
    output_dir="$repo_root/$output_dir"
fi

extension_version="$(node -e 'process.stdout.write(require(process.argv[1]).version)' \
    "$repo_root/vscode/package.json")"
if [[ ! "$extension_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "vsix-artifact-name: package.json declares no semver version" >&2
    exit 1
fi

printf '%s\n' "$output_dir/gdb-rtos-views-${extension_version}.vsix"
