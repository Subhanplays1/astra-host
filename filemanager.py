"""Lightweight HTTP file manager deployed inside VPS instances (stdlib only)."""

from __future__ import annotations

# Python source written to /tmp/vex-fm.py on the instance.
FILE_MANAGER_PY = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import shutil
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = ""
ROOT = Path("/")
BRAND = "Astra Host"
MAX_EDIT = 2 * 1024 * 1024

CSS = """
:root {
  --bg:#0f1115; --panel:#161a21; --panel2:#1a1f28; --border:#252a33;
  --text:#e6e8ec; --muted:#8b919c; --link:#7eb3ff; --link2:#a8c9ff;
  --ok:#3ecf8e; --err:#f07178; --warn:#e6b455;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
}
* { box-sizing:border-box; }
html { color-scheme:dark; }
body {
  margin:0; min-height:100vh; background:var(--bg); color:var(--text);
  font:14px/1.45 var(--sans);
}
a { color:var(--link); text-decoration:none; }
a:hover { color:var(--link2); text-decoration:underline; }
.shell { max-width:1080px; margin:0 auto; padding:28px 20px 56px; }

.top {
  display:flex; align-items:baseline; justify-content:space-between; gap:16px;
  padding-bottom:14px; border-bottom:1px solid var(--border); margin-bottom:18px;
}
.brand { display:flex; align-items:baseline; gap:10px; min-width:0; }
.brand-name { font-size:15px; font-weight:650; letter-spacing:-.01em; }
.brand-sub { font-size:12.5px; color:var(--muted); }
.top-meta { display:flex; gap:14px; font-size:12px; color:var(--muted); flex-wrap:wrap; justify-content:flex-end; }
.top-meta code {
  font-family:var(--mono); font-size:11.5px; color:#b7bec9;
  background:var(--panel); border:1px solid var(--border); border-radius:4px; padding:1px 6px;
}

.flash {
  display:flex; gap:10px; align-items:flex-start; margin:0 0 16px;
  padding:10px 12px; border-radius:6px; font-size:13.5px;
  background:var(--panel); border:1px solid var(--border); color:var(--text);
}
.flash.ok { border-color:#2a4a3a; color:#c8e6d4; }
.flash.err { border-color:#4a2a2e; color:#f0c4c8; }
.flash::before { content:"·"; color:var(--muted); font-weight:700; line-height:1.3; }
.flash.ok::before { content:"✓"; color:var(--ok); }
.flash.err::before { content:"!"; color:var(--err); }

.crumbs {
  display:flex; flex-wrap:wrap; align-items:center; gap:2px;
  margin:0 0 14px; padding:0; font-size:13.5px; color:var(--muted);
}
.crumbs a { color:var(--muted); padding:2px 4px; border-radius:4px; }
.crumbs a:hover { color:var(--text); background:var(--panel); text-decoration:none; }
.crumbs a:last-of-type { color:var(--text); font-weight:600; }
.crumbs .sep { opacity:.4; padding:0 2px; user-select:none; }

.toolbar {
  display:flex; flex-wrap:wrap; gap:8px; align-items:center;
  margin:0 0 14px; padding:10px 0; border-bottom:1px solid var(--border);
}
.toolbar form { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.toolbar input[type=text], .toolbar input:not([type]) {
  background:var(--panel); color:var(--text); border:1px solid var(--border);
  border-radius:5px; padding:7px 10px; font:13px var(--sans); min-width:0;
}
.toolbar input[type=text]:focus, .toolbar input:not([type]):focus {
  outline:none; border-color:#3d4a5c;
}
.toolbar input[type=file] {
  background:var(--panel); color:var(--muted); border:1px dashed var(--border);
  border-radius:5px; padding:6px 8px; font:12.5px var(--sans); max-width:260px;
}
.btn, .toolbar button {
  display:inline-flex; align-items:center; justify-content:center; gap:6px;
  background:var(--panel2); border:1px solid var(--border); color:var(--text);
  font:13px var(--sans); padding:7px 12px; border-radius:5px; cursor:pointer;
  text-decoration:none; transition:background .12s ease, border-color .12s ease;
}
.btn:hover, .toolbar button:hover { background:#202630; border-color:#333a46; text-decoration:none; color:var(--text); }
.btn:active, .toolbar button:active { background:#12161c; }
.btn.primary, .toolbar button[type=submit]:not(.ghost):not(.danger) {
  background:#2b3544; border-color:#3a4556; color:#e8edf5;
}
.btn.primary:hover, .toolbar button[type=submit]:not(.ghost):not(.danger):hover { background:#323d4e; }
.btn.danger { background:#2a1c1e; border-color:#4a2e32; color:#f0a0a8; }
.btn.danger:hover { background:#352226; border-color:#5c3840; }
.btn.ghost { background:transparent; border-color:transparent; color:var(--muted); padding:6px 8px; }
.btn.ghost:hover { background:var(--panel); color:var(--text); border-color:var(--border); }
.btn.sm { padding:4px 8px; font-size:12px; border-radius:4px; }

.count { font-size:12.5px; color:var(--muted); margin-left:auto; white-space:nowrap; }

.drop {
  display:flex; flex-wrap:wrap; gap:10px; align-items:center;
  margin:0 0 16px; padding:14px 12px; border:1px dashed var(--border);
  border-radius:6px; background:var(--panel);
  transition:border-color .15s ease, background .15s ease;
}
.drop.on { border-color:#4a5568; background:var(--panel2); }
.drop-hint { color:var(--muted); font-size:12.5px; flex:1 1 140px; min-width:120px; }
.drop-hint strong { color:#c5cad3; font-weight:600; }

.table-card {
  background:var(--panel); border:1px solid var(--border); border-radius:6px;
  overflow:hidden;
}
table { width:100%; border-collapse:collapse; }
th, td {
  padding:9px 14px; border-bottom:1px solid var(--border);
  text-align:left; vertical-align:middle;
}
th {
  font-size:11px; font-weight:600; letter-spacing:.04em; text-transform:uppercase;
  color:var(--muted); background:#12161d;
}
tr:last-child td { border-bottom:none; }
tbody tr { transition:background .1s ease; }
tbody tr:hover { background:#1a1f27; }

.name-cell { display:flex; align-items:center; gap:10px; min-width:0; }
.ico {
  flex:0 0 auto; width:28px; height:28px; border-radius:5px;
  display:grid; place-items:center; font-size:10px; font-weight:700;
  font-family:var(--mono); letter-spacing:.02em; text-transform:uppercase;
  background:#1e2430; border:1px solid var(--border); color:#9aa3b2;
}
.ico.folder { background:#1a2430; border-color:#2a3648; color:#7eb3ff; font-size:13px; }
.ico.code { background:#1a2820; border-color:#2a4030; color:#7ee0a8; }
.ico.cfg { background:#28241a; border-color:#403828; color:#e6c070; }
.ico.secret { background:#241a20; border-color:#402838; color:#e080a0; }
.ico.archive { background:#24201a; border-color:#403828; color:#d4b070; }
.ico.media { background:#1a2028; border-color:#2a3848; color:#80b8e0; }

.name-cell a {
  font-family:var(--mono); font-size:13px; word-break:break-all; color:var(--text);
}
.name-cell a:hover { color:var(--link); }
tr.is-dir .name-cell a { font-weight:600; color:#b8d4ff; }
tr.is-dir .name-cell a:hover { color:#d0e4ff; }

.ext {
  font-size:10px; font-weight:600; color:var(--muted); letter-spacing:.03em;
  text-transform:uppercase; margin-left:2px; white-space:nowrap;
}
.mono { font-family:var(--mono); font-size:13px; }
.right { text-align:right; color:var(--muted); white-space:nowrap; font-size:13px; }
.actions { display:flex; gap:4px; flex-wrap:wrap; justify-content:flex-end; align-items:center; }
.actions form { display:inline-flex; }

.empty {
  padding:48px 16px; text-align:center; color:var(--muted); font-size:13.5px;
}
.empty .big {
  display:block; font-size:22px; margin-bottom:8px; opacity:.5;
  font-family:var(--mono); font-weight:600;
}

.card {
  background:var(--panel); border:1px solid var(--border); border-radius:6px;
  padding:18px;
}
.editor-head {
  display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:space-between;
  margin-bottom:12px;
}
.editor-head .path {
  font-family:var(--mono); font-size:13px; color:#c5cad3;
  background:var(--panel2); border:1px solid var(--border);
  border-radius:4px; padding:4px 8px; word-break:break-all;
}
.editor-head .size { font-size:12.5px; color:var(--muted); }
.editor textarea {
  width:100%; min-height:440px; background:#0c0e12; color:var(--text);
  border:1px solid var(--border); border-radius:5px; padding:12px;
  font:13px/1.55 var(--mono); resize:vertical;
}
.editor textarea:focus { outline:none; border-color:#3d4a5c; }
.editor-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }

.foot {
  margin-top:28px; padding-top:14px; border-top:1px solid var(--border);
  color:var(--muted); font-size:12px;
}
.foot code {
  font-family:var(--mono); color:#b7bec9; background:var(--panel);
  border:1px solid var(--border); border-radius:3px; padding:0 5px;
}
.hide { display:none !important; }

@media (max-width:720px) {
  .shell { padding:18px 12px 40px; }
  th:nth-child(2), td:nth-child(2) { display:none; }
  .actions { justify-content:flex-start; }
  .toolbar input[type=file] { max-width:100%; }
  .count { margin-left:0; }
  .top { flex-direction:column; gap:8px; }
}
"""


