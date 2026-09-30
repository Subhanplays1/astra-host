"""Astra Host admin panel — internal, admin-only control center.

The panel runs inside the bot process so Discord commands and the web UI
share one ``Database`` and one ``VPSService``.
"""

from __future__ import annotations

__all__ = ["auth", "panel", "routes"]
