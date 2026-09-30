# Astra Host

**The Future of Free VPS Hosting.** — white-label Discord-first VPS platform: invite-gated access, clean LXD/Incus provisioning, an Astra status/personality engine, a real service layer, and an admin-only web control center.

Built on top of the VexDeploy engine (internal modules, tables and log names still use `vexdeploy` for compatibility) with **Astra Host** as the default brand. No legacy provider code.

---

## Features

- **Invite-gated `/createvps`** — users must hit a configurable invite goal
- **Plan catalog + creation UI** — `/plans`, `/createplan`, pick plan + OS in Discord, custom resources modal
- **12 OS images** — Ubuntu, Debian, Alpine, Rocky, Alma, Fedora, Oracle, openSUSE
- **Invite tracking** — join attribution, leave invalidation, dedup, no credit on ambiguity
- **Completion alerts** — one-time DM + optional channel ping when goal is reached
- **Clean LXD provider** — resource limits (RAM/CPU/disk), network, labels, bootstrap SSH
- **Service layer** — Discord commands *and* the admin panel go through `services/vps_service.py`, so provisioning logic exists exactly once
- **Astra status engine** — deterministic, rules-based personality: presence line, health snapshots and transition notes (`astra_status.py`); no external AI required
- **Admin panel** — internal Flask control center (dashboard, instance control, audit log) with one-time Discord sign-in, sessions, rate limiting, CSRF and audit logging
- **Post-deploy branding** — brand files + idempotent MOTD installer on every new VPS
- **Multi-profile branding** — versioned fields, switchable profiles, white-label ready (AytroCloud scrubbed)
- **Secure credentials** — passwords delivered via DM spoilers only
- **Web file manager** — browse/upload/edit/delete files over a token-protected localhost HTTP server, exposed via **localhost.run** (`/file_manager` or dashboard **📁 Files**)
- **SQLite** — zero external database
- **Auto-stop on bot offline** — when the bot shuts down (SIGINT/SIGTERM/disconnect), every managed VPS is stopped and marked `stopped` in the DB
- **Auto-start on bot ready** — when the bot reconnects, stopped VPS instances are started again (toggle via `autostart_on_ready` in settings; default `1`)

---

## Architecture

```text
Discord commands ─┐
                  ├──> services/ (VPSService · monitoring · tunnel) ──> LXDProvider
Admin panel ──────┘                                   │
                                                      └──> SQLite (one source of truth)
```

Per-instance flow:

```text
Discord slash command
        |
Invite / cooldown / blacklist / limits check
        |
LXDProvider.create_vps()   (staged progress panel, no fake steps)
        |
SQLite instance row
        |
Post-deploy: brand files + MOTD installer
        |
Credentials via DM
```

### Layout

```text
bot.py            # Discord bot + all slash commands + Astra panels
config.py         # defaults, env, resource limits, brand seed
database.py       # SQLite layer (incl. admin sessions/tokens/audit)
provider.py       # clean LXD/Incus VPS provider (the engine)
astra_status.py   # Astra status/personality engine (deterministic)
services/
  vps_service.py  # shared lifecycle service (Discord + panel)
  monitoring.py   # host metrics, provider health, dashboard stats
  tunnel.py       # admin-panel exposure abstraction
admin/            # admin-only Flask panel (routes, auth, templates, css)
invites.py        # invite tracker + completion notifications
branding.py       # branding manager + embed
motd.py           # MOTD generator + idempotent installer
filemanager.py    # token-protected in-VPS file manager + tunnel launcher
templates/        # MOTD variable reference
tests/            # smoke tests (python tests/test_panel_smoke.py)
requirements.txt
.env.example
```

Runtime files: `vexdeploy.db`, `vexdeploy.log`.

---

## Requirements

- Python 3.10+
- LXD or Incus reachable by the bot process (`lxc` or `incus` CLI; set `LXD_CLI` to override)
- Discord bot token with **Server Members Intent**