def _ok(req: "Handler") -> None:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
    if req.headers.get("X-FM-Token") == TOKEN:
        req._fm_authed = True
        return
    if q.get("token", [""])[0] == TOKEN:
        req._fm_authed = True
        return
    cookie = req.headers.get("Cookie", "")
    if f"fm_token={TOKEN}" in cookie:
        req._fm_authed = True
        return
    req.send_response(401)
    req.send_header("Content-Type", "text/html; charset=utf-8")
    req.end_headers()
    req.wfile.write(
        (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Unauthorized</title><style>" + CSS + "</style></head>"
            "<body><div class='shell'>"
            "<header class='top'><div class='brand'>"
            f"<span class='brand-name'>{BRAND}</span>"
            "<span class='brand-sub'>File Manager</span></div>"
            "<div class='top-meta'><span>locked</span></div></header>"
            "<div class='card'><h2 style='margin:0 0 8px;font-size:16px'>Unauthorized</h2>"
            f"<p style='color:var(--muted);margin:0'>Add token: <code class='mono'>{html.escape('/?token=' + TOKEN)}</code></p>"
            "</div></div></body></html>"
        ).encode()
    )
    raise PermissionError


def _auth_cookie() -> str:
    # Path=/ so folder links (which keep ?token=) and bare POSTs both work
    return f"fm_token={TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"


