#!/usr/bin/env bash
# Build and run FreeRTOS QEMU closed-loop tests for one or more variants.
#
# Usage:
#   run-qemu-matrix.sh [<target>] [<version>] [<variant>...]
#
# Optional environment (caller configuration, not internal plumbing):
#   FREERTOS_FIXTURE_CACHE   firmware cache root with
#                            <target>/<version>/<variant>/freertos.elf.
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

qemu_machine_for_target() {
    case "$1" in
    b-l475e-iot01a) echo "b-l475e-iot01a" ;;
    mps2-an385) echo "mps2-an385" ;;
    mps2-an521) echo "mps2-an521" ;;
    qemu-virt-rv64) echo "virt" ;;
    *) die "unknown FreeRTOS QEMU target: $1" ;;
    esac
}

# Toolchain bin dir per target (the ARM and RISC-V xPack toolchains ship in
# different directories; the caller's env provides the one matching the lane).
is_cross_gdb() {
    [[ "$GDR_GDB" == *riscv* || "$GDR_GDB" == *rv64* ]]
}

toolchain_path_for() {
    case "$1" in
    # Reason: RTOS_TOOLCHAIN_PATH is env-set to the ARM xPack dir (it is the
    # default for every other lane); the RISC-V-specific variable must win
    # so the rv64 lane cannot silently compile with the ARM compiler.
    qemu-virt-rv64) echo "${XPACK_RISCV_TOOLCHAIN_PATH:-${RTOS_TOOLCHAIN_PATH:-}}" ;;
    *) echo "${RTOS_TOOLCHAIN_PATH:-${XPACK_ARM_TOOLCHAIN_PATH:-}}" ;;
    esac
}

run_pytest() {
    local target="$1" version="$2" variant="$3"
    local -a runner
    runner=(
        env -u GDR_ELF_PATH -u GDR_FIRMWARE_PATH
        "GDR_RTOS=freertos"
        "GDR_QEMU_TARGET=$target"
        "GDR_VERSION=$version"
        "GDR_FIXTURE_VARIANT=$variant"
        "GDR_GDB=$GDR_GDB"
        "GDR_QEMU_MACHINE=$(qemu_machine_for_target "$target")"
    )
    if [[ -n "${CACHE_ROOT:-}" ]]; then
        runner+=("FREERTOS_FIXTURE_CACHE=$CACHE_ROOT")
    fi
    if [[ -n "${GDR_ELF_OVERRIDE:-}" ]]; then
        # Reason: the RISC-V lane boots the raw binary via QEMU -bios (the
        # virt machine loads it at 0x80000000), while GDB reads the ELF; the
        # ARM lanes pass the ELF to -kernel.  The firmware is always the .elf
        # on ARM lanes, the .bin on the RISC-V lane.
        # Reason: shellcheck SC2155 (declare+assign masks the exit status);
        # the rest of this lane treats a missing override as "use profile defaults".
        local firmware
        firmware="$GDR_ELF_OVERRIDE"
        if [[ "$target" == "qemu-virt-rv64" ]]; then
            firmware="${GDR_ELF_OVERRIDE%.elf}.bin"
        fi
        runner+=("GDR_ELF_PATH=$GDR_ELF_OVERRIDE" "GDR_FIRMWARE_PATH=$firmware")
    fi
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
    GDR_ELF_OVERRIDE="$cache_dir/freertos.elf" run_pytest "$target" "$version" "$variant"
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
        "$SCRIPT_DIR/build-fixture-cubel4.sh"
        "$SCRIPT_DIR/build-fixture-kernel.sh"
    )
    local newer
    newer="$(find "${sources[@]}" -newer "$elf" -print -quit 2>/dev/null || true)"
    [[ -n "$newer" ]]
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

    local variant cached_elf
    for variant in "${variants[@]}"; do
        cached_elf="$CACHE_ROOT/$target/$version/$variant/freertos.elf"
        if [[ ! -f "$cached_elf" || "${GDR_FORCE_BUILD:-0}" == 1 ]]; then
            build_one "$target" "$version" "$variant"
        elif fixture_sources_newer_than "$cached_elf" "$variant" "$target"; then
            log_matrix_entry "$target" "$version" "$variant" \
                "cached fixture is older than its sources; rebuilding"
            build_one "$target" "$version" "$variant"
        else
            log_matrix_entry "$target" "$version" "$variant" "reusing cached fixture"
            GDR_ELF_OVERRIDE="$cached_elf" run_pytest "$target" "$version" "$variant"
        fi
    done
}

main "$@"