```bash
pip install -r requirements.txt
```

---

## Setup

### One-shot (recommended)

```bash
# installs git + all apt packages, clones repo, Incus, venv, pip deps
curl -fsSL https://raw.githubusercontent.com/Arion-Team/VexDeploy/main/setup.sh -o /tmp/vex-setup.sh
sudo bash /tmp/vex-setup.sh

# or from an existing clone:
sudo bash setup.sh

# also install systemd unit:
sudo WITH_SYSTEMD=1 bash setup.sh

# skip auto-enter venv shell at the end:
ENTER_VENV=0 sudo -E bash setup.sh
```

Then edit `.env` (set `DISCORD_TOKEN`) and start the bot.

**pip notes:** prefers `.venv`; if venv is unavailable it falls back to  
`python3 -m pip install --break-system-packages …` (PEP 668 / Debian 12+).

### Manual

```bash
git clone https://github.com/Arion-Team/VexDeploy.git
cd VexDeploy
# install/configure LXD or Incus (remotes, network, .env LXD_CLI)
# Debian bookworm: prefers incus-base (containers only) to avoid qemu/backports conflicts
sudo bash setup_lxd.sh
cp .env.example .env
# edit .env — set DISCORD_TOKEN
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### .env

```env
DISCORD_TOKEN=
ADMIN_IDS=1210291131301101618
ADMIN_ROLE_ID=1376177459870961694
DATABASE_PATH=vexdeploy.db
LXD_NETWORK=vexdeploy
LXD_CLI=
DEFAULT_OS_IMAGE=ubuntu:22.04
MAX_CONTAINERS=100
MAX_VPS_PER_USER=3
DEFAULT_MEMORY_MB=1024
DEFAULT_CPUS=1
DEFAULT_DISK_GB=10
```

Resource validation:

| Resource | Min | Default | Max |
|----------|-----|---------|-----|
| Memory | 512 MB | 1024 MB | 65536 MB |
| CPU | 1 | 1 | 32 |
| Disk | **5 GB** | **10 GB** | 1000 GB |

Defaults always pass validation (fixes the old 5GB / min-10GB mismatch).

---

## Admin panel (internal only)

The **Astra Host Admin Control Center** is the only web interface and is strictly admin-only. It runs inside the bot process, so it shares the same `Database` and `VPSService` as Discord — one source of truth, no second implementation.

```env
ADMIN_PANEL_ENABLED=1
ADMIN_PANEL_HOST=127.0.0.1
ADMIN_PANEL_PORT=8787
ADMIN_PANEL_SESSION_HOURS=8
ADMIN_PANEL_LOGIN_TTL=300     # one-time code lifetime (seconds)
ADMIN_PANEL_RATE_LIMIT=10     # requests per window per IP
TUNNEL_PROVIDER=local         # local | localhost.run | cloudflare | tailscale | custom
```

**Sign-in flow**

1. `/admin login` → one-time code (single use, expires in `ADMIN_PANEL_LOGIN_TTL`)
2. `/admin` → **Admin Panel** button (or `/admin action:panel`) sends the URL **by DM only** — the URL is never posted publicly
3. Paste the code in the panel; you get an HttpOnly session cookie valid for `ADMIN_PANEL_SESSION_HOURS`

**Pages**

| Page | Contents |
|------|----------|
| Dashboard | instance/user counts, node CPU/memory/storage, provider health, Astra status, settings, recent activity |
| Instances | searchable table + start / stop / restart / suspend / resume / force stop / delete (delete requires typing the id) |
| Audit | every admin action (`admin_activity`) + deployment events, with result and source (`discord` / `panel`) |

**Security**

- Authorization re-checked on **every** request against `ADMIN_IDS` ∪ DB admins ∪ `ADMIN_ROLE_ID` (cached members only)
- Per-IP rate limiting, CSRF token per session, input validation, `Cache-Control: no-store`, `X-Frame-Options: DENY`, strict CSP
- Sessions are server-side rows — sign out or revoke from the DB at any time
- All panel actions are written to the audit log; VPS actions run through `VPSService`, which audits them too
- Keep `TUNNEL_PROVIDER=local` and reach the panel over SSH port-forwarding when possible; never publish the tunnel URL

If Flask is not installed the panel stays down and the bot keeps running.

---

## Commands

### User

`/createvps` (plan + OS picker UI) `/plans` `/invites` `/leaderboard` `/vps` `/list` `/manage_vps` `/file_manager` `/stop_file_manager` `/connect_vps` `/vps_stats` `/change_ssh_password` `/vps_shell` `/vps_console` `/vps_usage` `/transfer_vps` `/refresh-motd` `/help`

### Admin — access

`/admin` (control center: login code, status, panel link) `/setinvites` `/addinvites` `/removeinvites` `/resetinvites` `/resetcooldown` `/blacklist` `/unblacklist` `/ban_user` `/unban_user` `/list_banned` `/vps-enable` `/vps-disable` `/setlogchannel` `/setcompletionchannel` `/add_admin` `/remove_admin` `/list_admins`

### Admin — VPS

`/create_vps` `/vps_list` `/delete_vps` `/suspend_vps` `/unsuspend_vps` `/edit_vps` `/emergency_stop` `/emergency_remove` `/admin_stats` `/global_stats` `/system_info` `/cleanup_vps` `/backup_data` `/restore_data` `/container_limit` `/reinstall_bot`

### Admin — plans

`/createplan` `/editplan` `/deleteplan` `/listplans`

Seed via `.env`:

```env
PLANS=Starter:1024:1:10:Free,Pro:2048:2:25:Popular:$4,Business:4096:4:50:Best
```

### Admin — branding

`/brand` `/brand-name` `/brand-tagline` `/brand-website` `/brand-discord` `/brand-support` `/brand-motd` `/brand-colors` `/brand-reset` `/brand-create` `/brand-use` `/brand-list` `/brand-template` `/brand-reinstall` `/brand-update-existing`

---

## VPS dashboard (`/manage_vps`)

`/manage_vps <vps_id>` opens an interactive button dashboard (ephemeral, owner/admin only):

| Button | Action |
|--------|--------|
| ▶ Start / ⏹ Stop / ↻ Restart | Lifecycle |
| 📊 Stats | Live CPU/memory/disk, plan, image, IP |
| 🌐 Network | Addresses, gateway, listening ports, public IP |
| 📁 Files | Web file manager (browse/upload/edit/delete) via **localhost.run** tunnel |
| 📋 Logs | Last 50 lines of container logs |
| 🎨 Rebrand | Push current MOTD + `/etc/issue` banners |
| 🔑 SSH | DM: normal SSH + password + **sshx** / web terminal |
| 🔐 Password | Modal — change SSH password in place |
| ⚡ Command | Modal — run a shell command, show exit code + output |
| 🔁 Reinstall | Two-step confirm — recreates container (same plan/owner), wipes data |
| 🗑 Delete | Two-step confirm — removes container + DB row |

### Login branding (MOTD + issue)

On SSH login, users see the provider name and host details:

```text
  Astra Host — FREE VPS HOSTING
  The Future of Free VPS Hosting.
  ─────────────────────────────
  Provider   Astra Host
  Host / OS / CPU / Memory / Disk / IP …
  Website / Discord / Support
  The Future of Free VPS Hosting.
