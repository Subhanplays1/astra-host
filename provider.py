"""Clean LXD/Incus VPS provider — VexDeploy (no legacy code)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import string
import subprocess
import time
from dataclasses import dataclass, field

from config import (
    LXD_CLI,
    LXD_NETWORK,
    LXD_STORAGE,
    MAX_CONTAINERS,
    MAX_CPUS,
    MAX_DISK_GB,
    MAX_MEMORY_MB,
    MIN_CPUS,
    MIN_DISK_GB,
    MIN_MEMORY_MB,
)

logger = logging.getLogger("vexdeploy.provider")

LABEL_MANAGED = "user.vexdeploy.managed"
LABEL_OWNER = "user.vexdeploy.owner"
LABEL_VPS_ID = "user.vexdeploy.vps_id"

IMAGE_MAP = {
    "ubuntu:22.04": "images:ubuntu/22.04",
    "ubuntu:24.04": "images:ubuntu/24.04",
    "debian:12": "images:debian/12",
    "debian:13": "images:debian/13",
    "alpine:3.20": "images:alpine/3.20/cloud",
    "alpine:3.21": "images:alpine/3.21/cloud",
    "rocky:9": "images:rockylinux/9",
    "almalinux:9": "images:almalinux/9",
    "fedora:40": "images:fedora/40",
    "fedora:41": "images:fedora/41",
    "oracle:9": "images:oracle/9",
    "opensuse:15": "images:opensuse/15.6",
}

IMAGE_CANDIDATES: dict[str, list[str]] = {
    "ubuntu:22.04": [
        "images:ubuntu/22.04",
        "ubuntu:22.04",
        "ubuntu:jammy",
        "images:ubuntu/jammy",
    ],
    "ubuntu:24.04": [
        "images:ubuntu/24.04",
        "ubuntu:24.04",
        "ubuntu:noble",
        "images:ubuntu/noble",
    ],
    "debian:12": ["images:debian/12", "images:debian/bookworm"],
    "debian:13": ["images:debian/13", "images:debian/trixie"],
    "alpine:3.20": ["images:alpine/3.20/cloud", "images:alpine/3.20"],
    "alpine:3.21": ["images:alpine/3.21/cloud", "images:alpine/3.21"],
    "rocky:9": ["images:rockylinux/9", "images:rockylinux/9/cloud"],
    "almalinux:9": ["images:almalinux/9", "images:almalinux/9/cloud"],
    "fedora:40": ["images:fedora/40", "images:fedora/40/cloud"],
    "fedora:41": ["images:fedora/41", "images:fedora/41/cloud"],
    "oracle:9": ["images:oracle/9", "images:oracle/9/cloud"],
    "opensuse:15": [
        "images:opensuse/15.6",
        "images:opensuse/15",
        "images:opensuse/leap/15.6",
    ],
}


class ProviderError(Exception):
    """Provider-level failure with a user-safe message."""


def _cli_snippet(out: str, limit: int = 1200) -> str:
    """Prefer the tail of CLI output — progress spam sits at the head."""
    text = (out or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    # collapse huge progress-line noise
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ""
    if len(text) <= limit:
        return text
    # keep last lines that often hold the real error
    tail = "\n".join(lines[-40:])
    if len(tail) > limit:
        tail = tail[-limit:]
    return tail


def validate_resources(memory_mb: int, cpus: int, disk_gb: int) -> None:
    if memory_mb < MIN_MEMORY_MB or memory_mb > MAX_MEMORY_MB:
        raise ProviderError(
            f"Memory must be between {MIN_MEMORY_MB}MB and {MAX_MEMORY_MB}MB"
        )
    if cpus < MIN_CPUS or cpus > MAX_CPUS:
        raise ProviderError(f"CPU must be between {MIN_CPUS} and {MAX_CPUS}")
    if disk_gb < MIN_DISK_GB or disk_gb > MAX_DISK_GB:
        raise ProviderError(
            f"Disk space must be between {MIN_DISK_GB}GB and {MAX_DISK_GB}GB"
        )


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_name() -> str:
    return f"vex-{secrets.token_hex(4)}"


def _detect_cli() -> str:
    override = (LXD_CLI or "").strip()
    if override:
        if shutil.which(override) or os.path.isfile(override):
            return override
        raise ProviderError(f"LXD_CLI is set but not executable: {override}")
    for cand in ("incus", "lxc"):
        path = shutil.which(cand)
        if path:
            return path
    for path in ("/snap/bin/lxc", "/snap/bin/incus", "/usr/bin/lxc", "/usr/bin/incus"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise ProviderError(
        "Neither lxc nor incus CLI found (install LXD/Incus or set LXD_CLI)"
    )


@dataclass
class VPSResult:
    container_id: str
    container_name: str
    ip_address: str
    ssh_port: int
    username: str
    password: str
    image: str
    memory_mb: int
    cpus: int
    disk_gb: int
    status: str = "running"
    notes: list[str] = field(default_factory=list)


class LXDProvider:
    """Creates and manages VPS instances on local LXD/Incus via the lxc CLI."""

    def __init__(self, network: str = LXD_NETWORK) -> None:
        self._cli = _detect_cli()
        self._exec_flags = self._detect_exec_flags()
        self.network_name = network
        self.storage_pool = (LXD_STORAGE or "").strip()
        self._tools_cache: dict[str, str] = {}
        self._ensure_storage()
        self._ensure_network()
        logger.info(
            "LXD provider ready (cli=%s, network=%s, storage=%s, exec=%s)",
            self._cli,
            self.network_name,
            self.storage_pool or "auto",
            " ".join(self._exec_flags) or "default",
        )

    def _run(
        self, args: list[str], *, timeout: int = 60, check: bool = True
    ) -> tuple[int, str]:
        cmd = [self._cli, *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"LXD CLI not found: {self._cli}") from exc
        except subprocess.TimeoutExpired as exc:
            partial = ""
            if exc.stdout:
                partial += (
                    exc.stdout.decode("utf-8", "replace")
                    if isinstance(exc.stdout, bytes)
                    else str(exc.stdout)
                )
            if exc.stderr:
                partial += (
                    exc.stderr.decode("utf-8", "replace")
                    if isinstance(exc.stderr, bytes)
                    else str(exc.stderr)
                )
            raise ProviderError(
                f"LXD command timed out after {timeout}s: "
                f"{' '.join(args[:4])}. {_cli_snippet(partial)}"
            ) from exc
        out = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise ProviderError(
                f"LXD command failed ({proc.returncode}): {_cli_snippet(out)}"
            )
        return proc.returncode, out

    def _detect_exec_flags(self) -> list[str]:
        candidates = (
            ["--mode", "non-interactive"],
            ["--force-noninteractive"],
            [],
        )
        for flags in candidates:
            code, out = self._run(
                ["exec", "__vex_probe__", *flags, "--", "true"],
                check=False,
                timeout=15,
            )
            low = (out or "").lower()
            if any(
                s in low
                for s in (
                    "unknown",
                    "unexpected",
                    "flag provided",
                    "invalid argument",
                    "unknown option",
                )
            ):
                continue
            return flags
        return []

    def _list_storage_pools(self) -> list[str]:
        code, out = self._run(
            ["storage", "list", "--format", "csv", "-c", "n"],
            check=False,
            timeout=30,
        )
        if code != 0:
            return []
        names: list[str] = []
        for line in (out or "").splitlines():
            name = line.strip().strip(",").split(",")[0].strip()
            if name and name not in names:
                names.append(name)
        return names

    def _ensure_storage(self) -> None:
        """Resolve a storage pool so launch can attach a root disk (-s)."""
        pools = self._list_storage_pools()

        if self.storage_pool:
            if self.storage_pool in pools:
                return
            if pools:
                logger.warning(
                    "LXD_STORAGE=%s not found (pools=%s) — ignoring",
                    self.storage_pool,
                    pools,
                )
                self.storage_pool = ""

        # prefer well-known names, then any pool
        if not self.storage_pool:
            for pref in ("default", "vexdeploy", "local", "backend"):
                if pref in pools:
                    self.storage_pool = pref
                    break
            if not self.storage_pool and pools:
                self.storage_pool = pools[0]
        if self.storage_pool and self.storage_pool in pools:
            return

        # create a simple dir pool (use LXD_STORAGE name if provided)
        candidates = []
        if self.storage_pool:
            candidates.append(self.storage_pool)
        for name in ("default", "vexdeploy"):
            if name not in candidates:
                candidates.append(name)

        for name in candidates:
            try:
                self._run(
                    ["storage", "create", name, "dir"],
                    check=False,
                    timeout=60,
                )
                code, _ = self._run(
                    ["storage", "show", name], check=False, timeout=15
                )
                if code == 0:
                    self.storage_pool = name
                    logger.info("Created LXD storage pool %s (dir)", name)
                    return
            except ProviderError as exc:
                logger.warning("storage create %s failed: %s", name, exc)

        raise ProviderError(
            "No LXD/Incus storage pool found. Create one:\n"
            "  incus storage create default dir\n"
            "  # or: lxc storage create default dir\n"
            "then set LXD_STORAGE=default in .env"
        )

    def _ensure_network(self) -> None:
        code, _ = self._run(
            ["network", "show", self.network_name], check=False, timeout=30
        )
        if code == 0:
            return
        try:
            self._run(
                [
                    "network",
                    "create",
                    self.network_name,
                    "ipv4.address=auto",
                    "ipv4.nat=true",
                ],
                timeout=60,
            )
            logger.info("Created LXD network %s", self.network_name)
        except ProviderError as exc:
            code2, _ = self._run(
                ["network", "show", self.network_name], check=False, timeout=30
            )
            if code2 != 0:
                raise ProviderError(f"Failed to create LXD network: {exc}") from exc

    def _list_all_json(self) -> list:
        code, out = self._run(["list", "--format", "json"], check=False, timeout=90)
        if code != 0:
            raise ProviderError(f"Failed to list instances: {out.strip()[:400]}")
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            raise ProviderError("Failed to parse LXD instance list") from exc
        return data if isinstance(data, list) else []

    def _list_managed_json(self) -> list:
        managed = []
        for inst in self._list_all_json():
            cfg = inst.get("config") or {}
            if str(cfg.get(LABEL_MANAGED, "")) == "1":
                managed.append(inst)
        return managed

    def managed_count(self) -> int:
        return len(self._list_managed_json())

    def assert_capacity(self, max_containers: int = MAX_CONTAINERS) -> None:
        total = self.managed_count()
        limit = max(1, int(max_containers))
        if total >= limit:
            raise ProviderError(
                f"Server is at capacity ({total}/{limit} instances)."
            )

    def list_managed(self) -> list:
        return self._list_managed_json()

    def _resolve_image(self, image: str) -> str:
        image = (image or "").strip()
        if image in IMAGE_MAP:
            return IMAGE_MAP[image]
        if ":" in image and not image.startswith("images:"):
            remote = image.split(":", 1)[0]
            if remote in {"ubuntu", "ubuntu-daily", "images", "simplestreams"}:
                return image
        if "/" in image and not image.startswith("images:"):
            return f"images:{image}"
        return image

    def _image_candidates(self, image: str) -> list[str]:
        image = (image or "").strip()
        if image in IMAGE_CANDIDATES:
            return list(IMAGE_CANDIDATES[image])
        if image in IMAGE_MAP:
            primary = IMAGE_MAP[image]
            alts = IMAGE_CANDIDATES.get(image, [])
            out = [primary]
            for alt in alts:
                if alt not in out:
                    out.append(alt)
            return out
        resolved = self._resolve_image(image)
        out = [resolved]
        for key, alts in IMAGE_CANDIDATES.items():
            if image == key or resolved == IMAGE_MAP.get(key):
                for alt in alts:
                    if alt not in out:
                        out.append(alt)
        if image not in out:
            out.append(image)
        return out

    @staticmethod
    def _is_missing_image_error(text: str) -> bool:
        low = (text or "").lower()
        return any(
            s in low
            for s in (
                "image couldn't be found",
                "image not found",
                "failed getting remote image",
                "failed getting image info",
                "no such image",
                "remote image info",
                "image not available",
                "not found in remote",
            )
        )

    @staticmethod
    def _is_transient_error(text: str) -> bool:
        """Network/disk blips during rootfs download — worth retrying."""
        low = (text or "").lower()
        return any(
            s in low
            for s in (
                "connection reset",
                "connection refused",
                "connection aborted",
                "broken pipe",
                "network is unreachable",
                "no route to host",
                "temporary failure",
                "timed out",
                "timeout",
                "unexpected end of",
                "incomplete read",
                "error while downloading",
                "download error",
                "transfer failed",
                "tls handshake",
                "certificate",
                "http error 5",
                "http error 429",
                "server disconnected",
                "no space left",
                "disk quota exceeded",
                "context deadline",
            )
        )

    def _launch_once(self, args: list[str], *, timeout: int = 600) -> None:
        name = args[2] if len(args) > 2 else ""
        try:
            self._run(args, timeout=timeout)
        except ProviderError:
            self._safe_remove(name)
            raise

    def _launch_instance(
        self,
        *,
        image_uri: str,
        name: str,
        owner_id: str,
        memory_mb: int,
        cpus: int,
        vps_id: str,
    ) -> str:
        """Launch with image fallbacks + one transient retry. Returns working image URI."""
        args = [
            "launch",
            image_uri,
            name,
            "-n",
            self.network_name,
            "-c",
            f"limits.memory={int(memory_mb)}MB",
            "-c",
            f"limits.cpu={int(cpus)}",
            "-c",
            f"{LABEL_MANAGED}=1",
            "-c",
            f"{LABEL_OWNER}={str(owner_id)}",
            "-c",
            f"{LABEL_VPS_ID}={vps_id or ''}",
        ]
        if self.storage_pool:
            args.extend(["-s", self.storage_pool])
        # first pull can take a while on cold cache
        launch_timeout = int(os.environ.get("LXD_LAUNCH_TIMEOUT", "600"))

        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                self._launch_once(args, timeout=launch_timeout)
                return image_uri
            except ProviderError as exc:
                last_err = exc
                err = str(exc)
                if self._is_missing_image_error(err):
                    logger.warning("image %s missing: %s", image_uri, err)
                    break
                if attempt < 2 and self._is_transient_error(err):
                    logger.warning(
                        "launch %s transient failure (attempt %s): %s",
                        name,
                        attempt,
                        err,
                    )
                    time.sleep(3)
                    continue
                raise ProviderError(f"Failed to create instance: {err}") from exc

        candidates = self._image_candidates(image_uri)
        if image_uri in candidates:
            candidates = [c for c in candidates if c != image_uri] + [image_uri]
        for alt in candidates:
            args_alt = list(args)
            args_alt[1] = alt
            try:
                self._launch_once(args_alt, timeout=launch_timeout)
                logger.info("Launched %s with fallback image %s", name, alt)
                return alt
            except ProviderError as exc:
                last_err = exc
                err = str(exc)
                if self._is_transient_error(err):
                    logger.warning("fallback %s transient: %s", alt, err)
                    try:
                        time.sleep(3)
                        self._launch_once(args_alt, timeout=launch_timeout)
                        logger.info("Launched %s with %s after retry", name, alt)
                        return alt
                    except ProviderError as exc2:
                        last_err = exc2
                        err = str(exc2)
                if not self._is_missing_image_error(err):
                    raise ProviderError(f"Failed to create instance: {err}") from exc
                logger.warning("image %s failed: %s", alt, err)
                continue
        raise ProviderError(
            "Failed to create instance: no usable image "
            f"(tried {', '.join(candidates)}). "
            f"Last error: {last_err}"
        )

    # ── create ──────────────────────────────────────────────
    def create_vps(
        self,
        *,
        owner_id: str,
        memory_mb: int,
        cpus: int,
        disk_gb: int,
        image: str,
        vps_id: str = "",
        max_containers: int = MAX_CONTAINERS,
    ) -> VPSResult:
        validate_resources(memory_mb, cpus, disk_gb)
        self.assert_capacity(max_containers)

        password = generate_password()
        name = generate_name()
        username = "root"
        notes: list[str] = []

        image_uri = self._launch_instance(
            image_uri=self._resolve_image(image),
            name=name,
            owner_id=owner_id,
            memory_mb=memory_mb,
            cpus=cpus,
            vps_id=vps_id,
        )

        code, _ = self._run(
            [
                "config",
                "device",
                "override",
                name,
                "root",
                f"size={int(disk_gb)}GB",
            ],
            check=False,
            timeout=30,
        )
        if code != 0:
            notes.append(
                f"Disk quota {disk_gb}GB may not be enforced on this storage backend."
            )

        try:
            self._wait_exec_ready(name, timeout=90)
            self._bootstrap(name, password)
            ip = self._instance_ip(name)
            self._wait_ssh(name, timeout=90)
            ip = self._instance_ip(name) or ip
            return VPSResult(
                container_id=name,
                container_name=name,
                ip_address=ip,
                ssh_port=22,
                username=username,
                password=password,
                image=image,
                memory_mb=memory_mb,
                cpus=cpus,
                disk_gb=disk_gb,
                status="running",
                notes=notes,
            )
        except Exception:
            self._safe_remove(name)
            raise

    @staticmethod
    def _bootstrap_script(password: str) -> str:
        return (
            "set -e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then "
            "  apt-get update -qq && apt-get install -y -qq "
            "openssh-server sudo curl wget ca-certificates bash; "
            "elif command -v apk >/dev/null 2>&1; then "
            "  apk add --no-cache openssh-server sudo curl wget bash; "
            "fi; "
            "mkdir -p /var/run/sshd /root/.ssh; "
            "chmod 700 /root/.ssh; "
            "echo 'root:" + password + "' | chpasswd; "
            "if [ -f /etc/ssh/sshd_config ]; then "
            "  sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config; "
            "  sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
            "  sed -i 's/^#\\?PermitEmptyPasswords.*/PermitEmptyPasswords no/' /etc/ssh/sshd_config; "
            "fi; "
            "if command -v ssh-keygen >/dev/null 2>&1 && [ ! -f /etc/ssh/ssh_host_rsa_key ]; then "
            "  ssh-keygen -A; "
            "fi; "
            "if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then "
            "  systemctl enable ssh >/dev/null 2>&1 || systemctl enable sshd >/dev/null 2>&1 || true; "
            "  systemctl restart ssh >/dev/null 2>&1 || systemctl restart sshd >/dev/null 2>&1 || true; "
            "elif command -v rc-service >/dev/null 2>&1; then "
            "  rc-update add sshd default >/dev/null 2>&1 || true; "
            "  rc-service sshd restart >/dev/null 2>&1 || rc-service sshd start >/dev/null 2>&1 || true; "
            "else "
            "  (setsid /usr/sbin/sshd -e >/tmp/vex-sshd.log 2>&1 &); "
            "fi; "
            "echo BOOTSTRAP_OK"
        )

    def _wait_exec_ready(self, name: str, timeout: int = 90) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.status(name) == "missing":
                raise ProviderError("Instance disappeared during startup")
            code, _ = self._run(
                ["exec", name, "--", "true"], check=False, timeout=15
            )
            if code == 0:
                return
            time.sleep(1)
        raise ProviderError("Instance never became ready for exec")

    def _bootstrap(self, name: str, password: str) -> None:
        script = self._bootstrap_script(password)
        args = ["exec", name, *self._exec_flags, "--", "sh", "-lc", script]
        code, out = self._run(args, check=False, timeout=240)
        if code != 0:
            raise ProviderError(
                f"SSH bootstrap failed (exit {code}): {out.strip()[:500]}"
            )
        if "BOOTSTRAP_OK" not in out:
            logger.warning("bootstrap finished without OK marker: %s", out[-400:])

    def _instance_json(self, name: str) -> dict | None:
        code, out = self._run(
            ["list", name, "--format", "json"], check=False, timeout=30
        )
        if code != 0:
            return None
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError:
            return None
        if isinstance(data, list) and data:
            return data[0]
        return None

    def _instance_ip(self, name: str) -> str:
        inst = self._instance_json(name)
        if not inst:
            return ""
        network = ((inst.get("state") or {}).get("network")) or {}
        candidates = []
        for iface, info in network.items():
            if iface == "lo":
                continue
            for addr in info.get("addresses") or []:
                if addr.get("family") != "inet":
                    continue
                ip = str(addr.get("address") or "").strip()
                if not ip:
                    continue
                if addr.get("scope") == "link":
                    candidates.append(ip)
                else:
                    return ip
        return candidates[0] if candidates else ""

    def _wait_ssh(self, name: str, timeout: int = 90) -> None:
        probe = (
            "nc -z 127.0.0.1 22 >/dev/null 2>&1 || "
            "grep -qE ':0016[[:space:]]' /proc/net/tcp 2>/dev/null || "
            "ss -lnt 2>/dev/null | grep -q ':22'"
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status(name)
            if st == "missing":
                raise ProviderError("Instance stopped during bootstrap")
            if st == "running":
                code, _ = self._run(
                    ["exec", name, *self._exec_flags, "--", "sh", "-lc", probe],
                    check=False,
                    timeout=15,
                )
                if code == 0:
                    return
            time.sleep(2)
        logger.warning("SSH readiness check timed out for %s", name)

    # ── lifecycle ───────────────────────────────────────────
    def get_container(self, container_id: str) -> str:
        code, _ = self._run(["info", container_id], check=False, timeout=30)
        if code != 0:
            raise ProviderError("Instance not found")
        return container_id

    def start(self, container_id: str) -> None:
        code, out = self._run(["start", container_id], check=False, timeout=90)
        if code != 0:
            low = (out or "").lower()
            if "already running" in low or "instance is already running" in low:
                return
            raise ProviderError(f"Start failed: {out.strip()[:400]}")

    def stop(self, container_id: str, timeout: int = 10) -> None:
        args = ["stop", container_id]
        if timeout and int(timeout) > 0:
            args += ["--timeout", str(int(timeout))]
        else:
            args.append("--force")
        code, out = self._run(args, check=False, timeout=max(60, int(timeout) + 30))
        if code != 0:
            low = (out or "").lower()
            if "not running" in low or "already stopped" in low:
                return
            raise ProviderError(f"Stop failed: {out.strip()[:400]}")

    def restart(self, container_id: str, timeout: int = 10) -> None:
        args = ["restart", container_id]
        if timeout and int(timeout) > 0:
            args += ["--timeout", str(int(timeout))]
        code, out = self._run(args, check=False, timeout=max(90, int(timeout) + 30))
        if code != 0:
            raise ProviderError(f"Restart failed: {out.strip()[:400]}")

    def remove(self, container_id: str, force: bool = True) -> None:
        args = ["delete", container_id]
        if force:
            args.append("--force")
        code, out = self._run(args, check=False, timeout=90)
        self._tools_cache.pop(container_id, None)
        if code != 0:
            low = (out or "").lower()
            if "not found" in low or "does not exist" in low:
                return
            raise ProviderError(f"Delete failed: {out.strip()[:400]}")

    def _safe_remove(self, container_id: str) -> None:
        if not container_id:
            return
        try:
            self.remove(container_id, force=True)
        except Exception as exc:
            logger.error("Cleanup failed for %s: %s", container_id, exc)

    def status(self, container_id: str) -> str:
        code, out = self._run(
            ["list", container_id, "--format", "csv", "-c", "s"],
            check=False,
            timeout=30,
        )
        if code != 0 or not out.strip():
            return "missing"
        raw = out.strip().splitlines()[0].strip().lower()
        if raw == "running":
            return "running"
        if raw in {"stopped", "frozen"}:
            return "stopped"
        if raw in {"ready", "started"}:
            return "running"
        if raw in {"error", "errored"}:
            return "missing"
        return raw or "missing"

    def set_password(self, container_id: str, password: str) -> None:
        code, out = self.exec_command(
            container_id, f"echo 'root:{password}' | chpasswd", timeout=30
        )
        if code != 0:
            raise ProviderError(
                "Failed to update SSH password inside instance"
            )

    def exec_command(
        self, container_id: str, command: str, timeout: int = 60
    ) -> tuple[int, str]:
        # sh (not bash) — minimal images / Alpine may lack bash
        args = [
            "exec",
            container_id,
            *self._exec_flags,
            "--",
            "sh",
            "-lc",
            command,
        ]
        code, out = self._run(args, check=False, timeout=timeout)
        return code, out

    def logs(self, container_id: str, tail: int = 50) -> str:
        n = max(1, int(tail))
        code, raw = self._run(
            ["info", container_id, "--show-log"], check=False, timeout=30
        )
        if code == 0 and (raw or "").strip():
            lines = raw.strip().splitlines()
            return "\n".join(lines[-n:])
        code2, out = self.exec_command(
            container_id,
            "journalctl -n %d --no-pager 2>/dev/null || "
            "dmesg 2>/dev/null | tail -n %d || true" % (n, n),
            timeout=30,
        )
        if code2 == 0:
            return out
        return raw or out or ""

    # ── reverse SSH (sshx) ───────────────────────────────────
    def _ensure_remote_tools(self, container_id: str, *, force: bool = False) -> str:
        """Install curl/sshx once per instance (cached).

        A full apt + download pass used to run on every sshx attempt (~3 min).
        Probe first; only install when missing; cache the log.
        """
        if not force and container_id in self._tools_cache:
            return self._tools_cache[container_id]

        # Fast path: tools already present (or a prior run left a ready marker).
        try:
            _, probe = self.exec_command(
                container_id,
                "if [ -f /tmp/vex-tools.ready ]; then echo TOOLS_READY; fi; "
                "command -v sshx >/dev/null 2>&1 && echo HAVE_SSHX || echo NO_SSHX; "
                "command -v curl >/dev/null 2>&1 && echo HAVE_CURL || echo NO_CURL",
                timeout=12,
            )
        except Exception as exc:
            probe = f"probe error: {exc}"
        text = probe or ""
        if "TOOLS_READY" in text or ("HAVE_SSHX" in text and "HAVE_CURL" in text):
            self._tools_cache[container_id] = text
            return text

        # Install pass — tight timeouts (worst ~70s, usually much less).
        script = r"""set +e
