#!/usr/bin/env bash
# Build the release VSIX from the checked-out tag.
#
# The extension version tracks GDR's own year.serial release (GDR `2026.4`
# ships as extension `2026.4.0`), so the file name comes from
# `vscode/package.json`, never from the tag string: a tag cannot spell the
# semver patch.  The tag still has to agree with the extension version, which
# this script checks after normalizing both sides (`2026.04` and `2026.4`
# name the same release).  `ci/check-vendored-schema.py` separately enforces
# that the vendored runtime and its envelope schema match this checkout.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 <release-tag> [output-directory]" >&2
    exit 2
fi

release_tag="$1"
if [[ ! "$release_tag" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
    echo "release tag may contain only letters, digits, '.', '_', and '-'" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
output_dir="dist"
if [[ $# -eq 2 ]]; then
    output_dir="$2"
fi
if [[ "$output_dir" != /* ]]; then
    output_dir="$repo_root/$output_dir"
fi

# Reason: release tags pad the month (`2026.04`) while semver does not
# (`2026.4.0`); strip leading zeros per component before comparing.
normalize_version() {
    local normalized=""
    local part=""
    IFS='.' read -ra parts <<<"$1"
    for part in "${parts[@]}"; do
        part="$(printf '%s' "$part" | sed 's/^0*\([0-9]\)/\1/')"
        normalized="${normalized:+$normalized.}$part"
    done
    printf '%s' "$normalized"
}

extension_version="$(node -e 'process.stdout.write(require(process.argv[1]).version)' \
    "$repo_root/vscode/package.json")"
if [[ -z "$extension_version" ]]; then
    echo "build-vsix: could not read the extension version" >&2
    exit 1
fi

normalized_tag="$(normalize_version "$release_tag")"
if [[ "$extension_version" != "$normalized_tag" &&
    "$extension_version" != "$normalized_tag".* ]]; then
    echo "build-vsix: extension $extension_version does not track release $release_tag" >&2
    exit 1
fi

vsix_name="gdb-rtos-views-${extension_version}.vsix"
vsix_path="$output_dir/$vsix_name"
if [[ -e "$vsix_path" ]]; then
    echo "refusing to overwrite existing VSIX: $vsix_path" >&2
    exit 1
fi

mkdir -p "$output_dir"
(
    cd "$repo_root/vscode"
    npm ci --no-audit --no-fund
    npm run vendor
    python3 ../ci/check-vendored-schema.py .
    npm run build
    npx vsce package --no-dependencies --out "$vsix_path"
)

printf 'created %s\n' "$vsix_path"
