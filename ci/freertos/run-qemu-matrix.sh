#!/usr/bin/env bash
# Build and run FreeRTOS closed-loop tests for one or more variants.
#
# Usage:
#   run-qemu-matrix.sh [<target>] [<version>] [<variant>...]
#
# ``snapshot`` is a file-only variant (no QEMU). Target/version select the
# calling cell; the snapshot ELF itself is always kernel V11.1.0:
#   run-qemu-matrix.sh mps2-an385 10.4.6 snapshot
#
# Optional environment (caller configuration, not internal plumbing):
#   FREERTOS_FIXTURE_CACHE   firmware cache root with
#                            <target>/<version>/<variant>/freertos.elf
#                            and snapshot/snapshot.elf.
#                            Default: ~/Project/gdr-fixture/freertos.
#                            A cached fixture is reused; missing ones are built
#                            and then installed into this cache.
#   GDR_FORCE_BUILD=1        rebuild even when the cached fixture exists
#   FREERTOS_KERNEL_DIR      local FreeRTOS-Kernel checkout for non-CubeL4 lanes
#   RTOS_TOOLCHAIN_PATH      compiler bin directory (or XPACK_ARM_TOOLCHAIN_PATH)
#   GDR_GDB                  GDB binary for the closed-loop tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEFAULT_TARGET="b-l475e-iot01a"
DEFAULT_VERSION="10.3.1"
DEFAULT_VARIANT="base"
DEFAULT_BUILD_DIR="/tmp/gdr-freertos-build"
DEFAULT_CACHE_ROOT="$HOME/Project/gdr-fixture/freertos"
SNAPSHOT_VERSION="11.1.0"

die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

log_matrix_entry() {
    echo "[gdr-ci] freertos/$1/$2/$3: $4"
}

is_cube_lane() {
    [[ "$1" == "b-l475e-iot01a" ]]
}

kernel_tag_for_version() {
    case "$1" in
    10.3.1) echo "V10.3.1-kernel-only" ;;
    10.4.*) echo "V10.4.6" ;;
    10.5.*) echo "V10.5.1" ;;
    11.1.*) echo "V11.1.0" ;;
    11.3.*) echo "V11.3.1" ;;
    *) echo "V$1" ;;
    esac
}

# Toolchain bin dir per target (the ARM and RISC-V xPack toolchains ship in
# different directories; the caller's env provides the one matching the lane).
toolchain_path_for() {
    case "$1" in
    # Reason: RTOS_TOOLCHAIN_PATH is env-set to the ARM xPack dir (it is the
    # default for every other lane); the RISC-V-specific variable must win
    # so the rv64 lane cannot silently compile with the ARM compiler.
    qemu-virt-rv64) echo "${XPACK_RISCV_TOOLCHAIN_PATH:-${RTOS_TOOLCHAIN_PATH:-}}" ;;
    *) echo "${RTOS_TOOLCHAIN_PATH:-${XPACK_ARM_TOOLCHAIN_PATH:-}}" ;;
    esac
}

# Parameterize pytest through the shared loader (tests.support.loader).
# Target/version/variant/cache/gdb are the only knobs; QEMU machine, ELF
# and firmware paths are derived on the Python side from that tuple.
run_pytest() {
    local target="$1" version="$2" variant="$3"
    # Reason: the runner already decided whether to rebuild; leaving
    # GDR_FORCE_BUILD set would make the snapshot tests compile twice.
    local -a runner=(
        env -u GDR_ELF_PATH -u GDR_FORCE_BUILD
        "GDR_RTOS=freertos"
        "GDR_QEMU_TARGET=$target"
        "GDR_VERSION=$version"
        "GDR_FIXTURE_VARIANT=$variant"
        "GDR_GDB=$GDR_GDB"
        "FREERTOS_FIXTURE_CACHE=$CACHE_ROOT"
    )
    log_matrix_entry "$target" "$version" "$variant" "pytest"
    (cd "$REPO_ROOT" && "${runner[@]}" uv run pytest tests/integration/freertos -v --tb=short)
}

