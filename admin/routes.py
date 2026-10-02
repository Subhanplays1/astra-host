"""HTTP routes for the Astra Host admin panel."""

from __future__ import annotations

import csv
import functools
import io
import json
import logging
import platform
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

import config
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from admin import auth
from services.monitoring import dashboard_stats, recent_activity
from services.vps_service import ServiceError

log = logging.getLogger("astra.admin.routes")

LIFECYCLE_ACTIONS = {
    "start",
    "stop",
    "restart",
    "suspend",
    "unsuspend",
    "emergency_stop",
    "remove",
}

EXPORT_KINDS = {"users", "vps", "plans", "audit", "deployments", "backup"}
SECRET_FIELDS = {"password_plain", "password_hash", "token", "secret", "csrf_token"}
USER_FILTERS = {"all", "banned", "eligible", "with_vps", "admins", "empty"}
PAGE_LIMIT = 500


# ── plumbing ──────────────────────────────────────────────────
def _bot() -> Any:
    return current_app.extensions["astra_bot"]


def _login_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        bot = _bot()
        row = auth.session_row(bot, request)
        if row is None:
            if request.path.startswith("/api/") or request.path == "/healthz":
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        g.session = row
        g.admin_id = str(row["admin_id"])
        g.csrf = auth.csrf_token(bot.db, row["session_id"])
        return view(*args, **kwargs)

    return wrapped


def _csrf_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        session = getattr(g, "session", None)
        sid = str(session["session_id"]) if session is not None else ""
        token = request.form.get(auth.CSRF_FIELD) or request.headers.get("X-CSRF-Token")
        if not sid or not auth.csrf_ok(_bot().db, sid, token):
            return jsonify({"ok": False, "error": "bad csrf"}), 400
        return view(*args, **kwargs)

    return wrapped


def _rate_limited() -> bool:
    """Per-IP gate for anything that mutates state."""
    return not auth.limiter.allow(auth.client_ip(request))


def _security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # Explicit directives: browsers warn when everything falls back to
    # default-src, and browser extensions then blame our policy for their own
    # eval() attempts.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'",
    )
    return response


def _audit(action: str, target: str = "", result: str = "ok", detail: str = "") -> None:
    bot = _bot()
    bot.db.log_admin_action(getattr(g, "admin_id", "unknown"), action, target, result, detail, "panel")


# ── presentation helpers ──────────────────────────────────────
def _display_name(bot: Any, user_id: Any) -> str:
    """Cached Discord username for a snowflake, or '' when unknown."""
    try:
        user = bot.get_user(int(user_id))
    except Exception:  # noqa: BLE001
        return ""
    if user is None:
        return ""
    name = str(getattr(user, "name", "") or "")
    disc = str(getattr(user, "discriminator", "0") or "0")
    if disc and disc != "0":
        return f"{name}#{disc}"
    return name


def _scrub(row: Any) -> dict:
    """Copy a row without credentials — never exported, never rendered."""
    data = dict(row) if not isinstance(row, dict) else dict(row)
    for key in list(data):
        if key.lower() in SECRET_FIELDS:
            data.pop(key, None)
    return data


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _csv_response(rows: list[dict], filename: str) -> Response:
    if not rows:
        rows = [{"note": "no rows"}]
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})
    resp = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


def _json_response(payload: Any, filename: str) -> Response:
    resp = Response(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False),
        mimetype="application/json; charset=utf-8",
    )
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


def _export_payload(bot: Any, kind: str) -> Any:
    """Rows for an export. Credentials are always stripped."""
    db = bot.db
    if kind == "users":
        rows = [_scrub(r) for r in db.list_user_reports()]
        for row in rows:
            row["username"] = _display_name(bot, row.get("user_id", ""))
        return rows
    if kind == "vps":
        return [_scrub(r) for r in db.export_backup().get("vps_instances", [])]
    if kind == "plans":
        return [_scrub(r) for r in db.list_plans(enabled_only=False)]
    if kind == "audit":
        return [_scrub(r) for r in db.export_backup().get("admin_activity", [])]
    if kind == "deployments":
        return [_scrub(r) for r in db.export_backup().get("deployment_logs", [])]
    if kind == "backup":
        # Full table dump for restore — but the panel download never carries
        # stored instance passwords (Discord `/backup_data` keeps full fidelity).
        data = db.export_backup()
        for rows in data.values():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        row.pop("password_plain", None)
        return data
    abort(404)