export DEBIAN_FRONTEND=noninteractive
export APT_LISTCHANGES_FRONTEND=none
echo '--- install ---'
if command -v apt-get >/dev/null 2>&1; then
  # try without update first (existing lists are often enough / faster)
  timeout 40 apt-get install -y -qq --no-install-recommends curl ca-certificates \
    >>/tmp/vex-apt.log 2>&1 || {
    timeout 20 apt-get update -qq >/tmp/vex-apt.log 2>&1 || true
    timeout 35 apt-get install -y -qq --no-install-recommends curl ca-certificates \
      >>/tmp/vex-apt.log 2>&1 || true
  }
elif command -v apk >/dev/null 2>&1; then
  timeout 35 apk add --no-cache curl ca-certificates >/tmp/vex-apt.log 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  timeout 40 yum install -y curl ca-certificates >/tmp/vex-apt.log 2>&1 || true
fi

# sshx: official installer
if ! command -v sshx >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
  echo 'sshx: official installer'
  if timeout 20 curl -sSf --retry 1 --connect-timeout 8 https://sshx.io/get -o /tmp/sshx-get.sh; then
    timeout 25 sh /tmp/sshx-get.sh >/tmp/vex-sshx-install.log 2>&1 || \
      echo 'sshx installer failed'
  else
    echo 'sshx get script download failed'
  fi
