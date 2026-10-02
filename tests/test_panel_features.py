"""Production-feature tests for the Astra Host admin panel.

Users, instance detail, plans, settings and CSV/JSON exports.

Run from the project root:  python tests/test_panel_features.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("DISCORD_TOKEN", "dummy-token-for-tests")
_WORK = Path(tempfile.mkdtemp(prefix="astra-features-"))
os.environ["DATABASE_PATH"] = str(_WORK / "bot.db")
os.environ["LOG_FILE"] = str(_WORK / "bot.log")
os.environ["ADMIN_PANEL_ENABLED"] = "1"
os.environ["ADMIN_PANEL_RATE_LIMIT"] = "500"

import config  # noqa: E402
from database import Database  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="astra-panel-features-"))
db = Database(str(tmp / "features.db"))

from admin.routes import create_app  # noqa: E402
from services.tunnel import TunnelManager  # noqa: E402
from services.vps_service import VPSService  # noqa: E402

ADMIN_ID = str(next(iter(config.ADMIN_IDS)))
OWNER_ID = "424242424242424242"
BANNED_ID = "999888777666555444"


class BrandingStub:
    def brand_label(self) -> str:
        return config.BRAND_NAME


class BotStub:
    def __init__(self):
        self.db = db
        self.provider = None
        self.astra = None
        self.branding = BrandingStub()
        self.vps_service = VPSService(db, None)
        self.tunnel = TunnelManager(db)
        self.started_at = None
        self.guilds = []

    def get_user(self, *a, **k):
        return None

    def is_ready(self):
        return False


app = create_app(BotStub())
app.config["TESTING"] = True
client = app.test_client()

# ── sign in ──────────────────────────────────────────────────
code = db.create_admin_login_token(ADMIN_ID, 300)
r = client.post("/login", data={"code": code}, follow_redirects=False)
assert r.status_code == 302, r.status_code

html = client.get("/").get_data(as_text=True)
csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)

# ── seed data ────────────────────────────────────────────────
db.set_invites(OWNER_ID, 9)
db.set_invites(BANNED_ID, 2)
db.ban_user(BANNED_ID, ADMIN_ID)
vps_id = db.create_vps_record(
    owner_id=OWNER_ID,
    container_id="c-test-1",
    container_name="ast-0001",
    memory_mb=2048,
    cpus=2,
    disk_gb=20,
    os_image="ubuntu-22.04",
    ip_address="10.0.0.5",
    ssh_port=2222,
    username="root",
    password="supersecret-1",
    status="running",
)
db.log_deployment(OWNER_ID, vps_id, "create", "ok", "instance ready")
db.log_deployment(OWNER_ID, vps_id, "create", "failed", "quota exceeded")

# ── users page ───────────────────────────────────────────────
r = client.get("/users")
assert r.status_code == 200, r.status_code
page = r.get_data(as_text=True)
assert OWNER_ID in page and BANNED_ID in page, "seeded users missing"
assert "Invites" in page and "Eligibility" in page
assert OWNER_ID in page and str(db.get_invite_row(OWNER_ID)["valid_invites"]) in page

r = client.get("/users?f=banned")
banned_page = r.get_data(as_text=True)
assert BANNED_ID in banned_page and OWNER_ID not in banned_page, "ban filter broken"

r = client.get("/users?f=with_vps")
with_vps = r.get_data(as_text=True)
assert OWNER_ID in with_vps and BANNED_ID not in with_vps, "vps filter broken"

r = client.get("/users?q=424242")
assert OWNER_ID in r.get_data(as_text=True), "user search broken"

# ── user detail ──────────────────────────────────────────────
r = client.get(f"/users/{OWNER_ID}")
assert r.status_code == 200, r.status_code
detail = r.get_data(as_text=True)
assert vps_id in detail, "user instances missing"
assert "Activity" in detail and "Invites" in detail

# unknown user still renders (no 500)
assert client.get("/users/1234567890").status_code == 200

# ── user actions ─────────────────────────────────────────────
r = client.post(
    f"/users/{OWNER_ID}/invites",
    data={"csrf_token": csrf, "invites": "11"},
    follow_redirects=True,
)
assert r.status_code == 200
assert int(db.get_invite_row(OWNER_ID)["valid_invites"]) == 11

r = client.post(f"/users/{OWNER_ID}/ban", data={"csrf_token": csrf}, follow_redirects=True)
assert r.status_code == 200 and db.is_banned(OWNER_ID)
r = client.post(f"/users/{OWNER_ID}/unban", data={"csrf_token": csrf}, follow_redirects=True)
assert r.status_code == 200 and not db.is_banned(OWNER_ID)

# unknown action -> 404, csrf missing -> 400
assert client.post(f"/users/{OWNER_ID}/nuke", data={"csrf_token": csrf}).status_code == 404
assert client.post(f"/users/{OWNER_ID}/ban", data={}).status_code == 400

# ── instance detail ──────────────────────────────────────────
r = client.get(f"/vps/{vps_id}")
assert r.status_code == 200, r.status_code
page = r.get_data(as_text=True)
assert vps_id in page and "Connection" in page and OWNER_ID in page
assert "supersecret" not in page, "password leaked to the UI"
assert "Deployment history" in page

# live metrics endpoint without a provider -> clean error, not a 500
r = client.get(f"/api/vps/{vps_id}/stats")
assert r.status_code in (404, 503), r.status_code

# password change without a provider -> flash + redirect
r = client.post(
    f"/vps/{vps_id}/password",
    data={"csrf_token": csrf, "password": "newpassword123"},
    follow_redirects=True,
)
assert r.status_code == 200, r.status_code

# missing instance -> redirect with flash (no 500)
r = client.get("/vps/vx_missing")
assert r.status_code in (302, 404), r.status_code

# ── plans ────────────────────────────────────────────────────
assert client.post("/plans", data={}).status_code == 400  # csrf

r = client.post(
    "/plans",
    data={
        "csrf_token": csrf,
        "name": "Production L",
        "memory_mb": "8192",
        "cpus": "4",
        "disk_gb": "80",
        "min_invites": "10",
        "sort_order": "3",
        "badge": "PRO",
        "price": "Free",
        "description": "Big instance",
    },
    follow_redirects=True,
)
assert r.status_code == 200
plan = db.get_plan("Production L")
assert plan is not None, "plan not created"
assert int(plan["memory_mb"]) == 8192
plan_id = str(plan["plan_id"])

page = client.get("/plans").get_data(as_text=True)
assert "Production L" in page and plan_id in page

# invalid plan -> flash, no crash
r = client.post(
    "/plans",
    data={"csrf_token": csrf, "name": "", "memory_mb": "x", "cpus": "1", "disk_gb": "1"},
    follow_redirects=True,
)
assert r.status_code == 200 and db.get_plan("") is None

# toggle
client.post(f"/plans/{plan_id}/toggle", data={"csrf_token": csrf}, follow_redirects=True)
assert int(db.get_plan(plan_id)["enabled"]) == 0
client.post(f"/plans/{plan_id}/toggle", data={"csrf_token": csrf}, follow_redirects=True)
assert int(db.get_plan(plan_id)["enabled"]) == 1

# update
client.post(
    f"/plans/{plan_id}/update",
    data={
        "csrf_token": csrf,
        "name": "Production L",
        "memory_mb": "16384",
        "cpus": "8",
        "disk_gb": "160",
        "min_invites": "12",
        "sort_order": "1",
    },
    follow_redirects=True,
)
assert int(db.get_plan(plan_id)["memory_mb"]) == 16384

# delete needs the typed confirmation
client.post(f"/plans/{plan_id}/delete", data={"csrf_token": csrf}, follow_redirects=True)
assert db.get_plan(plan_id) is not None, "delete without confirmation must not happen"
client.post(
    f"/plans/{plan_id}/delete",
    data={"csrf_token": csrf, "confirm": plan_id},
    follow_redirects=True,
)
assert db.get_plan(plan_id) is None, "delete with confirmation failed"

# ── settings page ────────────────────────────────────────────
r = client.get("/settings")
assert r.status_code == 200
page = r.get_data(as_text=True)
assert "Data exports" in page and "Panel access" in page
assert "max_vps_per_user" in page and "max_total_vps" in page

r = client.post(
    "/settings",
    data={
        "csrf_token": csrf,
        "max_vps_per_user": "7",
        "max_total_vps": "99",
        "required_invites": "6",
        "vps_enabled": "on",
    },
    follow_redirects=True,
)
assert r.status_code == 200
assert str(db.get_setting("max_vps_per_user")) == "7"
assert str(db.get_setting("max_total_vps")) == "99"

# ── exports ──────────────────────────────────────────────────
fixture_plan = db.create_plan(name="Export Fixture", memory_mb=512, cpus=1, disk_gb=5)
def get_csv(path: str):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True)[:200])
    assert "attachment" in r.headers.get("Content-Disposition", ""), r.headers
    text = r.get_data(as_text=True)
    return list(csv.DictReader(io.StringIO(text)))


users_csv = get_csv("/export/users.csv")
assert any(row["user_id"] == OWNER_ID for row in users_csv), "user missing from export"
assert "password_plain" not in users_csv[0], "credentials in user export"

vps_csv = get_csv("/export/vps.csv")
assert any(row["vps_id"] == vps_id for row in vps_csv)
assert "password_plain" not in vps_csv[0] and "password_hash" not in vps_csv[0], vps_csv[0].keys()

audit_csv = get_csv("/export/audit.csv")
assert len(audit_csv) >= 1 and "admin_id" in audit_csv[0], audit_csv[:1]
assert len(get_csv("/export/deployments.csv")) >= 2

plans_csv = get_csv("/export/plans.csv")
assert "name" in plans_csv[0] and "memory_mb" in plans_csv[0], plans_csv[:1]
assert any(row["name"] == "Export Fixture" for row in plans_csv), plans_csv[:3]

r = client.get("/export/users.json")
assert r.status_code == 200
payload = json.loads(r.get_data(as_text=True))
assert any(u["user_id"] == OWNER_ID for u in payload)

r = client.get("/export/backup.json")
assert r.status_code == 200
backup = json.loads(r.get_data(as_text=True))
assert "vps_instances" in backup and "_meta" in backup
for row in backup["vps_instances"]:
    assert "password_plain" not in row or not row.get("password_plain"), "backup leaks credentials"

# backup is JSON only
r = client.get("/export/backup.csv")
assert r.status_code in (302, 400), r.status_code

# unknown kind / format
assert client.get("/export/nope.csv").status_code == 404
assert client.get("/export/users.xml").status_code == 404

# audit trail recorded for every export
actions = [str(a["action"]) for a in db.recent_admin_actions(50)]
for kind in ("users", "vps", "backup", "audit", "deployments", "plans"):
    assert f"export_{kind}" in actions, (kind, actions)

# ── JSON API ─────────────────────────────────────────────────
r = client.get("/api/users")
assert r.status_code == 200
body = r.get_json()
assert body["ok"] is True and any(u["user_id"] == OWNER_ID for u in body["users"])

# ── auth walls on every new page ─────────────────────────────
client.delete_cookie("astra_session")
for path in ("/users", f"/users/{OWNER_ID}", "/plans", "/settings", "/export/users.csv"):
    r = client.get(path)
    assert r.status_code == 302 and "/login" in r.headers["Location"], (path, r.status_code)

# static assets + security headers
r = client.get("/static/app.js")
assert r.status_code == 200 and b"data-filter-input" in r.get_data()
r = client.get("/static/favicon.svg")
assert r.status_code == 200 and b"<svg" in r.get_data(), "favicon missing"
r = client.get("/login")
for h in ("X-Frame-Options", "X-Content-Type-Options", "Content-Security-Policy"):
    assert h in r.headers, h
csp = r.headers["Content-Security-Policy"]
for directive in ("script-src 'self'", "connect-src 'self'", "img-src 'self' data:", "object-src 'none'"):
    assert directive in csp, (directive, csp)
assert "favicon.svg" in r.get_data(as_text=True), "favicon link missing from base template"

db.close()
print("ALL PANEL FEATURE CHECKS PASSED")