def _plan_form(form: Any) -> dict:
    """Validate the plan create/edit form. Raises ValueError with a message."""
    name = str(form.get("name", "")).strip()[:40]
    if not name:
        raise ValueError("Plan name is required.")
    out: dict[str, Any] = {"name": name}
    for field, lo, hi in (
        ("memory_mb", 64, 65536),
        ("cpus", 1, 64),
        ("disk_gb", 1, 4096),
        ("min_invites", 0, 1000),
        ("sort_order", 0, 9999),
    ):
        raw = str(form.get(field, "") or "").strip()
        if not raw:
            raise ValueError(f"{field} is required.")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{field} must be a number.") from exc
        if not lo <= value <= hi:
            raise ValueError(f"{field} must be between {lo} and {hi}.")
        out[field] = value
    out["badge"] = str(form.get("badge", "") or "").strip()[:24]
    out["description"] = str(form.get("description", "") or "").strip()[:200]
    out["price"] = str(form.get("price", "") or "").strip()[:24]
    return out


def _blank_user(user_id: str) -> dict:
    return {
        "user_id": str(user_id),
        "username": "",
        "valid_invites": 0,
        "fake_invites": 0,
        "eligible": 0,
        "completion_notified": 0,
        "invites_updated_at": "",
        "vps_total": 0,
        "vps_running": 0,
        "vps_suspended": 0,
        "first_vps_at": "",
        "last_seen": "",
        "banned": 0,
        "banned_at": "",
        "banned_by": "",
        "is_admin": 0,
    }


