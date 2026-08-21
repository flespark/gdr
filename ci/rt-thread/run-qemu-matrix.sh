#!/usr/bin/env bash
# Build every supported RT-Thread fixture for one QEMU target and run pytest.
#
# Usage:
#   run-qemu-matrix.sh <cortex-a9|rv64> [version-refs...]
#
# Each version-ref is an RT-Thread release tag or branch checked out for that
# matrix entry (for example v4.0.5 or lts-v4.1.x). The leading "v" is stripped
# to form the GDR version string passed to pytest.
#
# Optional environment (caller configuration, not internal plumbing):
#   RT_THREAD_FIXTURE_CACHE  firmware cache root with
#                            <target>/<version>/rtthread.elf (and rtthread.bin
#                            on RV64). When set, compilation is skipped.
#   RT_THREAD_REFS           whitespace-separated version refs when none are
#                            passed on the command line
#   RT_THREAD_REPO           upstream git URL used to populate the source cache
#   RT_THREAD_SOURCE_CACHE   bare clone used as a local fetch source
#   RTOS_TOOLCHAIN_PATH      compiler bin directory (or XPACK_*_TOOLCHAIN_PATH)
#   GDR_GDB                  GDB binary for the closed-loop tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEFAULT_REPO="https://github.com/RT-Thread/rt-thread.git"
DEFAULT_SOURCE_CACHE="/tmp/rt-thread-build/xpack/source"
DEFAULT_BUILD_ROOT="/tmp/rt-thread-build/xpack"

die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

# Populate REFS / TOOLCHAIN_PATH / TOOLCHAIN_PREFIX for one QEMU target.
resolve_matrix() {
    local target="$1"
    shift
    case "$target" in
        cortex-a9)
            TOOLCHAIN_PATH="${RTOS_TOOLCHAIN_PATH:-${XPACK_ARM_TOOLCHAIN_PATH:-}}"
            TOOLCHAIN_PREFIX="arm-none-eabi-"
            if [[ $# -gt 0 ]]; then
                REFS=("$@")
            elif [[ -n "${RT_THREAD_REFS:-}" ]]; then
                read -r -a REFS <<<"$RT_THREAD_REFS"
            else
                REFS=(v3.1.0 v3.1.1 v3.1.2 v3.1.3 v3.1.4 v3.1.5 v4.0.0 v4.0.5 v4.1.1)
            fi
            ;;
        rv64)
            TOOLCHAIN_PATH="${RTOS_TOOLCHAIN_PATH:-${XPACK_RISCV_TOOLCHAIN_PATH:-}}"
            TOOLCHAIN_PREFIX="riscv64-unknown-elf-"
            if [[ $# -gt 0 ]]; then
                REFS=("$@")
            elif [[ -n "${RT_THREAD_REFS:-}" ]]; then
                read -r -a REFS <<<"$RT_THREAD_REFS"
            else
                REFS=(v4.0.4 v4.0.5 v4.1.0 v4.1.1)
            fi
            ;;
        *)
            echo "usage: $0 <cortex-a9|rv64> [version-refs...]" >&2
            echo "  version-ref: RT-Thread release tag or branch (e.g. v4.0.5)" >&2
            die "unknown target: $target"
            ;;
    esac
}

# Cortex-A9 builds every listed version for fixture coverage, but only these
# tags exercise the full pytest suite (legacy enum, enum migration, final layout).
should_run_pytest() {
    local target="$1" ref="$2"
    if [[ "$target" != "cortex-a9" ]]; then
        return 0
    fi
    case "$ref" in
        v3.1.0|v3.1.3|v3.1.5|v4.0.0|v4.0.2|v4.0.5|v4.1.1) return 0 ;;
        *) return 1 ;;
    esac
}

prepare_source_cache() {
    local source_repo="$1" source_cache="$2"
    shift 2
    local ref
    if [[ ! -d "$source_cache" ]]; then
        mkdir -p "$(dirname "$source_cache")"
        git init --bare "$source_cache"
        git -C "$source_cache" remote add origin "$source_repo"
    fi
    for ref in "$@"; do
        if ! git -C "$source_cache" rev-parse --verify --quiet "refs/tags/$ref^{commit}" >/dev/null; then
            git -C "$source_cache" fetch --depth=1 --no-tags origin "refs/tags/$ref:refs/tags/$ref"
        fi
    done
}

