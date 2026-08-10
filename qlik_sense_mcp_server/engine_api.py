"""Backwards-compatible import path for the Engine client.

The implementation moved into the `engine` package (see
`engine/api.py`); this module keeps `from .engine_api import
QlikEngineAPI` working for anything that already imports it, tests
included.
"""

from .engine import QlikEngineAPI

__all__ = ["QlikEngineAPI"]