def _safe(path: str) -> Path:
    raw = urllib.parse.unquote(path or "/")
    p = (ROOT / raw.lstrip("/")).resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        p = ROOT.resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        raise PermissionError("outside root")
    return p


def _human(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


_CODE_EXT = {".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bash", ".rb", ".go", ".rs", ".c", ".h", ".cpp", ".java", ".php"}
_CFG_EXT = {".yml", ".yaml", ".toml", ".cfg", ".conf", ".ini", ".env", ".json", ".xml", ".properties"}
_SECRET_EXT = {".key", ".crt", ".pem", ".p12", ".pfx"}
_ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"}
_MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".mp4", ".webm", ".mp3", ".wav", ".pdf"}
_HTML_EXT = {".html", ".htm", ".css", ".scss", ".less"}


def _icon_class(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    ext = Path(name).suffix.lower()
    if ext in _CODE_EXT or ext in {".md", ".txt", ".log"}:
        return "code"
    if ext in _CFG_EXT:
        return "cfg"
    if ext in _SECRET_EXT:
        return "secret"
    if ext in _ARCHIVE_EXT:
        return "archive"
    if ext in _MEDIA_EXT or ext in _HTML_EXT:
        return "media"
    return ""


def _icon_label(name: str, is_dir: bool) -> str:
    if is_dir:
        return "▸"
    ext = Path(name).suffix.lower().lstrip(".")
    if not ext:
        return "·"
    return ext[:3]


def _kind(name: str, is_dir: bool) -> str:
    if is_dir:
        return ""
    ext = Path(name).suffix.lower().lstrip(".")
    return ext


def _note(msg: str) -> str:
    safe = html.escape(msg or "Something went wrong")
    return (
        "<div class='card'><div class='empty'>"
        "<span class='big'>!</span>"
        f"{safe}</div></div>"
    )


def _page(title: str, body: str, flash: str = "", flash_cls: str = "") -> bytes:
    flash_html = f'<div class="flash {flash_cls}"><span>{html.escape(flash)}</span></div>' if flash else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body>
<div class="shell">
<header class="top">
  <div class="brand">
    <span class="brand-name">{html.escape(BRAND)}</span>
    <span class="brand-sub">File Manager</span>
  </div>
  <div class="top-meta">
    <span>127.0.0.1</span>
    <code>localhost.run</code>
  </div>
</header>
{flash_html}
{body}
<footer class="foot">Bound to <code>127.0.0.1</code> · exposed via localhost.run · do not share the full URL with token</footer>
</div>
{_DROP_JS}
</body></html>"""
    return doc.encode("utf-8", errors="replace")


_DROP_JS = """
<script>
(function(){
  var d=document.getElementById("drop");
  if(!d) return;
  ["dragenter","dragover"].forEach(function(e){
    d.addEventListener(e,function(ev){ev.preventDefault();d.classList.add("on");});
  });
  ["dragleave","drop"].forEach(function(e){
    d.addEventListener(e,function(ev){ev.preventDefault();d.classList.remove("on");});
  });
  d.addEventListener("drop",function(ev){
    var f=document.getElementById("upfile");
    if(f&&ev.dataTransfer&&ev.dataTransfer.files&&ev.dataTransfer.files.length){
      f.files=ev.dataTransfer.files;
      if(f.form) f.form.submit();
    }
  });
})();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "VexFM/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter
        pass

    def _redirect(self, loc: str) -> None:
        self.send_response(302)
        self.send_header("Location", loc)
        if getattr(self, "_fm_authed", False):
            self.send_header("Set-Cookie", _auth_cookie())
        self.end_headers()

    def _html(self, data: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_fm_authed", False):
            self.send_header("Set-Cookie", _auth_cookie())
        self.end_headers()
        self.wfile.write(data)

    def _qs(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def do_GET(self) -> None:
        try:
            _ok(self)
        except PermissionError:
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/list"):
            self._list(qs)
            return
        if path == "/edit":
            self._edit_get(qs)
            return
        if path in ("/download", "/file"):
            self._download(qs)
            return
        if path == "/logout":
            self.send_response(200)
            self.send_header("Set-Cookie", "fm_token=; Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(b"logged out")
            return
        self._html(_page("Not found", "<div class='card'>Not found</div>"), 404)

    def do_POST(self) -> None:
        try:
            _ok(self)
        except PermissionError:
            return
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")

        if parsed.path == "/upload":
            self._upload(body, ctype, qs_from_body=False)
            return
        if parsed.path == "/delete":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            target = form.get("path", [""])[0]
            try:
                p = _safe(target)
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists() or p.is_symlink():
                    p.unlink()
                else:
                    raise FileNotFoundError(target)
                self._redirect("/?token=" + TOKEN + "&ok=" + urllib.parse.quote("Deleted " + target))
            except Exception as exc:
                self._html(_page("Delete", _note(str(exc) or "Delete failed"), str(exc), "err"))
            return
        if parsed.path == "/mkdir":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            parent = form.get("path", ["/"])[0]
            name = form.get("name", [""])[0].strip()
            try:
                if not name or "/" in name or name in (".", ".."):
                    raise ValueError("invalid name")
                (_safe(parent) / name).mkdir(exist_ok=False)
                self._redirect("/?token=" + TOKEN + "&path=" + urllib.parse.quote(parent) + "&ok=" + urllib.parse.quote("Created " + name))
            except Exception as exc:
                self._html(_page("Mkdir", _note(str(exc) or "Create folder failed"), str(exc), "err"))
            return
        if parsed.path == "/save":
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            target = form.get("path", [""])[0]
            content = form.get("content", [""])[0]
            try:
                p = _safe(target)
                p.write_text(content, encoding="utf-8", errors="replace")
                parent = str(p.parent.relative_to(ROOT) or "/")
                if parent == ".":
                    parent = "/"
                self._redirect("/?token=" + TOKEN + "&path=" + urllib.parse.quote("/" + parent.strip("/") + ("/" if parent.strip("/") else "")) + "&ok=" + urllib.parse.quote("Saved " + p.name))
            except Exception as exc:
                self._html(_page("Save", _note(str(exc) or "Save failed"), str(exc), "err"))
            return
        self._html(_page("Bad", "<div class='card'>bad request</div>"), 400)

    def _list(self, qs: dict) -> None:
        rel = qs.get("path", ["/"])[0]
        flash = (qs.get("ok") or qs.get("err") or [""])[0]
        fcls = "ok" if qs.get("ok") else ("err" if qs.get("err") else "")
        try:
            cur = _safe(rel)
            if not cur.exists():
                cur = ROOT
            if cur.is_file():
                # redirect to edit/download
                parent = "/" + str(cur.parent.relative_to(ROOT)).strip("/")
                if parent == "/.":
                    parent = "/"
                self._redirect("/edit?token=" + TOKEN + "&path=" + urllib.parse.quote(str(cur.relative_to(ROOT))))
                return
        except Exception as exc:
            self._html(_page("List", _note("bad path"), str(exc), "err"))
            return

        rel_disp = "/" + str(cur.relative_to(ROOT)).strip("/")
        if rel_disp.startswith("/."):
            rel_disp = rel_disp
        crumbs = ["<a href='/?token=" + TOKEN + "'>/</a>"]
        acc = ""
        parts = [p for p in rel_disp.strip("/").split("/") if p]
        for part in parts:
            acc += part + "/"
            crumbs.append(
                f"<a href='/?token={TOKEN}&path={urllib.parse.quote('/' + acc)}'>{html.escape(part)}</a>"
            )

        rows = []
        try:
            entries = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as exc:
            self._html(_page("List", _note("Permission denied"), str(exc), "err"))
            return
        for ent in entries:
            name = ent.name
            if ent.is_dir():
                href = f"/?token={TOKEN}&path={urllib.parse.quote((rel_disp.rstrip('/') + '/' + name))}"
                rows.append(
                    f"<tr class='is-dir'><td><div class='name-cell'>"
                    f"<span class='ico folder'>{_icon_label(name, True)}</span>"
                    f"<a href='{href}'>{html.escape(name)}</a></div></td>"
                    f"<td class='right'>—</td><td class='actions'>"
                    f"<a class='btn ghost sm' href='{href}'>Open</a>"
                    f"<form method='post' action='/delete?token={TOKEN}' onsubmit=\"return confirm('Delete {html.escape(name)}?')\">"
                    f"<input type='hidden' name='path' value='{html.escape(str(ent))}'>"
                    f"<button class='btn danger sm' type='submit'>Delete</button></form></td></tr>"
                )
            else:
                try:
                    size = _human(ent.stat().st_size)
                except OSError:
                    size = "?"
                edit = f"/edit?token={TOKEN}&path={urllib.parse.quote(str(ent.relative_to(ROOT)))}"
                dl = f"/download?token={TOKEN}&path={urllib.parse.quote(str(ent.relative_to(ROOT)))}"
                kind = _kind(name, False)
                kind_html = f"<span class='ext'>{html.escape(kind)}</span>" if kind else ""
                rows.append(
                    f"<tr><td><div class='name-cell'>"
                    f"<span class='ico {_icon_class(name, False)}'>{_icon_label(name, False)}</span>"
                    f"<a href='{edit}'>{html.escape(name)}</a>{kind_html}</div></td>"
                    f"<td class='right'>{size}</td><td class='actions'>"
                    f"<a class='btn ghost sm' href='{edit}'>Edit</a>"
                    f"<a class='btn ghost sm' href='{dl}'>Download</a>"
                    f"<form method='post' action='/delete?token={TOKEN}' onsubmit=\"return confirm('Delete {html.escape(name)}?')\">"
                    f"<input type='hidden' name='path' value='{html.escape(str(ent))}'>"
                    f"<button class='btn danger sm' type='submit'>Delete</button></form></td></tr>"
                )

        parent_rel = str(cur.parent.relative_to(ROOT)) if cur != ROOT else ""
        parent_href = f"/?token={TOKEN}" + (
            f"&path={urllib.parse.quote('/' + parent_rel.strip('/'))}" if parent_rel and parent_rel != "." else ""
        )
        up = "" if cur == ROOT else f"<a class='btn ghost' href='{parent_href}'>↑</a>"

        crumb_html = " <span class='sep'>/</span> ".join(crumbs)
        n_dirs = sum(1 for e in entries if e.is_dir())
        n_files = len(entries) - n_dirs
        body = f"""
<nav class="crumbs">{crumb_html}</nav>
<div class="toolbar">
  {up}
  <form method="get" action="/">
    <input type="hidden" name="token" value="{TOKEN}">
    <input type="text" name="path" placeholder="/etc" value="{html.escape(rel_disp)}">
    <button type="submit">Go</button>
  </form>
  <form method="post" action="/mkdir?token={TOKEN}">
    <input type="hidden" name="path" value="{html.escape(str(cur))}">
    <input type="text" name="name" placeholder="new folder" required>
    <button type="submit">New folder</button>
  </form>
  <span class="count">{n_dirs} folders · {n_files} files</span>
</div>
<div class="drop" id="drop">
  <form method="post" action="/upload?token={TOKEN}" enctype="multipart/form-data">
    <input type="hidden" name="path" value="{html.escape(str(cur))}">
    <input type="file" id="upfile" name="file" required multiple>
    <button type="submit">Upload</button>
  </form>
  <div class="drop-hint"><strong>Drop files</strong> into this folder, or use Upload</div>
</div>
<div class="table-card">
<table><thead><tr><th>Name</th><th class="right">Size</th><th class="right">Actions</th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan=3><div class='empty'><span class='big'>[]</span>This folder is empty</div></td></tr>"}</tbody></table>
</div>
"""
        self._html(_page(f"Files {rel_disp}", body, flash, fcls))

    def _edit_get(self, qs: dict) -> None:
        try:
            p = _safe(qs.get("path", [""])[0])
            if not p.is_file():
                raise FileNotFoundError("not a file")
            if p.stat().st_size > MAX_EDIT:
                raise ValueError("file too large to edit in browser")
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self._html(_page("Edit", _note(str(exc) or "Cannot edit file"), str(exc), "err"))
            return
        rel = str(p.relative_to(ROOT))
        parent = "/" + str(p.parent.relative_to(ROOT)).strip("/")
        if parent == "/.":
            parent = "/"
        back = f"/?token={TOKEN}" + (
            f"&path={urllib.parse.quote(parent if parent != '/' else '/')}" if p.parent != ROOT else ""
        )
        if p.parent == ROOT:
            back = f"/?token={TOKEN}"
        else:
            back = f"/?token={TOKEN}&path={urllib.parse.quote('/' + str(p.parent.relative_to(ROOT)).strip('/'))}"
        body = f"""
<nav class="crumbs"><a href="{back}">←</a> <span class="sep">/</span> <span class="mono">{html.escape(rel)}</span></nav>
<div class="card editor">
  <div class="editor-head">
    <span class="path">{html.escape(p.name)}</span>
    <span class="size">{html.escape(_human(p.stat().st_size))}</span>
  </div>
  <form method="post" action="/save?token={TOKEN}">
    <input type="hidden" name="path" value="{html.escape(rel)}">
    <textarea name="content" spellcheck="false">{html.escape(text)}</textarea>
    <div class="editor-actions">
      <button type="submit" class="primary">Save</button>
      <a class="btn ghost" href="/download?token={TOKEN}&path={urllib.parse.quote(rel)}">Download</a>
      <a class="btn ghost" href="{back}">Cancel</a>
    </div>
  </form>
</div>"""
        self._html(_page(f"Edit {p.name}", body))

    def _download(self, qs: dict) -> None:
        try:
            p = _safe(qs.get("path", [""])[0])
            if not p.is_file():
                raise FileNotFoundError("not a file")
            data = p.read_bytes()
        except Exception as exc:
            self._html(_page("Download", _note(str(exc) or "Download failed"), str(exc), "err"))
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _upload(self, body: bytes, ctype: str, qs_from_body: bool) -> None:
        # parse multipart manually (no external deps)
        boundary = ""
        for part in ctype.split(";"):
            part = part.strip()
            if part.lower().startswith("boundary="):
                boundary = part.split("=", 1)[1].strip().strip('"')
        form_path = "/"
        saved = []
        if boundary:
            b = b"--" + boundary.encode()
            chunks = body.split(b)
            for chunk in chunks:
                if b"\r\n\r\n" not in chunk:
                    continue
                head, _, data = chunk.partition(b"\r\n\r\n")
                data = data[:-2] if data.endswith(b"\r\n") else data
                head_l = head.decode("utf-8", "replace")
                if 'name="path"' in head_l:
                    form_path = data.decode("utf-8", "replace").strip() or "/"
                if 'name="file"' in head_l and "filename=" in head_l:
                    fname = head_l.split("filename=", 1)[1].split("\r\n", 1)[0].strip().strip('"')
                    if not fname:
                        continue
                    dest_dir = _safe(form_path)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / Path(fname).name
                    dest.write_bytes(data)
                    saved.append(dest.name)
        if not saved:
            self._html(_page("Upload", _note("No file received"), "No file received", "err"))
            return
        parent = "/" + str(_safe(form_path).relative_to(ROOT)).strip("/")
        loc = f"/?token={TOKEN}"
        if parent and parent != "/":
            loc += f"&path={urllib.parse.quote('/' + parent)}"
        loc += "&ok=" + urllib.parse.quote("Uploaded " + ", ".join(saved))
        self._redirect(loc)


def main() -> None:
    global TOKEN, ROOT, BRAND
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", required=True)
    ap.add_argument("--root", default="/")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--brand", default="Astra Host")
    args = ap.parse_args()
    TOKEN = args.token
    ROOT = Path(args.root).resolve()
    BRAND = (args.brand or "Astra Host")[:64]
    if not ROOT.is_dir():
        ROOT = Path("/")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"VexFM listening on {args.host}:{args.port} root={ROOT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
'''


def build_start_script(token: str, port: int = 8765, brand: str = "Astra Host") -> str:
    """Free non-auth tunnel via localhost.run.

    No -N: the URL is printed by a remote session helper.
    stdin is held open with `tail -f /dev/null` so EOF does not kill the tunnel.
    """
    import base64

    fm_b64 = base64.b64encode(FILE_MANAGER_PY.encode("utf-8")).decode("ascii")
    p = int(port)
    t = token
    b = (brand or "Astra Host").replace("'", "'\"'\"'")
    return f"""set +e
export DEBIAN_FRONTEND=noninteractive
if ! command -v python3 >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq python3 openssh-client curl ca-certificates >/dev/null 2>&1 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 openssh-client curl ca-certificates >/dev/null 2>&1 || true
  fi
fi
command -v python3 >/dev/null 2>&1 || {{ echo NO_PYTHON; exit 2; }}
if [ -f /tmp/vex-fm.pid ]; then kill "$(cat /tmp/vex-fm.pid)" >/dev/null 2>&1 || true; fi
if [ -f /tmp/vex-tunnel.pid ]; then kill "$(cat /tmp/vex-tunnel.pid)" >/dev/null 2>&1 || true; fi
rm -f /tmp/vex-fm.log /tmp/vex-fm.pid /tmp/vex-tunnel.log /tmp/vex-tunnel.pid /tmp/vex-tunnel.stop
echo {fm_b64} | base64 -d > /tmp/vex-fm.py || {{ echo FM_WRITE_FAIL; exit 3; }}
setsid sh -c 'echo $$ > /tmp/vex-fm.pid; exec python3 /tmp/vex-fm.py --host 127.0.0.1 --port {p} --token {t} --root / --brand '"'"'{b}'"'"'' >/tmp/vex-fm.log 2>&1 &
sleep 1
if [ ! -s /tmp/vex-fm.pid ] || ! kill -0 "$(cat /tmp/vex-fm.pid 2>/dev/null)" 2>/dev/null; then
  echo FM_START_FAIL
  cat /tmp/vex-fm.log 2>/dev/null || true
  exit 4
fi
command -v ssh >/dev/null 2>&1 || {{
  command -v apt-get >/dev/null 2>&1 && apt-get install -y -qq openssh-client >/dev/null 2>&1 || true
}}
command -v ssh >/dev/null 2>&1 || {{ echo NO_SSH; exit 6; }}
printf '#!/bin/sh\\necho\\n' > /tmp/vex-askpass.sh
chmod +x /tmp/vex-askpass.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/vex-askpass.sh SSH_ASKPASS_REQUIRE=force
extract_url() {{
  U=$(grep -Eio 'https://[A-Za-z0-9._-]+\\.(localhost\\.run|lhr\\.life|lhrtunnel\\.link|lhr\\.rocks|lhr\\.link)[A-Za-z0-9._/-]*' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
  if [ -z "$U" ]; then
    H=$(grep -Eio '[A-Za-z0-9._-]+\\.(localhost\\.run|lhr\\.life|lhrtunnel\\.link|lhr\\.rocks|lhr\\.link)' /tmp/vex-tunnel.log 2>/dev/null | head -n1)
    if [ -n "$H" ]; then U="https://$H"; fi
  fi
  echo "$U"
}}
# Without a remote session, localhost.run never prints its URL.
# stdin is held open by tail so EOF cannot kill the tunnel.
pkill -f 'ssh.*nokey@localhost.run' >/dev/null 2>&1 || true
rm -f /tmp/vex-tunnel.log /tmp/vex-tunnel.pid /tmp/vex-tunnel.fifo /tmp/vex-tunnel-keeper.pid
setsid sh -c 'tail -f /dev/null | ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -o NumberOfPasswordPrompts=1 -o TCPKeepAlive=yes -R 80:127.0.0.1:{p} nokey@localhost.run > /tmp/vex-tunnel.log 2>&1' &
sleep 0.5
TUN_PID=$(pgrep -f 'ssh.*nokey@localhost.run' 2>/dev/null | head -n1)
if [ -n "$TUN_PID" ]; then echo "$TUN_PID" > /tmp/vex-tunnel.pid; fi
URL=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  URL=$(extract_url)
  if [ -n "$URL" ]; then break; fi
  if [ -s /tmp/vex-tunnel.pid ]; then
    PID=$(cat /tmp/vex-tunnel.pid 2>/dev/null)
    if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then
      TUN_PID=$(pgrep -f 'ssh.*nokey@localhost.run' 2>/dev/null | head -n1)
      if [ -n "$TUN_PID" ]; then echo "$TUN_PID" > /tmp/vex-tunnel.pid; else break; fi
    fi
  else
    TUN_PID=$(pgrep -f 'ssh.*nokey@localhost.run' 2>/dev/null | head -n1)
    if [ -n "$TUN_PID" ]; then echo "$TUN_PID" > /tmp/vex-tunnel.pid; fi
  fi
  sleep 1
done
echo "TOKEN={t}"
echo "PORT={p}"
echo "URL=${{URL:-}}"
if [ -z "$URL" ]; then
  echo FM_TUNNEL_FAIL
  echo '--- tunnel log ---'
  cat /tmp/vex-tunnel.log 2>/dev/null || true
  echo '--- net diag ---'
  getent ahosts localhost.run 2>/dev/null | head -n3 || echo NO_DNS
  ip -4 route show default 2>/dev/null | head -n1 || echo NO_IPV4_ROUTE
  ip -6 route show default 2>/dev/null | head -n1 || echo NO_IPV6_ROUTE
  timeout 5 bash -c 'exec 3<>/dev/tcp/localhost.run/22' && echo LR22_OK || echo LR22_FAIL
  curl -sS -m 6 -o /dev/null -w 'LR_HTTP %{{http_code}}\n' https://localhost.run || echo LR_HTTP_FAIL
  exit 5
fi
sleep 2
if [ ! -s /tmp/vex-tunnel.pid ] || ! kill -0 "$(cat /tmp/vex-tunnel.pid 2>/dev/null)" 2>/dev/null; then
  TUN_PID=$(pgrep -f 'ssh.*nokey@localhost.run' 2>/dev/null | head -n1)
  if [ -n "$TUN_PID" ]; then echo "$TUN_PID" > /tmp/vex-tunnel.pid; fi
fi
if [ ! -s /tmp/vex-tunnel.pid ] || ! kill -0 "$(cat /tmp/vex-tunnel.pid 2>/dev/null)" 2>/dev/null; then
  echo FM_TUNNEL_DIED
  cat /tmp/vex-tunnel.log 2>/dev/null || true
  exit 7
fi
if [ ! -s /tmp/vex-fm.pid ] || ! kill -0 "$(cat /tmp/vex-fm.pid 2>/dev/null)" 2>/dev/null; then
  echo FM_DIED
  cat /tmp/vex-fm.log 2>/dev/null || true
  exit 8
fi
echo FM_OK
"""


def build_stop_script() -> str:
    return r"""set +e
if [ -f /tmp/vex-fm.pid ]; then kill "$(cat /tmp/vex-fm.pid)" >/dev/null 2>&1 || true; rm -f /tmp/vex-fm.pid; fi
if [ -f /tmp/vex-tunnel.pid ]; then kill "$(cat /tmp/vex-tunnel.pid)" >/dev/null 2>&1 || true; rm -f /tmp/vex-tunnel.pid; fi
pkill -f 'nokey@localhost\.run' >/dev/null 2>&1 || true
rm -f /tmp/vex-tunnel.log /tmp/vex-tunnel.fifo /tmp/vex-tunnel-keeper.pid /tmp/vex-tunnel.stop
echo FM_STOPPED
"""