```

Also written to `/etc/issue` + `/etc/issue.net` (console/SSH pre-auth banner)
and `/etc/vexdeploy/brand`. Re-push anytime with dashboard **🎨 Rebrand** or
`/refresh-motd` / `/brand-reinstall`.

### Reverse SSH (sshx)

Started from the dashboard **SSH** button when the container has no public IP:

- Tools install on demand inside the container (curl + package manager)
- sshx is detached via PID file with stdin held open (session survives launcher exit)
- Session lives while the container runs
- Normal `ssh user@ip -p 22` still works via `/connect_vps` / `/vps_shell`

### Web file manager (localhost.run)

From the dashboard **📁 Files** button or `/file_manager <vps_id>`:

- Python stdlib HTTP server on `127.0.0.1:8765` inside the VPS (no public IP needed)
- Reverse tunnel via free non-auth **localhost.run** (`ssh -R 80:127.0.0.1:8765 nokey@localhost.run`) → HTTPS URL (`*.localhost.run` / `*.lhr.life` / `*.lhrtunnel.link`)
- No browser screening / dashboard wall (unlike Pinggy free)
- Token-protected: open `https://….localhost.run/?token=…` (token sent via DM spoiler)
- Features: browse, upload (multi-file), download, edit text files, mkdir, delete
- Stop with `/stop_file_manager <vps_id>` (PID-file kill only — never `pkill -f`)
- Requires `python3` + `openssh-client` (installed on demand via apt/apk)

