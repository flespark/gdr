#!/usr/bin/env bash
# Build an RT-Thread QEMU test fixture for GDR CI.
#
# Inputs (env):
#   RT_THREAD_REPO  - rt-thread git URL or local path (default: upstream)
#   RT_THREAD_REF   - ref to checkout (default: v4.0.5)
#   RT_THREAD_TARGET - cortex-a9 or rv64 (default: cortex-a9)
#   RT_THREAD_BSP   - BSP path override (default: selected from target + ref)
#   PATCH_DIR       - directory of *.patch files to apply (overrides auto selection)
#   BUILD_DIR        - working dir (default: /tmp/rt-thread-build)
#   OUT_ELF          - destination for rtthread.elf (default: BSP output)
#   OUT_BIN          - destination for rtthread.bin (RV64 only; default: BSP output)
#   CROSS_TOOL_PREFIX - toolchain prefix (selected from target by default)
#   RTOS_TOOLCHAIN_PATH - directory containing the selected compiler binaries
set -euo pipefail

RT_THREAD_REPO="${RT_THREAD_REPO:-https://github.com/RT-Thread/rt-thread.git}"
RT_THREAD_REF="${RT_THREAD_REF:-v4.0.5}"
RT_THREAD_TARGET="${RT_THREAD_TARGET:-cortex-a9}"
# Resolve paths to ABSOLUTE *before* any `cd` later in the script.
# Reason: if PATCH_DIR is left relative to the repo root, it becomes
# unresolvable after we `cd "$BUILD_DIR"` into the cloned tree, and the
# patch glob silently fails — leaving patches unapplied and breaking
# the build downstream.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH_ROOT="$SCRIPT_DIR/patches"
BUILD_DIR="${BUILD_DIR:-/tmp/rt-thread-build}"

case "$RT_THREAD_TARGET" in
    cortex-a9)
        BSP_DIR="${RT_THREAD_BSP:-bsp/qemu-vexpress-a9}"
        CROSS_TOOL_PREFIX="${CROSS_TOOL_PREFIX:-arm-none-eabi-}"
        case "$RT_THREAD_REF" in
            v4.0.0|v4.0.1)
                PATCH_SET="4.0.0-4.0.1"
                ;;
            v4.0.2|v4.0.3)
                PATCH_SET="${RT_THREAD_REF#v}"
                ;;
            v4.0.4|v4.0.5)
                PATCH_SET="4.0.4-4.0.5"
                ;;
            v4.1.0|v4.1.1|v4.1.0-beta|v4.1.1-beta|lts-v4.1.x|origin/lts-v4.1.x)
                PATCH_SET="4.1.x"
                ;;
            *)
                echo "[gdr-ci] FAILED: no Cortex-A9 patch set for RT_THREAD_REF=$RT_THREAD_REF" >&2
                exit 1
                ;;
        esac
        ;;
    rv64)
        CROSS_TOOL_PREFIX="${CROSS_TOOL_PREFIX:-riscv64-unknown-elf-}"
        case "$RT_THREAD_REF" in
            v4.0.4|v4.0.5)
                BSP_DIR="${RT_THREAD_BSP:-bsp/qemu-riscv-virt64}"
                PATCH_SET="4.0.4-4.0.5"
                ;;
            v4.1.0)
                BSP_DIR="${RT_THREAD_BSP:-bsp/qemu-riscv-virt64}"
                PATCH_SET="4.1.0"
                ;;
            v4.1.1)
                BSP_DIR="${RT_THREAD_BSP:-bsp/qemu-virt64-riscv}"
                PATCH_SET="4.1.1"
                ;;
            *)
                echo "[gdr-ci] FAILED: RV64 QEMU BSP is available only for v4.0.4-v4.1.1" >&2
                exit 1
                ;;
        esac
        ;;
    *)
        echo "[gdr-ci] FAILED: unknown RT_THREAD_TARGET=$RT_THREAD_TARGET" >&2
        exit 1
        ;;
