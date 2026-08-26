#!/usr/bin/env bash
# Build a FreeRTOS QEMU fixture directly from FreeRTOS-Kernel (no CubeL4).
#
# Usage:
#   build-fixture-kernel.sh [options]
#
# Options:
#   --repo URL              FreeRTOS-Kernel git URL
#   --tag TAG               kernel tag (e.g. V11.1.0)
#   --kernel-dir DIR        local FreeRTOS-Kernel checkout
#   --target BOARD          board name (default: mps2-an385)
#   --variant NAME          fixture config variant (default: base)
#   --version VER           kernel version used as the cache coordinate
#                           (default: derived from --tag, e.g. V10.5.1 -> 10.5.1)
#   --build-dir DIR         object and map output directory
#   --out-elf PATH          destination ELF
#   --out-bin PATH          destination BIN
#   --cache-dir DIR         fixture cache directory receiving freertos.{elf,bin}
#                           (default: $FREERTOS_FIXTURE_CACHE/<target>/<version>/<variant>,
#                            FREERTOS_FIXTURE_CACHE defaults to ~/Project/gdr-fixture/freertos)
#   --no-cache-install      build only, do not copy into the fixture cache
#   --toolchain-path DIR    directory containing arm-none-eabi-* binaries
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO="https://github.com/FreeRTOS/FreeRTOS-Kernel.git"
DEFAULT_TAG="V11.1.0"
DEFAULT_KERNEL_DIR="/tmp/gdr-freertos-kernel-source"
DEFAULT_TARGET="mps2-an385"
DEFAULT_VARIANT="base"
DEFAULT_BUILD_DIR="/tmp/gdr-freertos-kernel-build"
TOOLCHAIN_PREFIX="arm-none-eabi-"

die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

usage() {
    sed -n '3,23p' "$0" | sed 's/^# \?//'
}

# V10.5.1 / V10.3.1-kernel-only -> 10.5.1 / 10.3.1
version_from_tag() {
    local tag="${1#V}"
    echo "${tag%-kernel-only}"
}

setup_toolchain() {
    local gcc tool
    if [[ -z "$TOOLCHAIN_PATH" ]]; then
        gcc="$(command -v "${TOOLCHAIN_PREFIX}gcc" || true)"
        [[ -n "$gcc" ]] || die "${TOOLCHAIN_PREFIX}gcc is not on PATH"
        TOOLCHAIN_PATH="$(dirname "$gcc")"
    fi
    for tool in gcc objcopy; do
        [[ -x "$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}$tool" ]] ||
            die "required tool not found: $TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}$tool"
    done
}

prepare_kernel() {
    local source="$KERNEL_DIR"
    if [[ "${KERNEL_DIR_EXPLICIT:-0}" == 1 ]]; then
        # Reason: never checkout a caller-owned reference tree. The build uses
        # a private clone whose worktree may safely be detached at TAG.
        KERNEL_DIR="$BUILD_DIR/kernel-source"
        rm -rf "$KERNEL_DIR"
        git clone --depth=1 --branch "$TAG" "$source" "$KERNEL_DIR"
        return
    fi
    if [[ ! -d "$KERNEL_DIR/.git" ]]; then
        mkdir -p "$(dirname "$KERNEL_DIR")"
        git clone --depth=1 --branch "$TAG" "$REPO" "$KERNEL_DIR"
        return
    fi
    if ! git -C "$KERNEL_DIR" rev-parse "$TAG" >/dev/null 2>&1; then
        git -C "$KERNEL_DIR" fetch --depth=1 origin "refs/tags/$TAG:refs/tags/$TAG"
    fi
    git -C "$KERNEL_DIR" checkout --detach "$TAG"
}

heap_source() {
    local kernel="$1"
    case "$VARIANT" in
    static-only) return 0 ;;
    heap-1) echo "$kernel/portable/MemMang/heap_1.c" ;;
    heap-2) echo "$kernel/portable/MemMang/heap_2.c" ;;
    heap-3) echo "$kernel/portable/MemMang/heap_3.c" ;;
    heap-5) echo "$kernel/portable/MemMang/heap_5.c" ;;
    *) echo "$kernel/portable/MemMang/heap_4.c" ;;
    esac
}

board_flags() {
    case "$TARGET" in
    mps2-an385)
        echo "-mcpu=cortex-m3 -mthumb"
        ;;
    *)
        die "unsupported kernel-direct target: $TARGET"
        ;;
    esac
}

