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


def _url_host(url: str) -> str:
    tail = url.split("//", 1)[-1]
    return tail.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].lower()


# localhost.run greets you with its own console/docs links before it prints the
# tunnel URL. Without this list the panel ends up advertising admin.localhost.run.
_NON_TUNNEL_HOSTS = {
    "localhost.run",
    "www.localhost.run",
    "admin.localhost.run",
    "docs.localhost.run",
    "blog.localhost.run",
    "ssh.localhost.run",
    "api.localhost.run",
    "app.localhost.run",
    "status.localhost.run",
    "lhr.life",
    "lhrtunnel.link",
    "lhr.rocks",
    "lhr.link",
    "developers.cloudflare.com",
    "dash.cloudflare.com",
    "github.com",
    "tailscale.com",
    "login.tailscale.com",
}


def is_tunnel_url(url: str) -> bool:
    """True for a real tunnel URL, False for marketing/docs links in the banner."""
    return bool(url) and _url_host(url) not in _NON_TUNNEL_HOSTS


def _scan_url(text: str) -> Optional[str]:
    for match in URL_RE.findall(text or ""):
        url = match.rstrip(".,)")
        if is_tunnel_url(url):
            return url
    return None


# ssh options must sit *before* the host, otherwise they become the remote command
_SSH_OPTS = (
    "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
    "-o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes"
)


def _harden_ssh(command: str) -> str:
    """Make an ``ssh -R`` tunnel non-interactive so it can never hang startup.

    A first connection would otherwise prompt for the host key and the panel
    would sit there until the tunnel timeout.
    """
    parts = command.split(None, 1)
    if not parts:
        return command
    binary = parts[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if binary != "ssh" or "StrictHostKeyChecking" in command:
        return command
    rest = parts[1] if len(parts) > 1 else ""
    return f"ssh {_SSH_OPTS} {rest}".rstrip()


class CommandTunnel(TunnelProvider):
    """Runs a shell command and scrapes the first public URL it prints."""

    def __init__(self, name: str, command: str, *, filter_output: bool = True) -> None:
        self.name = name
        self.command = _harden_ssh(command or "")
        self.filter_output = filter_output
        self._proc: Optional[subprocess.Popen] = None

    def _candidate(self, line: str) -> Optional[str]:
        if not self.filter_output:
            # Static TUNNEL_URL_PATTERN — the operator chose this URL on purpose.
            match = URL_RE.search(line or "")
            return match.group(0).rstrip(".,)") if match else None
        return _scan_url(line)

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
                        found = self._candidate(line)
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
            # The command may have printed the URL and exited already — give the
            # reader a moment to drain the pipe before declaring failure.
            thread.join(timeout=1.0)
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
        filter_output = True
        if url_pattern and not command:
            # Static URL (e.g. a Tailscale/Cloudflare static address).
            command = f"echo {url_pattern} && sleep infinity"
            filter_output = False
        super().__init__("custom", command, filter_output=filter_output)


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
            url = str(self.db.get_setting("admin_tunnel_url", "") or "")
        except Exception:  # noqa: BLE001
            return ""
        # Drop a stale banner URL (admin.localhost.run) captured by an old build.
        return url if is_tunnel_url(url) else ""

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
