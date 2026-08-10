#!/usr/bin/env bash
# Build the locked FreeRTOS V10.3.1 B-L475E-IOT01A QEMU fixture.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CUBE_REPO="${STM32CUBE_L4_REPO:-https://github.com/STMicroelectronics/STM32CubeL4.git}"
CUBE_REF="${STM32CUBE_L4_REF:-v1.18.2}"
CUBE_DIR="${STM32CUBE_L4_DIR:-/tmp/stm32cubel4-v1.18.2}"
BUILD_DIR="${BUILD_DIR:-/tmp/gdr-freertos-build}"
OUT_ELF="${OUT_ELF:-$BUILD_DIR/freertos_b_l475e_iot01a.elf}"
OUT_BIN="${OUT_BIN:-$BUILD_DIR/freertos_b_l475e_iot01a.bin}"
TOOLCHAIN_DIR="${RTOS_TOOLCHAIN_PATH:-${XPACK_ARM_TOOLCHAIN_PATH:-}}"

if [[ -z "$TOOLCHAIN_DIR" ]]; then
    CROSS_GCC="$(command -v arm-none-eabi-gcc || true)"
    if [[ -z "$CROSS_GCC" ]]; then
        echo "[gdr-ci] FAILED: arm-none-eabi-gcc is not on PATH" >&2
        exit 1
    fi
    TOOLCHAIN_DIR="$(dirname "$CROSS_GCC")"
fi
CC="$TOOLCHAIN_DIR/arm-none-eabi-gcc"
OBJCOPY="$TOOLCHAIN_DIR/arm-none-eabi-objcopy"
for tool in "$CC" "$OBJCOPY"; do
    if [[ ! -x "$tool" ]]; then
        echo "[gdr-ci] FAILED: required tool not found: $tool" >&2
        exit 1
    fi
done

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

KERNEL="$CUBE_DIR/Middlewares/Third_Party/FreeRTOS/Source"
DEVICE="$CUBE_DIR/Drivers/CMSIS/Device/ST/STM32L4xx"
PROJECT="$CUBE_DIR/Projects/B-L475E-IOT01A/Applications/FreeRTOS/FreeRTOS_LowPower_LPTIM"
mkdir -p "$BUILD_DIR" "$(dirname "$OUT_ELF")" "$(dirname "$OUT_BIN")"

echo "[gdr-ci] STM32CubeL4: $CUBE_REPO@$CUBE_REF ($(git -C "$CUBE_DIR" rev-parse HEAD))"
echo "[gdr-ci] FreeRTOS: $(git -C "$KERNEL/.." rev-parse HEAD)"
echo "[gdr-ci] compiler: $($CC --version | head -1)"

CFLAGS=(
    -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard
    -Og -g3 -ffunction-sections -fdata-sections -fno-lto
    -Wall -Wextra -Werror -std=c11
    -DSTM32L475xx -DUSE_FULL_LL_DRIVER
    -I"$SCRIPT_DIR/fixture"
    -I"$CUBE_DIR/Drivers/CMSIS/Core/Include"
    -I"$DEVICE/Include"
    -I"$KERNEL/include"
    -I"$KERNEL/portable/GCC/ARM_CM4F"
)
SOURCES=(
    "$SCRIPT_DIR/fixture/main.c"
    "$PROJECT/Src/system_stm32l4xx.c"
    "$PROJECT/STM32CubeIDE/Applications/Startup/startup_stm32l475vgtx.s"
    "$KERNEL/tasks.c" "$KERNEL/queue.c" "$KERNEL/list.c" "$KERNEL/timers.c"
    "$KERNEL/portable/MemMang/heap_4.c" "$KERNEL/portable/GCC/ARM_CM4F/port.c"
)

"$CC" "${CFLAGS[@]}" "${SOURCES[@]}" \
    -T"$PROJECT/STM32CubeIDE/STM32L475VGTX_FLASH.ld" \
    -Wl,--gc-sections -Wl,-Map,"$BUILD_DIR/freertos.map" \
    --specs=nano.specs --specs=nosys.specs -o "$OUT_ELF"
"$OBJCOPY" -O binary "$OUT_ELF" "$OUT_BIN"
echo "[gdr-ci] built ELF: $OUT_ELF"
echo "[gdr-ci] built BIN: $OUT_BIN"
