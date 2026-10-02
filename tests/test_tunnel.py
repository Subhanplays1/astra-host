"""Admin panel tunnel: banner filtering, ssh hardening, stored-URL hygiene.

Run from the project root:  python tests/test_tunnel.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("DISCORD_TOKEN", "dummy-token-for-tests")
_WORK = Path(tempfile.mkdtemp(prefix="astra-tunnel-"))
os.environ["DATABASE_PATH"] = str(_WORK / "bot.db")
os.environ["LOG_FILE"] = str(_WORK / "bot.log")
os.environ["TUNNEL_AUTO_INSTALL"] = "0"  # tests must never download binaries

from services.tunnel import (  # noqa: E402
    CommandTunnel,
    TunnelError,
    TunnelManager,
    _harden_ssh,
    _scan_url,
    is_tunnel_url,
)

# ── 1. banner URLs vs real tunnel URLs ───────────────────────
BANNER = "Welcome! Sign up at https://admin.localhost.run/ and read https://docs.localhost.run/setup"
REAL = "https://chatty-quiet-dolphin.localhost.run"

assert not is_tunnel_url("https://admin.localhost.run/")
assert not is_tunnel_url("https://localhost.run")
assert not is_tunnel_url("https://docs.localhost.run/x")
assert is_tunnel_url(REAL)
assert is_tunnel_url("https://random-words-here.trycloudflare.com")
assert is_tunnel_url("https://abcd1234.tailnet.ts.net")

assert _scan_url(BANNER) is None, "banner links must never be picked"
assert _scan_url(f"{BANNER}\n{REAL}") == REAL, "real tunnel URL must win"
assert _scan_url("connect via " + REAL + ".") == REAL
print("scan/filter ok")

# ── 2. ssh options must precede the host ─────────────────────
hardened = _harden_ssh("ssh -R 80:localhost:8080 localhost.run")
host_at = hardened.rfind("localhost.run")
opts_at = hardened.find("-o StrictHostKeyChecking=no")
assert 0 < opts_at < host_at, hardened
assert "-R 80:localhost:8080" in hardened
assert _harden_ssh("ssh -o StrictHostKeyChecking=yes -R 80:{port} h").count(
    "StrictHostKeyChecking"
) == 1, "user options are respected"
assert _harden_ssh("cloudflared tunnel --url http://127.0.0.1:{port}") == (
    "cloudflared tunnel --url http://127.0.0.1:{port}"
), "non-ssh commands are untouched"
print("ssh hardening ok")

# ── 3. live scrape: banner first, real URL second ────────────
fake = "echo https://admin.localhost.run/ && echo " + REAL
tunnel = CommandTunnel("fake", fake)
result = tunnel.start(8080, timeout=10)
assert result.url == REAL, result.url
tunnel.stop()

# only a banner URL -> must fail loudly instead of advertising it
try:
    CommandTunnel("fake", "echo https://admin.localhost.run/").start(8080, timeout=1)
except TunnelError as exc:
    assert "admin.localhost.run" in str(exc)
else:
    raise AssertionError("banner-only output should raise TunnelError")
print("live scrape ok")

# ── 4. static TUNNEL_URL_PATTERN is never filtered ───────────
# CustomTunnel passes filter_output=False for TUNNEL_URL_PATTERN, so an
# operator-chosen URL (even an admin.localhost.run CNAME) still works.
static = CommandTunnel("custom", "echo https://admin.localhost.run/", filter_output=False)
r = static.start(8080, timeout=5)
assert r.url == "https://admin.localhost.run/", r.url
static.stop()
print("static pattern ok")

# ── 5. a stale banner URL stored in the DB is never served ───
import config  # noqa: E402
from database import Database  # noqa: E402

db = Database(config.DATABASE_PATH)
db.set_setting("admin_tunnel_url", "https://admin.localhost.run/")
mgr = TunnelManager(db)
assert mgr.url == "", f"stale banner URL leaked: {mgr.url!r}"
db.set_setting("admin_tunnel_url", REAL)
assert mgr.url == REAL, mgr.url
db.close()
print("stored url hygiene ok")

# ── 6. ssh destination must carry an explicit anonymous user ─
from services.tunnel import _anonymous_localhost_run, _asks_for_password  # noqa: E402

assert _anonymous_localhost_run("ssh -R 80:localhost:8080 localhost.run") == (
    "ssh -R 80:localhost:8080 nokey@localhost.run"
), "bare localhost.run would log in as root and ask for a password"
assert _anonymous_localhost_run("ssh -R 80:{port} nokey@localhost.run").count("nokey@") == 1
assert _anonymous_localhost_run("ssh -R 80:{port} me@localhost.run") == (
    "ssh -R 80:{port} me@localhost.run"
), "an operator-provided account must be left alone"
assert _anonymous_localhost_run("ssh -R 80:{port} ssh.localhost.run").endswith(
    "nokey@ssh.localhost.run"
)
assert _anonymous_localhost_run("cloudflared tunnel --url http://127.0.0.1:{port}") == (
    "cloudflared tunnel --url http://127.0.0.1:{port}"
), "non-ssh commands are untouched"
hardened = _harden_ssh(_anonymous_localhost_run("ssh -R 80:localhost:8080 localhost.run"))
assert "-o StrictHostKeyChecking=no" in hardened
assert hardened.index("-o StrictHostKeyChecking=no") < hardened.index("nokey@localhost.run")
assert ":8080 nokey@localhost.run" in hardened

assert _asks_for_password("root@localhost.run's password: ")
assert _asks_for_password("Prefix: x\nPassword:")
assert not _asks_for_password("https://localhost.run/docs/")
assert not _asks_for_password(REAL)
print("anonymous user ok")

# ── 7. a password prompt fails fast with a usable hint ───────
try:
    CommandTunnel("fake", "echo root@localhost.run password:").start(8080, timeout=20)
except TunnelError as exc:
    assert "nokey@localhost.run" in str(exc), exc
else:
    raise AssertionError("password prompt should raise TunnelError")
print("password prompt ok")

# ── 8. real localhost.run command built by the provider ──────
from services.tunnel import LocalhostRunTunnel  # noqa: E402

lt = LocalhostRunTunnel()
assert "nokey@localhost.run" in lt.command, lt.command
assert "StrictHostKeyChecking=no" in lt.command
print("provider command ok")

# ── 9. provider registry: pinggy (ssh-only, no install) ──────
from services.tunnel import get_tunnel_provider  # noqa: E402

pinggy = get_tunnel_provider("pinggy")
cmd = pinggy.command
assert "free.pinggy.io" in cmd and "{port}" in cmd, cmd
assert "-p 443" in cmd and "-R0:localhost:{port}" in cmd, cmd
assert cmd.startswith("ssh "), cmd
assert cmd.index("-o StrictHostKeyChecking=no") < cmd.index("free.pinggy.io"), cmd
assert get_tunnel_provider("cloudflare").name == "cloudflare"
assert get_tunnel_provider("").name == "local"

# pinggy-style banner: https first, plain http ignored by the scanner
fake_pinggy = CommandTunnel("pinggy", "echo URL: https://abc123.pinggy.io")
r = fake_pinggy.start(8080, timeout=5)
assert r.url == "https://abc123.pinggy.io", r.url
fake_pinggy.stop()
print("provider registry ok")

# ── 10. cloudflared resolution never downloads while building ─
from services.tunnel import cloudflared_bin  # noqa: E402

cf = get_tunnel_provider("cloudflare")
assert "cloudflared tunnel --url" in cf.command, cf.command
assert "{port}" in cf.command
assert cf.name == "cloudflare"
# TUNNEL_AUTO_INSTALL=0 (set above) -> either a real binary or a clear error,
# never a silent download during tests.
try:
    resolved = cloudflared_bin()
    assert Path(resolved).exists(), resolved
except TunnelError as exc:
    assert "cloudflared" in str(exc), exc
assert not (ROOT / ".bin").exists(), "tests must not download binaries"
print("cloudflared resolution ok")

print("TUNNEL CHECKS PASSED")