---

## White-label branding

Default profile is **Astra Host**. Change it without a redeploy:

```text
/brand-name My Hosting
/brand-website https://example.com
/brand-support support@example.com
/brand-colors cyan magenta
```

- Field changes bump `version` (e.g. `My Hosting v4`)
- New VPS stamp `brand_label`
- `/brand-update-existing` pushes current brand to all running instances
- `/brand-create` + `/brand-use` for multiple brands

### Custom MOTD template

```text
/brand-template Welcome to {brand_name}
CPU: {cpu} · Disk: {disk}
{website}
```

Dynamic vars: `{hostname} {os} {kernel} {uptime} {cpu} {ram_used} {ram_total} {ram_percent} {disk} {ip} {users} {processes}`  
Static vars: `{brand_name} {tagline} {footer} {website} {discord} {support}`

---

## MOTD installer

Idempotent by design:

1. One-time backups: `*.backup-brand`
2. Default `update-motd.d` scripts moved to `/etc/update-motd.d.disabled` once
3. Single marked `pam_motd` line (no duplicates)
4. Installer at `/etc/update-motd.d/00-brand-motd`
5. Reports `MOTD_OK` on success

Branding failure never fails a created VPS — retry with `/brand-reinstall`.

---

## Invite rules

- Credit only when invite use increase is unambiguous and inviter is known
- Duplicate joins do not double-credit
- Leave → inviter count decreases, attribution cleared
- Completion DM sent once until `/resetinvites`

---

## LXD provider

- Creates labeled instances on network `vexdeploy`
- Limits: `limits.memory`, `limits.cpu`, best-effort root `size=` disk override
- Bootstraps OpenSSH inside the instance and starts `sshd`
- Password generated per VPS, stored for DM delivery only
- Friendly image keys map to remotes: `ubuntu:22.04`, `images:debian/12`, `images:alpine/3.20/cloud`

```text
user.vexdeploy.managed=1
user.vexdeploy.owner=<user_id>
user.vexdeploy.vps_id=<id>
```

---

## Security

- Secrets only in `.env` (gitignored)
- No credentials in logs or public channels
- Admin gate: `ADMIN_IDS` ∪ `ADMIN_ROLE_ID` ∪ Discord Administrator
- Blacklist + cooldown + per-user/global caps before provision

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Invites not counting | Enable Server Members Intent; bot needs Manage Guild |
| Disk validation error | Use ≥5GB (`/createvps` defaults to 10GB) |
| LXD offline | Bot user needs `lxc`/`incus` access (set `LXD_CLI` if not on PATH) |
| MOTD missing | `/refresh-motd` or `/brand-reinstall` |
| Cannot DM credentials | User must allow DMs from server members |
| Admin panel not starting | Set `ADMIN_PANEL_ENABLED=1`, `pip install flask`, check the port is free |
| Admin panel login rejected | Generate a fresh code with `/admin login` (codes expire and are single-use) |

---

## License

All rights reserved unless a license file is added later.
