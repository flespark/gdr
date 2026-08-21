#!/usr/bin/env bash
# Build an RT-Thread QEMU test fixture for GDR CI.
#
# Usage:
#   build-rtt.sh --target <cortex-a9|rv64> --ref <version-ref> [options]
#
# --ref is the RT-Thread release tag or branch to check out (for example
# v4.0.5 or lts-v4.1.x). Patch sets and BSP paths are selected from that
# version-ref.
#
# Options:
#   --repo URL              RT-Thread git URL or local path
#   --ref VERSION-REF       RT-Thread release tag or branch (default: v4.0.5)
#   --bsp PATH              BSP path override
#   --patch-dir DIR         directory of *.patch files (replaces auto selection)
#   --build-dir DIR         working clone directory
#   --out-elf PATH          destination for rtthread.elf
#   --out-bin PATH          destination for rtthread.bin (RV64)
#   --toolchain-path DIR    directory containing the compiler binaries
#   --toolchain-prefix STR  cross-compiler prefix
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH_ROOT="$SCRIPT_DIR/patches"
DEFAULT_REPO="https://github.com/RT-Thread/rt-thread.git"
DEFAULT_BUILD_DIR="/tmp/rt-thread-build"

die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

usage() {
    sed -n '3,20p' "$0" | sed 's/^# \?//'
}

# Fill BSP_DIR / PATCH_SET / TOOLCHAIN_PREFIX from TARGET + REF when unset.
resolve_target_defaults() {
    case "$TARGET" in
        cortex-a9)
            BSP_DIR="${BSP_DIR:-bsp/qemu-vexpress-a9}"
            TOOLCHAIN_PREFIX="${TOOLCHAIN_PREFIX:-arm-none-eabi-}"
            case "$REF" in
                v3.1.0|v3.1.1|v3.1.2|v3.1.3|v3.1.4) PATCH_SET="3.1.x" ;;
                v3.1.5) PATCH_SET="3.1.5" ;;
                v4.0.0|v4.0.1) PATCH_SET="4.0.0-4.0.1" ;;
                v4.0.2|v4.0.3) PATCH_SET="${REF#v}" ;;
                v4.0.4|v4.0.5) PATCH_SET="4.0.4-4.0.5" ;;
                v4.1.0|v4.1.1|v4.1.0-beta|v4.1.1-beta|lts-v4.1.x|origin/lts-v4.1.x)
                    PATCH_SET="4.1.x"
                    ;;
                *) die "no Cortex-A9 patch set for ref=$REF" ;;
            esac
            ;;
        rv64)
            TOOLCHAIN_PREFIX="${TOOLCHAIN_PREFIX:-riscv64-unknown-elf-}"
            case "$REF" in
                v4.0.4|v4.0.5)
                    BSP_DIR="${BSP_DIR:-bsp/qemu-riscv-virt64}"
                    PATCH_SET="4.0.4-4.0.5"
                    ;;
                v4.1.0)
                    BSP_DIR="${BSP_DIR:-bsp/qemu-riscv-virt64}"
                    PATCH_SET="4.1.0"
                    ;;
                v4.1.1)
                    BSP_DIR="${BSP_DIR:-bsp/qemu-virt64-riscv}"
                    PATCH_SET="4.1.1"
                    ;;
                *) die "RV64 QEMU BSP is available only for v4.0.4-v4.1.1" ;;
            esac
            ;;
        *)
            die "unknown target=$TARGET"
            ;;
    esac
}

# Resolve TOOLCHAIN_PATH and verify the cross tools can preprocess newlib headers.
setup_toolchain() {
    local gcc tool
    if [[ -z "$TOOLCHAIN_PATH" ]]; then
        gcc="$(command -v "${TOOLCHAIN_PREFIX}gcc" || true)"
        [[ -n "$gcc" ]] || die "${TOOLCHAIN_PREFIX}gcc is not on PATH"
        TOOLCHAIN_PATH="$(dirname "$gcc")"
    fi
    gcc="$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}gcc"
    [[ -x "$gcc" ]] || die "expected compiler not found: $gcc"
    for tool in g++ ar objcopy objdump size; do
        [[ -x "$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}$tool" ]] || \
            die "expected tool not found: ${TOOLCHAIN_PREFIX}$tool"
    done
    if ! printf '#include <stdio.h>\n' | "$gcc" -E -x c - >/dev/null; then
        die "${TOOLCHAIN_PREFIX}gcc cannot locate newlib stdio.h"
    fi
}

