"""HTTP routes for the Astra Host admin panel."""

from __future__ import annotations

import functools
import logging
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
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    return response


def _audit(action: str, target: str = "", result: str = "ok", detail: str = "") -> None:
    bot = _bot()
    bot.db.log_admin_action(getattr(g, "admin_id", "unknown"), action, target, result, detail, "panel")


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
        return {
            "brand_name": brand,
            "brand_tagline": config.BRAND_TAGLINE,
            "csrf_token": getattr(g, "csrf", ""),
            "admin_id": getattr(g, "admin_id", ""),
            "maintenance_mode": maintenance,
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
        before = {
            k: str(bot.db.get_setting(k, d))
            for k, d in (
                ("maintenance_mode", "0"),
                ("vps_enabled", "1"),
                ("required_invites", "5"),
            )
        }
        bot.db.set_setting("maintenance_mode", "1" if request.form.get("maintenance_mode") else "0")
        bot.db.set_setting("vps_enabled", "0" if request.form.get("vps_enabled") is None else "1")
        raw_inv = str(request.form.get("required_invites") or "").strip()
        if raw_inv:
            try:
                invites = max(0, min(1000, int(raw_inv)))
                bot.db.set_setting("required_invites", invites)
            except ValueError:
                flash("Invites must be a number 0-1000.", "error")
        after = {
            k: str(bot.db.get_setting(k, d))
            for k, d in (
                ("maintenance_mode", "0"),
                ("vps_enabled", "1"),
                ("required_invites", "5"),
            )
        }
        _audit("settings_change", "", "ok", f"{before} -> {after}")
        flash("Settings saved.", "ok")
        return redirect(url_for("index"))

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

    # ── JSON API (same guards) ───────────────────────────────
    @app.get("/api/overview")
    @_login_required
    def api_overview():
        return jsonify(_stats(_bot()))

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


def _stats(bot: Any) -> dict:
    stats = dashboard_stats(bot.db, bot.provider, astra=getattr(bot, "astra", None))
    stats["panel"] = {
        "enabled": bool(config.ADMIN_PANEL_ENABLED),
        "tunnel": config.TUNNEL_PROVIDER or "local",
        "url": bot.tunnel.url or "",
        "session_hours": config.ADMIN_PANEL_SESSION_HOURS,
    }
    return stats


def _audit_local(bot: Any, action: str, target: str, result: str, detail: str) -> None:
    try:
        bot.db.log_admin_action("anonymous", action, target, result, detail, "panel")
    except Exception:  # noqa: BLE001
        pass


__all__ = ["LIFECYCLE_ACTIONS", "create_app"]
