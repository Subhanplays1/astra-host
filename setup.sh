#!/usr/bin/env bash
# VexDeploy — one-shot host bootstrap
# Installs apt packages (git, python, openssh, …), clones/updates repo,
# runs setup_lxd.sh, creates venv, installs pip deps, optional systemd unit.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Arion-Team/VexDeploy.git}"
INSTALL_DIR="${INSTALL_DIR:-}"
WITH_LXD="${WITH_LXD:-1}"
WITH_SYSTEMD="${WITH_SYSTEMD:-0}"
SKIP_CLONE="${SKIP_CLONE:-0}"
# drop into activated .venv shell when setup finishes (tty only)
ENTER_VENV="${ENTER_VENV:-1}"
# PEP 668 fallback when venv is unavailable
PIP_BREAK_SYSTEM_PACKAGES="${PIP_BREAK_SYSTEM_PACKAGES:-1}"

log()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    OK: %s\n' "$*"; }
warn() { printf '    WARN: %s\n' "$*"; }
die()  { printf '    ERROR: %s\n' "$*" >&2; exit 1; }

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
  fi
}

apt_ok() { "$@" >/dev/null 2>&1; }

pip_install() {
  # Prefer active venv pip; else system pip with --break-system-packages (Debian/Ubuntu PEP 668)
  local extra=()
  if [[ "$PIP_BREAK_SYSTEM_PACKAGES" == "1" ]]; then
    extra+=(--break-system-packages)
  fi

  if [[ -n "${VENV_PIP:-}" && -x "${VENV_PIP}" ]]; then
    "$VENV_PIP" install "$@" && return 0
    warn "venv pip failed — retrying with --break-system-packages"
  fi

  if python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install "${extra[@]}" "$@" && return 0
    # last try without the flag (older pip)
    python3 -m pip install "$@" && return 0
  fi

  if command -v pip3 >/dev/null 2>&1; then
    pip3 install "${extra[@]}" "$@" && return 0
    pip3 install "$@" && return 0
  fi

  die "pip install failed: $*"
}

detect_os() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-debian}"
    OS_VER_ID="${VERSION_ID:-}"
    OS_CODENAME="${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}"
  else
    OS_ID="unknown"
    OS_VER_ID=""
    OS_CODENAME=""
  fi
  log "OS: $OS_ID $OS_VER_ID ($OS_CODENAME)"
}

install_apt_packages() {
  log "Installing apt packages (git, python, ssh, curl, …)"
  export DEBIAN_FRONTEND=noninteractive

  apt-get update -qq || true

  # core
  apt-get install -y -qq \
    ca-certificates \
    curl \
    wget \
    gnupg \
    gpg \
    jq \
    git \
    unzip \
    zip \
    tar \
    bzip2 \
    xz-utils \
    sudo \
    less \
    nano \
    vim-tiny \
    procps \
    psmisc \
    net-tools \
    iproute2 \
    iputils-ping \
    traceroute \
    dnsutils \
    openssh-client \
    openssh-server \
    openssl \
    lsb-release \
    apt-transport-https \
    software-properties-common \
    || warn "Some core packages failed (continuing)"

  # python
  apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-full \
    python3-dev \
    || apt-get install -y -qq python3 python3-pip python3-venv \
    || die "python3 install failed"

  # build helpers (psutil may need them on some hosts)
  apt-get install -y -qq \
    build-essential \
    gcc \
    make \
    pkg-config \
    libffi-dev \
    libssl-dev \
    || warn "build-essential optional packages failed"

  # file manager / MOTD / tunnels inside guest prep (host tools)
  apt-get install -y -qq \
    base-files \
    sudo \
    || true

  # useful ops tools
  apt-get install -y -qq \
    htop \
    tmux \
    rsync \
    screen \
    || warn "optional ops tools failed"

  # ensure ssh client for localhost.run file manager tunnel
  command -v ssh >/dev/null 2>&1 || apt-get install -y -qq openssh-client || true

  ok "apt packages installed"
}