# Populate PATCH_DIRS for the selected target/ref (or an explicit --patch-dir).
collect_patch_dirs() {
    PATCH_DIRS=()
    if [[ -n "$PATCH_DIR" ]]; then
        PATCH_DIRS+=("$(cd "$PATCH_DIR" && pwd)")
        return
    fi
    PATCH_DIRS+=("$PATCH_ROOT/$TARGET/$PATCH_SET")
    if [[ "$TARGET" == "cortex-a9" ]]; then
        case "$REF" in
            v3.1.0|v3.1.1|v3.1.2)
                PATCH_DIRS+=("$PATCH_ROOT/$TARGET/3.1.0-3.1.2")
                ;;
            v3.1.3|v3.1.4|v3.1.5)
                PATCH_DIRS+=("$PATCH_ROOT/$TARGET/3.1.3-3.1.5")
                ;;
        esac
        # Reason: v3.1.5 has a different main.c baseline, but still needs the
        # non-fixture compatibility patches shared by the full 3.1 series.
        if [[ "$REF" == "v3.1.5" ]]; then
            PATCH_DIRS+=("$PATCH_ROOT/$TARGET/3.1.x")
        fi
        # Reason: only 3.1.0 predates the DFS _EXFUN guard needed by xPack's
        # modern newlib; it was incorporated upstream in 3.1.1.
        if [[ "$REF" == "v3.1.0" ]]; then
            PATCH_DIRS+=("$PATCH_ROOT/$TARGET/3.1.0")
        fi
    fi
}

checkout_source() {
    # Reason: always re-checkout the ref so stale changes do not survive CI reruns.
    if [[ -d "$BUILD_DIR/.git" ]]; then
        echo "[gdr-ci] existing clone found; reusing"
        git -C "$BUILD_DIR" fetch --depth=1 origin "$REF"
        git -C "$BUILD_DIR" checkout "$REF"
    else
        mkdir -p "$BUILD_DIR"
        git clone --depth=1 --branch "$REF" "$REPO" "$BUILD_DIR"
    fi
    # BUILD_DIR is a disposable, version-specific clone. Reset tracked files and
    # remove untracked/ignored SCons outputs so every invocation is a clean build.
    git -C "$BUILD_DIR" reset --hard "$REF"
    git -C "$BUILD_DIR" clean -ffdxq
}

