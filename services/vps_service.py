"""Astra Host VPS service — one path for every lifecycle operation.

Both the Discord bot and the admin panel call into this service so there is
never a second implementation of provisioning logic. The actual engine stays
the existing ``LXDProvider`` / Incus backend.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from provider import ProviderError, validate_resources

log = logging.getLogger("astra.service.vps")


class ServiceError(RuntimeError):
    """Raised when an operation cannot be completed."""


class VPSService:
    def __init__(self, db, provider=None) -> None:
        self.db = db
        self.provider = provider

    # ── provider plumbing ────────────────────────────────────
    def set_provider(self, provider) -> None:
        self.provider = provider

    @property
    def available(self) -> bool:
        return self.provider is not None

    def _require_provider(self):
        if self.provider is None:
            raise ServiceError("LXD/Incus provider unavailable")
        return self.provider

    def provider_health(self) -> dict[str, Any]:
        provider = self.provider
        if provider is None:
            return {"connected": False, "error": "provider not initialised"}
        try:
            ok = bool(provider.ping())
            return {"connected": ok, "error": "" if ok else "provider ping failed"}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)[:200]}

    # ── reads ────────────────────────────────────────────────
    def get(self, vps_id: str):
        return self.db.get_vps(vps_id)

    def list_all(self):
        return self.db.list_all_vps()

    def list_user(self, owner_id: str):
        return self.db.list_user_vps(str(owner_id))

    def counts(self) -> dict[str, int]:
        total = self.db.count_vps()
        status = self.db.vps_status_counts()
        online = int(status.get("running", 0))
        offline = int(status.get("stopped", 0))
        suspended = int(status.get("suspended", 0))
        return {
            "total": total,
            "online": online,
            "offline": offline,
            "suspended": suspended,
            "other": max(0, total - online - offline - suspended),
        }

    def stats(self, vps_id: str) -> dict[str, Any]:
        row = self.get(vps_id)
        if not row:
            raise ServiceError("VPS not found")
        provider = self._require_provider()
        data = provider.stats(row["container_id"])
        data["vps"] = dict(row)
        return data

    def exec(self, vps_id: str, command: str, timeout: int = 60) -> tuple[int, str]:
        row = self.get(vps_id)
        if not row:
            raise ServiceError("VPS not found")
        provider = self._require_provider()
        return provider.exec_command(row["container_id"], command, timeout)

    # ── lifecycle ────────────────────────────────────────────
    def _apply(self, vps_id: str, action: str, actor: str = "", source: str = "panel"):
        row = self.get(vps_id)
        if not row:
            raise ServiceError("VPS not found")
        provider = self._require_provider()
        cid = row["container_id"]
        status = row["status"]
        try:
            if action == "start":
                provider.start(cid)
                status = "running"
            elif action == "stop":
                provider.stop(cid)
                status = "stopped"
            elif action == "restart":
                provider.restart(cid)
                status = "running"
            elif action == "suspend":
                provider.stop(cid)
                status = "suspended"
            elif action == "unsuspend":
                provider.start(cid)
                status = "running"
            elif action == "emergency_stop":
                provider.stop(cid, 0)
                status = "stopped"
            else:
                raise ServiceError(f"unknown action {action}")
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface provider failures
            if actor:
                self.db.log_admin_action(actor, action, vps_id, "error", str(exc)[:300], source)
            raise ServiceError(str(exc)) from exc
        self.db.update_vps_status(vps_id, status)
        if actor:
            self.db.log_admin_action(actor, action, vps_id, "ok", status, source)
        return {"vps_id": vps_id, "status": status, "row": dict(self.get(vps_id) or row)}

    def start(self, vps_id: str, actor: str = "", source: str = "panel") -> dict:
        return self._apply(vps_id, "start", actor, source)

    def stop(self, vps_id: str, actor: str = "", source: str = "panel") -> dict:
        return self._apply(vps_id, "stop", actor, source)

    def restart(self, vps_id: str, actor: str = "", source: str = "panel") -> dict:
        return self._apply(vps_id, "restart", actor, source)

    def suspend(self, vps_id: str, actor: str = "", source: str = "panel") -> dict:
        return self._apply(vps_id, "suspend", actor, source)

    def unsuspend(self, vps_id: str, actor: str = "", source: str = "panel") -> dict:
        return self._apply(vps_id, "unsuspend", actor, source)

    def emergency_stop(self, vps_id: str, actor: str = "", source: str = "panel") -> dict:
        return self._apply(vps_id, "emergency_stop", actor, source)

    def remove(self, vps_id: str, actor: str = "", source: str = "panel") -> None:
        row = self.get(vps_id)
        if not row:
            raise ServiceError("VPS not found")
        provider = self.provider
        if provider is not None:
            try:
                provider.remove(row["container_id"])
            except ProviderError as exc:
                log.warning("remove %s failed (continuing with DB delete): %s", vps_id, exc)
        self.db.delete_vps(vps_id)
        if actor:
            self.db.log_admin_action(actor, "delete_vps", vps_id, "ok", "", source)

    def emergency_remove(self, vps_id: str, actor: str = "", source: str = "panel") -> None:
        self.remove(vps_id, actor=actor, source=source)

    def edit(
        self,
        vps_id: str,
        *,
        memory_mb: Optional[int] = None,
        cpus: Optional[int] = None,
        disk_gb: Optional[int] = None,
        actor: str = "",
        source: str = "panel",
    ) -> dict:
        row = self.get(vps_id)
        if not row:
            raise ServiceError("VPS not found")
        mem = int(memory_mb) if memory_mb else int(row["memory_mb"])
        cpu = int(cpus) if cpus else int(row["cpus"])
        disk = int(disk_gb) if disk_gb else int(row["disk_gb"])
        try:
            validate_resources(mem, cpu, disk)
        except ProviderError as exc:
            raise ServiceError(str(exc)) from exc
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE vps_instances SET memory_mb=?, cpus=?, disk_gb=? WHERE vps_id=?",
                (mem, cpu, disk, vps_id),
            )
            self.db.conn.commit()
        if actor:
            self.db.log_admin_action(
                actor, "edit_vps", vps_id, "ok", f"{mem}MB/{cpu}C/{disk}GB", source
            )
        return {"vps_id": vps_id, "memory_mb": mem, "cpus": cpu, "disk_gb": disk}

    def set_password(
        self, vps_id: str, password: str, actor: str = "", source: str = "panel"
    ) -> None:
        row = self.get(vps_id)
        if not row:
            raise ServiceError("VPS not found")
        if len(password) < 8:
            raise ServiceError("Password must be at least 8 characters")
        provider = self._require_provider()
        try:
            provider.set_password(row["container_id"], password)
        except Exception as exc:  # noqa: BLE001
            if actor:
                self.db.log_admin_action(actor, "set_password", vps_id, "error", str(exc)[:300], source)
            raise ServiceError(str(exc)) from exc
        self.db.update_vps_password(vps_id, password)
        if actor:
            self.db.log_admin_action(actor, "set_password", vps_id, "ok", "", source)


__all__ = ["VPSService", "ServiceError"]
