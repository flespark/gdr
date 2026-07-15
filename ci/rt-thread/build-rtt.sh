#!/usr/bin/env bash
# Build the RT-Thread qemu-vexpress-a9 test fixture ELF for GDR CI.
#
# Inputs (env):
#   RT_THREAD_REPO  - rt-thread git URL or local path (default: upstream)
#   RT_THREAD_REF   - ref to checkout (default: v4.0.5)
#   PATCH_DIR       - directory of *.patch files to apply (overrides auto selection)
#   BUILD_DIR        - working dir (default: /tmp/rt-thread-build)
#   OUT_ELF          - destination for rtthread.elf (default: BUILD_DIR/rtthread.elf)
#   CROSS_TOOL_PREFIX - arm-none-eabi- (default)
set -euo pipefail

RT_THREAD_REPO="${RT_THREAD_REPO:-https://github.com/RT-Thread/rt-thread.git}"
RT_THREAD_REF="${RT_THREAD_REF:-v4.0.5}"
# Resolve paths to ABSOLUTE *before* any `cd` later in the script.
# Reason: if PATCH_DIR is left relative to the repo root, it becomes
# unresolvable after we `cd "$BUILD_DIR"` into the cloned tree, and the
# patch glob silently fails — leaving patches unapplied and breaking
# the build downstream.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH_ROOT="$SCRIPT_DIR/patches"
BUILD_DIR="${BUILD_DIR:-/tmp/rt-thread-build}"
OUT_ELF="${OUT_ELF:-$BUILD_DIR/bsp/qemu-vexpress-a9/rtthread.elf}"
CROSS_TOOL_PREFIX="${CROSS_TOOL_PREFIX:-arm-none-eabi-}"
# Default to the system cross-toolchain; detect leaf toolchain.
RTOS_TOOLCHAIN_PATH="${RTOS_TOOLCHAIN_PATH:-$(dirname "$(command -v ${CROSS_TOOL_PREFIX}gcc || echo /usr/bin/${CROSS_TOOL_PREFIX}gcc)")}"

# Reason: always re-checkout the ref so we start from a pristine tree; the
# ≠ dangling изменений that would otherwise survive across CI reruns.
echo "[gdr-ci] RT-Thread repo: $RT_THREAD_REPO@$RT_THREAD_REF"
echo "[gdr-ci] build dir: $BUILD_DIR"

PATCH_DIRS=()
if [[ -n "${PATCH_DIR:-}" ]]; then
    PATCH_DIRS+=("$(cd "$PATCH_DIR" && pwd)")
else
    case "$RT_THREAD_REF" in
        v4.0.0|v4.0.1)
            PATCH_DIRS+=("$PATCH_ROOT/4.0.0-4.0.1")
            ;;
        v4.0.2)
            PATCH_DIRS+=("$PATCH_ROOT/4.0.2")
            ;;
        v4.0.3)
            PATCH_DIRS+=("$PATCH_ROOT/4.0.3")
            ;;
        v4.0.4|v4.0.5)
            PATCH_DIRS+=("$PATCH_ROOT/4.0.4-4.0.5")
            ;;
        v4.1.0|v4.1.1|v4.1.0-beta|v4.1.1-beta|lts-v4.1.x|origin/lts-v4.1.x)
            PATCH_DIRS+=("$PATCH_ROOT/4.1.x")
            ;;
        *)
            echo "[gdr-ci] FAILED: no default patch set for RT_THREAD_REF=$RT_THREAD_REF" >&2
            echo "[gdr-ci] set PATCH_DIR explicitly or add a versioned patch set" >&2
            exit 1
            ;;
    esac
fi
echo "[gdr-ci] patch dirs:"
for dir in "${PATCH_DIRS[@]}"; do
    echo "  $dir"
done

if [[ -d "$BUILD_DIR/.git" ]]; then
    echo "[gdr-ci] existing clone found; reusing"
    cd "$BUILD_DIR"
    git fetch --depth=1 origin "$RT_THREAD_REF"
    git checkout "$RT_THREAD_REF"
    # Hard-reset so any leftovers from a previous (failed) patch run are gone.
    git reset --hard "$RT_THREAD_REF" 2>/dev/null || true
else
    mkdir -p "$BUILD_DIR"
    git clone --depth=1 --branch "$RT_THREAD_REF" "$RT_THREAD_REPO" "$BUILD_DIR"
    cd "$BUILD_DIR"
fi

echo "[gdr-ci] applying patches"
shopt -s nullglob
patches=()
for dir in "${PATCH_DIRS[@]}"; do
    patches+=("$dir"/*.patch)
done
shopt -u nullglob
if [[ ${#patches[@]} -eq 0 ]]; then
    echo "[gdr-ci] FAILED: no .patch files found" >&2
    exit 1
fi
for patch in "${patches[@]}"; do
    name="$(basename "$patch")"
    if [[ "$RT_THREAD_REF" == v4.1.0* && "$name" == "003-warn-fix.patch" ]]; then
        echo "  $name (skipped for $RT_THREAD_REF)"
        continue
    fi
    if [[ "$RT_THREAD_REF" == lts-v4.1.x || "$RT_THREAD_REF" == origin/lts-v4.1.x ]] && [[ "$name" == "004-scons-deque-list.patch" ]]; then
        echo "  $name (skipped for $RT_THREAD_REF)"
        continue
    fi
    if [[ "$RT_THREAD_REF" == v4.0.0 && "$name" == "010-automac-python3-compat.patch" ]]; then
        echo "  $name (skipped for $RT_THREAD_REF)"
        continue
    fi
    if [[ "$RT_THREAD_REF" == v4.0.0 && "$name" == "009-newlib-posix-compat.patch" ]]; then
        echo "  $name (skipped for $RT_THREAD_REF)"
        continue
    fi
    if [[ "$RT_THREAD_REF" == v4.0.1 && "$name" == "011-v4.0.0-newlib-posix-compat.patch" ]]; then
        echo "  $name (skipped for $RT_THREAD_REF)"
        continue
    fi
    echo "  $name"
    # Apply strictly: after `git reset --hard` above the tree is pristine,
    # so any failure here is a real conflict, not a "already applied".
    if ! git apply --whitespace=fix "$patch"; then
        echo "[gdr-ci] FAILED: patch $name did not apply cleanly" >&2
        exit 1
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
