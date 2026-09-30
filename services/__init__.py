"""Astra Host service layer.

Single source of truth for platform operations. The Discord bot and the
admin panel both go through these services instead of each growing its own
copy of the provisioning logic.

    Discord Commands ─┐
                      ├── Service Layer ── LXD/Incus (provider.py)
    Admin Panel ──────┘
"""

from __future__ import annotations

__all__ = ["vps_service", "monitoring", "tunnel"]
