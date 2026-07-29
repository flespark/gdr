#!/usr/bin/env bash
# Publish an existing tag's source archives as a CNB Release.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 <release-tag> [archive-directory]" >&2
    exit 2
fi

if [[ -z "${CNB_TOKEN:-}" ]]; then
    echo "CNB_TOKEN must be available to publish a CNB Release" >&2
    exit 1
fi

for command in curl git node npx; do
    if ! command -v "$command" >/dev/null; then
        echo "required command is unavailable: $command" >&2
        exit 1
    fi
done

release_tag="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
archive_dir="${2:-dist}"
if [[ "$archive_dir" != /* ]]; then
    archive_dir="$repo_root/$archive_dir"
fi

shopt -s nullglob
archives=(
    "$archive_dir"/gdr-*-${release_tag}.tar.gz
    "$archive_dir"/gdr-*-${release_tag}.zip
    "$archive_dir"/gdr-*-latest.tar.gz
    "$archive_dir"/gdr-*-latest.zip
)
if [[ ${#archives[@]} -ne 4 ]]; then
    echo "expected one versioned and one latest archive in each format" >&2
    exit 1
fi

repository="flespark-2026/gdr"
target_commit="$(git -C "$repo_root" rev-parse --verify "${release_tag}^{commit}")"
release_notes="$($script_dir/extract-changelog.sh "$release_tag")"
cnb_cli=(npx --yes @cnbcool/cnb-cli@1.6.2)

release_json="$("${cnb_cli[@]}" releases post-release \
    --repo "$repository" \
    --name "GDR ${release_tag}" \
    --body "$release_notes" \
    --tag-name "$release_tag" \
    --target-commitish "$target_commit" \
    --make-latest true \
    --verbose)"
release_id="$(node -e '
    const response = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
    const id = response.data?.id;
    if (!id) {
        throw new Error("CNB did not return a release id");
    }
    process.stdout.write(id);
' <<<"$release_json")"

upload_asset() {
    local archive_path="$1"
    local asset_name
    local asset_size
    local upload_json
    local upload_url
    local upload_token
    local asset_path

    asset_name="$(basename "$archive_path")"
    asset_size="$(wc -c <"$archive_path" | tr -d '[:space:]')"
    upload_json="$("${cnb_cli[@]}" releases post-release-asset-upload-url \
        --repo "$repository" \
        --release-id "$release_id" \
        --asset-name "$asset_name" \
        --size "$asset_size" \
        --overwrite \
        --verbose)"
    IFS=$'\t' read -r upload_url upload_token asset_path < <(
        node -e '
            const response = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
            const data = response.data ?? {};
            const verificationUrl = new URL(data.verify_url);
            const token = verificationUrl.searchParams.get("upload_token");
            const path = verificationUrl.searchParams.get("asset_path");
            if (!data.upload_url || !token || !path) {
                throw new Error("CNB returned incomplete asset upload metadata");
            }
            process.stdout.write([data.upload_url, token, path].join("\t") + "\n");
        ' <<<"$upload_json"
    )

    curl --fail --silent --show-error --upload-file "$archive_path" "$upload_url"
    "${cnb_cli[@]}" releases post-release-asset-upload-confirmation \
        --repo "$repository" \
        --release-id "$release_id" \
        --upload-token "$upload_token" \
        --asset-path "$asset_path" \
        --ttl 0 >/dev/null
}

for archive in "${archives[@]}"; do
    upload_asset "$archive"
done
printf 'published CNB Release %s with source archives\n' "$release_tag"
