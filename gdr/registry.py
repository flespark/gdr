"""Registry for exactly one active RTOS semantic adapter per GDB session."""

from __future__ import annotations

from gdr.adapter_api import RtosAdapter

_active: RtosAdapter | None = None


def register(adapter: RtosAdapter) -> None:
    """Register the current session's adapter, refusing adapter replacement."""
    global _active
    if _active is None:
        _active = adapter


def active() -> RtosAdapter | None:
    """Return the selected RTOS adapter, or ``None`` before ``gdr init``."""
    return _active


def is_initialized() -> bool:
    """Return whether an RTOS adapter has been registered."""
    return _active is not None
