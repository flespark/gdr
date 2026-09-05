#!/usr/bin/env bash
# Shared fixture-builder plumbing for the FreeRTOS closed-loop lanes.
#
# Sourced by build-fixture-cubel4.sh / build-fixture-kernel.sh (and the
# snapshot builder for die/usage).  Each builder keeps its own source
# acquisition (Cube sparse clone vs FreeRTOS-Kernel tag clone), compile
# flags and cache coordinates; everything generic (errors, usage text,
# toolchain resolution, heap source selection, cache install) lives here
# so the three builders cannot drift.

# Error with a uniform [gdr-ci] prefix.
die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

# Print the caller's leading comment block as usage (the `#` header).
# Usage: usage_from_header <last-line-of-header>
usage_from_header() {
    sed -n "2,${1}p" "$0" | sed 's/^# \?//' | sed '/^$/d'
}

# Resolve TOOLCHAIN_PATH and verify gcc/objcopy exist.
# Takes the toolchain prefix (e.g. arm-none-eabi- or riscv-none-elf-).
setup_toolchain() {
    local prefix="${1:-arm-none-eabi-}"
    local gcc tool
    if [[ -z "$TOOLCHAIN_PATH" ]]; then
        gcc="$(command -v "${prefix}gcc" || true)"
        [[ -n "$gcc" ]] || die "${prefix}gcc is not on PATH"
        TOOLCHAIN_PATH="$(dirname "$gcc")"
    fi
    for tool in gcc objcopy; do
        [[ -x "$TOOLCHAIN_PATH/${prefix}$tool" ]] ||
            die "required tool not found: $TOOLCHAIN_PATH/${prefix}$tool"
    done
}

# Select the heap allocator source for a variant.  Every lane maps its
# variant to one MemMang source; heap-5-protector (kernel-direct) is the
# only extra spelling and reuses heap_5.c.
heap_source() {
    local kernel="$1"
    case "$VARIANT" in
    static-only) return 0 ;;
    heap-1) echo "$kernel/portable/MemMang/heap_1.c" ;;
    heap-2) echo "$kernel/portable/MemMang/heap_2.c" ;;
    heap-3) echo "$kernel/portable/MemMang/heap_3.c" ;;
    heap-5 | heap-5-protector) echo "$kernel/portable/MemMang/heap_5.c" ;;
    *) echo "$kernel/portable/MemMang/heap_4.c" ;;
    esac
}

# Copy the freshly linked artifacts into the shared fixture cache so every
# consumer (pytest, run-qemu-matrix.sh, other machines' rsync) reads one
# canonical layout: <cache>/<target>/<version>/<variant>/freertos.{elf,bin}.
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

# Default fixture cache root (overridable via FREERTOS_FIXTURE_CACHE).
fixture_cache_root() {
    echo "${FREERTOS_FIXTURE_CACHE:-$HOME/Project/gdr-fixture/freertos}"
}