# ── app factory ───────────────────────────────────────────────
def create_app(bot: Any) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=auth._secret(bot.db),  # noqa: SLF001 — shared signing material
        SESSION_COOKIE_NAME="astra_panel",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        JSON_SORT_KEYS=True,
        ASTRA_BOT=bot,
    )
    app.extensions["astra_bot"] = bot
    app.after_request(_security_headers)

    @app.context_processor
    def inject_globals() -> dict:
        try:
            brand = bot.branding.brand_label()
        except Exception:  # noqa: BLE001
            brand = config.BRAND_NAME
        try:
            maintenance = str(bot.db.get_setting("maintenance_mode", "0")) in {"1", "true"}
        except Exception:  # noqa: BLE001
            maintenance = False
        nav_counts: dict[str, Any] = {"vps": None, "users": None}
        try:
            nav_counts = {"vps": bot.db.count_vps(), "users": bot.db.count_users()}
        except Exception:  # noqa: BLE001
            pass
        panel_tunnel = ""
        try:
            url = str(bot.tunnel.url or "")
            panel_tunnel = url.split("//", 1)[-1].split("/", 1)[0]
        except Exception:  # noqa: BLE001
            panel_tunnel = ""
        return {
            "brand_name": brand,
            "brand_tagline": config.BRAND_TAGLINE,
            "csrf_token": getattr(g, "csrf", ""),
            "admin_id": getattr(g, "admin_id", ""),
            "maintenance_mode": maintenance,
            "nav_counts": nav_counts,
            "panel_tunnel": panel_tunnel,
        }

    # ── health (no auth, reveals nothing) ────────────────────
    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "astra-admin"})

    # ── auth ─────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        bot = _bot()
        if auth.session_row(bot, request) is not None:
            return redirect(url_for("index"))
        if request.method == "GET":
            return render_template("login.html", error="")

        ip = auth.client_ip(request)
        if not auth.limiter.allow(ip, max(3, config.ADMIN_PANEL_RATE_LIMIT // 4)):
            _audit_local(bot, "panel_login", "", "rate_limited", ip)
            return render_template(
                "login.html", error="Too many attempts — try again in a few minutes."
            ), 429

        ok, msg, session_id = auth.login_with_code(
            bot,
            request.form.get("code", ""),
            ip,
            str(request.user_agent)[:200],
        )
        if not ok:
            return render_template("login.html", error=msg), 401
        resp = redirect(url_for("index"))
        resp.set_cookie(
            auth.SESSION_COOKIE,
            session_id or "",
            httponly=True,
            samesite="Strict",
            secure=request.is_secure,
            max_age=config.ADMIN_PANEL_SESSION_HOURS * 3600,
        )
        return resp

    @app.post("/logout")
    @_login_required
    def logout():
        bot = _bot()
        _audit("panel_logout")
        auth.destroy_session(bot, request)
        resp = redirect(url_for("login"))
        resp.delete_cookie(auth.SESSION_COOKIE)
        return resp

    # ── dashboard ────────────────────────────────────────────
    @app.get("/")
    @_login_required
    def index():
        bot = _bot()
        stats = _stats(bot)
        return render_template(
            "dashboard.html",
            stats=stats,
            panel_url=bot.tunnel.url or f"http://{config.ADMIN_PANEL_HOST}:{config.ADMIN_PANEL_PORT}",
        )

    # ── instances ────────────────────────────────────────────
    @app.get("/vps")
    @_login_required
    def vps_list():
        bot = _bot()
        rows = [dict(r) for r in bot.vps_service.list_all()]
        q = (request.args.get("q") or "").strip().lower()
        if q:
            rows = [
                r
                for r in rows
                if q in str(r.get("vps_id", "")).lower()
                or q in str(r.get("container_name", "")).lower()
                or q in str(r.get("owner_id", "")).lower()
                or q in str(r.get("status", "")).lower()
            ]
        return render_template("vps.html", rows=rows, q=q, counts=bot.vps_service.counts())

    @app.get("/vps/<vps_id>")
    @_login_required
    def vps_detail(vps_id: str):
        bot = _bot()
        row = bot.db.get_vps(vps_id)
        if row is None:
            flash(f"No instance with id `{vps_id}`.", "error")
            return redirect(url_for("vps_list"))
        instance = dict(row)
        live: dict[str, Any] = {}
        live_error = ""
        try:
            live = bot.vps_service.stats(vps_id)
            live.pop("vps", None)
        except ServiceError as exc:
            live_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            live_error = str(exc)[:200]
        owner = bot.db.get_user_report(instance.get("owner_id", "")) or _blank_user(
            instance.get("owner_id", "")
        )
        owner["username"] = _display_name(bot, owner["user_id"])
        return render_template(
            "vps_detail.html",
            r=instance,
            live=live,
            live_error=live_error,
            owner=owner,
            siblings=[
                dict(s)
                for s in bot.db.list_user_vps(instance.get("owner_id", ""))
                if str(s["vps_id"]) != str(vps_id)
            ],
            events=[dict(e) for e in bot.db.deployment_logs_for_vps(vps_id, 50)],
        )

    @app.post("/vps/<vps_id>/password")
    @_login_required
    @_csrf_required
    def vps_password(vps_id: str):
        bot = _bot()
        if _rate_limited():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        password = str(request.form.get("password", ""))
        try:
            bot.vps_service.set_password(
                vps_id, password, actor=getattr(g, "admin_id", "unknown"), source="panel"
            )
        except ServiceError as exc:
            _audit("set_password", vps_id, "error", str(exc)[:300])
            flash(str(exc), "error")
        else:
            flash(f"SSH password updated for `{vps_id}`.", "ok")
        return redirect(url_for("vps_detail", vps_id=vps_id))

    @app.post("/vps/<vps_id>/<action>")
    @_login_required
    @_csrf_required
    def vps_action(vps_id: str, action: str):
        bot = _bot()
        if action not in LIFECYCLE_ACTIONS:
            abort(404)
        if _rate_limited():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        if action == "remove":
            if request.form.get("confirm") != vps_id:
                flash("Type the instance id to confirm deletion.", "error")
                return redirect(url_for("vps_list"))
        svc = bot.vps_service
        actor = getattr(g, "admin_id", "unknown")
        try:
            if action == "remove":
                svc.remove(vps_id, actor=actor, source="panel")
            else:
                getattr(svc, action)(vps_id, actor=actor, source="panel")
        except ServiceError as exc:
            _audit(action, vps_id, "error", str(exc)[:300])
            flash(str(exc), "error")
        except Exception as exc:  # noqa: BLE001
            _audit(action, vps_id, "error", str(exc)[:300])
            log.exception("panel action failed")
            flash("Unexpected error — see the audit log.", "error")
        else:
            flash(f"{action.replace('_', ' ')} issued for `{vps_id}`.", "ok")
        if request.form.get("want_json"):
            return jsonify({"ok": True, "action": action, "vps_id": vps_id})
        return redirect(request.referrer or url_for("vps_list"))

    # ── settings ─────────────────────────────────────────────
    @app.post("/settings")
    @_login_required
    @_csrf_required
    def settings():
        bot = _bot()
        if _rate_limited():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        tracked = (
            ("maintenance_mode", "0"),
            ("vps_enabled", "1"),
            ("required_invites", "5"),
            ("max_vps_per_user", "3"),
            ("max_total_vps", "20"),
        )
        before = {k: str(bot.db.get_setting(k, d)) for k, d in tracked}
        bot.db.set_setting("maintenance_mode", "1" if request.form.get("maintenance_mode") else "0")
        bot.db.set_setting("vps_enabled", "0" if request.form.get("vps_enabled") is None else "1")
        for key, lo, hi in (
            ("required_invites", 0, 1000),
            ("max_vps_per_user", 1, 100),
            ("max_total_vps", 1, 10000),
        ):
            raw = str(request.form.get(key) or "").strip()
            if not raw:
                continue
            try:
                bot.db.set_setting(key, max(lo, min(hi, int(raw))))
            except ValueError:
                flash(f"{key.replace('_', ' ')} must be a number.", "error")
        after = {k: str(bot.db.get_setting(k, d)) for k, d in tracked}
        _audit("settings_change", "", "ok", f"{before} -> {after}")
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))

    # ── logs ─────────────────────────────────────────────────
    @app.get("/logs")
    @_login_required
    def logs():
        bot = _bot()
        return render_template(
            "logs.html",
            admin_actions=[dict(r) for r in bot.db.recent_admin_actions(60)],
            events=[dict(r) for r in bot.db.recent_logs(60)],
            activity=recent_activity(bot.db),
        )

    # ── users ─────────────────────────────────────────────────
    @app.get("/users")
    @_login_required
    def users_list():
        bot = _bot()
        rows = [dict(r) for r in bot.db.list_user_reports()]
        for r in rows:
            r["username"] = _display_name(bot, r["user_id"])
        counts = {
            "total": len(rows),
            "banned": sum(1 for r in rows if r["banned"]),
            "eligible": sum(1 for r in rows if r["eligible"]),
            "with_vps": sum(1 for r in rows if r["vps_total"]),
            "invites": sum(int(r["valid_invites"] or 0) for r in rows),
        }
        f = (request.args.get("f") or "all").strip().lower()
        if f not in USER_FILTERS:
            f = "all"
        q = (request.args.get("q") or "").strip().lower()
        if q:
            rows = [
                r
                for r in rows
                if q in r["user_id"] or q in str(r.get("username") or "").lower()
            ]
        if f == "banned":
            rows = [r for r in rows if r["banned"]]
        elif f == "eligible":
            rows = [r for r in rows if r["eligible"]]
        elif f == "with_vps":
            rows = [r for r in rows if r["vps_total"]]
        elif f == "admins":
            rows = [r for r in rows if r["is_admin"]]
        elif f == "empty":
            rows = [r for r in rows if not r["vps_total"]]
        total = len(rows)
        shown = rows[:PAGE_LIMIT]
        return render_template(
            "users.html",
            rows=shown,
            q=q,
            f=f,
            counts=counts,
            total=total,
            shown=len(shown),
            needs_invites=_required_invites(bot),
        )

    @app.get("/users/<user_id>")
    @_login_required
    def user_detail(user_id: str):
        bot = _bot()
        report = bot.db.get_user_report(user_id)
        user = dict(report) if report else _blank_user(user_id)
        user["username"] = _display_name(bot, user_id)
        if not user["username"] and report is None:
            flash(f"No tracked activity for `{user_id}` yet.", "error")
        return render_template(
            "user_detail.html",
            u=user,
            instances=[dict(r) for r in bot.db.list_user_vps(user_id)],
            events=[dict(e) for e in bot.db.deployment_logs_for(user_id, 50)],
            needs_invites=_required_invites(bot),
        )

    @app.post("/users/<user_id>/<action>")
    @_login_required
    @_csrf_required
    def user_action(user_id: str, action: str):
        bot = _bot()
        if _rate_limited():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        actor = getattr(g, "admin_id", "unknown")
        if action == "ban":
            bot.db.ban_user(user_id, actor)
            _audit("ban_user", user_id, "ok")
            flash(f"Banned `{user_id}`.", "ok")
        elif action == "unban":
            bot.db.unban_user(user_id)
            _audit("unban_user", user_id, "ok")
            flash(f"Unbanned `{user_id}`.", "ok")
        elif action == "invites":
            raw = str(request.form.get("invites", "") or "").strip()
            try:
                value = max(0, min(10000, int(raw)))
            except ValueError:
                flash("Invites must be a number.", "error")
                return redirect(request.referrer or url_for("users_list"))
            bot.db.set_invites(user_id, value)
            _audit("set_invites", user_id, "ok", f"valid_invites={value}")
            flash(f"Set invites for `{user_id}` to {value}.", "ok")
        else:
            abort(404)
        if request.form.get("want_json"):
            return jsonify({"ok": True, "action": action, "user_id": user_id})
        return redirect(request.referrer or url_for("users_list"))

    # ── plans ─────────────────────────────────────────────────
    @app.get("/plans")
    @_login_required
    def plans_list():
        bot = _bot()
        plans = [dict(p) for p in bot.db.list_plans(enabled_only=False)]
        return render_template("plans.html", plans=plans)

    @app.post("/plans")
    @_login_required
    @_csrf_required
    def plans_create():
        bot = _bot()
        if _rate_limited():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        try:
            fields = _plan_form(request.form)
            plan_id = bot.db.create_plan(**fields)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("plans_list"))
        except Exception as exc:  # noqa: BLE001
            log.exception("plan create failed")
            flash(f"Could not create plan: {exc}", "error")
            return redirect(url_for("plans_list"))
        _audit("plan_create", plan_id, "ok", fields["name"])
        flash(f"Plan `{fields['name']}` created.", "ok")
        return redirect(url_for("plans_list"))

    @app.post("/plans/<plan_id>/<action>")
    @_login_required
    @_csrf_required
    def plan_action(plan_id: str, action: str):
        bot = _bot()
        if _rate_limited():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        if action not in {"update", "toggle", "delete"}:
            abort(404)
        if action == "delete":
            if request.form.get("confirm") != plan_id:
                flash("Type the plan id to confirm deletion.", "error")
                return redirect(url_for("plans_list"))
            ok = bot.db.delete_plan(plan_id)
            _audit("plan_delete", plan_id, "ok" if ok else "error")
            flash("Plan deleted." if ok else "Plan not found.", "ok" if ok else "error")
        elif action == "toggle":
            row = bot.db.get_plan(plan_id)
            if not row:
                flash("Plan not found.", "error")
            else:
                bot.db.update_plan(plan_id, enabled=0 if row["enabled"] else 1)
                _audit("plan_toggle", plan_id, "ok", "enabled" if not row["enabled"] else "disabled")
                flash("Plan availability toggled.", "ok")
        else:
            try:
                fields = _plan_form(request.form)
                ok = bot.db.update_plan(plan_id, **fields)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("plans_list"))
            _audit("plan_update", plan_id, "ok" if ok else "error", fields["name"])
            flash("Plan updated." if ok else "Plan not found.", "ok" if ok else "error")
        return redirect(url_for("plans_list"))

    # ── settings page ─────────────────────────────────────────
    @app.get("/settings")
    @_login_required
    def settings_page():
        bot = _bot()
        return render_template("settings.html", stats=_stats(bot))

    # ── exports (CSV / JSON downloads) ────────────────────────
    @app.get("/export/<kind>.<fmt>")
    @_login_required
    def export(kind: str, fmt: str):
        bot = _bot()
        if kind not in EXPORT_KINDS or fmt not in {"csv", "json"}:
            abort(404)
        ip = auth.client_ip(request)
        if not auth.limiter.allow(ip, config.ADMIN_PANEL_RATE_LIMIT * 3):
            abort(429)
        if kind == "backup" and fmt != "json":
            flash("The full backup export is JSON only.", "error")
            return redirect(url_for("index"))
        payload = _export_payload(bot, kind)
        stamp = _now_stamp()
        _audit(f"export_{kind}", "", "ok", f"format={fmt}")
        if fmt == "json":
            return _json_response(payload, f"astra_{kind}_{stamp}.json")
        return _csv_response(payload, f"astra_{kind}_{stamp}.csv")


    # ── JSON API (same guards) ───────────────────────────────
    @app.get("/api/overview")
    @_login_required
    def api_overview():
        return jsonify(_stats(_bot()))

    @app.get("/api/users")
    @_login_required
    def api_users():
        bot = _bot()
        rows = [dict(r) for r in bot.db.list_user_reports()]
        for r in rows:
            r["username"] = _display_name(bot, r["user_id"])
        return jsonify({"ok": True, "users": rows})

    @app.get("/api/vps/<vps_id>/stats")
    @_login_required
    def api_vps_stats(vps_id: str):
        bot = _bot()
        try:
            data = bot.vps_service.stats(vps_id)
        except ServiceError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)[:200]}), 503
        data.pop("vps", None)
        data["password_plain"] = None
        data.pop("password_hash", None)
        return jsonify({"ok": True, "stats": data})

    @app.get("/api/vps")
    @_login_required
    def api_vps():
        rows = [dict(r) for r in _bot().vps_service.list_all()]
        for r in rows:
            r.pop("password_plain", None)
            r.pop("password_hash", None)
        return jsonify({"ok": True, "instances": rows})

    @app.get("/api/activity")
    @_login_required
    def api_activity():
        return jsonify(recent_activity(_bot().db))

    return app