verify_tools() {
  log "Verifying tools"
  local missing=()
  local t
  for t in git python3 pip3 curl wget ssh; do
    if command -v "$t" >/dev/null 2>&1; then
      ok "$t → $(command -v "$t")"
    else
      missing+=("$t")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing: ${missing[*]}"
  fi
  ok "Python $(python3 --version 2>&1)"
}

resolve_install_dir() {
  local script_dir
  script_dir="$(cd "$(dirname "$0")" && pwd)"

  if [[ -n "$INSTALL_DIR" ]]; then
    cd "$INSTALL_DIR"
  elif [[ -f "$script_dir/bot.py" ]]; then
    cd "$script_dir"
  elif [[ -f "$PWD/bot.py" ]]; then
    : # already in repo
  else
    INSTALL_DIR="${HOME}/VexDeploy"
    log "Repo dir not found — will clone to $INSTALL_DIR"
  fi
}

clone_or_update() {
  if [[ "$SKIP_CLONE" == "1" ]]; then
    warn "SKIP_CLONE=1 — not cloning"
    [[ -f bot.py ]] || die "No bot.py in $(pwd) and clone skipped"
    return
  fi

  if [[ -f bot.py ]]; then
    log "Repo present: $(pwd)"
    if [[ -d .git ]]; then
      git pull --ff-only || warn "git pull failed (ok if local changes)"
    fi
    return
  fi

  if [[ -z "${INSTALL_DIR:-}" ]]; then
    INSTALL_DIR="${HOME}/VexDeploy"
  fi
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Updating $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only || warn "git pull failed"
    cd "$INSTALL_DIR"
  else
    log "Cloning $REPO_URL → $INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi
  ok "Working dir: $(pwd)"
}

setup_lxd() {
  if [[ "$WITH_LXD" != "1" ]]; then
    warn "WITH_LXD=0 — skipping Incus/LXD"
    return
  fi
  if [[ -f setup_lxd.sh ]]; then
    log "Running setup_lxd.sh"
    bash setup_lxd.sh
  else
    warn "setup_lxd.sh missing"
  fi
}

setup_venv() {
  log "Creating Python venv + installing requirements"
  VENV_DIR="$(pwd)/.venv"
  VENV_PIP=""
  VENV_MODE="system"

  # ensure pip module exists
  if ! python3 -m pip --version >/dev/null 2>&1; then
    apt-get install -y -qq python3-pip python3-venv python3-full 2>/dev/null || true
  fi

  if [[ ! -d "$VENV_DIR" ]]; then
    if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
      warn "venv create failed — installing python3-venv"
      apt-get install -y -qq python3-venv python3-full 2>/dev/null || true
      python3 -m venv "$VENV_DIR" 2>/dev/null || warn "venv still unavailable"
    fi
  fi

  if [[ -x "$VENV_DIR/bin/pip" ]]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    VENV_PIP="$VENV_DIR/bin/pip"
    VENV_MODE="venv"
    ok "venv activated: $VENV_DIR"
    VENV_PIP install --upgrade pip wheel setuptools || \
      pip_install --upgrade pip wheel setuptools
    if [[ -f requirements.txt ]]; then
      VENV_PIP install -r requirements.txt || pip_install -r requirements.txt
    else
      VENV_PIP install "discord.py>=2.3.0" "python-dotenv>=1.0.0" "psutil>=5.9.0" \
        || pip_install "discord.py>=2.3.0" "python-dotenv>=1.0.0" "psutil>=5.9.0"
    fi
    ok "deps installed in venv"
  else
    warn "No venv — falling back to system pip --break-system-packages"
    export PIP_BREAK_SYSTEM_PACKAGES=1
    # some older pip don't know the flag; pip_install also retries without it
    python3 -m pip install --break-system-packages --upgrade pip wheel setuptools 2>/dev/null \
      || python3 -m pip install --upgrade pip wheel setuptools || true
    if [[ -f requirements.txt ]]; then
      pip_install -r requirements.txt
    else
      pip_install "discord.py>=2.3.0" "python-dotenv>=1.0.0" "psutil>=5.9.0"
    fi
    VENV_MODE="system-pip"
    ok "deps installed system-wide (--break-system-packages)"
  fi

  echo "$VENV_MODE" > "$(pwd)/.vex-venv-mode"
}

