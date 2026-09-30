"""Hosts the Astra Host admin panel inside the bot process.

Flask is imported lazily and every failure is contained: a broken or missing
panel must never take the Discord bot down.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import config

log = logging.getLogger("astra.admin.panel")


class AdminPanel:
    """Owns the WSGI server thread (and the optional exposure tunnel)."""

    def __init__(self, app: Any, server: Any, thread: threading.Thread, port: int, url: str) -> None:
        self.app = app
        self.server = server
        self.thread = thread
        self.port = port
        self.url = url
        self._stopped = False

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001
            log.debug("panel server shutdown failed", exc_info=True)
        try:
            self.thread.join(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        log.info("admin panel stopped")


def start_admin_panel(bot: Any) -> Optional[AdminPanel]:
    """Start the panel + tunnel. Returns None when disabled or unavailable."""
    if not config.ADMIN_PANEL_ENABLED:
        log.info("admin panel disabled (ADMIN_PANEL_ENABLED=0)")
        return None
    try:
        from flask import Flask
        from werkzeug.serving import make_server
    except Exception as exc:  # noqa: BLE001 — Flask not installed
        log.error("admin panel unavailable — install flask: %s", exc)
        bot.db.log_admin_action("system", "panel_start", "", "error", str(exc)[:200], "panel")
        return None

    try:
        from admin.routes import create_app

        app: Flask = create_app(bot)
        host = config.ADMIN_PANEL_HOST
        port = int(config.ADMIN_PANEL_PORT)
        server = make_server(host, port, app, threaded=True)
    except Exception as exc:  # noqa: BLE001 — port in use, bad template, …
        log.error("admin panel failed to start: %s", exc)
        bot.db.log_admin_action("system", "panel_start", "", "error", str(exc)[:200], "panel")
        return None

    thread = threading.Thread(
        target=server.serve_forever,
        name="astra-admin-panel",
        daemon=True,
        kwargs={"poll_interval": 0.5},
    )
    thread.start()

    local_url = f"http://{host}:{port}"
    url = local_url
    try:
        url = bot.tunnel.start(port, config.TUNNEL_PROVIDER or None) or local_url
    except Exception as exc:  # noqa: BLE001
        log.warning("admin tunnel failed, staying on loopback: %s", exc)

    panel = AdminPanel(app, server, thread, port, url)
    bot._admin_panel = panel  # bot.setup_hook() keeps the same reference
    log.info("admin panel listening on %s (tunnel: %s)", local_url, url)
    bot.db.log_admin_action("system", "panel_start", "", "ok", url, "panel")
    return panel


def stop_admin_panel(bot: Any) -> None:
    panel = getattr(bot, "_admin_panel", None)
    if panel is None:
        return
    bot._admin_panel = None
    try:
        panel.stop()
    except Exception:  # noqa: BLE001
        log.debug("panel stop failed", exc_info=True)
    try:
        bot.tunnel.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        bot.db.log_admin_action("system", "panel_stop", "", "ok", "", "panel")
    except Exception:  # noqa: BLE001
        pass


__all__ = ["AdminPanel", "start_admin_panel", "stop_admin_panel"]