def _required_invites(bot: Any) -> int:
    try:
        return int(str(bot.db.get_setting("required_invites", "5")))
    except Exception:  # noqa: BLE001
        return 5


def _stats(bot: Any) -> dict:
    stats = dashboard_stats(bot.db, bot.provider, astra=getattr(bot, "astra", None))
    stats["panel"] = {
        "enabled": bool(config.ADMIN_PANEL_ENABLED),
        "tunnel": config.TUNNEL_PROVIDER or "local",
        "url": bot.tunnel.url or "",
        "session_hours": config.ADMIN_PANEL_SESSION_HOURS,
        "host": config.ADMIN_PANEL_HOST,
        "port": config.ADMIN_PANEL_PORT,
        "login_ttl": config.ADMIN_PANEL_LOGIN_TTL_SECONDS,
        "rate_limit": config.ADMIN_PANEL_RATE_LIMIT,
        "debug": bool(config.ADMIN_PANEL_DEBUG),
    }
    started = getattr(bot, "started_at", None)
    stats["runtime"] = {
        "uptime_seconds": (time.time() - float(started)) if started else None,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "guilds": len(getattr(bot, "guilds", []) or []),
        "ready": bool(_bot_ready(bot)),
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat()
        if started
        else "",
        "pid": _pid(),
    }
    stats["counts"] = {
        "instances": stats["vps"]["total"],
        "users": stats["users"],
        "banned": _count_banned(bot),
    }
    return stats


def _bot_ready(bot: Any) -> bool:
    try:
        return bool(bot.is_ready())
    except Exception:  # noqa: BLE001
        return False


def _pid() -> int:
    try:
        import os

        return int(os.getpid())
    except Exception:  # noqa: BLE001
        return 0


def _count_banned(bot: Any) -> int:
    try:
        return len(bot.db.list_banned())
    except Exception:  # noqa: BLE001
        return 0


def _audit_local(bot: Any, action: str, target: str, result: str, detail: str) -> None:
    try:
        bot.db.log_admin_action("anonymous", action, target, result, detail, "panel")
    except Exception:  # noqa: BLE001
        pass


__all__ = ["LIFECYCLE_ACTIONS", "create_app"]
