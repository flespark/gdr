#!/usr/bin/env bash
# Build the RT-Thread qemu-vexpress-a9 test fixture ELF for GDR CI.
#
# Inputs (env):
#   RT_THREAD_REPO  - rt-thread git URL or local path (default: upstream)
#   RT_THREAD_REF   - ref to checkout (default: v4.0.5)
#   PATCH_DIR       - directory of *.patch files to apply (default: this script's dir)
#   BUILD_DIR        - working dir (default: /tmp/rt-thread-build)
#   OUT_ELF          - destination for rtthread.elf (default: BUILD_DIR/rtthread.elf)
#   CROSS_TOOL_PREFIX - arm-none-eabi- (default)
set -euo pipefail

RT_THREAD_REPO="${RT_THREAD_REPO:-https://github.com/RT-Thread/rt-thread.git}"
RT_THREAD_REF="${RT_THREAD_REF:-v4.0.5}"
PATCH_DIR="${PATCH_DIR:-$(dirname "$0")/patches}"
BUILD_DIR="${BUILD_DIR:-/tmp/rt-thread-build}"
OUT_ELF="${OUT_ELF:-$BUILD_DIR/bsp/qemu-vexpress-a9/rtthread.elf}"
CROSS_TOOL_PREFIX="${CROSS_TOOL_PREFIX:-arm-none-eabi-}"
# Default to the system cross-toolchain; detect leaf toolchain.
RTOS_TOOLCHAIN_PATH="${RTOS_TOOLCHAIN_PATH:-$(dirname "$(command -v ${CROSS_TOOL_PREFIX}gcc || echo /usr/bin/${CROSS_TOOL_PREFIX}gcc)")}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[gdr-ci] RT-Thread repo: $RT_THREAD_REPO@$RT_THREAD_REF"
echo "[gdr-ci] build dir: $BUILD_DIR"

if [[ -d "$BUILD_DIR/.git" ]]; then
    echo "[gdr-ci] existing clone found; reusing"
    cd "$BUILD_DIR"
    git fetch --depth=1 origin "$RT_THREAD_REF"
    git checkout "$RT_THREAD_REF"
else
    mkdir -p "$BUILD_DIR"
    git clone --depth=1 --branch "$RT_THREAD_REF" "$RT_THREAD_REPO" "$BUILD_DIR"
    cd "$BUILD_DIR"
fi

echo "[gdr-ci] applying patches from $PATCH_DIR"
# Reset any previously applied patches so re-runs are idempotent.
git checkout -- . 2>/dev/null || true
for patch in "$PATCH_DIR"/*.patch; do
    echo "  $(basename "$patch")"
    if git apply --check --whitespace=fix "$patch" 2>/dev/null; then
        git apply --whitespace=fix "$patch"
    else
        echo "    (already applied or conflicts; skipping — checkout was reset above)"
        git apply --whitespace=nowarn --reject "$patch" || true
    fi
done

cd bsp/qemu-vexpress-a9

# RT-Thread scons picks up RTT_EXEC_PATH for the toolchain, RTT_CC for compiler.
# Use the host cross-toolchain instead of the env-managed one.
export RTT_CC=gcc
export RTT_EXEC_PATH="$RTOS_TOOLCHAIN_PATH"

echo "[gdr-ci] scons (may take a minute)..."
# Prefer bare scons, fall back to uvx for a clean Python env.
SCONS_BIN="${SCONS_BIN:-scons}"
if ! command -v "$SCONS_BIN" >/dev/null 2>&1; then
    SCONS_BIN="uvx --from scons scons"
fi
# Note: on macOS getconf _NPROCESSORS_ONLN; on Linux nproc. Both have getconf.
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu || echo 4)"
$SCONS_BIN -j"$JOBS"

if [[ ! -f rtthread.elf ]]; then
    echo "[gdr-ci] FAILED: rtthread.elf not produced" >&2
    exit 1
fi

ELF_SIZE=$(du -h rtthread.elf | cut -f1)
ELF_ABS="$BUILD_DIR/bsp/qemu-vexpress-a9/rtthread.elf"
echo "[gdr-ci] build OK: rtthread.elf ($ELF_SIZE)"
echo "[gdr-ci] OUT_ELF=$ELF_ABS"
# Copy to caller-specified destination if OUT_ELF differs from the in-tree one.
if [[ "$OUT_ELF" != "$ELF_ABS" ]]; then
    mkdir -p "$(dirname "$OUT_ELF")"
    cp rtthread.elf "$OUT_ELF"
    echo "[gdr-ci] copied to $OUT_ELF"
fi
echo "$ELF_ABS"