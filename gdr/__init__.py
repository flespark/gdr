"""RTOS-agnostic GDB helper core for embedded firmware debugging.

Provides generic GDB bridge, layout, table abstraction, and pretty-printer
support. RTOS-specific packages supply layouts, navigation, adapters, and
commands.
"""

__all__ = ["__version__"]

__version__ = "2026.2"