compile_fixture() {
    local cc="$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}gcc"
    local objcopy="$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}objcopy"
    local config_dir="$SCRIPT_DIR/fixture/config/$VARIANT"
    local board_dir="$SCRIPT_DIR/fixture/board/$TARGET"
    local heap
    local -a cflags sources cpu_flags
    local port_dir

    [[ -d "$config_dir" ]] || die "unknown variant: $VARIANT ($config_dir)"
    [[ -d "$board_dir" ]] || die "unknown board: $TARGET ($board_dir)"
    heap="$(heap_source "$KERNEL_DIR")"
    read -r -a cpu_flags <<<"$(board_flags)"

    case "$TARGET" in
    mps2-an385) port_dir="$KERNEL_DIR/portable/GCC/ARM_CM3" ;;
    esac

    mkdir -p "$BUILD_DIR" "$(dirname "$OUT_ELF")" "$(dirname "$OUT_BIN")"
    echo "[gdr-ci] FreeRTOS-Kernel: $REPO@$TAG ($(git -C "$KERNEL_DIR" rev-parse HEAD))"
    echo "[gdr-ci] target: $TARGET variant: $VARIANT"
    echo "[gdr-ci] compiler: $($cc --version | head -1)"

    cflags=(
        "${cpu_flags[@]}"
        -Og -g3 -ffunction-sections -fdata-sections -fno-lto
        -Wall -Wextra -Werror -std=c11
        -I"$config_dir"
        -I"$SCRIPT_DIR/fixture/config"
        -I"$board_dir"
        -I"$KERNEL_DIR/include"
        -I"$port_dir"
    )
    sources=(
        "$SCRIPT_DIR/fixture/main.c"
        "$board_dir/system_init.c"
        "$board_dir/syscalls.c"
        "$board_dir/startup.s"
        "$KERNEL_DIR/tasks.c" "$KERNEL_DIR/queue.c" "$KERNEL_DIR/list.c"
        "$KERNEL_DIR/timers.c" "$KERNEL_DIR/event_groups.c"
        "$KERNEL_DIR/stream_buffer.c"
        "$port_dir/port.c"
    )
    if [[ -n "$heap" ]]; then
        sources+=("$heap")
    fi

    "$cc" "${cflags[@]}" "${sources[@]}" \
        -T"$board_dir/linker.ld" \
        -Wl,--gc-sections -Wl,--undefined=gdr_freertos_version_num \
        -Wl,-Map,"$BUILD_DIR/freertos.map" \
        --specs=nano.specs --specs=nosys.specs -nostartfiles -o "$OUT_ELF"
    "$objcopy" -O binary "$OUT_ELF" "$OUT_BIN"
    echo "[gdr-ci] built ELF: $OUT_ELF"
    echo "[gdr-ci] built BIN: $OUT_BIN"
}

# Mirror the artifacts into the shared fixture cache; see build-fixture-cubel4.sh.
install_to_cache() {
    [[ "$CACHE_INSTALL" == 1 ]] || return 0
    local elf="$CACHE_DIR/freertos.elf"
    local bin="$CACHE_DIR/freertos.bin"
    mkdir -p "$CACHE_DIR"
    [[ "$OUT_ELF" == "$elf" ]] || cp -f "$OUT_ELF" "$elf"
    [[ "$OUT_BIN" == "$bin" ]] || cp -f "$OUT_BIN" "$bin"
    if [[ -f "$BUILD_DIR/freertos.map" ]]; then
        cp -f "$BUILD_DIR/freertos.map" "$CACHE_DIR/freertos.map"
    fi
    echo "[gdr-ci] cached fixture: $CACHE_DIR"
}

parse_args() {
    local -a leftover=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
        --repo)
            REPO="$2"
            shift 2
            ;;
        --tag)
            TAG="$2"
            shift 2
            ;;
        --kernel-dir)
            KERNEL_DIR="$2"
            KERNEL_DIR_EXPLICIT=1
            shift 2
            ;;
        --target)
            TARGET="$2"
            shift 2
            ;;
        --variant)
            VARIANT="$2"
            shift 2
            ;;
        --version)
            VERSION="$2"
            shift 2
            ;;
        --build-dir)
            BUILD_DIR="$2"
            shift 2
            ;;
        --out-elf)
            OUT_ELF="$2"
            shift 2
            ;;
        --out-bin)
            OUT_BIN="$2"
            shift 2
            ;;
        --cache-dir)
            CACHE_DIR="$2"
            shift 2
            ;;
        --no-cache-install)
            CACHE_INSTALL=0
            shift
            ;;
        --toolchain-path)
            TOOLCHAIN_PATH="$2"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            leftover+=("$1")
            shift
            ;;
        esac
    done
    if [[ ${#leftover[@]} -ne 0 ]]; then
        die "unknown argument: ${leftover[*]}"
    fi
}

main() {
    REPO="$DEFAULT_REPO"
    TAG="$DEFAULT_TAG"
    KERNEL_DIR="$DEFAULT_KERNEL_DIR"
    TARGET="$DEFAULT_TARGET"
    VARIANT="$DEFAULT_VARIANT"
    VERSION=""
    BUILD_DIR="$DEFAULT_BUILD_DIR"
    OUT_ELF=""
    OUT_BIN=""
    CACHE_DIR=""
    CACHE_INSTALL=1
    TOOLCHAIN_PATH=""
    KERNEL_DIR_EXPLICIT=0

    parse_args "$@"
    OUT_ELF="${OUT_ELF:-$BUILD_DIR/freertos.elf}"
    OUT_BIN="${OUT_BIN:-$BUILD_DIR/freertos.bin}"
    VERSION="${VERSION:-$(version_from_tag "$TAG")}"
    CACHE_ROOT="${FREERTOS_FIXTURE_CACHE:-$HOME/Project/gdr-fixture/freertos}"
    CACHE_DIR="${CACHE_DIR:-$CACHE_ROOT/$TARGET/$VERSION/$VARIANT}"
    setup_toolchain
    prepare_kernel
    compile_fixture
    install_to_cache
}

main "$@"