enter_venv_shell() {
  if [[ "$ENTER_VENV" != "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    return 0
  fi
  if [[ -f "$(pwd)/.venv/bin/activate" ]]; then
    log "Entering venv shell (.venv) — type 'exit' to leave"
    # shellcheck disable=SC1091
    source "$(pwd)/.venv/bin/activate"
    export VENV_AUTO_ENTERED=1
    exec bash --noprofile --norc -c '
      if [ -f .venv/bin/activate ]; then . .venv/bin/activate; fi
      echo "VexDeploy venv active: python=$(command -v python) pip=$(command -v pip)"
      echo "Run: python bot.py   |   exit to leave venv"
      exec bash -i
    '
  fi
  warn "No .venv to enter (system pip mode)"
}

setup_env() {
  if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
      cp .env.example .env
      ok "Created .env from .env.example — set DISCORD_TOKEN"
    else
      warn ".env.example missing"
    fi
  else
    ok ".env already exists"
  fi
}

setup_systemd() {
  if [[ "$WITH_SYSTEMD" != "1" ]]; then
    warn "WITH_SYSTEMD=0 — skipping systemd unit"
    return
  fi
  command -v systemctl >/dev/null 2>&1 || { warn "no systemctl"; return; }

  local unit=/etc/systemd/system/vexdeploy.service
  local workdir
  workdir="$(pwd)"
  local py
  py="${workdir}/.venv/bin/python"
  [[ -x "$py" ]] || py="$(command -v python3)"

  log "Writing $unit"
  cat > "$unit" <<EOF
[Unit]
Description=VexDeploy Discord VPS bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${workdir}
ExecStart=${py} bot.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
User=root
Group=root
# limit noise; bot needs root for incus/lxc socket access
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable vexdeploy.service
  ok "systemd unit installed (start with: systemctl start vexdeploy)"
}

print_next() {
  local mode
  mode="$(cat "$(pwd)/.vex-venv-mode" 2>/dev/null || echo unknown)"
  echo ""
  echo "=============================================="
  echo " Done."
  echo " Working dir: $(pwd)"
  echo " Python mode: $mode"
  echo ""
  echo " Next:"
  echo "   1) edit .env  → set DISCORD_TOKEN"
  if [[ -f "$(pwd)/.venv/bin/activate" ]]; then
    echo "   2) source .venv/bin/activate   # auto if ENTER_VENV=1"
    echo "   3) python bot.py"
  else
    echo "   2) python3 bot.py   # system pip (--break-system-packages)"
    echo "      pip fallback: python3 -m pip install --break-system-packages -r requirements.txt"
  fi
  echo "      or: systemctl start vexdeploy   (if WITH_SYSTEMD=1)"
  echo ""
  echo " Re-run LXD only:  sudo bash setup_lxd.sh"
  echo " Full re-run:      sudo bash setup.sh"
  echo " With systemd:      sudo WITH_SYSTEMD=1 bash setup.sh"
  echo " No auto-venv:     ENTER_VENV=0 sudo -E bash setup.sh"
  echo "=============================================="
}

main() {
  ensure_root "$@"

  log "VexDeploy host bootstrap"
  detect_os
  install_apt_packages
  verify_tools
  resolve_install_dir
  clone_or_update
  setup_lxd
  setup_venv
  setup_env
  setup_systemd
  print_next
  enter_venv_shell
}

main "$@"
