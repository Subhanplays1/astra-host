"""Astra Status Engine — real infrastructure context, calm status lines.

Deterministic, rules-based personality layer for Astra Host. It reads *actual*
system and platform state (host metrics, VPS counts, deployment activity,
provider health, maintenance flag, bot uptime) and derives:

    snapshot()  -> dict of measured values
    state()     -> one of MAINTENANCE | ERROR | WARNING | DEPLOYING |
                   HIGH_LOAD | BUSY | HEALTHY | CALM | MORNING | NIGHT
    message()   -> one short Astra line for that state
    presence()  -> Discord presence text built from real statistics

No external AI is required. Nothing here fabricates metrics: every number
comes from psutil, the SQLite database, the LXD/Incus provider or the bot's
own deployment counters.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("astra.status")

STATE_MAINTENANCE = "MAINTENANCE"
STATE_ERROR = "ERROR"
STATE_WARNING = "WARNING"
STATE_DEPLOYING = "DEPLOYING"
STATE_HIGH_LOAD = "HIGH_LOAD"
STATE_BUSY = "BUSY"
STATE_HEALTHY = "HEALTHY"
STATE_CALM = "CALM"
STATE_MORNING = "MORNING"
STATE_NIGHT = "NIGHT"

# ── message pools (deterministic pick — no spam, no randomness drift) ──
MESSAGES: dict[str, tuple[str, ...]] = {
    STATE_MORNING: (
        "☀️ Good morning. Astra is already watching your servers.",
        "🌅 Sunrise over the network. Everything is accounted for.",
        "☕ Morning. Infra is calm and under observation.",
    ),
    STATE_CALM: (
        "🛰️ Watching your servers while you work.",
        "🌌 Quiet skies. The infrastructure is steady.",
        "🪐 Nothing urgent. I'm keeping watch.",
        "🛸 Orbit is stable. All systems nominal.",
    ),
    STATE_HEALTHY: (
        "🛡️ Everything looks calm. I've got the infrastructure.",
        "✅ All instances answering. Nothing needs you right now.",
        "🧭 Systems aligned — you focus on your project.",
    ),
    STATE_DEPLOYING: (
        "🚀 Someone is launching a new VPS. I'm watching the deployment.",
        "🛠️ Build in progress. I'm tracking every stage.",
        "📡 Deployment running — compute is being allocated now.",
    ),
    STATE_BUSY: (
        "🛰️ Traffic is picking up. Still well within limits.",
        "⚙️ Things are getting busy, but everything is holding.",
        "📊 Load is rising — I'm watching the headroom.",
    ),
    STATE_HIGH_LOAD: (
        "🔥 Things are getting busy. I've got the load on my shoulder.",
        "⚡ Host is working hard. I'm monitoring the pressure closely.",
        "🫱 Heavy load detected — watching memory and CPU closely.",
    ),
    STATE_WARNING: (
        "⚠️ One of the instances needs attention. I'm checking it.",
        "🔍 A few signals look off — inspecting them now.",
        "🧩 Something wants a closer look. I'm on it.",
    ),
    STATE_ERROR: (
        "🚨 Infrastructure issue detected. Diagnostics are running.",
        "🛑 Provider is not responding cleanly — I'm on the fault.",
        "🩺 An error needs an administrator. Details are in the logs.",
    ),
    STATE_MAINTENANCE: (
        "🧰 Maintenance window active. Services resume shortly.",
        "🛠️ Astra is in maintenance mode — provisioning is paused.",
        "🔧 Scheduled maintenance. I'm holding the line.",
    ),
    STATE_NIGHT: (
        "🌙 The network is quiet tonight. I'm still watching your servers.",
        "🌌 Night shift. Low traffic, full attention.",
        "✨ Midnight over Astra. Everything is asleep but me.",
    ),
}

STATE_PRIORITY: tuple[str, ...] = (
    STATE_MAINTENANCE,
    STATE_ERROR,
    STATE_WARNING,
    STATE_DEPLOYING,
    STATE_HIGH_LOAD,
    STATE_BUSY,
    STATE_HEALTHY,
    STATE_MORNING,
    STATE_CALM,
    STATE_NIGHT,
)

# Announced only on transition, and never more often than this (seconds).
ANNOUNCE_COOLDOWN = 1800
ANNOUNCE_STATES = {STATE_ERROR, STATE_WARNING, STATE_DEPLOYING, STATE_MAINTENANCE}


def _psutil_snapshot() -> dict[str, Any]:
    """Host metrics — real, never invented. Degrades gracefully."""
    out: dict[str, Any] = {
        "cpu_percent": None,
        "memory_percent": None,
        "memory_used_mb": None,
        "memory_total_mb": None,
        "disk_percent": None,
        "disk_used_gb": None,
        "disk_total_gb": None,
        "load_avg": None,
    }
    try:
        import psutil

        # interval=None: non-blocking, samples since the previous call
        out["cpu_percent"] = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        out["memory_percent"] = float(vm.percent)
        out["memory_used_mb"] = float(vm.used) / (1024 * 1024)
        out["memory_total_mb"] = float(vm.total) / (1024 * 1024)
        du = psutil.disk_usage("/")
        out["disk_percent"] = float(du.percent)
        out["disk_used_gb"] = float(du.used) / (1024 ** 3)
        out["disk_total_gb"] = float(du.total) / (1024 ** 3)
        try:
            out["load_avg"] = list(psutil.getloadavg())
        except (AttributeError, OSError):
            out["load_avg"] = None
    except Exception as exc:  # noqa: BLE001 — metrics are best effort
        log.debug("psutil snapshot failed: %s", exc)
    return out


class AstraStatusEngine:
    """Collects real context and picks an appropriate Astra line."""

    def __init__(
        self,
        db,
        provider=None,
        *,
        started_at: Optional[float] = None,
        deployment_counter: Optional[Callable[[], int]] = None,
    ) -> None:
        self.db = db
        self.provider = provider
        self.started_at = started_at if started_at is not None else time.time()
        self._deployment_counter = deployment_counter
        self._last_state: Optional[str] = None
        self._last_announce: float = 0.0

    # ── data collection ──────────────────────────────────────
    def snapshot(self, *, ping_provider: bool = False) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        snap: dict[str, Any] = {
            "time": now,
            "hour": now.hour,
            "weekday": now.strftime("%A"),
            "uptime_seconds": max(0.0, time.time() - self.started_at),
            "active_deployments": 0,
            "vps_total": 0,
            "vps_online": 0,
            "vps_offline": 0,
            "vps_suspended": 0,
            "vps_other": 0,
            "users": 0,
            "recent_errors": 0,
            "recent_deployments": 0,
            "maintenance": False,
            "maintenance_message": "",
            "provider_ok": None,
            "provider_error": "",
        }

        try:
            counts = self.db.vps_status_counts()
            snap["vps_total"] = sum(counts.values())
            snap["vps_online"] = int(counts.get("running", 0))
            snap["vps_suspended"] = int(counts.get("suspended", 0))
            snap["vps_offline"] = int(counts.get("stopped", 0))
            snap["vps_other"] = max(
                0, snap["vps_total"] - snap["vps_online"] - snap["vps_offline"] - snap["vps_suspended"]
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("vps counts failed: %s", exc)

        try:
            snap["users"] = self.db.count_users()
        except Exception:
            pass

        try:
            rows = self.db.recent_logs(50)
            for row in rows:
                created = str(row["created_at"] or "")
                try:
                    ts = datetime.fromisoformat(created)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = now.timestamp() - ts.timestamp()
                except ValueError:
                    continue
                if age > 3600 or age < -5:
                    continue
                status = str(row["status"] or "").lower()
                stage = str(row["stage"] or "").lower()
                if status in {"failed", "error"}:
                    snap["recent_errors"] += 1
                if stage == "create" and status == "ok":
                    snap["recent_deployments"] += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("log scan failed: %s", exc)

        if self._deployment_counter is not None:
            try:
                snap["active_deployments"] = max(0, int(self._deployment_counter()))
            except Exception:
                snap["active_deployments"] = 0

        try:
            snap["maintenance"] = str(self.db.get_setting("maintenance_mode", "0")) in {
                "1",
                "true",
                "True",
            }
            snap["maintenance_message"] = str(
                self.db.get_setting("maintenance_message", "") or ""
            )
        except Exception:
            pass

        if ping_provider and self.provider is not None:
            try:
                snap["provider_ok"] = bool(self.provider.ping())
            except Exception as exc:  # noqa: BLE001
                snap["provider_ok"] = False
                snap["provider_error"] = str(exc)[:200]

        snap.update(_psutil_snapshot())
        return snap

    # ── state machine ────────────────────────────────────────
    @staticmethod
    def state_of(snap: dict[str, Any]) -> str:
        if snap.get("maintenance"):
            return STATE_MAINTENANCE
        if snap.get("provider_ok") is False:
            return STATE_ERROR
        if int(snap.get("recent_errors") or 0) >= 3:
            return STATE_ERROR
        if int(snap.get("vps_other") or 0) > 0:
            return STATE_WARNING
        if int(snap.get("active_deployments") or 0) > 0:
            return STATE_DEPLOYING

        cpu = snap.get("cpu_percent")
        mem = snap.get("memory_percent")
        if (cpu is not None and cpu >= 85) or (mem is not None and mem >= 90):
            return STATE_HIGH_LOAD
        if (cpu is not None and cpu >= 65) or (mem is not None and mem >= 75):
            return STATE_BUSY

        hour = int(snap.get("hour") or 0)
        online = int(snap.get("vps_online") or 0)
        if 5 <= hour < 11:
            return STATE_MORNING
        if hour >= 23 or hour < 4:
            return STATE_NIGHT
        if online > 0:
            return STATE_HEALTHY
        return STATE_CALM

    def state(self, snap: Optional[dict[str, Any]] = None) -> str:
        return self.state_of(snap if snap is not None else self.snapshot())

    # ── messaging ────────────────────────────────────────────
    @staticmethod
    def message_for(state: str, snap: Optional[dict[str, Any]] = None) -> str:
        pool = MESSAGES.get(state) or MESSAGES[STATE_CALM]
        hour = int((snap or {}).get("hour") or datetime.now(timezone.utc).hour)
        # stable per-minute pick: varies over the day, never randomly chatters
        return pool[hour % len(pool)]

    def line(self, snap: Optional[dict[str, Any]] = None) -> tuple[str, str]:
        """Return (state, message) for the current real context."""
        snap = snap if snap is not None else self.snapshot()
        state = self.state_of(snap)
        return state, self.message_for(state, snap)

    def reassurance(self, snap: Optional[dict[str, Any]] = None) -> str:
        """A calm, user-facing line (used in embeds/footers)."""
        snap = snap if snap is not None else self.snapshot()
        state = self.state_of(snap)
        if state in {STATE_ERROR, STATE_WARNING}:
            return "You focus on your project. I'll watch the infrastructure."
        if state == STATE_HIGH_LOAD:
            return "I'm carrying the server load so you don't have to."
        if state == STATE_DEPLOYING:
            return "New compute is coming online. I'm tracking it."
        pool = (
            "You've got things to do. I've got your servers.",
            "How's your day going? I'm watching the servers.",
            "You focus on your project. I'll watch the infrastructure.",
        )
        hour = int(snap.get("hour") or 0)
        return pool[hour % len(pool)]

    # ── Discord presence ─────────────────────────────────────
    @staticmethod
    def presence_for(snap: dict[str, Any], state: str) -> str:
        total = int(snap.get("vps_total") or 0)
        online = int(snap.get("vps_online") or 0)
        if state == STATE_DEPLOYING:
            return "Deploying another VPS"
        if state == STATE_MAINTENANCE:
            return "Maintenance in progress"
        if state == STATE_ERROR:
            return "Investigating infrastructure"
        if total <= 0:
            return "Watching Astra Network"
        options = (
            f"Watching {total} VPS",
            f"{total} VPS • {online} Online",
            "Watching your servers",
            "Monitoring Astra Nodes",
        )
        hour = int(snap.get("hour") or 0)
        return options[(hour + total) % len(options)]

    def presence(self, snap: Optional[dict[str, Any]] = None) -> str:
        snap = snap if snap is not None else self.snapshot()
        return self.presence_for(snap, self.state_of(snap))

    # ── change detection (for optional log-channel notes) ────
    def transition(self, state: str) -> bool:
        """True only when the state changed and a note is worth sending."""
        changed = self._last_state is not None and state != self._last_state
        self._last_state = state
        if not changed or state not in ANNOUNCE_STATES:
            return False
        now = time.time()
        if now - self._last_announce < ANNOUNCE_COOLDOWN:
            return False
        self._last_announce = now
        return True

    # ── admin panel / embed payload ──────────────────────────
    def report(self, snap: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        snap = snap if snap is not None else self.snapshot(ping_provider=True)
        state, message = self.line(snap)
        return {
            "state": state,
            "message": message,
            "reassurance": self.reassurance(snap),
            "snapshot": snap,
            "presence": self.presence_for(snap, state),
        }

    @staticmethod
    def uptime_text(seconds: float) -> str:
        seconds = max(0, int(seconds))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)


__all__ = [
    "AstraStatusEngine",
    "MESSAGES",
    "STATE_MAINTENANCE",
    "STATE_ERROR",
    "STATE_WARNING",
    "STATE_DEPLOYING",
    "STATE_HIGH_LOAD",
    "STATE_BUSY",
    "STATE_HEALTHY",
    "STATE_CALM",
    "STATE_MORNING",
    "STATE_NIGHT",
]
