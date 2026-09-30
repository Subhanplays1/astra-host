"""Smoke test for the Astra Host admin panel.

Run from the project root:  python tests/test_panel_smoke.py
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
_WORK = Path(tempfile.mkdtemp(prefix="astra-tests-"))
os.environ["DATABASE_PATH"] = str(_WORK / "bot.db")
os.environ["LOG_FILE"] = str(_WORK / "bot.log")
os.environ["ADMIN_PANEL_ENABLED"] = "1"

import config  # noqa: E402
from database import Database  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="astra-panel-"))
db = Database(str(tmp / "test.db"))
print("db ready")

from admin.routes import create_app  # noqa: E402
from services.tunnel import TunnelManager  # noqa: E402
from services.vps_service import VPSService  # noqa: E402

ADMIN_ID = str(next(iter(config.ADMIN_IDS)))


class BrandingStub:
    def brand_label(self) -> str:
        return config.BRAND_NAME

    def embed(self, *, title, description=""):
        raise AssertionError("panel must not build discord embeds")


class BotStub:
    def __init__(self):
        self.db = db
        self.provider = None
        self.astra = None
        self.branding = BrandingStub()
        self.vps_service = VPSService(db, None)
        self.tunnel = TunnelManager(db)

    def get_user(self, *a, **k):
        return None

    guilds = []


bot = BotStub()
app = create_app(bot)
client = app.test_client()

# health (no auth)
r = client.get("/healthz")
assert r.status_code == 200 and r.get_json()["ok"] is True, r.status_code

# unauthenticated pages redirect to login
r = client.get("/")
assert r.status_code == 302 and "/login" in r.headers["Location"], (r.status_code, r.headers)
r = client.get("/api/vps")
assert r.status_code == 401, r.status_code

# bad code -> 401 + no cookie
r = client.post("/login", data={"code": "nope"})
assert r.status_code == 401, r.status_code
assert "astra_session" not in r.headers.get("Set-Cookie", "")

# real code -> session cookie
code = db.create_admin_login_token(ADMIN_ID, 300)
r = client.post("/login", data={"code": code}, follow_redirects=False)
assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:300])
set_cookie = r.headers.get("Set-Cookie", "")
assert "astra_session=" in set_cookie, set_cookie
auth_cookie_name = "astra_session"

# code is single use (clear the session cookie first)
client.delete_cookie(auth_cookie_name)
r = client.post("/login", data={"code": code})
assert r.status_code == 401, r.status_code

# authed dashboard renders (sign in again with a fresh code)
code2 = db.create_admin_login_token(ADMIN_ID, 300)
r = client.post("/login", data={"code": code2}, follow_redirects=False)
assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:300])
r = client.get("/")
assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:400])
html = r.get_data(as_text=True)
assert "Instances" in html and config.BRAND_NAME in html, "dashboard content missing"

# CSRF: POST without token rejected
r = client.post("/settings", data={"vps_enabled": "on"})
assert r.status_code == 400, r.status_code

# grab csrf from dashboard HTML
import re  # noqa: E402

m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
assert m, "csrf token not rendered"
csrf = m.group(1)

# CSRF: bad token rejected
r = client.post("/settings", data={"csrf_token": "deadbeef"})
assert r.status_code == 400, r.status_code

# CSRF: good token accepted
r = client.post(
    "/settings",
    data={"csrf_token": csrf, "required_invites": "7"},
    follow_redirects=True,
)
assert r.status_code == 200, r.status_code
assert str(db.get_setting("required_invites", "5")) == "7", db.get_setting("required_invites")

# instances + logs pages
assert client.get("/vps").status_code == 200
assert client.get("/logs").status_code == 200
assert client.get("/api/overview").status_code == 200

# unknown lifecycle action -> 404
r = client.post("/vps/vps_deadbeef/nuke", data={"csrf_token": csrf})
assert r.status_code == 404, r.status_code

# service action without provider -> clean flash, no 500
r = client.post(
    "/vps/vps_deadbeef/start",
    data={"csrf_token": csrf},
    follow_redirects=True,
)
assert r.status_code == 200, r.status_code
actions = [str(a["action"]) for a in db.recent_admin_actions(5)]
assert "start" in actions, actions

# security headers
r = client.get("/login")
for h in ("X-Frame-Options", "X-Content-Type-Options", "Cache-Control", "Content-Security-Policy"):
    assert h in r.headers, h

# logout clears session
r = client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
assert r.status_code == 302, r.status_code
assert client.get("/").status_code == 302

db.close()
print("ALL PANEL CHECKS PASSED")


# ── 2. real HTTP server + tunnel lifecycle (uses the real bot object) ──
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import bot as botmod  # noqa: E402
from admin.panel import start_admin_panel, stop_admin_panel  # noqa: E402

real_bot = botmod.bot
panel = start_admin_panel(real_bot)
assert panel is not None, "panel failed to start"
base = f"http://{config.ADMIN_PANEL_HOST}:{config.ADMIN_PANEL_PORT}"

with urllib.request.urlopen(base + "/healthz", timeout=5) as resp:
    assert resp.status == 200
    assert b'"ok"' in resp.read()

with urllib.request.urlopen(base + "/", timeout=5) as resp:
    body = resp.read().decode("utf-8", "replace")
    assert resp.status == 200 and "One-time code" in body, body[:300]

with urllib.request.urlopen(base + "/login", timeout=5) as resp:
    assert config.BRAND_NAME in resp.read().decode("utf-8", "replace")

stop_admin_panel(real_bot)
assert real_bot._admin_panel is None
try:
    urllib.request.urlopen(base + "/healthz", timeout=3)
    raise AssertionError("panel still serving after stop")
except (urllib.error.URLError, ConnectionError, OSError):
    pass

print("SERVER LIFECYCLE OK")