esac

OUT_ELF="${OUT_ELF:-$BUILD_DIR/$BSP_DIR/rtthread.elf}"
OUT_BIN="${OUT_BIN:-$BUILD_DIR/$BSP_DIR/rtthread.bin}"
if [[ -z "${RTOS_TOOLCHAIN_PATH:-}" ]]; then
    CROSS_GCC="$(command -v "${CROSS_TOOL_PREFIX}gcc" || true)"
    if [[ -z "$CROSS_GCC" ]]; then
        echo "[gdr-ci] FAILED: ${CROSS_TOOL_PREFIX}gcc is not on PATH" >&2
        exit 1
    fi
    RTOS_TOOLCHAIN_PATH="$(dirname "$CROSS_GCC")"
fi

CROSS_GCC="$RTOS_TOOLCHAIN_PATH/${CROSS_TOOL_PREFIX}gcc"
if [[ ! -x "$CROSS_GCC" ]]; then
    echo "[gdr-ci] FAILED: expected compiler not found: $CROSS_GCC" >&2
    exit 1
fi
for tool in g++ ar objcopy objdump size; do
    if [[ ! -x "$RTOS_TOOLCHAIN_PATH/${CROSS_TOOL_PREFIX}$tool" ]]; then
        echo "[gdr-ci] FAILED: expected tool not found: ${CROSS_TOOL_PREFIX}$tool" >&2
        exit 1
    fi
done
if ! printf '#include <stdio.h>\n' | "$CROSS_GCC" -E -x c - >/dev/null; then
    echo "[gdr-ci] FAILED: ${CROSS_TOOL_PREFIX}gcc cannot locate newlib stdio.h" >&2
    exit 1
fi

# Reason: always re-checkout the ref so stale changes do not survive CI reruns.
echo "[gdr-ci] RT-Thread repo: $RT_THREAD_REPO@$RT_THREAD_REF"
echo "[gdr-ci] target: $RT_THREAD_TARGET ($BSP_DIR)"
echo "[gdr-ci] toolchain: $CROSS_GCC"
echo "[gdr-ci] build dir: $BUILD_DIR"

PATCH_DIRS=()
if [[ -n "${PATCH_DIR:-}" ]]; then
    PATCH_DIRS+=("$(cd "$PATCH_DIR" && pwd)")
else
    PATCH_DIRS+=("$PATCH_ROOT/$RT_THREAD_TARGET/$PATCH_SET")
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

cd "$BSP_DIR"

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
ELF_ABS="$BUILD_DIR/$BSP_DIR/rtthread.elf"
echo "[gdr-ci] build OK: rtthread.elf ($ELF_SIZE)"
echo "[gdr-ci] OUT_ELF=$ELF_ABS"
# Copy to caller-specified destination if OUT_ELF differs from the in-tree one.
if [[ "$OUT_ELF" != "$ELF_ABS" ]]; then
    mkdir -p "$(dirname "$OUT_ELF")"
    cp rtthread.elf "$OUT_ELF"
    echo "[gdr-ci] copied to $OUT_ELF"
fi

if [[ "$RT_THREAD_TARGET" == "rv64" ]]; then
    if [[ ! -f rtthread.bin ]]; then
        echo "[gdr-ci] FAILED: rtthread.bin not produced for RV64" >&2
        exit 1
    fi
    BIN_ABS="$BUILD_DIR/$BSP_DIR/rtthread.bin"
    echo "[gdr-ci] OUT_BIN=$BIN_ABS"
    if [[ "$OUT_BIN" != "$BIN_ABS" ]]; then
        mkdir -p "$(dirname "$OUT_BIN")"
        cp rtthread.bin "$OUT_BIN"
        echo "[gdr-ci] copied to $OUT_BIN"
    fi
fi
echo "$ELF_ABS"