fi

# find common install prefixes if not on PATH
for d in /usr/local/bin /usr/bin "$HOME/.local/bin" /root/.cargo/bin; do
  if [ -x "$d/sshx" ] && ! command -v sshx >/dev/null 2>&1; then
    ln -sf "$d/sshx" /usr/local/bin/sshx 2>/dev/null || cp -f "$d/sshx" /usr/local/bin/sshx 2>/dev/null || true
  fi
done

command -v sshx >/dev/null 2>&1 && echo HAVE_SSHX || echo NO_SSHX
command -v curl >/dev/null 2>&1 && echo HAVE_CURL || echo NO_CURL
if command -v sshx >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
  echo TOOLS_READY > /tmp/vex-tools.ready
  echo TOOLS_READY
fi
echo '--- apt tail ---'
tail -n 30 /tmp/vex-apt.log 2>/dev/null || true
echo '--- sshx install tail ---'
tail -n 20 /tmp/vex-sshx-install.log 2>/dev/null || true
"""
        try:
            code, out = self._run_script(container_id, script, timeout=70)
        except Exception as exc:
            logger.warning("remote tools install failed: %s", exc)
            text = f"install error: {exc}"
            self._tools_cache[container_id] = text
            return text
        text = out or ""
        if "NO_SSHX" in text:
            logger.warning("remote tools partial (exit %s): %s", code, text[-800:])
        self._tools_cache[container_id] = text
        return text

    def _run_script(
        self, container_id: str, script: str, timeout: int = 60
    ) -> tuple[int, str]:
        """Write script to a unique /tmp path and run with an outer timeout.

        Decodes via python3 first (byte-exact); falls back to base64(1).
        A fixed /tmp/vex-run.sh path raced and left "No such file" errors.
        """
        import base64

        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        rid = secrets.token_hex(4)
        path = f"/tmp/vex-run-{rid}.sh"
        # base64 alphabet has no single quotes — safe to embed
        decoder = (
            f"if command -v python3 >/dev/null 2>&1; then "
            f"python3 -c \"import base64;open('{path}','wb').write(base64.b64decode('{b64}'))\"; "
            f"else printf '%s' '{b64}' | base64 -d > '{path}'; fi"
        )
        wrapper = (
            f"set +e; {decoder}; "
            f"if [ ! -s '{path}' ]; then "
            f"echo SCRIPT_WRITE_FAIL; ls -l '{path}' 2>/dev/null; exit 78; "
            f"fi; "
            f"chmod 700 '{path}'; "
            f"timeout {max(5, int(timeout))} sh '{path}'; "
            f"ec=$?; rm -f '{path}'; exit $ec"
        )
        return self.exec_command(container_id, wrapper, timeout=timeout + 15)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        import re

        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "")
        text = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", text)
        return text.replace("\x1b", "")

    @staticmethod
    def _extract_sshx_url(text: str) -> str:
        import re

        text = LXDProvider._strip_ansi(text)
        # Prefer explicit LINK= lines (may include "path:url" from grep -H)
        m = re.search(r"^LINK=(\S+)", text, re.MULTILINE)
        if m and m.group(1).strip():
            raw = m.group(1).strip().rstrip(".,);'\"")
            # strip "/tmp/foo.log:" grep filename prefix
            m2 = re.search(r"(https?://\S+|ssh\s+\S*@sshx\.io\S*)", raw)
            if m2:
                return m2.group(1).rstrip(".,);'\"")
            if raw.startswith("http") or "sshx.io" in raw:
                return raw
        # Bare URL anywhere, including "path:https://..." from multi-file grep
        for pat in (
            r"(https://sshx\.io/s/[A-Za-z0-9]+#[A-Za-z0-9]+)",
            r"(https://sshx\.io/s/[A-Za-z0-9._/-]+#?[A-Za-z0-9._-]*)",
            r"(https://sshx\.io/\S+)",
            r"(ssh\s+\S+@sshx\.io\S*)",
            r"(sshx\.io/\S+)",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                url = m.group(1).strip().rstrip(".,);'\"")
                # drop any leading "/tmp/....:" 
                if ":" in url and not url.startswith("http") and not url.startswith("ssh"):
                    url = url.split(":", 1)[-1]
                url = re.sub(r"^/tmp/\S+:", "", url)
                url = re.sub(r"[\x00-\x1f]+$", "", url)
                if url.startswith("http") or "sshx.io" in url:
                    return url
        return ""

    def start_sshx(self, container_id: str, timeout: int = 25) -> str:
        """Install sshx if needed and return a share URL.

        sshx must keep a live process after the URL is printed. Redirecting
        stdin to /dev/null (or wrapping in `timeout`) makes it exit on EOF —
        link opens but "create terminal" does nothing. Hold stdin open with
        `tail -f /dev/null` and never time-box the sshx process itself.
        """
        install_log = self._ensure_remote_tools(container_id)

        script = f"""set +e
