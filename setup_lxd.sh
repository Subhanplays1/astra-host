#!/usr/bin/env bash
# VexDeploy — auto-setup LXD/Incus for the bot
set -euo pipefail

NETWORK="${LXD_NETWORK:-vexdeploy}"
STORAGE="${LXD_STORAGE:-}"
ENV_FILE="${ENV_FILE:-.env}"

log()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    OK: %s\n' "$*"; }
warn() { printf '    WARN: %s\n' "$*"; }
die()  { printf '    ERROR: %s\n' "$*" >&2; exit 1; }

detect_cli() {
  if [[ -n "${LXD_CLI:-}" ]]; then
    echo "$LXD_CLI"
    return
  fi
  if command -v incus >/dev/null 2>&1; then
    echo "incus"
  elif command -v lxc >/dev/null 2>&1; then
    echo "lxc"
  elif [[ -x /snap/bin/lxc ]]; then
    echo "/snap/bin/lxc"
  elif [[ -x /usr/bin/incus ]]; then
    echo "/usr/bin/incus"
  else
    echo ""
  fi
}

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
  fi
}

apt_try() {
  # run apt without aborting the whole script under set -e
  "$@" && return 0
  return 1
}

enable_backports() {
  if grep -qE 'bookworm-backports' /etc/apt/sources.list 2>/dev/null; then
    return 0
  fi
  if ls /etc/apt/sources.list.d/*.list >/dev/null 2>&1 \
    && grep -qE 'bookworm-backports' /etc/apt/sources.list.d/*.list 2>/dev/null; then
    return 0
  fi
  if ls /etc/apt/sources.list.d/*.sources >/dev/null 2>&1 \
    && grep -qE 'bookworm-backports' /etc/apt/sources.list.d/*.sources 2>/dev/null; then
    return 0
  fi
  echo "deb http://deb.debian.org/debian bookworm-backports main" \
    > /etc/apt/sources.list.d/bookworm-backports.list
}

install_incus_base() {
  # containers only — VexDeploy never uses --vm, so we skip qemu/backports VM deps
  local tried=0

  log "Trying incus-base (containers only)"
  if apt_try apt-get install -y -qq incus-base; then
    ok "incus-base installed (distro repo)"
    return 0
  fi
  tried=1

  if apt-cache show incus-base >/dev/null 2>&1 || apt-cache show incus >/dev/null 2>&1; then
    log "Trying bookworm-backports (incus-base)"
    enable_backports
    apt_try apt-get update -qq || true
    if apt_try apt-get install -y -qq -t bookworm-backports incus-base; then
      ok "incus-base installed (bookworm-backports)"
      return 0
    fi
    if apt_try apt-get install -y -qq -t bookworm-backports incus-base \
      || apt_try apt-get install -y -qq -t bookworm-backports \
         incus-base incus-client incus-agent; then
      ok "incus-base installed (bookworm-backports)"
      return 0
    fi
  fi

  log "Adding Zabbly Incus apt repository"
  . /etc/os-release
  install -d -m 0755 /etc/apt/keyrings
  if ! curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc; then
    warn "Could not download Zabbly key"
  else
    local arch
    arch="$(dpkg --print-architecture)"
    cat > /etc/apt/sources.list.d/zabbly-incus-stable.sources <<EOF
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: ${VERSION_CODENAME}
Components: main
Architectures: ${arch}
Signed-By: /etc/apt/keyrings/zabbly.asc
EOF
    echo "deb [signed-by=/etc/apt/keyrings/zabbly.asc] https://pkgs.zabbly.com/incus/stable ${VERSION_CODENAME} main" \
      > /etc/apt/sources.list.d/incus.list
    apt_try apt-get update -qq || true
    if apt_try apt-get install -y -qq incus-base; then
      ok "incus-base installed (Zabbly)"
      return 0
    fi
    if apt_try apt-get install -y -qq incus; then
      ok "incus installed (Zabbly)"
      return 0
    fi
  fi

  # last-ditch: full package name variants
  apt_try apt-get install -y -qq -t bookworm-backports \
    incus-base incus-client || true

  command -v incus >/dev/null 2>&1
}

install_backend() {
  if [[ -n "$(detect_cli)" ]]; then
    ok "CLI already present: $(detect_cli)"
    return
  fi

  log "No lxc/incus found — installing Incus"
  export DEBIAN_FRONTEND=noninteractive
  apt_try apt-get update -qq || true

  if ! install_incus_base; then
    log "apt Incus failed — trying snap LXD"
    apt_try apt-get install -y -qq snapd apparmor || true
    systemctl enable --now snapd snapd.apparmor snapd.socket 2>/dev/null || true
    sleep 2
    snap install lxd || true
  fi

  # full package if base missing binary but full is fine
  if ! command -v incus >/dev/null 2>&1; then
    apt_try apt-get install -y -qq -t bookworm-backports incus || true
  fi

  [[ -n "$(detect_cli)" ]] || die "Install failed. Try manually:
  apt-get install -y -t bookworm-backports incus-base
  # or: apt-get install incus-base
  # or: snap install lxd
  then set LXD_CLI in .env"
  ok "Installed: $(detect_cli)"
}

init_daemon() {
  local cli
  cli="$(detect_cli)"
  log "Initializing $cli (if needed)"

  if ! "$cli" version >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl enable --now "snapd" 2>/dev/null || true
      systemctl enable --now "incus" "incus.socket" 2>/dev/null || true
      systemctl start "incus" 2>/dev/null || true
      systemctl start "incus.socket" 2>/dev/null || true
      sleep 2
    fi
  fi

  if ! "$cli" version >/dev/null 2>&1; then
    # first-use may auto-start for snap lxc; incus needs daemon
    if [[ "$cli" == *incus* ]]; then
      incus admin init --auto >/dev/null 2>&1 || true
    elif [[ "$cli" == *lxc* ]]; then
      lxd init --auto >/dev/null 2>&1 || true
    fi
    sleep 2
  fi

  if ! "$cli" version >/dev/null 2>&1; then
    if command -v incus >/dev/null 2>&1; then
      incus admin init --auto >/dev/null 2>&1 || true
    fi
    if command -v lxd >/dev/null 2>&1; then
      lxd init --auto >/dev/null 2>&1 || true
    fi
    if command -v snap >/dev/null 2>&1; then
      snap start lxd 2>/dev/null || true
    fi
    sleep 3
  fi

  "$cli" version >/dev/null 2>&1 || die "$cli daemon not responding (try: snap start lxd / systemctl start incus)"
  ok "$cli is up ($("$cli" version | head -n1))"
}

ensure_group() {
  local cli group_user
  cli="$(detect_cli)"
  group_user="${SUDO_USER:-${USER:-root}}"

  local grp="lxd"
  if [[ "$cli" == *incus* ]]; then
    grp="incus"
  fi

  if getent group "$grp" >/dev/null 2>&1; then
    if ! id -nG "$group_user" 2>/dev/null | tr ' ' '\n' | grep -qx "$grp"; then
      usermod -aG "$grp" "$group_user"
      warn "Added $group_user to group $grp (re-login may be needed)"
    else
      ok "$group_user already in $grp"
    fi
  fi
}

ensure_remote() {
  local cli name url proto
  cli="$(detect_cli)"
  name="$1"
  url="$2"
  proto="$3"

  if "$cli" remote get-url "$name" >/dev/null 2>&1; then
    ok "remote $name exists ($("$cli" remote get-url "$name"))"
    return
  fi

  log "Adding remote $name → $url"
  if "$cli" remote add "$name" "$url" --protocol "$proto" --force-remote >/dev/null 2>&1 \
    || "$cli" remote add "$name" "$url" --protocol "$proto" >/dev/null 2>&1; then
    ok "remote $name added"
  else
    warn "Could not add remote $name — trying image pull fallback later"
  fi
}

ensure_remotes() {
  local cli
  cli="$(detect_cli)"

  # images remote — primary for debian/alpine/ubuntu fallbacks
  ensure_remote "images" "https://images.linuxcontainers.org" "simplestreams"

  # optional ubuntu cloud-images remote
  if "$cli" remote get-url "ubuntu" >/dev/null 2>&1; then
    ok "remote ubuntu exists"
  else
    ensure_remote "ubuntu" "https://cloud-images.ubuntu.com/releases" "simplestreams" || true
  fi

  # prove images: works (bot prefers images:ubuntu/22.04)
  if "$cli" image list "images:" >/dev/null 2>&1 || "$cli" remote get-url "images" >/dev/null 2>&1; then
    ok "images remote usable"
  else
    warn "images remote may be broken — create may fail"
  fi
}

ensure_network() {
  local cli
  cli="$(detect_cli)"

  if "$cli" network show "$NETWORK" >/dev/null 2>&1; then
    ok "network $NETWORK exists"
    return
  fi

  log "Creating managed bridge network: $NETWORK"
  if "$cli" network create "$NETWORK" ipv4.address=auto ipv4.nat=true ipv6.address=none >/dev/null 2>&1; then
    ok "network $NETWORK created"
  else
    # maybe created without ipv6 flag on older builds
    if "$cli" network create "$NETWORK" ipv4.address=auto ipv4.nat=true >/dev/null 2>&1; then
      ok "network $NETWORK created"
    else
      warn "network create failed (may already exist): $("$cli" network show "$NETWORK" 2>&1 | head -n3)"
    fi
  fi
}

ensure_storage() {
  local cli
  cli="$(detect_cli)"

  # list pool names (csv)
  local pools
  pools="$("$cli" storage list --format csv -c n 2>/dev/null | tr -d '"' || true)"
  if [[ -n "$pools" ]]; then
    if [[ -z "$STORAGE" ]]; then
      # prefer known names
      local p
      for p in default vexdeploy local backend; do
        if echo "$pools" | tr ',' '\n' | grep -qx "$p"; then
          STORAGE="$p"
          break
        fi
      done
      if [[ -z "$STORAGE" ]]; then
        STORAGE="$(echo "$pools" | head -n1 | cut -d, -f1)"
      fi
    fi
    ok "storage pool: $STORAGE (available: $(echo "$pools" | tr '\n' ' '))"
    return
  fi

  log "No storage pool — creating default (dir)"
  if "$cli" storage create default dir >/dev/null 2>&1 \
    || "$cli" storage create vexdeploy dir >/dev/null 2>&1; then
    STORAGE="${STORAGE:-default}"
    ok "created storage pool $STORAGE"
  else
    # maybe partially created
    if "$cli" storage show default >/dev/null 2>&1; then
      STORAGE="default"
    elif "$cli" storage show vexdeploy >/dev/null 2>&1; then
      STORAGE="vexdeploy"
    fi
    if [[ -n "$STORAGE" ]]; then
      ok "storage pool exists: $STORAGE"
    else
      warn "could not create storage pool — launch may fail with 'No root device'"
      echo "    Manual: $cli storage create default dir"
    fi
  fi
}

warm_images() {
  local cli
  cli="$(detect_cli)"
  log "Prefetching images:ubuntu/22.04 (best-effort)"

  "$cli" image list "images:" >/dev/null 2>&1 || true

  if "$cli" image list "images:ubuntu/22.04" >/dev/null 2>&1 \
    || "$cli" image info "images:ubuntu/22.04" >/dev/null 2>&1 \
    || "$cli" image list "ubuntu:22.04" >/dev/null 2>&1; then
    ok "ubuntu 22.04 image resolvable"
  else
    warn "Could not list ubuntu image — check network / remotes"
    echo "    Manual test:"
    echo "      $cli remote list"
    echo "      $cli image list images:ubuntu/22.04"
  fi
}

update_env() {
  local cli env_path
  cli="$(detect_cli)"
  env_path="$ENV_FILE"

  [[ -f "$env_path" ]] || touch "$env_path"

  if grep -q '^LXD_CLI=' "$env_path"; then
    sed -i "s|^LXD_CLI=.*|LXD_CLI=$cli|" "$env_path"
  else
    echo "LXD_CLI=$cli" >> "$env_path"
  fi

  if grep -q '^LXD_NETWORK=' "$env_path"; then
    sed -i "s|^LXD_NETWORK=.*|LXD_NETWORK=$NETWORK|" "$env_path"
  else
    echo "LXD_NETWORK=$NETWORK" >> "$env_path"
  fi

  if [[ -n "$STORAGE" ]]; then
    if grep -q '^LXD_STORAGE=' "$env_path"; then
      sed -i "s|^LXD_STORAGE=.*|LXD_STORAGE=$STORAGE|" "$env_path"
    else
      echo "LXD_STORAGE=$STORAGE" >> "$env_path"
    fi
  fi

  ok "Updated $env_path (LXD_CLI=$cli, LXD_NETWORK=$NETWORK, LXD_STORAGE=$STORAGE)"
}

verify() {
  local cli
  cli="$(detect_cli)"
  log "Verification"
  "$cli" version >/dev/null || die "CLI failed"
  ok "CLI: $cli"

  "$cli" remote list >/dev/null 2>&1 || true
  echo "    Remotes:"
  "$cli" remote list 2>/dev/null | sed 's/^/      /' || true

  if "$cli" network show "$NETWORK" >/dev/null 2>&1; then
    ok "network $NETWORK"
  else
    warn "network $NETWORK missing"
  fi

  if [[ -n "$STORAGE" ]] && "$cli" storage show "$STORAGE" >/dev/null 2>&1; then
    ok "storage $STORAGE"
  else
    warn "storage pool missing — create with: $cli storage create default dir"
  fi

  echo ""
  echo "Done. Next:"
  echo "  cd $(pwd)"
  echo "  git pull"
  echo "  # ensure .env has LXD_CLI=$cli"
  echo "  systemctl restart vexdeploy   # or: python bot.py"
  echo "  In Discord: /admin_stats  → LXD should be online"
}

main() {
  ensure_root "$@"
  cd "$(dirname "$0")"

  log "VexDeploy LXD/Incus setup"
  install_backend
  init_daemon
  ensure_group
  ensure_remotes
  ensure_network
  warm_images
  update_env
  verify
}

main "$@"