build_one() {
    local target="$1" version="$2" variant="$3"
    local build_dir="$DEFAULT_BUILD_DIR/$target/$version/$variant"
    local cache_dir="$CACHE_ROOT/$target/$version/$variant"
    local elf_path="$build_dir/freertos.elf"
    local bin_path="$build_dir/freertos.bin"
    local toolchain_path
    toolchain_path="$(toolchain_path_for "$target")"
    local -a build_args
    mkdir -p "$build_dir"
    if is_cube_lane "$target"; then
        build_args=(
            --variant "$variant"
            --build-dir "$build_dir"
            --out-elf "$elf_path"
            --out-bin "$bin_path"
            --cache-dir "$cache_dir"
        )
        if [[ -n "$toolchain_path" ]]; then
            build_args+=(--toolchain-path "$toolchain_path")
        fi
        log_matrix_entry "$target" "$version" "$variant" "building CubeL4 fixture"
        bash "$SCRIPT_DIR/build-fixture-cubel4.sh" "${build_args[@]}"
    else
        build_args=(
            --tag "$(kernel_tag_for_version "$version")"
            --target "$target"
            --variant "$variant"
            --version "$version"
            --build-dir "$build_dir"
            --out-elf "$elf_path"
            --out-bin "$bin_path"
            --cache-dir "$cache_dir"
        )
        # Reason: only an explicitly configured checkout is trusted; the build
        # script clones the tag itself otherwise, so no developer-specific path
        # is baked into the lane.
        if [[ -d "${FREERTOS_KERNEL_DIR:-}" ]]; then
            build_args+=(--kernel-dir "$FREERTOS_KERNEL_DIR")
        fi
        if [[ -n "$toolchain_path" ]]; then
            build_args+=(--toolchain-path "$toolchain_path")
        fi
        log_matrix_entry "$target" "$version" "$variant" "building kernel fixture"
        bash "$SCRIPT_DIR/build-fixture-kernel.sh" "${build_args[@]}"
    fi
    run_pytest "$target" "$version" "$variant"
}

# Newest mtime among the fixture sources that end up inside a firmware image.
# Reason: a cached ELF built before a fixture edit boots the *old* objects, so
# assertions about new fixture state fail (or worse, stale state silently
# passes) with nothing in the output pointing at the cache. Compare timestamps
# and rebuild instead of trusting mere file existence.
fixture_sources_newer_than() {
    local elf="$1" variant="$2" target="$3"
    local -a sources=(
        "$SCRIPT_DIR/fixture/main.c"
        "$SCRIPT_DIR/fixture/config/gdr_fixture_common.h"
        "$SCRIPT_DIR/fixture/config/$variant"
        "$SCRIPT_DIR/fixture/board/$target"
        "$SCRIPT_DIR/fixture/common"
        "$SCRIPT_DIR/build-fixture-cubel4.sh"
        "$SCRIPT_DIR/build-fixture-kernel.sh"
    )
    local newer
    newer="$(find "${sources[@]}" -newer "$elf" -print -quit 2>/dev/null || true)"
    [[ -n "$newer" ]]
}

snapshot_sources_newer_than() {
    local elf="$1"
    local -a sources=(
        "$SCRIPT_DIR/snapshot/snapshot.c"
        "$SCRIPT_DIR/snapshot/snapshot_heap.c"
        "$SCRIPT_DIR/snapshot/FreeRTOSConfig.h"
        "$SCRIPT_DIR/snapshot/linker.ld"
        "$SCRIPT_DIR/build-fixture-snapshot.sh"
    )
    local newer
    newer="$(find "${sources[@]}" -newer "$elf" -print -quit 2>/dev/null || true)"
    [[ -n "$newer" ]]
}