export TERM=xterm-256color
# stop previous session + stdin keepers
for f in /tmp/sshx.pid /tmp/sshx-keeper.pid; do
  if [ -f "$f" ]; then kill "$(cat "$f" 2>/dev/null)" >/dev/null 2>&1 || true; fi
done
pkill -x sshx >/dev/null 2>&1 || true
# only kill our tail keepers (full cmdline match), not unrelated tails
pkill -f 'tail -f /dev/null' >/dev/null 2>&1 || true
rm -f /tmp/sshx.log /tmp/sshx.pid /tmp/sshx.out /tmp/sshx.typescript /tmp/sshx-run.log /tmp/sshx-keeper.pid /tmp/sshx.stdin
: > /tmp/sshx.log
echo '--- sshx diag ---'
command -v sshx >/dev/null 2>&1 && echo HAVE_SSHX_BIN || echo NO_SSHX_BIN
command -v sshx >/dev/null 2>&1 && ls -l "$(command -v sshx)" 2>/dev/null || true

extract_link() {{
  cat /tmp/sshx.log /tmp/sshx.typescript /tmp/sshx-run.log 2>/dev/null \\
    | grep -Eo 'https://sshx\\.io/s/[A-Za-z0-9]+#[A-Za-z0-9]+|https://sshx\\.io/[^[:space:]]+|ssh [A-Za-z0-9._-]+@sshx\\.io[^[:space:]]*' \\
    | head -n1
}}

