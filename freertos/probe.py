"""FreeRTOS presence and version evidence for ``gdr probe``.

The version macros live in the kernel compilation unit, so on a ``-g2`` build
they are simply absent.  Instead of giving up, this probe narrows the layout
era from a structural fingerprint (the SMP-era TCB carries ``xTaskRunState``,
and a multicore build exposes ``pxCurrentTCBs``) and reports that as a
``range`` with the fingerprint spelled out as evidence.

The marker symbols, the fingerprint field and the version sources all come
from the modules that own them (``layout.py`` and ``version.py``), so this
module holds no kernel symbol spelling of its own.
"""

from __future__ import annotations

from freertos.layout import PROBE_MARKERS, SMP_ERA_FIELD, SMP_ERA_SYMBOL
from freertos.version import VERSION_SOURCES, detect_target_version
from gdr.gdb_bridge import symbol_exists, type_field_names
from gdr.probe import ProbeCandidate
from gdr.version import format_version


def probe() -> ProbeCandidate | None:
    """Report whether this target runs FreeRTOS, and which version it exports.

    ``None`` means no marker matched, i.e. this adapter has nothing to claim.
    """
    matched = tuple(name for name in PROBE_MARKERS if symbol_exists(name))
    if not matched:
        return None

    evidence = [f"symbol: {name}" for name in matched]
    version = detect_target_version()
    if version is not None:
        evidence.append(
            f"version: {format_version(version)} from {'/'.join(VERSION_SOURCES)}"
        )
        return ProbeCandidate(
            rtos="freertos",
            confidence="exact",
            version=format_version(version),
            evidence=tuple(evidence),
        )

    evidence.append("version: none of " + ", ".join(VERSION_SOURCES) + " is readable")
    smp_era = symbol_exists(SMP_ERA_SYMBOL) or SMP_ERA_FIELD in type_field_names(
        "struct tskTaskControlBlock"
    )
    if smp_era:
        evidence.append(f"layout: {SMP_ERA_FIELD}/{SMP_ERA_SYMBOL} present")
        era = "the V11 SMP-era layout"
    else:
        evidence.append(f"layout: {SMP_ERA_FIELD} absent")
        era = "the pre-SMP V10.x layout"
    return ProbeCandidate(
        rtos="freertos",
        confidence="range",
        evidence=tuple(evidence),
        detail=(
            f"FreeRTOS is present but exports no version macro; the TCB matches "
            f"{era}, so the exact version must be confirmed by the user. "
            "Building with -g3 makes the version macros available"
        ),
    )
