#!/usr/bin/env bash
# Build the release VSIX from the checked-out tree.
#
# The extension version tracks GDR's own year.serial release (GDR `2026.4`
# ships as extension `2026.4.0`), so the file name comes from
# `ci/vsix-artifact-name.sh`, never from the tag string: a tag cannot spell
# the semver patch. A tag release always ships patch `0`; an extension-only
# fix (packaging or UI, same runtime) bumps the patch and is built with
# `--patch` instead, then attached to the existing release by hand or by the
# release workflow's manual dispatch. `ci/check-vendored-schema.py` separately
# enforces that the vendored runtime and its envelope schema match this
# checkout.
set -euo pipefail

patch_release=0
if [[ "${1:-}" == "--patch" ]]; then
    patch_release=1
    shift
fi
if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 [--patch] <release-tag> [output-directory]" >&2
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
if [[ ! "$extension_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "build-vsix: package.json declares no semver version" >&2
    exit 1
fi

normalized_tag="$(normalize_version "$release_tag")"
if [[ "$patch_release" -eq 1 ]]; then
    # Reason: a patch VSIX reuses the release tag (the runtime is unchanged)
    # and says so in its own patch number; anything else is a mistagged tree.
    if [[ "$extension_version" != "$normalized_tag".* ||
        "$extension_version" == "$normalized_tag".0 ]]; then
        echo "build-vsix: --patch needs extension ${normalized_tag}.<nonzero-patch>," \
            "got $extension_version" >&2
        exit 1
    fi
elif [[ "$extension_version" != "$normalized_tag".0 ]]; then
    # Reason: tag releases always ship patch 0; a nonzero patch means an
    # extension-only fix is in flight and must go out through --patch (or the
    # workflow dispatch), never as the release's main artifact.
    echo "build-vsix: release $release_tag needs extension ${normalized_tag}.0," \
        "got $extension_version (use --patch for an extension-only fix)" >&2
    exit 1
fi

vsix_path="$("$script_dir/vsix-artifact-name.sh" "$output_dir")"
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
