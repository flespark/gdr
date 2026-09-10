#!/usr/bin/env bash
# Build a FreeRTOS QEMU fixture directly from FreeRTOS-Kernel.
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
#
# ``--variant snapshot`` builds the static snapshot instead: a file-only
# Cortex-M33 ELF pair from fixture/config/snapshot/ for the data-corruption
# negatives a healthy kernel cannot produce. It compiles against kernel
# headers only (no kernel sources, port or board) and never boots QEMU, so
# --out-bin is unused on this variant.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/fixture-common.sh"
DEFAULT_REPO="https://github.com/FreeRTOS/FreeRTOS-Kernel.git"
DEFAULT_TAG="V11.1.0"
DEFAULT_KERNEL_DIR="/tmp/gdr-freertos-kernel-source"
DEFAULT_TARGET="mps2-an385"
DEFAULT_VARIANT="base"
DEFAULT_BUILD_DIR="/tmp/gdr-freertos-kernel-build"
TOOLCHAIN_PREFIX="arm-none-eabi-"
# Reason: the RISC-V lane compiles with the riscv-none-elf- toolchain; the
# prefix is resolved after --target is parsed (setup_toolchain runs in main).
toolchain_prefix_for() {
    case "$TARGET" in
    qemu-virt-rv64) echo "riscv-none-elf-" ;;
    *) echo "$TOOLCHAIN_PREFIX" ;;
    esac
}

# V10.5.1 / V10.3.1-kernel-only -> 10.5.1 / 10.3.1
version_from_tag() {
    local tag="${1#V}"
    echo "${tag%-kernel-only}"
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

board_flags() {
    case "$TARGET" in
    mps2-an385)
        echo "-mcpu=cortex-m3 -mthumb"
        ;;
    mps2-an521)
        # Reason: the AN521 runs two Cortex-M33s; pin the FPU off to match
        # configENABLE_FPU 0 so the lazy-stacking path stays out of the build.
        echo "-mcpu=cortex-m33 -mthumb -mfloat-abi=soft"
        ;;
    qemu-virt-rv64)
        # Reason: integer-only rv64imac keeps portContext.h's FPU sections out
        # (configENABLE_FPU 0); medany makes every RAM address reachable from
        # the 0x80000000 load base.  The explicit _zicsr suffix is required by
        # the xPack 15.2 assembler, which splits the I extension and rejects
        # CSR opcodes (port.c/portASM.S use csrr/csrs heavily) under plain
        # rv64imac.
        echo "-march=rv64imac_zicsr -mabi=lp64 -mcmodel=medany"
        ;;
    *)
        die "unsupported kernel-direct target: $TARGET"
        ;;
    esac
}

