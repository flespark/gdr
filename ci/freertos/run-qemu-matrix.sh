#!/usr/bin/env bash
# Build and run the FreeRTOS QEMU closed-loop smoke test.
#
# Usage:
#   run-qemu-matrix.sh
#
# Optional environment (caller configuration, not internal plumbing):
#   FREERTOS_FIXTURE_CACHE   firmware cache root with
#                            <target>/<version>/freertos.elf. When set,
#                            compilation is skipped.
#   RTOS_TOOLCHAIN_PATH      compiler bin directory (or XPACK_ARM_TOOLCHAIN_PATH)
#   GDR_GDB                  GDB binary for the closed-loop tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEFAULT_TARGET="b-l475e-iot01a"
DEFAULT_VERSION="10.3.1"
DEFAULT_BUILD_DIR="/tmp/gdr-freertos-build"
DEFAULT_ELF_NAME="freertos_b_l475e_iot01a.elf"
DEFAULT_BIN_NAME="freertos_b_l475e_iot01a.bin"

die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

log_matrix_entry() {
    echo "[gdr-ci] freertos/$1/$2: $3"
}

main() {
    local fixture_cache="${FREERTOS_FIXTURE_CACHE:-}"
    local target="$DEFAULT_TARGET"
    local version="$DEFAULT_VERSION"
    local build_dir="$DEFAULT_BUILD_DIR"
    local elf_path="$build_dir/$DEFAULT_ELF_NAME"
    local bin_path="$build_dir/$DEFAULT_BIN_NAME"
    local toolchain_path="${RTOS_TOOLCHAIN_PATH:-${XPACK_ARM_TOOLCHAIN_PATH:-}}"
    local fixture_dir
    local -a build_args runner

    export GDR_GDB="${GDR_GDB:-gdb-multiarch}"
    bash "$REPO_ROOT/ci/check-gdb-python.sh"
    export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/gdr-venv}"
    uv sync --group dev

    if [[ -n "$fixture_cache" ]]; then
        fixture_dir="$fixture_cache/$target/$version"
        [[ -f "$fixture_dir/freertos.elf" ]] || \
            die "cached fixture missing: $fixture_dir/freertos.elf"
        log_matrix_entry "$target" "$version" "pytest"
        runner=(
            env -u GDR_ELF_PATH -u GDR_FIRMWARE_PATH
            "GDR_RTOS=freertos"
            "GDR_QEMU_TARGET=$target"
            "GDR_VERSION=$version"
            "GDR_GDB=$GDR_GDB"
            "FREERTOS_FIXTURE_CACHE=$fixture_cache"
        )
        (cd "$REPO_ROOT" && "${runner[@]}" uv run pytest tests/integration/freertos -v --tb=short)
        return
    fi

    build_args=(--build-dir "$build_dir" --out-elf "$elf_path" --out-bin "$bin_path")
    if [[ -n "$toolchain_path" ]]; then
        build_args+=(--toolchain-path "$toolchain_path")
    fi
    log_matrix_entry "$target" "$version" "building fixture"
    bash "$SCRIPT_DIR/build-freertos.sh" "${build_args[@]}"

    log_matrix_entry "$target" "$version" "pytest"
    runner=(
        env -u GDR_ELF_PATH -u GDR_FIRMWARE_PATH
        "GDR_RTOS=freertos"
        "GDR_QEMU_TARGET=$target"
        "GDR_VERSION=$version"
        "GDR_GDB=$GDR_GDB"
        "GDR_ELF_PATH=$elf_path"
        "GDR_FIRMWARE_PATH=$elf_path"
    )
    (cd "$REPO_ROOT" && "${runner[@]}" uv run pytest tests/integration/freertos -v --tb=short)
}

main "$@"
