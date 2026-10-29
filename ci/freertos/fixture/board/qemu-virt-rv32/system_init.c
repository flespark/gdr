/* QEMU RISC-V virt board glue: M-mode setup + semihosting console. */
#include <stdint.h>

void SystemInit(void)
{
}

/* Reason: qemu-system-riscv64 supports the RISC-V semihosting spec when
 * started with -semihosting (-semihosting-config enable=on, which the
 * GDR test harness passes); the fixture's ready marker then reaches QEMU's
 * stdout and the harness sees "GDR FreeRTOS fixture ready."  Operation
 * numbers follow the ARM semihosting ABi shared by the RISC-V spec:
 * SYS_WRITE0 == 4 (a0 = op, a1 = NUL-terminated string). */
void gdr_semihosting_write0(const char *message)
{
    register unsigned long a0 __asm__("a0") = 4UL;
    register const char *a1 __asm__("a1") = message;
    __asm__ volatile(
        ".option push\n"
        ".option norvc\n"
        "slli x0, x0, 0x1f\n"
        "ebreak\n"
        "srai x0, x0, 0x7\n"
        ".option pop\n" ::"r"(a0),
        "r"(a1)
        : "memory");
}
