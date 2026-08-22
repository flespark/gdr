#!/usr/bin/env bash
# Publish an existing tag's source archives as a CNB Release.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 <release-tag> [archive-directory]" >&2
    exit 2
fi

if [[ -z "${CNB_TOKEN:-}" ]]; then
    echo "CNB_TOKEN with repo-code:r and repo-release:rw must be available to publish a CNB Release" >&2
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
    "$archive_dir"/gdr-${release_tag}.tar.gz
    "$archive_dir"/gdr-${release_tag}.zip
)
# Reason: CNB release URLs already scope assets by release tag; upload stable
# names so download commands do not need to include a release version.
if [[ ${#archives[@]} -ne 2 ]]; then
    echo "expected one versioned archive in each format" >&2
    exit 1
fi

repository="flespark-2026/gdr"
target_commit="$(git -C "$repo_root" rev-parse --verify "${release_tag}^{commit}")"
release_notes="$($script_dir/extract-changelog.sh "$release_tag")"
cnb_cli=(npx --yes @cnbcool/cnb-cli@1.6.2)

existing_release_json="$("${cnb_cli[@]}" releases get-release-by-tag \
    --repo "$repository" \
    --tag "$release_tag" \
    --verbose)"
# Reason: cnb-cli returns HTTP error payloads on stdout without a non-zero exit status.
release_id="$(node -e '
    const response = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
    const status = Number(response.status);
    const data = response.data ?? {};
    if (status === 404) {
        process.exit(0);
    }
    if (!Number.isInteger(status) || status < 200 || status >= 300 || !data.id) {
        const detail = [data.errcode, data.errmsg].filter(Boolean).join(": ");
        throw new Error(
            `CNB release lookup failed with HTTP ${response.status ?? "unknown"}${
                detail ? ` (${detail})` : ""
            }`,
        );
    }
    process.stdout.write(data.id);
' <<<"$existing_release_json")"

if [[ -z "$release_id" ]]; then
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
        const status = Number(response.status);
        const data = response.data ?? {};
        if (!Number.isInteger(status) || status < 200 || status >= 300 || !data.id) {
            const detail = [data.errcode, data.errmsg].filter(Boolean).join(": ");
            throw new Error(
                `CNB release creation failed with HTTP ${response.status ?? "unknown"}${
                    detail ? ` (${detail})` : ""
                }`,
            );
        }
        process.stdout.write(data.id);
    ' <<<"$release_json")"
fi

upload_asset() {
    local archive_path="$1"
    local asset_name
    local asset_size
    local upload_json
    local upload_url
    local upload_token
    local asset_path

    asset_name="$(basename "$archive_path")"
    case "$asset_name" in
        *.tar.gz)
            asset_name="${asset_name%-$release_tag.tar.gz}.tar.gz"
            ;;
        *.zip)
            asset_name="${asset_name%-$release_tag.zip}.zip"
            ;;
    esac
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
            const status = Number(response.status);
            const data = response.data ?? {};
            if (!Number.isInteger(status) || status < 200 || status >= 300) {
                const detail = [data.errcode, data.errmsg].filter(Boolean).join(": ");
                throw new Error(
                    `CNB release asset upload request failed with HTTP ${response.status ?? "unknown"}${
                        detail ? ` (${detail})` : ""
                    }`,
                );
            }
            if (!data.upload_url || !data.verify_url) {
                throw new Error("CNB returned incomplete asset upload metadata");
            }
            let verificationUrl;
            try {
                verificationUrl = new URL(data.verify_url);
            } catch {
                throw new Error("CNB returned an invalid asset upload verification URL");
            }
            let token = verificationUrl.searchParams.get("upload_token");
            let path = verificationUrl.searchParams.get("asset_path");
            if (!token || !path) {
                // Reason: CNB moved these fields from the query string into the
                // verification URL path, while the generated CLI help still
                // documents the older query-string response shape.
                const parts = verificationUrl.pathname.split("/").filter(Boolean);
                const marker = parts.lastIndexOf("asset-upload-confirmation");
                const encodedPath = marker >= 0 ? parts.slice(marker + 2).join("/") : "";
                if (!token && marker >= 0) {
                    token = parts[marker + 1];
                }
                if (!path && encodedPath) {
                    try {
                        path = decodeURIComponent(encodedPath);
                    } catch {
                        throw new Error("CNB returned an invalid asset upload verification path");
                    }
                }
            }
            if (!token || !path) {
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
printf 'published CNB Release %s with stable source archives\n' "$release_tag"
