"""Astra Host configuration — white-label defaults."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "vexdeploy.db"))
LOG_FILE = str(BASE_DIR / "vexdeploy.log")

# ── public brand (user-facing) ────────────────────────────────
BRAND_NAME = "Astra Host"
BRAND_TAGLINE = "The Future of Free VPS Hosting."


def _flag(raw: str, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip() not in {"0", "false", "False", "no", "off", ""}


def _int_env(key: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(str(os.getenv(key, str(default))).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
COMMAND_PREFIX = "!"

# Access
def _ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


ADMIN_IDS: set[int] = _ids(os.getenv("ADMIN_IDS", "1210291131301101618"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1376177459870961694") or 0)

# LXD defaults
LXD_NETWORK = os.getenv("LXD_NETWORK", "vexdeploy")
LXD_CLI = os.getenv("LXD_CLI", "").strip()
LXD_STORAGE = os.getenv("LXD_STORAGE", "").strip()
DEFAULT_OS_IMAGE = os.getenv("DEFAULT_OS_IMAGE", "ubuntu:22.04")
MAX_CONTAINERS = int(os.getenv("MAX_CONTAINERS", "100"))

# Resource validation (defaults MUST pass validation)
MIN_MEMORY_MB = 512
MAX_MEMORY_MB = 65536
DEFAULT_MEMORY_MB = int(os.getenv("DEFAULT_MEMORY_MB", "1024"))

MIN_CPUS = 1
MAX_CPUS = 32
DEFAULT_CPUS = int(os.getenv("DEFAULT_CPUS", "1"))

MIN_DISK_GB = 5
MAX_DISK_GB = 1000
DEFAULT_DISK_GB = int(os.getenv("DEFAULT_DISK_GB", "10"))

MAX_VPS_PER_USER = int(os.getenv("MAX_VPS_PER_USER", "3"))

# ── Astra Host admin panel (admin-only web control center) ────
ADMIN_PANEL_ENABLED = _flag(os.getenv("ADMIN_PANEL_ENABLED", ""), default=False)
ADMIN_PANEL_HOST = os.getenv("ADMIN_PANEL_HOST", "127.0.0.1").strip() or "127.0.0.1"
ADMIN_PANEL_PORT = _int_env("ADMIN_PANEL_PORT", 8787, 1)
ADMIN_PANEL_SESSION_HOURS = _int_env("ADMIN_PANEL_SESSION_HOURS", 8, 1)
ADMIN_PANEL_LOGIN_TTL_SECONDS = _int_env("ADMIN_PANEL_LOGIN_TTL", 300, 30)
ADMIN_PANEL_RATE_LIMIT = _int_env("ADMIN_PANEL_RATE_LIMIT", 10, 1)
ADMIN_PANEL_DEBUG = _flag(os.getenv("ADMIN_PANEL_DEBUG", ""), default=False)

# Temporary admin tunnel (see services/tunnel.py)
TUNNEL_PROVIDER = os.getenv("TUNNEL_PROVIDER", "").strip().lower()
TUNNEL_LOCALHOSTRUN = os.getenv("TUNNEL_LOCALHOSTRUN", "ssh -R 80:127.0.0.1:{port} nokey@localhost.run").strip()
TUNNEL_CLOUDFLARE = os.getenv("TUNNEL_CLOUDFLARE", "").strip()
TUNNEL_TAILSCALE = os.getenv("TUNNEL_TAILSCALE", "").strip()
TUNNEL_CUSTOM_COMMAND = os.getenv("TUNNEL_CUSTOM_COMMAND", "").strip()
TUNNEL_URL_PATTERN = os.getenv("TUNNEL_URL_PATTERN", "").strip()
# ssh-only provider: free temporary https://<random>.pinggy.io (60 min per run)
TUNNEL_PINGGY = os.getenv("TUNNEL_PINGGY", "").strip()
# Download the cloudflared binary when it is missing (official GitHub release)
TUNNEL_AUTO_INSTALL = _flag(os.getenv("TUNNEL_AUTO_INSTALL", ""), default=True)

# ── Discord presence / Astra personality ──────────────────────
PRESENCE_INTERVAL_MIN = _int_env("PRESENCE_INTERVAL_MIN", 15, 1)
ASTRA_STATUS_INTERVAL_MIN = _int_env("ASTRA_STATUS_INTERVAL_MIN", 10, 1)
ASTRA_MESSAGES_ENABLED = _flag(os.getenv("ASTRA_MESSAGES_ENABLED", ""), default=True)

# AI (Google Gemini)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
AI_CHAT_ENABLED = os.getenv("AI_CHAT_ENABLED", "1").strip() not in {"0", "false", "False"}
AI_CHAT_COOLDOWN = int(os.getenv("AI_CHAT_COOLDOWN", "30"))
AI_CHAT_TIMEOUT = int(os.getenv("AI_CHAT_TIMEOUT", "25"))
AI_VERIFICATION_ENABLED = os.getenv("AI_VERIFICATION_ENABLED", "1").strip() not in {"0", "false", "False"}
VERIFICATION_TIMEOUT = int(os.getenv("VERIFICATION_TIMEOUT", "600"))
TRUST_ROLE_NAMES: dict[str, str] = {
    "trusted": os.getenv("TRUSTED_ROLE_NAME", "Trusted"),
    "untrusted": os.getenv("UNTRUSTED_ROLE_NAME", "Untrusted"),
    "scammer": os.getenv("SCAMMER_ROLE_NAME", "Scammer"),
    "spammer": os.getenv("SPAMMER_ROLE_NAME", "Spammer"),
}
TRUST_VERDICTS = ("trusted", "untrusted", "scammer", "spammer")
TRUST_VERDICT_COLORS: dict[str, int] = {
    "trusted": 0x2ECC71,
    "untrusted": 0xF1C40F,
    "scammer": 0xE74C3C,
    "spammer": 0x992D22,
}

# Runtime settings seeded into DB
DEFAULT_SETTINGS: dict[str, str] = {
    "required_invites": "5",
    "vps_enabled": "1",
    "vps_cooldown_hours": "24",
    "max_total_vps": "20",
    "max_vps_per_user": str(MAX_VPS_PER_USER),
    "default_vps_memory": str(DEFAULT_MEMORY_MB),
    "default_vps_cpu": str(DEFAULT_CPUS),
    "default_vps_disk": str(DEFAULT_DISK_GB),
    "log_channel_id": "0",
    "completion_channel_id": "0",
    "autostart_on_ready": "1",
    "ai_chat_enabled": "1" if AI_CHAT_ENABLED else "0",
    "ai_chat_cooldown": str(AI_CHAT_COOLDOWN),
    "ai_verification_enabled": "1" if AI_VERIFICATION_ENABLED else "0",
    # Astra Host operational state
    "maintenance_mode": "0",
    "maintenance_message": "",
    "admin_tunnel_url": "",
    "admin_panel_enabled": "1" if ADMIN_PANEL_ENABLED else "0",
}

# White-label brand (admin-editable)
DEFAULT_BRAND: dict[str, object] = {
    "brand_name": BRAND_NAME,
    "brand_tagline": BRAND_TAGLINE,
    "website": "https://astra.host.example",
    "discord": "https://discord.gg/example",
    "support_email": "support@astra.host.example",
    "motd_enabled": True,
    "motd_type": "premium",
    "primary_color": "cyan",
    "secondary_color": "magenta",
    "logo": "AUTO",
    "footer": BRAND_TAGLINE,
    "motd_template": "",
}

ANSI_COLORS: dict[str, str] = {
    "black": "0;30",
    "red": "0;31",
    "green": "0;32",
    "yellow": "0;33",
    "blue": "0;34",
    "magenta": "0;35",
    "cyan": "0;36",
    "white": "0;37",
    "bright_black": "1;30",
    "bright_red": "1;31",
    "bright_green": "1;32",
    "bright_yellow": "1;33",
    "bright_blue": "1;34",
    "bright_magenta": "1;35",
    "bright_cyan": "1;36",
    "bright_white": "1;37",
}

BRAND_FIELDS = {
    "brand_name": str,
    "brand_tagline": str,
    "website": str,
    "discord": str,
    "support_email": str,
    "motd_enabled": bool,
    "motd_type": str,
    "primary_color": str,
    "secondary_color": str,
    "logo": str,
    "footer": str,
    "motd_template": str,
}

OS_CHOICES = {
    "ubuntu:22.04": "Ubuntu 22.04 LTS",
    "ubuntu:24.04": "Ubuntu 24.04 LTS",
    "debian:12": "Debian 12",
    "debian:13": "Debian 13",
    "alpine:3.20": "Alpine 3.20",
    "alpine:3.21": "Alpine 3.21",
    "rocky:9": "Rocky Linux 9",
    "almalinux:9": "AlmaLinux 9",
    "fedora:40": "Fedora 40",
    "fedora:41": "Fedora 41",
    "oracle:9": "Oracle Linux 9",
    "opensuse:15": "openSUSE Leap 15",
}


def _parse_plans(raw: str) -> list[dict[str, object]]:
    """Parse PLANS=name:mem:cpu:disk[:badge[:price]],name2:..."""
    plans: list[dict[str, object]] = []
    for i, part in enumerate(raw.replace("\n", ",").split(",")):
        part = part.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split(":")]
        if len(bits) < 4:
            continue
        name, mem_s, cpu_s, disk_s = bits[0], bits[1], bits[2], bits[3]
        badge = bits[4] if len(bits) > 4 else ""
        price = bits[5] if len(bits) > 5 else ""
        if not name:
            continue
        try:
            mem, cpu, disk = int(mem_s), int(cpu_s), int(disk_s)
        except ValueError:
            continue
        plans.append(
            {
                "name": name,
                "memory_mb": mem,
                "cpus": cpu,
                "disk_gb": disk,
                "badge": badge,
                "price": price,
                "sort_order": i + 1,
            }
        )
    return plans


# Seed plans from .env (optional). Format:
# PLANS=Starter:1024:1:10:Free,Pro:2048:2:25:Popular:$4,Business:4096:4:50
ENV_PLANS: list[dict[str, object]] = _parse_plans(os.getenv("PLANS", ""))

# Discord embed colors for brand primary_color
BRAND_DISCORD_COLORS: dict[str, int] = {
    "cyan": 0x00FFFF,
    "magenta": 0xFF00FF,
    "blue": 0x3498DB,
    "green": 0x00FF00,
    "red": 0xFF0000,
    "yellow": 0xFFFF00,
    "white": 0xFFFFFF,
    "black": 0x000000,
    "bright_cyan": 0x5FFFFF,
    "bright_magenta": 0xFF5FFF,
    "bright_blue": 0x5F5FFF,
    "bright_green": 0x5FFF5F,
    "bright_red": 0xFF5F5F,
    "bright_yellow": 0xFFFF5F,
    "bright_white": 0xFFFFFF,
    "bright_black": 0x5F5F5F,
}


def parse_created_at(value: str | None):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
