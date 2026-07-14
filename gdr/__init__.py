"""GDR core framework: GDB helper for debugging RTOS-based firmware.

This package is RTOS-agnostic. Concrete RTOS support lives in sibling
packages (e.g. ``rtthread``) which provide layout descriptions, adapters
and commands on top of the abstractions defined here.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