apply_patches() {
    local dir patches=() patch name
    echo "[gdr-ci] applying patches"
    shopt -s nullglob
    for dir in "${PATCH_DIRS[@]}"; do
        patches+=("$dir"/*.patch)
    done
    shopt -u nullglob
    [[ ${#patches[@]} -gt 0 ]] || die "no .patch files found"
    for patch in "${patches[@]}"; do
        name="$(basename "$patch")"
        if [[ "$REF" == v3.1.5 && \
            "$patch" == "$PATCH_ROOT/cortex-a9/3.1.x/001-test-fixture-main.patch" ]]; then
            echo "  $name (replaced by the v3.1.5-specific fixture patch)"
            continue
        fi
        if [[ "$REF" == v4.1.0* && "$name" == "003-warn-fix.patch" ]]; then
            echo "  $name (skipped for $REF)"
            continue
        fi
        if [[ "$REF" == lts-v4.1.x || "$REF" == origin/lts-v4.1.x ]] && \
            [[ "$name" == "004-scons-deque-list.patch" ]]; then
            echo "  $name (skipped for $REF)"
            continue
        fi
        if [[ "$REF" == v4.0.0 && "$name" == "010-automac-python3-compat.patch" ]]; then
            echo "  $name (skipped for $REF)"
            continue
        fi
        if [[ "$REF" == v4.0.0 && "$name" == "009-newlib-posix-compat.patch" ]]; then
            echo "  $name (skipped for $REF)"
            continue
        fi
        if [[ "$REF" == v4.0.1 && "$name" == "011-v4.0.0-newlib-posix-compat.patch" ]]; then
            echo "  $name (skipped for $REF)"
            continue
        fi
        echo "  $name"
        # Apply strictly: after reset/clean above the tree is pristine, so any
        # failure here is a real conflict, not an "already applied" patch.
        if ! git -C "$BUILD_DIR" apply --whitespace=fix "$patch"; then
            die "patch $name did not apply cleanly"
        fi
    done
}

build_bsp() {
    local elf_abs="$BUILD_DIR/$BSP_DIR/rtthread.elf"
    local bin_abs="$BUILD_DIR/$BSP_DIR/rtthread.bin"
    local jobs python2 scons python2_lib

    (
        cd "$BUILD_DIR/$BSP_DIR"
        # RT-Thread scons picks up RTT_EXEC_PATH for the toolchain, RTT_CC for
        # the compiler. Use the host cross-toolchain instead of the env-managed one.
        export RTT_CC=gcc
        export RTT_EXEC_PATH="$TOOLCHAIN_PATH"
        echo "[gdr-ci] scons (may take a minute)..."
        jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu || echo 4)"
        # Reason: RT-Thread 3.1.x's tools/building.py has Python 2 syntax, while
        # 4.x is verified with the default Python 3/SCons 4 environment.
        if [[ -n "${SCONS_BIN:-}" ]]; then
            echo "[gdr-ci] scons command: $SCONS_BIN"
            "$SCONS_BIN" -j"$jobs"
        elif [[ "$REF" == v3.1.* ]]; then
            python2="${PYTHON2_BIN:-/opt/python2/bin/python}"
            scons="${PYTHON2_SCONS:-/opt/python2/bin/scons}"
            # Reason: the relocated /opt/python2 interpreter needs its private
            # libpython/OpenSSL path only for this process, not the whole container.
            python2_lib="$(cd "$(dirname "$python2")/../lib" && pwd)"
            echo "[gdr-ci] scons command: $python2 $scons"
            env LD_LIBRARY_PATH="$python2_lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
                "$python2" "$scons" -j"$jobs"
        else
            echo "[gdr-ci] scons command: /opt/scons-venv/bin/scons"
            /opt/scons-venv/bin/scons -j"$jobs"
        fi
        [[ -f rtthread.elf ]] || die "rtthread.elf not produced"
        if [[ "$TARGET" == "rv64" && ! -f rtthread.bin ]]; then
            die "rtthread.bin not produced for RV64"
        fi
    )
    echo "[gdr-ci] build OK: rtthread.elf ($(du -h "$elf_abs" | cut -f1))"
    echo "[gdr-ci] OUT_ELF=$elf_abs"
    if [[ "$OUT_ELF" != "$elf_abs" ]]; then
        mkdir -p "$(dirname "$OUT_ELF")"
        cp "$elf_abs" "$OUT_ELF"
        echo "[gdr-ci] copied to $OUT_ELF"
    fi
    if [[ "$TARGET" == "rv64" ]]; then
        echo "[gdr-ci] OUT_BIN=$bin_abs"
        if [[ "$OUT_BIN" != "$bin_abs" ]]; then
            mkdir -p "$(dirname "$OUT_BIN")"
            cp "$bin_abs" "$OUT_BIN"
            echo "[gdr-ci] copied to $OUT_BIN"
        fi
    fi
    echo "$elf_abs"
}

parse_args() {
    local -a leftover=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target) TARGET="$2"; shift 2 ;;
            --ref) REF="$2"; shift 2 ;;
            --repo) REPO="$2"; shift 2 ;;
            --bsp) BSP_DIR="$2"; shift 2 ;;
            --patch-dir) PATCH_DIR="$2"; shift 2 ;;
            --build-dir) BUILD_DIR="$2"; shift 2 ;;
            --out-elf) OUT_ELF="$2"; shift 2 ;;
            --out-bin) OUT_BIN="$2"; shift 2 ;;
            --toolchain-path) TOOLCHAIN_PATH="$2"; shift 2 ;;
            --toolchain-prefix) TOOLCHAIN_PREFIX="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) leftover+=("$1"); shift ;;
        esac
    done
    if [[ ${#leftover[@]} -ne 0 ]]; then
        die "unknown argument: ${leftover[*]}"
    fi
}

main() {
    TARGET="cortex-a9"
    REF="v4.0.5"
    REPO="$DEFAULT_REPO"
    BSP_DIR=""
    PATCH_DIR=""
    BUILD_DIR="$DEFAULT_BUILD_DIR"
    OUT_ELF=""
    OUT_BIN=""
    TOOLCHAIN_PATH=""
    TOOLCHAIN_PREFIX=""
    PATCH_SET=""
    local dir
    local -a PATCH_DIRS

    parse_args "$@"
    resolve_target_defaults
    OUT_ELF="${OUT_ELF:-$BUILD_DIR/$BSP_DIR/rtthread.elf}"
    OUT_BIN="${OUT_BIN:-$BUILD_DIR/$BSP_DIR/rtthread.bin}"
    setup_toolchain
    collect_patch_dirs

    echo "[gdr-ci] RT-Thread repo: $REPO@$REF"
    echo "[gdr-ci] target: $TARGET ($BSP_DIR)"
    echo "[gdr-ci] toolchain: $TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}gcc"
    echo "[gdr-ci] build dir: $BUILD_DIR"
    echo "[gdr-ci] patch dirs:"
    for dir in "${PATCH_DIRS[@]}"; do
        echo "  $dir"
    done

    checkout_source
    apply_patches
    build_bsp
}

main "$@"