wait_for_link() {{
  i=0
  while [ "$i" -lt "$1" ]; do
    L=$(extract_link)
    if [ -n "$L" ]; then echo "$L"; return 0; fi
    i=$((i+1))
    sleep 1
  done
  extract_link
}}

alive() {{
  [ -f /tmp/sshx.pid ] || return 1
  PID=$(cat /tmp/sshx.pid 2>/dev/null)
  [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null
}}

# Whole launch lives in a new session (setsid) so:
# - our shell exit does not SIGHUP it
# - outer process-group kill (timeout) does not kill tail/sshx (EOF bug)
# stdin is held open by `tail -f /dev/null` inside that session.

# --- try 1: installed binary under script(1) PTY (or direct) ---
if command -v sshx >/dev/null 2>&1; then
  if command -v script >/dev/null 2>&1; then
    setsid sh -c 'tail -f /dev/null | script -q -e -c sshx /tmp/sshx.typescript >/tmp/sshx.log 2>&1' &
  else
    setsid sh -c 'tail -f /dev/null | sshx >/tmp/sshx.log 2>&1' &
  fi
  sleep 0.4
  SSHX_PID=$(pgrep -x sshx 2>/dev/null | head -n1)
  if [ -z "$SSHX_PID" ]; then
    SSHX_PID=$(pgrep -f 'script .*sshx' 2>/dev/null | head -n1)
  fi
  # record sshx itself (not the setsid wrapper) so kill -0 is meaningful
  if [ -n "$SSHX_PID" ]; then echo "$SSHX_PID" > /tmp/sshx.pid; fi
  LINK=$(wait_for_link {timeout})
  sleep 1
  if [ -n "$LINK" ] && alive; then
    echo "LINK=$LINK"
    echo "PID=$(cat /tmp/sshx.pid)"
    echo SSHX_OK
    exit 0
  fi
  # dead or no link — clean up and try run mode
  if [ -f /tmp/sshx.pid ]; then kill "$(cat /tmp/sshx.pid)" >/dev/null 2>&1 || true; fi
  pkill -x sshx >/dev/null 2>&1 || true
fi

# --- try 2: official CI runner (same setsid + open-stdin pattern) ---
echo '--- sshx run mode ---'
if [ -f /tmp/sshx-get.sh ]; then
  setsid sh -c 'tail -f /dev/null | sh /tmp/sshx-get.sh run >/tmp/sshx-run.log 2>&1' &
elif command -v curl >/dev/null 2>&1; then
  setsid sh -c 'tail -f /dev/null | sh -c "curl -sSf https://sshx.io/get | sh -s run" >/tmp/sshx-run.log 2>&1' &
else
  echo 'no sshx runner (need binary or curl)'
fi
sleep 0.4
SSHX_PID=$(pgrep -x sshx 2>/dev/null | head -n1)
if [ -z "$SSHX_PID" ]; then
  SSHX_PID=$(pgrep -f 'sshx' 2>/dev/null | head -n1)
fi
if [ -n "$SSHX_PID" ]; then echo "$SSHX_PID" > /tmp/sshx.pid; fi
LINK=$(wait_for_link {timeout})
sleep 1
if [ -n "$LINK" ] && alive; then
  echo "LINK=$LINK"
  echo "PID=$(cat /tmp/sshx.pid)"
  echo SSHX_OK
  exit 0
fi

echo '--- sshx.log ---'
cat /tmp/sshx.log 2>/dev/null || true
echo '--- sshx.typescript ---'
cat /tmp/sshx.typescript 2>/dev/null || true
echo '--- sshx-run.log ---'
cat /tmp/sshx-run.log 2>/dev/null || true
echo '--- net diag ---'
getent ahosts sshx.io 2>/dev/null | head -n3 || echo NO_DNS
ip -4 route show default 2>/dev/null | head -n1 || echo NO_IPV4_ROUTE
ip -6 route show default 2>/dev/null | head -n1 || echo NO_IPV6_ROUTE
timeout 5 bash -c 'exec 3<>/dev/tcp/sshx.io/22' && echo SSHX22_OK || echo SSHX22_FAIL
timeout 5 bash -c 'exec 3<>/dev/tcp/sshx.io/443' && echo SSHX443_OK || echo SSHX443_FAIL
if [ -n "$LINK" ]; then
  echo "LINK_DEAD=$LINK"
fi
echo SSHX_NO_LINK
exit 3
"""
        try:
            code, out = self._run_script(container_id, script, timeout=timeout + 30)
        except Exception as exc:
            raise ProviderError(f"sshx exec failed: {exc}") from exc
        out = self._strip_ansi(out or "")

        url = ""
        for line in out.splitlines():
            if line.startswith("LINK=") and line[5:].strip() and not line.startswith("LINK_DEAD"):
                url = self._extract_sshx_url(line[5:].strip()) or line[5:].strip()
                break
            if line.startswith("LINK_DEAD="):
                dead = line[10:].strip()
                raise ProviderError(
                    "sshx printed a link but the process exited immediately "
                    f"(session dead): {dead}"
                )
        if not url:
            url = self._extract_sshx_url(out)
        if not url:
            _, out2 = self.exec_command(
                container_id,
                "cat /tmp/sshx.log /tmp/sshx.typescript /tmp/sshx-run.log 2>/dev/null || true",
                timeout=15,
            )
            url = self._extract_sshx_url(out2 or "")
        url = self._strip_ansi(url or "").strip()
        if not url:
            tail = out[-700:].strip() or self._strip_ansi(install_log or "")[-400:].strip()
            raise ProviderError(
                "sshx did not return a share link "
                f"(exit {code}). Output: {tail or 'empty'}"
            )
        # final liveness check
        _, alive_chk = self.exec_command(
            container_id,
            "if [ -f /tmp/sshx.pid ] && kill -0 \"$(cat /tmp/sshx.pid)\" 2>/dev/null; then echo ALIVE; else echo DEAD; fi",
            timeout=10,
        )
        if "DEAD" in (alive_chk or ""):
            raise ProviderError(
                f"sshx process is not running after start (link may be dead): {url}"
            )
        return url

    def stop_remote_share(self, container_id: str, tool: str = "all") -> None:
        """Best-effort stop of sshx/ttyd sessions inside an instance."""
        if tool in ("sshx", "all"):
            self.exec_command(
                container_id,
                "if [ -f /tmp/sshx.pid ]; then kill \"$(cat /tmp/sshx.pid 2>/dev/null)\" >/dev/null 2>&1 || true; fi; "
                "if [ -f /tmp/sshx-keeper.pid ]; then kill \"$(cat /tmp/sshx-keeper.pid 2>/dev/null)\" >/dev/null 2>&1 || true; fi; "
                "pkill -x sshx >/dev/null 2>&1 || true; "
                "pkill -f 'tail -f /dev/null' >/dev/null 2>&1 || true; true",
                timeout=15,
            )
        if tool in ("web", "all"):
            self.exec_command(
                container_id,
                "if [ -f /tmp/vex-ttyd.pid ]; then kill \"$(cat /tmp/vex-ttyd.pid 2>/dev/null)\" >/dev/null 2>&1 || true; fi; "
                "if [ -f /tmp/vex-ttyd-tunnel.pid ]; then kill \"$(cat /tmp/vex-ttyd-tunnel.pid 2>/dev/null)\" >/dev/null 2>&1 || true; fi; "
                "pkill -f 'nokey@localhost\\.run' >/dev/null 2>&1 || true; "
                "rm -f /tmp/vex-ttyd-tunnel.fifo /tmp/vex-ttyd-tunnel-keeper.pid; "
                "pkill -x ttyd >/dev/null 2>&1 || true; true",
                timeout=15,
            )

    def start_web_terminal(self, container_id: str, timeout: int = 75) -> dict:
        """Browser shell via ttyd + localhost.run (works when sshx relays are blocked).

        File manager already proves this tunnel path from the instance.
        """
        token = secrets.token_urlsafe(16)
        port = 7681
        # ARCH-aware ttyd static binary; fall back to apt
        script = f"""set +e
export DEBIAN_FRONTEND=noninteractive
TOKEN={token}
PORT={port}
if [ -f /tmp/vex-ttyd.pid ]; then kill "$(cat /tmp/vex-ttyd.pid)" >/dev/null 2>&1 || true; fi
if [ -f /tmp/vex-ttyd-tunnel.pid ]; then kill "$(cat /tmp/vex-ttyd-tunnel.pid)" >/dev/null 2>&1 || true; fi
pkill -f 'nokey@localhost\\.run' >/dev/null 2>&1 || true
rm -f /tmp/vex-ttyd.log /tmp/vex-ttyd.pid /tmp/vex-ttyd-tunnel.log /tmp/vex-ttyd-tunnel.pid /tmp/vex-ttyd-askpass.sh /tmp/vex-ttyd-tunnel.fifo /tmp/vex-ttyd-tunnel-keeper.pid

install_ttyd() {{
  command -v ttyd >/dev/null 2>&1 && return 0
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64|amd64) TA=x86_64 ;;
    aarch64|arm64) TA=aarch64 ;;
    armv7l|armhf) TA=armhf ;;
    i386|i686) TA=i686 ;;
    *) TA=x86_64 ;;
  esac
  TY_URL="https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${{TA}}"
  if command -v curl >/dev/null 2>&1; then
    timeout 25 curl -fsSL --connect-timeout 8 "$TY_URL" -o /tmp/ttyd.bin && {{
      chmod +x /tmp/ttyd.bin
      mv -f /tmp/ttyd.bin /usr/local/bin/ttyd 2>/dev/null || cp -f /tmp/ttyd.bin /usr/local/bin/ttyd
    }}
  fi
  command -v ttyd >/dev/null 2>&1 && return 0
  if command -v apt-get >/dev/null 2>&1; then
    timeout 30 apt-get install -y -qq ttyd >/tmp/vex-ttyd-apt.log 2>&1 || true
  fi
  command -v ttyd >/dev/null 2>&1
}}

