"""Smoke test for the Astra status engine, MOTD/issue scripts and FM launcher.

Run from the project root:  python tests/test_astra_smoke.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("DISCORD_TOKEN", "dummy-token-for-tests")
_WORK = Path(tempfile.mkdtemp(prefix="astra-status-"))
os.environ["DATABASE_PATH"] = str(_WORK / "bot.db")
os.environ["LOG_FILE"] = str(_WORK / "bot.log")

import config  # noqa: E402

# ── status engine ─────────────────────────────────────────────
from astra_status import AstraStatusEngine  # noqa: E402
from database import Database  # noqa: E402

db = Database(config.DATABASE_PATH)
engine = AstraStatusEngine(db, None, started_at=__import__("time").time())
report = engine.report()
assert report["state"], report
assert report["message"], report
assert "vps_total" in report["snapshot"]
assert engine.presence(), "empty presence line"
assert engine.line()[0]
assert engine.reassurance()
assert AstraStatusEngine.uptime_text(90061) == "1d 1h 1m"
assert engine.transition(report["state"]) in (True, False)
print("engine:", report["state"], "|", report["message"])

# ── bot wiring (panels, service, branding) ───────────────────
import bot as botmod  # noqa: E402

b = botmod.bot
panel_txt = botmod._admin_system_panel()
assert "ADMIN CONTROL CENTER" in panel_txt, panel_txt
assert b.vps_service.counts()["total"] == 0
assert b.branding.brand_label().startswith(config.BRAND_NAME)
assert b.branding.active().get("brand_name") == config.BRAND_NAME

from services.monitoring import dashboard_stats, host_metrics  # noqa: E402

stats = dashboard_stats(b.db, None, astra=b.astra)
assert stats["vps"]["total"] == 0 and "host" in stats and "panel" not in stats
host = host_metrics()
assert "cpu_percent" in host and "disk_percent" in host

# ── MOTD + issue banners ─────────────────────────────────────
from motd import build_issue_script, build_motd_script  # noqa: E402

brand = dict(config.DEFAULT_BRAND)
script = build_motd_script(brand)
issue = build_issue_script(brand)

assert script.startswith("#!/bin/bash")
assert "FREE VPS HOSTING" in script
assert "Powered by" not in script
assert "VexDeploy" not in script and "aytro" not in script.lower()
assert brand["brand_name"] in script
assert "Provider" in script and "Host" in script and "Memory" in script
# the issue banner is emitted base64-wrapped inside a shell command
import base64  # noqa: E402
import re  # noqa: E402

m = re.search(r"echo (\S+) \| base64 -d > /etc/issue\b", issue)
assert m, issue
payload = base64.b64decode(m.group(1)).decode("utf-8")
assert "Powered by" not in issue and "Powered by" not in payload
assert "VexDeploy" not in payload
assert brand["brand_name"] in payload
assert "FREE VPS HOSTING" in payload

bash = shutil.which("bash")
if bash:
    for name, text in (("motd", script), ("issue", issue)):
        p = subprocess.run([bash, "-n"], input=text, text=True, capture_output=True, timeout=30)
        assert p.returncode == 0, f"{name} shell syntax: {p.stderr}"
else:
    print("bash not available — skipped shell syntax check")

# ── file manager launcher quoting ────────────────────────────
from filemanager import build_start_script  # noqa: E402

fm = build_start_script("tok-123", 8765, brand="Astra's \"Host\"")
assert "--brand" in fm and "tok-123" in fm
if bash:
    p = subprocess.run([bash, "-n"], input=fm, text=True, capture_output=True, timeout=30)
    assert p.returncode == 0, f"fm shell syntax: {p.stderr}"

db.close()
print("ASTRA SMOKE OK")
