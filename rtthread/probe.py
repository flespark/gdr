"""RT-Thread presence and version evidence for ``gdr probe``.

The marker and version symbol tables belong to the modules that own them
(``navigation.py`` for the scheduler/tick symbols, ``version.py`` for the
version constants); this module only turns them into the neutral candidate
shape, so the probe adds no second copy of a kernel symbol spelling.
"""

from __future__ import annotations

from gdr.gdb_bridge import symbol_exists
from gdr.probe import ProbeCandidate
from gdr.version import format_version
from rtthread.navigation import PROBE_MARKERS
from rtthread.version import TARGET_VERSION_SYMBOLS, detect_target_version


def probe() -> ProbeCandidate | None:
    """Report whether this target runs RT-Thread, and which version it exports.

    ``None`` means no marker matched, i.e. this adapter has nothing to claim.
    """
    matched = tuple(name for name in PROBE_MARKERS if symbol_exists(name))
    if not matched:
        return None

    evidence = [f"symbol: {name}" for name in matched]
    sources = "/".join(name for name, _encodings in TARGET_VERSION_SYMBOLS)
    version = detect_target_version()
    if version is None:
        evidence.append(f"version: none of {sources} is readable")
        return ProbeCandidate(
            rtos="rtthread",
            confidence="undetected",
            evidence=tuple(evidence),
            detail=(
                "RT-Thread is present but exports no supported version constant; "
                "build with -g3 so DWARF carries RT_VER_NUM, or link a "
                "gdr_rtthread_version_num symbol"
            ),
        )

    evidence.append(f"version: {format_version(version)} from {sources}")
    return ProbeCandidate(
        rtos="rtthread",
        confidence="exact",
        version=format_version(version),
        evidence=tuple(evidence),
    )