build_snapshot_elf() {
    local source="$1" out_elf="$2" cache_name="$3"
    local -a build_args=(
        --source "$SCRIPT_DIR/snapshot/$source"
        --out-elf "$out_elf"
        --cache-name "$cache_name"
        --cache-dir "$CACHE_ROOT/snapshot"
    )
    if [[ -d "${FREERTOS_KERNEL_DIR:-}" ]]; then
        build_args+=(--kernel-dir "$FREERTOS_KERNEL_DIR")
    fi
    local toolchain_path
    toolchain_path="$(toolchain_path_for "mps2-an385")"
    if [[ -n "$toolchain_path" ]]; then
        build_args+=(--toolchain-path "$toolchain_path")
    fi
    bash "$SCRIPT_DIR/build-fixture-snapshot.sh" "${build_args[@]}"
}

run_snapshot() {
    local target="$1" version="$2"
    local cache_dir="$CACHE_ROOT/snapshot"
    local cached_elf="$cache_dir/snapshot.elf"
    local cached_heap="$cache_dir/snapshot_heap.elf"
    local out_dir="$SCRIPT_DIR/snapshot/out"
    mkdir -p "$out_dir"
    if [[ ! -f "$cached_elf" || "${GDR_FORCE_BUILD:-0}" == 1 ]] ||
        snapshot_sources_newer_than "$cached_elf"; then
        log_matrix_entry "$target" "$version" "snapshot" "building snapshot ELF"
        build_snapshot_elf "snapshot.c" "$out_dir/snapshot.elf" "snapshot.elf"
    else
        log_matrix_entry "$target" "$version" "snapshot" "reusing cached snapshot ELF"
    fi
    if [[ ! -f "$cached_heap" || "${GDR_FORCE_BUILD:-0}" == 1 ]] ||
        snapshot_sources_newer_than "$cached_heap"; then
        log_matrix_entry "$target" "$version" "snapshot" "building snapshot heap ELF"
        build_snapshot_elf "snapshot_heap.c" "$out_dir/snapshot_heap.elf" "snapshot_heap.elf"
    else
        log_matrix_entry "$target" "$version" "snapshot" "reusing cached snapshot heap ELF"
    fi
    # Reason: the snapshot image is compiled against V11.1.0 headers (SMP);
    # the matrix target/version only name the calling cell.
    run_pytest "$target" "$SNAPSHOT_VERSION" "snapshot"
}

run_live() {
    local target="$1" version="$2" variant="$3"
    local cached_elf="$CACHE_ROOT/$target/$version/$variant/freertos.elf"
    if [[ ! -f "$cached_elf" || "${GDR_FORCE_BUILD:-0}" == 1 ]]; then
        build_one "$target" "$version" "$variant"
    elif fixture_sources_newer_than "$cached_elf" "$variant" "$target"; then
        log_matrix_entry "$target" "$version" "$variant" \
            "cached fixture is older than its sources; rebuilding"
        build_one "$target" "$version" "$variant"
    else
        log_matrix_entry "$target" "$version" "$variant" "reusing cached fixture"
        run_pytest "$target" "$version" "$variant"
    fi
}

main() {
    local target="${1:-$DEFAULT_TARGET}"
    local version="${2:-$DEFAULT_VERSION}"
    shift $(($# >= 1 ? 1 : 0)) || true
    shift $(($# >= 1 ? 1 : 0)) || true
    local -a variants
    if [[ $# -gt 0 ]]; then
        variants=("$@")
    else
        variants=("$DEFAULT_VARIANT")
    fi

    export GDR_GDB="${GDR_GDB:-gdb-multiarch}"
    bash "$REPO_ROOT/ci/check-gdb-python.sh"
    export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/gdr-venv}"
    uv sync --group dev

    CACHE_ROOT="${FREERTOS_FIXTURE_CACHE:-$DEFAULT_CACHE_ROOT}"
    echo "[gdr-ci] fixture cache: $CACHE_ROOT"

    local variant
    for variant in "${variants[@]}"; do
        if [[ "$variant" == "snapshot" ]]; then
            run_snapshot "$target" "$version"
        else
            run_live "$target" "$version" "$variant"
        fi
    done
}

main "$@"
