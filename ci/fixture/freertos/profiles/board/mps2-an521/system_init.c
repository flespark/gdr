/* Minimal SystemInit for mps2-an521 (the runtime counter lives in
 * fixture/common/runtime_timer.c).  No board initialisation: QEMU powers up
 * the core ready to run, and the cross-core MHU doorbell interrupt is
 * intentionally not wired up (see cpu1_start.c). */
void SystemInit(void)
{
}