if ! install_ttyd; then
  echo TTYD_INSTALL_FAIL
  tail -n 20 /tmp/vex-ttyd-apt.log 2>/dev/null || true
  exit 2
fi
echo HAVE_TTYD

# basic-auth: vex / TOKEN (URL includes token for the tunnel query if needed)
setsid ttyd -p "$PORT" -W -i 127.0.0.1 -c "vex:$TOKEN" --once /bin/sh </dev/null >/tmp/vex-ttyd.log 2>&1 &
echo $! > /tmp/vex-ttyd.pid
sleep 1
if ! kill -0 "$(cat /tmp/vex-ttyd.pid)" 2>/dev/null; then
  echo TTYD_START_FAIL
  cat /tmp/vex-ttyd.log 2>/dev/null || true
  exit 4
fi

command -v ssh >/dev/null 2>&1 || {{
  command -v apt-get >/dev/null 2>&1 && apt-get install -y -qq openssh-client >/dev/null 2>&1 || true
}}
printf '#!/bin/sh\\necho\\n' > /tmp/vex-ttyd-askpass.sh
chmod +x /tmp/vex-ttyd-askpass.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/vex-ttyd-askpass.sh SSH_ASKPASS_REQUIRE=force
extract_url() {{
  U=$(grep -Eio 'https://[A-Za-z0-9._-]+\\.(localhost\\.run|lhr\\.life|lhrtunnel\\.link|lhr\\.rocks|lhr\\.link)[A-Za-z0-9._/-]*' /tmp/vex-ttyd-tunnel.log 2>/dev/null | head -n1)
  if [ -z "$U" ]; then
    H=$(grep -Eio '[A-Za-z0-9._-]+\\.(localhost\\.run|lhr\\.life|lhrtunnel\\.link|lhr\\.rocks|lhr\\.link)' /tmp/vex-ttyd-tunnel.log 2>/dev/null | head -n1)
    if [ -n "$H" ]; then U="https://$H"; fi
  fi
  echo "$U"
}}
# Without a remote session, localhost.run never prints its URL.
# stdin is held open by tail so the tunnel does not die on EOF.
pkill -f 'ssh.*nokey@localhost\\.run' >/dev/null 2>&1 || true
rm -f /tmp/vex-ttyd-tunnel.log /tmp/vex-ttyd-tunnel.pid /tmp/vex-ttyd-tunnel.fifo /tmp/vex-ttyd-tunnel-keeper.pid
setsid sh -c 'tail -f /dev/null | ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -o NumberOfPasswordPrompts=1 -o TCPKeepAlive=yes -R 80:127.0.0.1:'"$PORT"' nokey@localhost.run > /tmp/vex-ttyd-tunnel.log 2>&1' &
sleep 0.5
TUN_PID=$(pgrep -f 'ssh.*nokey@localhost\\.run' 2>/dev/null | head -n1)
if [ -n "$TUN_PID" ]; then echo "$TUN_PID" > /tmp/vex-ttyd-tunnel.pid; fi
URL=""
for i in $(seq 1 30); do
  URL=$(extract_url)
  if [ -n "$URL" ]; then break; fi
  TUN_PID=$(pgrep -f 'ssh.*nokey@localhost\\.run' 2>/dev/null | head -n1)
  if [ -n "$TUN_PID" ]; then echo "$TUN_PID" > /tmp/vex-ttyd-tunnel.pid; fi
  if [ -z "$TUN_PID" ] && [ "$i" -gt 3 ]; then break; fi
  sleep 1
