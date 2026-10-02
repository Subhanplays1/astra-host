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

print("TUNNEL CHECKS PASSED")
