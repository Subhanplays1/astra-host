"""Temporary admin tunnel abstraction for the Astra Host Admin Panel.

Tunnel logic is deliberately *not* spread across the app: pick an
implementation with ``TUNNEL_PROVIDER`` in ``.env`` and everything else talks
to the small ``TunnelProvider`` interface below.

Supported values for ``TUNNEL_PROVIDER``:

    localhost.run   free SSH reverse tunnel (no account required)
    cloudflare      cloudflared quick tunnel (requires ``cloudflared`` binary)
    tailscale       tailscale serve (requires ``tailscale`` CLI + tailnet)
    custom          run ``TUNNEL_CUSTOM_COMMAND`` ({port} is substituted)
    "" / none       bind locally only — no public URL

Credentials/URLs stay in ``.env`` / the settings table and are never logged.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

log = logging.getLogger("astra.service.tunnel")

URL_RE = re.compile(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")


class TunnelError(RuntimeError):
    pass


class TunnelResult:
    def __init__(self, url: str, process: Optional[subprocess.Popen] = None) -> None:
        self.url = url
        self.process = process

    def stop(self) -> None:
        proc = self.process
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass


class TunnelProvider(ABC):
    """Minimal interface every tunnel implementation must provide."""

    name: str = "base"

    @abstractmethod
    def start(self, port: int, *, timeout: float = 30.0) -> TunnelResult:
        """Expose ``127.0.0.1:{port}`` and return the public URL."""

    def stop(self) -> None:  # pragma: no cover — default no-op
        pass


def _scan_url(text: str) -> Optional[str]:
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,)")
        # localhost.run prints its own help URLs first — keep the tunnel host
        if "localhost.run" in url or "lhr.life" in url or "lhrtunnel" in url:
            if url.rstrip("/") in {"https://localhost.run", "https://localhost.run/"}:
                continue
        return url
    return None


class CommandTunnel(TunnelProvider):
    """Runs a shell command and scrapes the first public URL it prints."""

    def __init__(self, name: str, command: str) -> None:
        self.name = name
        self.command = command
        self._proc: Optional[subprocess.Popen] = None

    def start(self, port: int, *, timeout: float = 30.0) -> TunnelResult:
        cmd = self.command.format(port=port)
        if not cmd:
            raise TunnelError(f"{self.name}: no tunnel command configured")
        log.info("starting %s tunnel for admin panel", self.name)
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:  # noqa: BLE001
            raise TunnelError(f"{self.name}: {exc}") from exc
        self._proc = proc

        collected: list[str] = []
        result: dict[str, Optional[str]] = {"url": None}

        def reader() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    collected.append(line)
                    if result["url"] is None:
                        found = _scan_url(line)
                        if found:
                            result["url"] = found
            except Exception:  # noqa: BLE001
                pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.monotonic() + max(5.0, timeout)
        while time.monotonic() < deadline:
            if result["url"]:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.25)

        url = result["url"]
        if not url:
            tail = "".join(collected)[-400:]
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            raise TunnelError(f"{self.name}: no URL in output ({tail.strip()[:200]})")
        return TunnelResult(url, proc)


class LocalhostRunTunnel(CommandTunnel):
    def __init__(self) -> None:
        from config import TUNNEL_LOCALHOSTRUN

        super().__init__(
            "localhost.run",
            TUNNEL_LOCALHOSTRUN
            or "ssh -R 80:127.0.0.1:{port} nokey@localhost.run -o StrictHostKeyChecking=no",
        )


class CloudflareTunnel(CommandTunnel):
    def __init__(self) -> None:
        from config import TUNNEL_CLOUDFLARE

        super().__init__(
            "cloudflare",
            TUNNEL_CLOUDFLARE
            or "cloudflared tunnel --url http://127.0.0.1:{port} --no-autoupdate",
        )


class TailscaleTunnel(CommandTunnel):
    def __init__(self) -> None:
        from config import TUNNEL_TAILSCALE

        super().__init__(
            "tailscale",
            TUNNEL_TAILSCALE
            or "tailscale serve --bg --https=443 http://127.0.0.1:{port}",
        )


class CustomTunnel(CommandTunnel):
    def __init__(self) -> None:
        from config import TUNNEL_CUSTOM_COMMAND, TUNNEL_URL_PATTERN

        command = TUNNEL_CUSTOM_COMMAND
        url_pattern = TUNNEL_URL_PATTERN
        if url_pattern and not command:
            # Static URL (e.g. a Tailscale/Cloudflare static address).
            command = f"echo {url_pattern} && sleep infinity"
        super().__init__("custom", command)


class LocalTunnel(TunnelProvider):
    """No public exposure — panel stays on loopback."""

    name = "local"

    def start(self, port: int, *, timeout: float = 5.0) -> TunnelResult:
        return TunnelResult(f"http://127.0.0.1:{port}", None)


def get_tunnel_provider(name: Optional[str] = None) -> TunnelProvider:
    raw = (name if name is not None else os.getenv("TUNNEL_PROVIDER", "")).strip().lower()
    if raw in {"", "none", "off", "0", "local", "localhost"}:
        return LocalTunnel()
    if raw in {"localhost.run", "localhostrun", "localhostrun_ssh"}:
        return LocalhostRunTunnel()
    if raw in {"cloudflare", "cloudflared"}:
        return CloudflareTunnel()
    if raw in {"tailscale", "ts"}:
        return TailscaleTunnel()
    if raw in {"custom", "command"}:
        return CustomTunnel()
    raise TunnelError(f"unknown TUNNEL_PROVIDER '{raw}'")


class TunnelManager:
    """Owns the single active admin-panel tunnel for this process."""

    def __init__(self, db=None) -> None:
        self.db = db
        self._lock = threading.Lock()
        self._current: Optional[TunnelResult] = None
        self._provider_name: str = ""

    @property
    def url(self) -> str:
        with self._lock:
            if self._current:
                return self._current.url
        return self._stored_url()

    def _stored_url(self) -> str:
        if self.db is None:
            return ""
        try:
            return str(self.db.get_setting("admin_tunnel_url", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _store(self, url: str) -> None:
        if self.db is None:
            return
        try:
            self.db.set_setting("admin_tunnel_url", url)
        except Exception:  # noqa: BLE001
            pass

    def start(self, port: int, provider_name: Optional[str] = None) -> str:
        with self._lock:
            if self._current and self._provider_name == (provider_name or ""):
                return self._current.url
            self.stop_locked()
            provider = get_tunnel_provider(provider_name)
            if isinstance(provider, LocalTunnel):
                url = provider.start(port).url
                self._current = None
                self._provider_name = provider.name
                self._store(url)
                return url
            try:
                result = provider.start(port)
            except TunnelError as exc:
                log.warning("admin tunnel failed: %s", exc)
                url = f"http://127.0.0.1:{port}"
                self._store(url)
                return url
            self._current = result
            self._provider_name = provider.name
            self._store(result.url)
            log.info("admin panel tunnel ready via %s", provider.name)
            return result.url

    def stop_locked(self) -> None:
        if self._current is not None:
            self._current.stop()
            self._current = None

    def stop(self) -> None:
        with self._lock:
            self.stop_locked()


__all__ = [
    "TunnelProvider",
    "TunnelResult",
    "TunnelError",
    "TunnelManager",
    "LocalTunnel",
    "LocalhostRunTunnel",
    "CloudflareTunnel",
    "TailscaleTunnel",
    "CustomTunnel",
    "get_tunnel_provider",
]
