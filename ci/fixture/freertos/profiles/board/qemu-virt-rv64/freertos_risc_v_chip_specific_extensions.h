/* Board-specific RISC-V FreeRTOS port extensions for QEMU virt.
 *
 * Supplies the CLINT/mtime defaults and portasmHANDLE_INTERRUPT binding in the
 * board profile so portASM.S builds identically across V10.3.x through V11.x.
 */
#ifndef __FREERTOS_RISC_V_EXTENSIONS_H__
#define __FREERTOS_RISC_V_EXTENSIONS_H__

#define portasmHAS_SIFIVE_CLINT        1
#define portasmHAS_MTIME               1
#define portasmADDITIONAL_CONTEXT_SIZE 0
#define portasmHANDLE_INTERRUPT        gdr_rv_unhandled_irq

#ifdef __ASSEMBLER__
.macro portasmSAVE_ADDITIONAL_REGISTERS
    .endm

.macro portasmRESTORE_ADDITIONAL_REGISTERS
    .endm
#endif

#endif /* __FREERTOS_RISC_V_EXTENSIONS_H__ */
