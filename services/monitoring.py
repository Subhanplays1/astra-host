"""Astra Host monitoring — real host/provider/database statistics.

Used by the admin dashboard, the `/admin` control center and the Astra
status engine. Every number here is measured; nothing is simulated.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("astra.service.monitoring")

try:
    import psutil
except Exception:  # pragma: no cover — psutil is in requirements.txt
    psutil = None  # type: ignore[assignment]


def host_metrics() -> dict[str, Any]:
    out: dict[str, Any] = {
        "cpu_percent": None,
        "memory_percent": None,
        "memory_used_gb": None,
        "memory_total_gb": None,
        "disk_percent": None,
        "disk_used_gb": None,
        "disk_total_gb": None,
        "boot_time": None,
        "uptime_seconds": None,
        "load_avg": None,
        "process_count": None,
    }
    if psutil is None:
        return out
    try:
        out["cpu_percent"] = float(psutil.cpu_percent(interval=0.1))
        vm = psutil.virtual_memory()
        out["memory_percent"] = float(vm.percent)
        out["memory_used_gb"] = round(vm.used / (1024 ** 3), 2)
        out["memory_total_gb"] = round(vm.total / (1024 ** 3), 2)
        du = psutil.disk_usage("/")
        out["disk_percent"] = float(du.percent)
        out["disk_used_gb"] = round(du.used / (1024 ** 3), 2)
        out["disk_total_gb"] = round(du.total / (1024 ** 3), 2)
        out["boot_time"] = datetime.fromtimestamp(
            psutil.boot_time(), tz=timezone.utc
        ).isoformat()
        out["uptime_seconds"] = max(0.0, time.time() - psutil.boot_time())
        try:
            out["load_avg"] = [round(x, 2) for x in psutil.getloadavg()]
        except (AttributeError, OSError):
            out["load_avg"] = None
        out["process_count"] = len(psutil.pids())
    except Exception as exc:  # noqa: BLE001
        log.debug("host metrics failed: %s", exc)
    return out


def provider_health(provider) -> dict[str, Any]:
    if provider is None:
        return {"connected": False, "error": "provider not initialised", "managed": 0}
    try:
        managed = int(provider.managed_count())
    except Exception:
        managed = 0
    try:
        ok = bool(provider.ping())
        return {"connected": ok, "error": "" if ok else "ping failed", "managed": managed}
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "error": str(exc)[:200], "managed": managed}


def recent_activity(db, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    deployments: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        for row in db.recent_logs(limit):
            item = {
                "owner_id": row["owner_id"],
                "vps_id": row["vps_id"],
                "stage": row["stage"],
                "status": row["status"],
                "message": (row["message"] or "")[:300],
                "created_at": row["created_at"],
            }
            if str(row["status"]).lower() in {"failed", "error"}:
                errors.append(item)
            elif str(row["stage"]).lower() == "create":
                deployments.append(item)
    except Exception as exc:  # noqa: BLE001
        log.debug("activity query failed: %s", exc)
    admin_actions: list[dict[str, Any]] = []
    try:
        for row in db.recent_admin_actions(limit):
            admin_actions.append(
                {
                    "admin_id": row["admin_id"],
                    "action": row["action"],
                    "target": row["target"],
                    "result": row["result"],
                    "source": row["source"],
                    "created_at": row["created_at"],
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("admin activity query failed: %s", exc)
    return {
        "deployments": deployments[:10],
        "errors": errors[:10],
        "admin_actions": admin_actions[:15],
    }


def dashboard_stats(db, provider=None, *, astra=None) -> dict[str, Any]:
    """Everything the admin dashboard shows, computed from live sources."""
    status: dict[str, int] = {}
    total = 0
    try:
        status = db.vps_status_counts()
        total = db.count_vps()
    except Exception as exc:  # noqa: BLE001
        log.debug("vps counts failed: %s", exc)

    online = int(status.get("running", 0))
    offline = int(status.get("stopped", 0))
    suspended = int(status.get("suspended", 0))

    users = 0
    try:
        users = db.count_users()
    except Exception:
        pass

    host = host_metrics()
    health = provider_health(provider)

    report = None
    if astra is not None:
        try:
            report = astra.report()
        except Exception as exc:  # noqa: BLE001
            log.debug("astra report failed: %s", exc)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "users": users,
        "vps": {
            "total": total,
            "online": online,
            "offline": offline,
            "suspended": suspended,
            "other": max(0, total - online - offline - suspended),
        },
        "host": host,
        "provider": health,
        "activity": recent_activity(db),
        "astra": report,
        "settings": {
            "vps_enabled": str(db.get_setting("vps_enabled", "1")),
            "maintenance_mode": str(db.get_setting("maintenance_mode", "0")),
            "required_invites": str(db.get_setting("required_invites", "5")),
            "max_total_vps": str(db.get_setting("max_total_vps", "20")),
            "max_vps_per_user": str(db.get_setting("max_vps_per_user", "3")),
        },
    }


__all__ = ["host_metrics", "provider_health", "recent_activity", "dashboard_stats"]