done
echo "TOKEN=$TOKEN"
echo "PORT=$PORT"
echo "URL=${{URL:-}}"
if [ -z "$URL" ]; then
  echo WEB_TUNNEL_FAIL
  echo '--- tunnel log ---'
  cat /tmp/vex-ttyd-tunnel.log 2>/dev/null || true
  echo '--- net diag ---'
  getent ahosts localhost.run 2>/dev/null | head -n3 || echo NO_DNS
  ip -4 route show default 2>/dev/null | head -n1 || echo NO_IPV4_ROUTE
  ip -6 route show default 2>/dev/null | head -n1 || echo NO_IPV6_ROUTE
  timeout 5 bash -c 'exec 3<>/dev/tcp/localhost.run/22' && echo LR22_OK || echo LR22_FAIL
  curl -sS -m 6 -o /dev/null -w 'LR_HTTP %{{http_code}}\n' https://localhost.run || echo LR_HTTP_FAIL
  exit 5
fi
sleep 2
if [ ! -s /tmp/vex-ttyd-tunnel.pid ] || ! kill -0 "$(cat /tmp/vex-ttyd-tunnel.pid 2>/dev/null)" 2>/dev/null; then
  TUN_PID=$(pgrep -f 'ssh.*nokey@localhost\\.run' 2>/dev/null | head -n1)
  if [ -n "$TUN_PID" ]; then echo "$TUN_PID" > /tmp/vex-ttyd-tunnel.pid; fi
