# RT-Thread fixture builds

Use the [shared CLI](../README.md) to prepare and run firmware. `build.sh`
is the internal builder: it receives an immutable tagged source checkout,
creates a new private clone, applies the target/version patch set, and
publishes `rtthread.elf` (plus `rtthread.bin` on RV64). It never resets or
cleans an existing working tree.

## BSP and patch selection

| Target | Releases | Upstream BSP |
| --- | --- | --- |
| `cortex-a9` | 3.1.0–3.1.5, 4.0.0–4.1.1 | `bsp/qemu-vexpress-a9` |
| `rv64` | 4.0.4, 4.0.5, 4.1.0 | `bsp/qemu-riscv-virt64` |
| `rv64` | 4.1.1 | `bsp/qemu-virt64-riscv` |

Patch series live under `patches/<target>/<version>/`. A patch must apply
cleanly to its own BSP and toolchain; do not share patches between the
Cortex-A9 and RV64 directories without verifying both. A9 sets
`RT_NAME_MAX=16` to match canonical fixture names. Shared coverage and matrix
policy are documented in [testing](../../docs/testing.md).

## Build dependencies

The CLI registers `arm-none-eabi-*` for A9 and `riscv-none-elf-*` for RV64.
The RV64 BSP expects `riscv64-unknown-elf-*` aliases; the shared container
creates them in its compiler distribution. Both compilers must provide
newlib headers. Modern releases use Python 3/SCons 4; 3.1.x uses an isolated
Python 2/SCons 3 installation linked under the host tool cache.

Every shared-runner build enables `-g3 -gdwarf-4` and publishes a
`.debug-macros` marker alongside firmware so version-probe tests require
the corresponding macro evidence. GDB reads the ELF; the RV64 QEMU machine
boots its raw BIN sibling as BIOS. Build logs and patched private checkouts
remain under the shared cache for inspection.