compile_fixture() {
    local cc="$TOOLCHAIN_PATH/$(toolchain_prefix_for)gcc"
    local objcopy="$TOOLCHAIN_PATH/$(toolchain_prefix_for)objcopy"
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
    mps2-an521) port_dir="$KERNEL_DIR/portable/GCC/ARM_CM33_NTZ/non_secure" ;;
    qemu-virt-rv64) port_dir="$KERNEL_DIR/portable/GCC/RISC-V" ;;
    esac

    local -a port_sources=()
    local -a chip_include_paths=()
    case "$TARGET" in
    mps2-an521)
        # Reason: the CM33_NTZ port keeps context switching and the SVC/
        # PendSV handler in portasm.c -- ARM_CM3 has only port.c, so the
        # kernel-direct lane used to compile exactly one port file.  Omitting
        # portasm.c fails only at link time (missing SVC_Handler /
        # PendSV_Handler / vRestoreContextOfFirstTask), which reads like a
        # wrong-port error.  cpu1_start.c is this board's secondary-core
        # bootstrap and lives alongside the other board sources.
        port_sources+=("$port_dir/portasm.c" "$board_dir/cpu1_start.c")
        if [[ "$VARIANT" == "mpu" ]]; then
            # Reason: the MPU wrappers v2 (portable/Common/mpu_wrappers_v2.c
            # + this port's mpu_wrappers_v2_asm.c) implement the SVC gate;
            # without them a configENABLE_MPU build fails to link on the
            # MPU_xQueueGenericCreate / MPU_GetFreeIndexInKernelObjectPool
            # references from port.c.
            port_sources+=(
                "$KERNEL_DIR/portable/Common/mpu_wrappers_v2.c"
                "$port_dir/mpu_wrappers_v2_asm.c"
            )
        fi
        ;;
    qemu-virt-rv64)
        # Reason: the RISC-V port keeps the context switch and M-mode trap
        # handler in portASM.S (port.c alone would link but never switch
        # tasks).  The chip-specific extension header (mtime/CLINT macros)
        # is found through the preprocessor include path of the assembler
        # pass, exactly as the port readme demands; each entry carries the
        # -I prefix because it is appended to cflags, not sources.
        port_sources+=("$port_dir/portASM.S")
        chip_include_paths+=(
            "-I$KERNEL_DIR/portable/GCC/RISC-V/chip_specific_extensions/RISCV_MTIME_CLINT_no_extensions"
        )
        ;;
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
        -I"$SCRIPT_DIR/fixture/common"
        -I"$KERNEL_DIR/include"
        -I"$port_dir"
    )
    # Reason: set -u treats "${chip_include_paths[@]}" on an empty array as
    # an unbound-variable error on older bash (the same reason port_sources
    # is gated below), so only expand when the target added paths.
    if [[ ${#chip_include_paths[@]} -gt 0 ]]; then
        cflags+=("${chip_include_paths[@]}")
    fi
    if [[ "$VARIANT" == "mpu" ]]; then
        # Reason: upstream portable/Common/mpu_wrappers_v2.c and the CM33
        # mpu_wrappers_v2_asm.c are not -Werror-clean (unused parameters in
        # the wrapper thunks that only mirror their FreeRTOS.h prototypes);
        # the -Werror flag is ours, so the suppression is scoped to the
        # variant that links those files.
        cflags+=(-Wno-unused-parameter)
    fi
    # Reason: set -u treats "${port_sources[@]}" on an empty array as an
    # unbound-variable error on older bash, so gate the expansion on length.
    if [[ ${#port_sources[@]} -gt 0 ]]; then
        sources=(
            "$SCRIPT_DIR/fixture/main.c"
            "$board_dir/system_init.c"
            "$SCRIPT_DIR/fixture/common/syscalls.c"
            "$SCRIPT_DIR/fixture/common/runtime_timer.c"
            "$board_dir/startup.s"
            "$KERNEL_DIR/tasks.c" "$KERNEL_DIR/queue.c" "$KERNEL_DIR/list.c"
            "$KERNEL_DIR/timers.c" "$KERNEL_DIR/event_groups.c"
            "$KERNEL_DIR/stream_buffer.c"
            "$port_dir/port.c"
            "${port_sources[@]}"
        )
    else
        sources=(
            "$SCRIPT_DIR/fixture/main.c"
            "$board_dir/system_init.c"
            "$SCRIPT_DIR/fixture/common/syscalls.c"
            "$SCRIPT_DIR/fixture/common/runtime_timer.c"
            "$board_dir/startup.s"
            "$KERNEL_DIR/tasks.c" "$KERNEL_DIR/queue.c" "$KERNEL_DIR/list.c"
            "$KERNEL_DIR/timers.c" "$KERNEL_DIR/event_groups.c"
            "$KERNEL_DIR/stream_buffer.c"
            "$port_dir/port.c"
        )
    fi
    if [[ -n "$heap" ]]; then
        sources+=("$heap")
    fi

    # Reason: --specs=nano.specs/nosys.specs are ARM multilib specs; the
    # RISC-V toolchain carries its own nano/nosys variants, so gate the
    # specs flags per target.  The empty-array expansion must also be gated
    # (set -u on bash 3.2 errors on an empty "${specs_flags[@]}").
    local -a specs_flags=()
    if [[ "$TARGET" == "qemu-virt-rv64" ]]; then
        specs_flags=(--specs=nano.specs --specs=nosys.specs)
    fi

    if [[ ${#specs_flags[@]} -gt 0 ]]; then
        "$cc" "${cflags[@]}" "${sources[@]}" \
            -T"$board_dir/linker.ld" \
            -Wl,--gc-sections -Wl,--undefined=gdr_freertos_version_num \
            -Wl,-Map,"$BUILD_DIR/freertos.map" \
            -nostartfiles -o "$OUT_ELF" "${specs_flags[@]}"
    else
        "$cc" "${cflags[@]}" "${sources[@]}" \
            -T"$board_dir/linker.ld" \
            -Wl,--gc-sections -Wl,--undefined=gdr_freertos_version_num \
            -Wl,-Map,"$BUILD_DIR/freertos.map" \
            -nostartfiles -o "$OUT_ELF"
    fi
    "$objcopy" -O binary "$OUT_ELF" "$OUT_BIN"
    echo "[gdr-ci] built ELF: $OUT_ELF"
    echo "[gdr-ci] built BIN: $OUT_BIN"
}

# Reason: the static snapshot is the negative-testing variant — a single
# hand-written TU whose .data holds corrupt scheduler structures a healthy
# kernel cannot produce. It needs only kernel *headers* (no kernel sources,
# no port, no board), compiles freestanding with the portmacro.h/linker.ld
# that ship inside the variant directory, and produces two ELFs: the main
# snapshot plus the heap-only allocated-bit negative.
compile_snapshot() {
    local cc="$TOOLCHAIN_PATH/$(toolchain_prefix_for)gcc"
    local config_dir="$SCRIPT_DIR/fixture/config/$VARIANT"
    local heap_elf
    [[ -d "$config_dir" ]] || die "unknown variant: $VARIANT ($config_dir)"
    heap_elf="${OUT_ELF%.elf}_heap.elf"
    mkdir -p "$(dirname "$OUT_ELF")"
    echo "[gdr-ci] FreeRTOS-Kernel: $REPO@$TAG ($(git -C "$KERNEL_DIR" rev-parse HEAD))"
    echo "[gdr-ci] target: $TARGET variant: $VARIANT (static snapshot, no QEMU)"
    echo "[gdr-ci] compiler: $($cc --version | head -1)"
    local -a snap_flags=(
        -mcpu=cortex-m33 -mthumb -Og -g3 -std=c11
        -Wall -Wextra -Werror -ffunction-sections -fdata-sections -fno-lto
        -I"$config_dir" -I"$KERNEL_DIR/include"
    )
    "$cc" "${snap_flags[@]}" "$config_dir/snapshot.c" \
        -T"$config_dir/linker.ld" -nostdlib -nostartfiles \
        -Wl,--gc-sections -o "$OUT_ELF"
    "$cc" "${snap_flags[@]}" "$config_dir/snapshot_heap.c" \
        -T"$config_dir/linker.ld" -nostdlib -nostartfiles \
        -Wl,--gc-sections -o "$heap_elf"
    echo "[gdr-ci] built ELF: $OUT_ELF"
    echo "[gdr-ci] built ELF: $heap_elf"
}

# The snapshot installs its own artifact pair (no BIN/MAP: QEMU never boots
# it) instead of install_to_cache.
install_snapshot() {
    [[ "$CACHE_INSTALL" == 1 ]] || return 0
    local heap_elf="${OUT_ELF%.elf}_heap.elf"
    mkdir -p "$CACHE_DIR"
    [[ "$OUT_ELF" == "$CACHE_DIR/freertos.elf" ]] ||
        cp -f "$OUT_ELF" "$CACHE_DIR/freertos.elf"
    [[ "$heap_elf" == "$CACHE_DIR/snapshot_heap.elf" ]] ||
        cp -f "$heap_elf" "$CACHE_DIR/snapshot_heap.elf"
    echo "[gdr-ci] cached snapshot: $CACHE_DIR/freertos.elf + snapshot_heap.elf"
}

# Mirror the artifacts into the shared fixture cache.

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
            usage_from_header 28
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
    # shellcheck disable=SC2034 # read by the sourced install_to_cache()
    CACHE_INSTALL=1
    TOOLCHAIN_PATH=""
    KERNEL_DIR_EXPLICIT=0

    parse_args "$@"
    OUT_ELF="${OUT_ELF:-$BUILD_DIR/freertos.elf}"
    OUT_BIN="${OUT_BIN:-$BUILD_DIR/freertos.bin}"
    VERSION="${VERSION:-$(version_from_tag "$TAG")}"
    CACHE_ROOT="$(fixture_cache_root)"
    CACHE_DIR="${CACHE_DIR:-$CACHE_ROOT/$TARGET/$VERSION/$VARIANT}"
    setup_toolchain "$(toolchain_prefix_for)"
    prepare_kernel
    if [[ "$VARIANT" == "snapshot" ]]; then
        compile_snapshot
        install_snapshot
    else
        compile_fixture
        install_to_cache
    fi
}

main "$@"
