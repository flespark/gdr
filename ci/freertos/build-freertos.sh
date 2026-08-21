#!/usr/bin/env bash
# Build the locked FreeRTOS V10.3.1 B-L475E-IOT01A QEMU fixture.
#
# Usage:
#   build-freertos.sh [options]
#
# Options:
#   --cube-repo URL         STM32CubeL4 git URL
#   --cube-ref REF          STM32CubeL4 tag
#   --cube-dir DIR          local STM32CubeL4 checkout
#   --build-dir DIR         object and map output directory
#   --out-elf PATH          destination ELF
#   --out-bin PATH          destination BIN
#   --toolchain-path DIR    directory containing arm-none-eabi-* binaries
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_CUBE_REPO="https://github.com/STMicroelectronics/STM32CubeL4.git"
DEFAULT_CUBE_REF="v1.18.2"
DEFAULT_CUBE_DIR="/tmp/stm32cubel4-v1.18.2"
DEFAULT_BUILD_DIR="/tmp/gdr-freertos-build"
DEFAULT_ELF_NAME="freertos_b_l475e_iot01a.elf"
DEFAULT_BIN_NAME="freertos_b_l475e_iot01a.bin"
TOOLCHAIN_PREFIX="arm-none-eabi-"

die() {
    echo "[gdr-ci] FAILED: $*" >&2
    exit 1
}

usage() {
    sed -n '3,14p' "$0" | sed 's/^# \?//'
}

# Resolve TOOLCHAIN_PATH and verify arm-none-eabi-{gcc,objcopy}.
setup_toolchain() {
    local gcc tool
    if [[ -z "$TOOLCHAIN_PATH" ]]; then
        gcc="$(command -v "${TOOLCHAIN_PREFIX}gcc" || true)"
        [[ -n "$gcc" ]] || die "${TOOLCHAIN_PREFIX}gcc is not on PATH"
        TOOLCHAIN_PATH="$(dirname "$gcc")"
    fi
    for tool in gcc objcopy; do
        [[ -x "$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}$tool" ]] || \
            die "required tool not found: $TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}$tool"
    done
}

prepare_cube() {
    if [[ ! -d "$CUBE_DIR/.git" ]]; then
        git clone --depth=1 --branch "$CUBE_REF" --filter=blob:none --sparse \
            "$CUBE_REPO" "$CUBE_DIR"
    fi
    git -C "$CUBE_DIR" fetch --depth=1 origin "refs/tags/$CUBE_REF:refs/tags/$CUBE_REF"
    git -C "$CUBE_DIR" checkout --detach "$CUBE_REF"
    git -C "$CUBE_DIR" sparse-checkout set \
        Drivers/CMSIS/Core/Include \
        Projects/B-L475E-IOT01A/Applications/FreeRTOS/FreeRTOS_LowPower_LPTIM
    git -C "$CUBE_DIR" submodule update --init --depth=1 \
        Drivers/CMSIS/Device/ST/STM32L4xx Middlewares/Third_Party/FreeRTOS
}

compile_fixture() {
    local cc="$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}gcc"
    local objcopy="$TOOLCHAIN_PATH/${TOOLCHAIN_PREFIX}objcopy"
    local kernel="$CUBE_DIR/Middlewares/Third_Party/FreeRTOS/Source"
    local device="$CUBE_DIR/Drivers/CMSIS/Device/ST/STM32L4xx"
    local project="$CUBE_DIR/Projects/B-L475E-IOT01A/Applications/FreeRTOS/FreeRTOS_LowPower_LPTIM"
    local -a cflags sources

    mkdir -p "$BUILD_DIR" "$(dirname "$OUT_ELF")" "$(dirname "$OUT_BIN")"
    echo "[gdr-ci] STM32CubeL4: $CUBE_REPO@$CUBE_REF ($(git -C "$CUBE_DIR" rev-parse HEAD))"
    echo "[gdr-ci] FreeRTOS: $(git -C "$kernel/.." rev-parse HEAD)"
    echo "[gdr-ci] compiler: $($cc --version | head -1)"

    cflags=(
        -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard
        -Og -g3 -ffunction-sections -fdata-sections -fno-lto
        -Wall -Wextra -Werror -std=c11
        -DSTM32L475xx -DUSE_FULL_LL_DRIVER
        -I"$SCRIPT_DIR/fixture"
        -I"$CUBE_DIR/Drivers/CMSIS/Core/Include"
        -I"$device/Include"
        -I"$kernel/include"
        -I"$kernel/portable/GCC/ARM_CM4F"
    )
    sources=(
        "$SCRIPT_DIR/fixture/main.c"
        "$project/Src/system_stm32l4xx.c"
        "$project/STM32CubeIDE/Applications/Startup/startup_stm32l475vgtx.s"
        "$kernel/tasks.c" "$kernel/queue.c" "$kernel/list.c" "$kernel/timers.c"
        "$kernel/portable/MemMang/heap_4.c" "$kernel/portable/GCC/ARM_CM4F/port.c"
    )

    "$cc" "${cflags[@]}" "${sources[@]}" \
        -T"$project/STM32CubeIDE/STM32L475VGTX_FLASH.ld" \
        -Wl,--gc-sections -Wl,-Map,"$BUILD_DIR/freertos.map" \
        --specs=nano.specs --specs=nosys.specs -o "$OUT_ELF"
    "$objcopy" -O binary "$OUT_ELF" "$OUT_BIN"
    echo "[gdr-ci] built ELF: $OUT_ELF"
    echo "[gdr-ci] built BIN: $OUT_BIN"
}

parse_args() {
    local -a leftover=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --cube-repo) CUBE_REPO="$2"; shift 2 ;;
            --cube-ref) CUBE_REF="$2"; shift 2 ;;
            --cube-dir) CUBE_DIR="$2"; shift 2 ;;
            --build-dir) BUILD_DIR="$2"; shift 2 ;;
            --out-elf) OUT_ELF="$2"; shift 2 ;;
            --out-bin) OUT_BIN="$2"; shift 2 ;;
            --toolchain-path) TOOLCHAIN_PATH="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) leftover+=("$1"); shift ;;
        esac
    done
    if [[ ${#leftover[@]} -ne 0 ]]; then
        die "unknown argument: ${leftover[*]}"
    fi
}

main() {
    CUBE_REPO="$DEFAULT_CUBE_REPO"
    CUBE_REF="$DEFAULT_CUBE_REF"
    CUBE_DIR="$DEFAULT_CUBE_DIR"
    BUILD_DIR="$DEFAULT_BUILD_DIR"
    OUT_ELF=""
    OUT_BIN=""
    TOOLCHAIN_PATH=""

    parse_args "$@"
    OUT_ELF="${OUT_ELF:-$BUILD_DIR/$DEFAULT_ELF_NAME}"
    OUT_BIN="${OUT_BIN:-$BUILD_DIR/$DEFAULT_BIN_NAME}"
    setup_toolchain
    prepare_cube
    compile_fixture
}

main "$@"
