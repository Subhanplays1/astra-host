"""Auth for the Astra Host admin panel.

Flow:
    Discord `/admin login`  ->  one-time code (admin_login_tokens)
    Panel POST /login       ->  server-side session (admin_sessions, HttpOnly cookie)
    Every request           ->  session re-validated against the live admin list

Also owns rate limiting and CSRF token minting/verification.
"""

from __future__ import annotations

import hmac
import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

import config

SESSION_COOKIE = "astra_session"
CSRF_FIELD = "csrf_token"
_RATE_WINDOW = 300.0  # seconds
_RATE_MAX = 12  # attempts per window per key


# ── authorization ─────────────────────────────────────────────
def admin_id_set(db=None) -> set[str]:
    ids = {str(int(i)) for i in config.ADMIN_IDS if i}
    if db is not None:
        try:
            ids |= {str(x) for x in db.list_admins()}
        except Exception:  # noqa: BLE001
            pass
    return ids


def is_admin(bot: Any, admin_id: Any) -> bool:
    """Re-verify an identity on every request (ADMIN_IDS -> DB admins -> role)."""
    try:
        sid = str(int(admin_id))
    except (TypeError, ValueError):
        return False
    if sid in admin_id_set(getattr(bot, "db", None)):
        return True
    if not config.ADMIN_ROLE_ID:
        return False
    # Best effort: role check against the bot's member cache only (no API calls).
    try:
        for guild in getattr(bot, "guilds", []) or []:
            member = guild.get_member(int(sid))
            if member and any(r.id == config.ADMIN_ROLE_ID for r in getattr(member, "roles", [])):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ── rate limiting ─────────────────────────────────────────────
class RateLimiter:
    """Sliding-window limiter, keyed by client IP (or any string)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int | None = None) -> bool:
        limit = int(limit or config.ADMIN_PANEL_RATE_LIMIT)
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > _RATE_WINDOW:
                q.popleft()
            if len(q) >= max(1, limit):
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


limiter = RateLimiter()


def client_ip(request: Any) -> str:
    try:
        return str(request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr)
    except Exception:  # noqa: BLE001
        return "unknown"


# ── CSRF ──────────────────────────────────────────────────────
_secret_cache: Optional[bytes] = None


def _secret(db) -> bytes:
    """Stable per-process signing material (persisted when the DB allows)."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    raw = ""
    try:
        raw = str(db.get_setting("admin_panel_secret", "") or "")
    except Exception:  # noqa: BLE001
        raw = ""
    if not raw:
        raw = secrets.token_urlsafe(32)
        try:
            db.set_setting("admin_panel_secret", raw)
        except Exception:  # noqa: BLE001
            pass
    _secret_cache = raw.encode("utf-8")
    return _secret_cache


def csrf_token(db, session_id: str) -> str:
    return hmac.new(_secret(db), str(session_id).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def csrf_ok(db, session_id: str, token: Any) -> bool:
    if not session_id or not token:
        return False
    return hmac.compare_digest(str(token), csrf_token(db, session_id))


# ── sessions ──────────────────────────────────────────────────
def session_row(bot: Any, request: Any) -> Optional[Any]:
    """Return the live session row for this request, or None."""
    store = getattr(bot, "db", None)
    if store is None:
        return None
    sid = request.cookies.get(SESSION_COOKIE, "")
    if not sid:
        return None
    row = store.get_admin_session(sid)
    if not row:
        return None
    if not is_admin(bot, row["admin_id"]):
        store.delete_admin_session(sid)  # demoted since sign-in
        return None
    store.touch_admin_session(sid)
    return row


def issue_session(bot: Any, admin_id: str, ip: str = "", user_agent: str = "") -> tuple[str, str]:
    session_id, expires = bot.db.create_admin_session(
        str(admin_id), config.ADMIN_PANEL_SESSION_HOURS, ip, user_agent
    )
    return session_id, expires


def destroy_session(bot: Any, request: Any) -> None:
    sid = request.cookies.get(SESSION_COOKIE, "")
    if sid:
        try:
            bot.db.delete_admin_session(sid)
        except Exception:  # noqa: BLE001
            pass


# ── sign-in ───────────────────────────────────────────────────
def login_with_code(bot: Any, code: str, ip: str = "", user_agent: str = "") -> tuple[bool, str, Optional[str]]:
    """Exchange a one-time Discord code for a session. Returns (ok, message, session_id)."""
    code = (code or "").strip()
    if not code:
        return False, "Enter the code from `/admin login`.", None
    admin_id = bot.db.consume_admin_login_token(code)
    if not admin_id:
        return False, "That code is invalid or has expired.", None
    if not is_admin(bot, admin_id):
        bot.db.log_admin_action(admin_id, "panel_login", "", "denied", "not an admin", "panel")
        return False, "That identity is no longer an admin.", None
    session_id, _expires = issue_session(bot, admin_id, ip, user_agent)
    bot.db.log_admin_action(admin_id, "panel_login", "", "ok", ip, "panel")
    limiter.reset(ip)
    return True, "Signed in.", session_id


__all__ = [
    "CSRF_FIELD",
    "SESSION_COOKIE",
    "RateLimiter",
    "admin_id_set",
    "client_ip",
    "csrf_ok",
    "csrf_token",
    "destroy_session",
    "is_admin",
    "issue_session",
    "limiter",
    "login_with_code",
    "session_row",
]
