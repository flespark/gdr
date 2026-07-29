#!/usr/bin/env bash
# Print one version section from CHANGELOG.md for release notes.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <release-tag>" >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
changelog="$script_dir/../CHANGELOG.md"
release_tag="$1"

awk -v heading="## [${release_tag}]" '
    substr($0, 1, length(heading)) == heading {
        found = 1
        next
    }
    found && /^## / {
        exit
    }
    found {
        print
    }
    END {
        if (!found) {
            exit 1
        }
    }
' "$changelog"
