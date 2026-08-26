#!/usr/bin/env bash
# Build the FreeRTOS SMP static snapshot ELF (no QEMU, file-only GDB).
#
# Usage:
#   build-fixture-snapshot.sh [--kernel-dir DIR] [--tag TAG] [--out-elf PATH]
#                     [--cache-dir DIR] [--no-cache-install]
#                     [--toolchain-path DIR]
#
# The snapshot only needs FreeRTOS-Kernel *headers*. When no checkout is
# supplied it shallow-clones the tag into FREERTOS_KERNEL_CACHE
# (default /tmp/gdr-freertos-kernel-source), so the lane never depends on a
# developer-specific path and never mutates a caller-owned reference tree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SNAPSHOT_DIR="$SCRIPT_DIR/snapshot"
DEFAULT_REPO="https://github.com/FreeRTOS/FreeRTOS-Kernel.git"
# Reason: the snapshot is an SMP (configNUMBER_OF_CORES 2) image, which the
# kernel only supports from V11.0.0 onwards.
DEFAULT_TAG="V11.1.0"
DEFAULT_KERNEL_CACHE="/tmp/gdr-freertos-kernel-source"
DEFAULT_OUT="$SNAPSHOT_DIR/out/snapshot.elf"
TOOLCHAIN_PREFIX="arm-none-eabi-"

die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

KERNEL_DIR="${FREERTOS_KERNEL_DIR:-}"
TAG="$DEFAULT_TAG"
OUT_ELF="$DEFAULT_OUT"
CACHE_DIR=""
CACHE_INSTALL=1
TOOLCHAIN_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
    --kernel-dir)
        KERNEL_DIR="$2"
        shift 2
        ;;
    --tag)
        TAG="$2"
        shift 2
        ;;
    --out-elf)
        OUT_ELF="$2"
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
        sed -n '2,13p' "$0" | sed 's/^# \?//'
        exit 0
        ;;
    *) die "unknown argument: $1" ;;
    esac
done

CACHE_ROOT="${FREERTOS_FIXTURE_CACHE:-$HOME/Project/gdr-fixture/freertos}"
CACHE_DIR="${CACHE_DIR:-$CACHE_ROOT/snapshot}"

# Resolve the header source: caller-supplied checkout, else a private clone.
if [[ -n "$KERNEL_DIR" ]]; then
    [[ -d "$KERNEL_DIR/include" ]] ||
        die "FreeRTOS-Kernel include/ missing in $KERNEL_DIR"
else
    KERNEL_DIR="${FREERTOS_KERNEL_CACHE:-$DEFAULT_KERNEL_CACHE}"
    if [[ ! -d "$KERNEL_DIR/include" ]]; then
        mkdir -p "$(dirname "$KERNEL_DIR")"
        git clone --depth=1 --branch "$TAG" "$DEFAULT_REPO" "$KERNEL_DIR"
    fi
fi

if [[ -z "$TOOLCHAIN_PATH" ]]; then
    TOOLCHAIN_PATH="${RTOS_TOOLCHAIN_PATH:-${XPACK_ARM_TOOLCHAIN_PATH:-}}"
fi
if [[ -z "$TOOLCHAIN_PATH" ]]; then
    gcc="$(command -v "${TOOLCHAIN_PREFIX}gcc" || true)"
    [[ -n "$gcc" ]] || die "${TOOLCHAIN_PREFIX}gcc is not on PATH"
    TOOLCHAIN_PATH="$(dirname "$gcc")"
fi
cc="$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}gcc"
[[ -x "$cc" ]] || die "compiler not found: $cc"

echo "[gdr-ci] snapshot kernel headers: $KERNEL_DIR"
echo "[gdr-ci] compiler: $($cc --version | head -1)"

mkdir -p "$(dirname "$OUT_ELF")"
"$cc" -mcpu=cortex-m33 -mthumb -Og -g3 -std=c11 \
    -Wall -Wextra -Werror -ffunction-sections -fdata-sections -fno-lto \
    -I"$SNAPSHOT_DIR" -I"$KERNEL_DIR/include" \
    "$SNAPSHOT_DIR/snapshot.c" \
    -T"$SNAPSHOT_DIR/linker.ld" -nostdlib -nostartfiles \
    -Wl,--gc-sections -o "$OUT_ELF"
echo "[gdr-ci] built snapshot ELF: $OUT_ELF"

if [[ "$CACHE_INSTALL" == 1 ]]; then
    mkdir -p "$CACHE_DIR"
    if [[ "$OUT_ELF" != "$CACHE_DIR/snapshot.elf" ]]; then
        cp -f "$OUT_ELF" "$CACHE_DIR/snapshot.elf"
    fi
    echo "[gdr-ci] cached snapshot: $CACHE_DIR/snapshot.elf"
fi
