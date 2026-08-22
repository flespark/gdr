#!/usr/bin/env bash
# Create runtime archives from an exact Git reference.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <release-tag> [output-directory]" >&2
    exit 2
fi

release_tag="$1"
shift
if [[ ! "$release_tag" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
    echo "release tag may contain only letters, digits, '.', '_', and '-'" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
output_dir="dist"
if [[ $# -gt 0 && "$1" != --* ]]; then
    output_dir="$1"
    shift
fi
if [[ $# -gt 0 ]]; then
    echo "unknown option: $1" >&2
    exit 2
fi

if [[ "$output_dir" != /* ]]; then
    output_dir="$repo_root/$output_dir"
fi

commit="$(git -C "$repo_root" rev-parse --verify "${release_tag}^{commit}")"
archive_paths=(LICENSE gdr.py gdr rtthread freertos)
for source_path in "${archive_paths[@]}"; do
    if ! git -C "$repo_root" cat-file -e "${commit}:${source_path}"; then
        echo "release reference is missing required runtime path: $source_path" >&2
        exit 1
    fi
done

archive_stem="gdr"
archive_prefix="${archive_stem}/"
versioned_tarball="$output_dir/${archive_stem}-${release_tag}.tar.gz"
versioned_zipfile="$output_dir/${archive_stem}-${release_tag}.zip"
latest_tarball="$output_dir/${archive_stem}-latest.tar.gz"
latest_zipfile="$output_dir/${archive_stem}-latest.zip"

mkdir -p "$output_dir"
for archive in "$versioned_tarball" "$versioned_zipfile" "$latest_tarball" "$latest_zipfile"; do
    if [[ -e "$archive" ]]; then
        echo "refusing to overwrite existing archive: $archive" >&2
        exit 1
    fi
done

# Reason: one runtime archive keeps all supported adapters available to users
# without exposing CI, tests, fixtures, or other repository-only files.
git -C "$repo_root" archive --format=tar.gz --prefix="$archive_prefix" "$commit" \
    "${archive_paths[@]}" >"$versioned_tarball"
git -C "$repo_root" archive --format=zip --prefix="$archive_prefix" "$commit" \
    "${archive_paths[@]}" >"$versioned_zipfile"
cp "$versioned_tarball" "$latest_tarball"
cp "$versioned_zipfile" "$latest_zipfile"

printf 'created %s\ncreated %s\ncreated %s\ncreated %s\n' \
    "$versioned_tarball" "$versioned_zipfile" "$latest_tarball" "$latest_zipfile"