fi
if [ ! -s /tmp/vex-ttyd-tunnel.pid ] || ! kill -0 "$(cat /tmp/vex-ttyd-tunnel.pid 2>/dev/null)" 2>/dev/null; then
  echo WEB_TUNNEL_DIED
  cat /tmp/vex-ttyd-tunnel.log 2>/dev/null || true
  exit 7
fi
echo WEB_OK
"""
        try:
            code, out = self._run_script(container_id, script, timeout=timeout)
        except Exception as exc:
            raise ProviderError(f"web terminal exec failed: {exc}") from exc
        out = self._strip_ansi(out or "")
        url = self._extract_tunnel_url(out)
        if not url:
            _, out2 = self.exec_command(
                container_id,
                "cat /tmp/vex-ttyd-tunnel.log 2>/dev/null || true",
                timeout=15,
            )
            url = self._extract_tunnel_url(out2 or "")
        url = (url or "").strip()
        if not url or "WEB_OK" not in out:
            tail = out[-600:].strip()
            raise ProviderError(
                f"web terminal tunnel did not come up (exit {code}): {tail}"
            )
        # ttyd basic auth — open URL, browser prompts for user vex / token
        return {"url": url, "token": token, "port": port, "username": "vex"}

    def stop_web_terminal(self, container_id: str) -> None:
        try:
            self.exec_command(
                container_id,
                "if [ -f /tmp/vex-ttyd.pid ]; then kill \"$(cat /tmp/vex-ttyd.pid)\" >/dev/null 2>&1 || true; rm -f /tmp/vex-ttyd.pid; fi; "
                "if [ -f /tmp/vex-ttyd-tunnel.pid ]; then kill \"$(cat /tmp/vex-ttyd-tunnel.pid)\" >/dev/null 2>&1 || true; rm -f /tmp/vex-ttyd-tunnel.pid; fi; "
                "pkill -f 'nokey@localhost\\.run' >/dev/null 2>&1 || true; "
                "rm -f /tmp/vex-ttyd-tunnel.fifo /tmp/vex-ttyd-tunnel-keeper.pid; "
                "pkill -x ttyd >/dev/null 2>&1 || true; echo WEB_STOPPED",
                timeout=20,
            )
        except Exception as exc:
            raise ProviderError(f"web terminal stop failed: {exc}") from exc

    @staticmethod
    def _extract_tunnel_url(text: str) -> str:
        import re

        text = LXDProvider._strip_ansi(text)
        for pat in (
            r"https://[A-Za-z0-9._-]+\.localhost\.run[A-Za-z0-9._/-]*",
            r"https://[A-Za-z0-9._-]+\.lhr\.(?:life|link|rocks)[A-Za-z0-9._/-]*",
            r"https://[A-Za-z0-9._-]+\.lhrtunnel\.link[A-Za-z0-9._/-]*",
            r"[A-Za-z0-9._-]+\.localhost\.run",
            r"[A-Za-z0-9._-]+\.lhr\.(?:life|link|rocks)",
            r"[A-Za-z0-9._-]+\.lhrtunnel\.link",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                url = m.group(0).strip().rstrip(".,);'\"")
                if not url.lower().startswith("http"):
                    url = "https://" + url
                return url
        return ""

    def start_file_manager(
        self, container_id: str, timeout: int = 75, brand_name: str | None = None
    ) -> dict:
        """Start the in-VPS file manager on localhost and expose it via localhost.run."""
        from filemanager import build_start_script

        token = secrets.token_urlsafe(16)
        port = 8765
        script = build_start_script(token, port, brand=brand_name or "Astra Host")
        try:
            code, out = self._run_script(container_id, script, timeout=timeout)
        except Exception as exc:
            raise ProviderError(f"file manager exec failed: {exc}") from exc
        out = self._strip_ansi(out or "")
        url = self._extract_tunnel_url(out)
        if not url:
            code2, out2 = self.exec_command(
                container_id,
                "cat /tmp/vex-tunnel.log 2>/dev/null || true",
                timeout=15,
            )
            url = self._extract_tunnel_url(out2 or "")
            del code2
        url = (url or "").strip()
        if not url or "FM_OK" not in out:
            tail = out[-600:].strip()
            raise ProviderError(
                f"localhost.run tunnel did not come up (exit {code}): {tail}"
            )
        if "?" in url:
            full_url = f"{url}&token={token}"
        else:
            full_url = f"{url}?token={token}"
        return {"url": full_url, "token": token, "port": port}

    def stop_file_manager(self, container_id: str) -> None:
        from filemanager import build_stop_script

        try:
            self._run_script(container_id, build_stop_script(), timeout=20)
        except Exception as exc:
            raise ProviderError(f"file manager stop failed: {exc}") from exc

    def _cpu_sample(self, name: str) -> tuple[float | None, float]:
        t = time.time()
        code, out = self._run(
            ["query", f"/1.0/instances/{name}/state"], check=False, timeout=30
        )
        if code != 0:
            return None, t
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            return None, t
        usage = float((data.get("cpu") or {}).get("usage") or 0)
        return usage, t

    def stats(self, container_id: str) -> dict:
        inst = self._instance_json(container_id)
        if not inst:
            raise ProviderError("Instance not found")
        status_raw = str(inst.get("status") or "").lower()
        status = "running" if status_raw in {"running", "ready"} else status_raw or "unknown"
        if status_raw in {"frozen"}:
            status = "stopped"
        state = inst.get("state") or {}
        mem = float((state.get("memory") or {}).get("usage") or 0)
        cpu_percent = 0.0
        if status == "running":
            c1, t1 = self._cpu_sample(container_id)
            time.sleep(0.5)
            c2, t2 = self._cpu_sample(container_id)
            if c1 is not None and c2 is not None and t2 > t1:
                delta_ns = max(0.0, c2 - c1)
                wall_ns = (t2 - t1) * 1_000_000_000
                if wall_ns > 0:
                    cpu_percent = (delta_ns / wall_ns) * 100.0
        started_at = str(inst.get("created_at") or "")
        pid = 0
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return {
            "status": status,
            "cpu_percent": round(cpu_percent, 2),
            "mem_used_mb": round(mem / (1024 * 1024), 2),
            "started_at": started_at,
            "pid": pid,
        }

    def ping(self) -> bool:
        try:
            code, _ = self._run(["version"], check=False, timeout=15)
            return code == 0
        except ProviderError:
            return False