build_fixture() {
    local target="$1" ref="$2" repo="$3" build_dir="$4" bsp="$5"
    local -a build_args=(
        --target "$target"
        --ref "$ref"
        --repo "$repo"
        --build-dir "$build_dir"
        --out-elf "$build_dir/$bsp/rtthread.elf"
        --out-bin "$build_dir/$bsp/rtthread.bin"
        --toolchain-prefix "$TOOLCHAIN_PREFIX"
    )
    if [[ -n "$TOOLCHAIN_PATH" ]]; then
        build_args+=(--toolchain-path "$TOOLCHAIN_PATH")
    fi
    bash "$SCRIPT_DIR/build-rtt.sh" "${build_args[@]}"
}

run_rtthread_pytest() {
    local target="$1" version="$2" fixture_cache="${3:-}"
    local elf_path="${4:-}" firmware_path="${5:-}"
    local -a runner=(env -u GDR_ELF_PATH -u GDR_FIRMWARE_PATH)
    runner+=(
        "GDR_RTOS=rtthread"
        "GDR_QEMU_TARGET=$target"
        "GDR_VERSION=$version"
        "GDR_GDB=${GDR_GDB:-gdb-multiarch}"
    )
    if [[ -n "$fixture_cache" ]]; then
        runner+=("RT_THREAD_FIXTURE_CACHE=$fixture_cache")
    else
        runner+=("GDR_ELF_PATH=$elf_path")
        if [[ -n "$firmware_path" ]]; then
            runner+=("GDR_FIRMWARE_PATH=$firmware_path")
        fi
    fi
    (
        cd "$REPO_ROOT"
        "${runner[@]}" uv run pytest tests/integration/rtthread -v --tb=short
    )
}

main() {
    if [[ $# -lt 1 ]]; then
        echo "usage: $0 <cortex-a9|rv64> [version-refs...]" >&2
        echo "  version-ref: RT-Thread release tag or branch (e.g. v4.0.5)" >&2
        exit 2
    fi
    local target="$1"
    shift
    local fixture_cache="${RT_THREAD_FIXTURE_CACHE:-}"
    local ref version bsp build_dir elf_path firmware_path fixture_dir
    local source_repo source_cache repo
    local -a REFS
    local TOOLCHAIN_PATH="" TOOLCHAIN_PREFIX=""

    resolve_matrix "$target" "$@"

    export GDR_GDB="${GDR_GDB:-gdb-multiarch}"
    # Check the embedded interpreter before spending time fetching or building a
    # fixture. This is deliberately in the shared runner so all GDB/QEMU jobs use
    # the same compatibility gate.
    bash "$SCRIPT_DIR/../check-gdb-python.sh"
    # Reason: keep local container runs from creating a Linux virtualenv in the mounted repo.
    export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/gdr-venv}"
    uv sync --group dev

    if [[ -z "$fixture_cache" ]]; then
        source_repo="${RT_THREAD_REPO:-$DEFAULT_REPO}"
        source_cache="${RT_THREAD_SOURCE_CACHE:-$DEFAULT_SOURCE_CACHE}"
        prepare_source_cache "$source_repo" "$source_cache" "${REFS[@]}"
        # Reason: each version clone reads the cached tag locally instead of GitHub.
        repo="file://$source_cache"
    fi

    for ref in "${REFS[@]}"; do
        version="${ref#v}"
        if [[ "$target" == "rv64" && "$ref" == "v4.1.1" ]]; then
            bsp="bsp/qemu-virt64-riscv"
        elif [[ "$target" == "rv64" ]]; then
            bsp="bsp/qemu-riscv-virt64"
        else
            bsp="bsp/qemu-vexpress-a9"
        fi

        if [[ -n "$fixture_cache" ]]; then
            if should_run_pytest "$target" "$ref"; then
                fixture_dir="$fixture_cache/$target/$version"
                [[ -f "$fixture_dir/rtthread.elf" ]] || \
                    die "cached fixture missing: $fixture_dir/rtthread.elf"
                if [[ "$target" == "rv64" ]]; then
                    [[ -f "$fixture_dir/rtthread.bin" ]] || \
                        die "cached RV64 firmware missing: $fixture_dir/rtthread.bin"
                fi
                run_rtthread_pytest "$target" "$version" "$fixture_cache"
            fi
            continue
        fi

        build_dir="$DEFAULT_BUILD_ROOT/$target/$ref"
        elf_path="$build_dir/$bsp/rtthread.elf"
        firmware_path=""
        if [[ "$target" == "rv64" ]]; then
            firmware_path="$build_dir/$bsp/rtthread.bin"
        fi
        build_fixture "$target" "$ref" "$repo" "$build_dir" "$bsp"
        if should_run_pytest "$target" "$ref"; then
            run_rtthread_pytest "$target" "$version" "" "$elf_path" "$firmware_path"
        fi
    done
}

main "$@"
