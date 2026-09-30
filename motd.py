"""MOTD script generation and installation for Astra Host."""

from __future__ import annotations

import base64
import logging
import secrets
from pathlib import Path
from typing import Any

from config import BRAND_NAME

logger = logging.getLogger("vexdeploy.motd")

_EOF = "VEX_MOTD_EOF"
_MARKER = "VEXDEPLOY_MOTD"

_template_path = Path(__file__).resolve().parent / "templates" / "motd.sh"


def generate_ascii_logo(text: str, width: int = 56) -> str:
    text = (text or "").strip() or BRAND_NAME
    font = {
        "A": ["  #  ", " # # ", "#####", "#   #", "#   #"],
        "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
        "C": [" ####", "#    ", "#    ", "#    ", " ####"],
        "D": ["#### ", "#   #", "#   #", "#   #", "#### "],
        "E": ["#####", "#    ", "#### ", "#    ", "#####"],
        "F": ["#####", "#    ", "#### ", "#    ", "#    "],
        "G": [" ####", "#    ", "#  ##", "#   #", " ####"],
        "H": ["#   #", "#   #", "#####", "#   #", "#   #"],
        "I": ["#####", "  #  ", "  #  ", "  #  ", "#####"],
        "J": ["    #", "    #", "    #", "#   #", " ####"],
        "K": ["#   #", "#  # ", "###  ", "#  # ", "#   #"],
        "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
        "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
        "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
        "O": [" ### ", "#   #", "#   #", "#   #", " ### "],
        "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
        "Q": [" ### ", "#   #", "# # #", "#  # ", " ## #"],
        "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
        "S": [" ####", "#    ", " ### ", "    #", "#### "],
        "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
        "U": ["#   #", "#   #", "#   #", "#   #", " ### "],
        "V": ["#   #", "#   #", "#   #", " # # ", "  #  "],
        "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
        "X": ["#   #", " # # ", "  #  ", " # # ", "#   #"],
        "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "],
        "Z": ["#####", "   # ", "  #  ", " #   ", "#####"],
        "0": [" ### ", "#  ##", "# # #", "##  #", " ### "],
        "1": ["  #  ", " ##  ", "  #  ", "  #  ", " ### "],
        "2": [" ### ", "#   #", "   # ", "  #  ", "#####"],
        "3": ["#### ", "    #", " ### ", "    #", "#### "],
        "4": ["#  # ", "#  # ", "#####", "   # ", "   # "],
        "5": ["#####", "#    ", "#### ", "    #", "#### "],
        "6": [" ### ", "#    ", "#### ", "#   #", " ### "],
        "7": ["#####", "    #", "   # ", "  #  ", "  #  "],
        "8": [" ### ", "#   #", " ### ", "#   #", " ### "],
        "9": [" ### ", "#   #", " ####", "    #", " ### "],
        " ": ["     ", "     ", "     ", "     ", "     "],
        "-": ["     ", "     ", " ### ", "     ", "     "],
        ".": ["     ", "     ", "     ", "     ", "  #  "],
    }

    max_chars = max(1, min(len(text), (width - 4) // 6))
    sample = text.upper()[:max_chars]

    rows = [""] * 5
    for ch in sample:
        glyph = font.get(ch, font[" "])
        for i in range(5):
            rows[i] += glyph[i] + " "

    if not any(r.strip() for r in rows):
        return framed(text, width)

    out = ["  " + r.rstrip() for r in rows]
    return "\n".join(out)


def framed(text: str, width: int = 56) -> str:
    text = (text or "").strip() or BRAND_NAME
    inner = width - 4
    if len(text) > inner:
        text = text[:inner]
    pad = inner - len(text)
    left = pad // 2
    right = pad - left
    return (
        "+" + "-" * (width - 2) + "+\n"
        "| " + " " * left + text + " " * right + " |\n"
        "+" + "-" * (width - 2) + "+"
    )


def _ansi(code: str) -> str:
    return f"\\033[{code}m"


def build_motd_script(brand: dict[str, Any]) -> str:
    name = str(brand.get("brand_name") or BRAND_NAME)
    tagline = str(brand.get("brand_tagline") or "")
    footer = str(brand.get("footer") or "")
    website = str(brand.get("website") or "")
    discord_url = str(brand.get("discord") or "")
    support = str(brand.get("support_email") or "")
    logo = str(brand.get("logo") or "AUTO")
    template = str(brand.get("motd_template") or "").strip()

    from config import ANSI_COLORS

    p = ANSI_COLORS.get(str(brand.get("primary_color", "cyan")), "0;36")
    s = ANSI_COLORS.get(str(brand.get("secondary_color", "magenta")), "0;35")

    if logo.upper() in {"NONE", "OFF"}:
        logo_block = ""
    elif logo.upper() == "AUTO" or not logo:
        # the box rule is 52 wide — render just the first word of the brand
        word = name.split()[0] if name.split() else name
        logo_block = generate_ascii_logo(word)
    else:
        logo_block = logo

    if template:
        return _script_from_template(brand, template, p, s)

    footer_line = footer or (f"Need help? {support}" if support else "")

    logo_export = ""
    if logo_block:
        logo_export = (
            "cat <<'" + _EOF + "_LOGO'\n" + logo_block + "\n" + _EOF + "_LOGO\n"
        )

    # tagline sits under the wordmark unless the footer already carries it
    show_tagline = bool(tagline) and tagline.strip() != footer_line.strip()
    tagline_echo = (
        f'echo "${{S}}{tagline}${{R}}"' if show_tagline else 'echo ""'
    )
    footer_echo = (
        f'echo "${{S}}{footer_line}${{R}}"' if footer_line else 'echo ""'
    )
    support_echo = (
        f'echo "${{B}}Support${{R}}  {support}"' if support else 'echo ""'
    )
    web_echo = (
        f'echo "${{B}}Website${{R}}  {website}"' if website else 'echo ""'
    )
    disc_echo = (
        f'echo "${{B}}Discord${{R}}  {discord_url}"' if discord_url else 'echo ""'
    )

    return f"""#!/bin/bash
# {_MARKER} v1 brand={name}
P="{_ansi(p)}"
S="{_ansi(s)}"
B="\\033[1m"
R="\\033[0m"

HOST="$(hostname 2>/dev/null || echo vex)"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS="${{PRETTY_NAME:-Linux}}"
else
  OS="$(uname -s)"
fi
KERNEL="$(uname -r)"
UPTIME_STR="$(uptime -p 2>/dev/null || uptime | sed 's/.*up /up /')"
CPU="$(nproc 2>/dev/null || echo 1)"
MEM_TOTAL="$(free -h 2>/dev/null | awk '/Mem:/{{print $2}}')"
MEM_USED="$(free -h 2>/dev/null | awk '/Mem:/{{print $3}}')"
MEM_PCT="$(free | awk '/Mem:/{{printf "%.0f", $3/$2*100}}' 2>/dev/null || echo 0)"
DISK="$(df -h / 2>/dev/null | awk 'NR==2{{print $3"/"$2" ("$5)"}}')"
IP="$(hostname -I 2>/dev/null | awk '{{print $1}}')"
LOAD="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo n/a)"
USERS="$(who 2>/dev/null | wc -l)"
PROCS="$(ps -e --no-headers 2>/dev/null | wc -l)"
PROVIDER="{name}"
TAGLINE="{tagline}"

echo ""
{logo_export}
echo "${{P}}${{B}}${{PROVIDER}}${{R}}  ${{S}}— FREE VPS HOSTING${{R}}"
{tagline_echo}
echo "${{P}}────────────────────────────────────────────────────${{R}}"
echo "${{B}} Provider${{R}}  $PROVIDER"
echo "${{B}} Host${{R}}     $HOST"
echo "${{B}} OS${{R}}       $OS"
echo "${{B}} Kernel${{R}}   $KERNEL"
echo "${{B}} Uptime${{R}}   $UPTIME_STR"
echo "${{B}} CPU${{R}}      $CPU core(s)  load $LOAD"
echo "${{B}} Memory${{R}}   $MEM_USED/$MEM_TOTAL ($MEM_PCT%)"
echo "${{B}} Disk${{R}}     $DISK"
echo "${{B}} Processes${{R}} $PROCS"
echo "${{B}} Users${{R}}    $USERS online"
echo "${{B}} IP${{R}}       $IP"
echo "${{P}}────────────────────────────────────────────────────${{R}}"
{web_echo}
{disc_echo}
{support_echo}
{footer_echo}
echo ""
"""


def build_issue_script(brand: dict[str, Any]) -> str:
    """Console/login issue banner (no ANSI required — serial/getty)."""
    name = str(brand.get("brand_name") or BRAND_NAME)
    tagline = str(brand.get("brand_tagline") or "")
    footer = str(brand.get("footer") or "")
    website = str(brand.get("website") or "")
    support = str(brand.get("support_email") or "")
    lines = [
        "",
        f"  {name} — FREE VPS HOSTING",
    ]
    if tagline and tagline.strip() != footer.strip():
        lines.append(f"  {tagline}")
    lines.append("  " + "-" * min(50, max(20, len(name) + 20)))
    if website:
        lines.append(f"  Website:  {website}")
    if support:
        lines.append(f"  Support:  {support}")
    lines.append(f"  {footer or tagline or name}")
    lines.append("")
    content = "\n".join(lines)
    b64 = base64.b64encode(content.encode()).decode()
    return (
        f"echo {b64} | base64 -d > /etc/issue && "
        f"echo {b64} | base64 -d > /etc/issue.net && "
        f"chmod 644 /etc/issue /etc/issue.net && echo ISSUE_OK"
    )


def _script_from_template(
    brand: dict[str, Any], template: str, p_code: str, s_code: str
) -> str:
    replacements = {
        "brand_name": str(brand.get("brand_name", BRAND_NAME)),
        "tagline": str(brand.get("brand_tagline", "")),
        "footer": str(brand.get("footer", "")),
        "website": str(brand.get("website", "")),
        "discord": str(brand.get("discord", "")),
        "support": str(brand.get("support_email", "")),
        "hostname": "$(hostname)",
        "os": '$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)',
        "kernel": "$(uname -r)",
        "uptime": "$(uptime -p 2>/dev/null || uptime)",
        "cpu": "$(nproc 2>/dev/null || echo 1)",
        "ram_used": "$(free -h 2>/dev/null | awk '/Mem:/ {print $3}')",
        "ram_total": "$(free -h 2>/dev/null | awk '/Mem:/ {print $2}')",
        "ram_percent": "$(free 2>/dev/null | awk '/Mem:/ {printf \"%d\", $3/$2*100}')",
        "disk": "$(df -h / 2>/dev/null | awk 'NR==2 {print $3\"/\"$2\" (\"$5)\")}')",
        "ip": "$(hostname -I 2>/dev/null | awk '{print $1}')",
        "users": "$(who 2>/dev/null | wc -l)",
        "processes": "$(ps -e --no-headers 2>/dev/null | wc -l)",
    }

    body = template
    for key, expr in replacements.items():
        body = body.replace("{" + key + "}", expr)

    return f"""#!/bin/bash
# {_MARKER} v1 template brand={brand.get('brand_name')}
P="{_ansi(p_code)}"
S="{_ansi(s_code)}"
B="\\033[1m"
R="\\033[0m"
echo ""
echo "${{P}}{name_echo(brand)}${{R}}"
{f'echo "${{S}}{brand.get("tagline", brand.get("brand_tagline", ""))}${{R}}"' if brand.get("brand_tagline") else ''}
cat <<'{_EOF}'
{body}
{_EOF}
echo "${{S}}{str(brand.get("footer") or "")}${{R}}"
echo ""
"""


def name_echo(brand: dict[str, Any]) -> str:
    return str(brand.get("brand_name") or BRAND_NAME)


def build_installer(brand: dict[str, Any]) -> str:
    script = build_motd_script(brand)
    brand_name = str(brand.get("brand_name") or BRAND_NAME)
    issue_cmd = build_issue_script(brand)
    return f"""#!/bin/bash
set -e
# {_MARKER} installer brand={brand_name}

# backups (once)
for f in /etc/pam.d/sshd /etc/pam.d/login /etc/motd; do
  if [ -f "$f" ] && [ ! -f "$f.backup-brand" ]; then
    cp -a "$f" "$f.backup-brand"
  fi
done

# branded console/login issue banners
{issue_cmd} || true

# disable default MOTD scripts (once)
if [ -d /etc/update-motd.d ] && [ ! -d /etc/update-motd.d.disabled ]; then
  mkdir -p /etc/update-motd.d.disabled
  find /etc/update-motd.d -maxdepth 1 -type f -exec mv {{}} /etc/update-motd.d.disabled/ \\; 2>/dev/null || true
fi

# ensure single pam_motd entry
for pam in /etc/pam.d/sshd /etc/pam.d/login; do
  [ -f "$pam" ] || continue
  if ! grep -q "{_MARKER}" "$pam" 2>/dev/null; then
    if grep -qE '^session\\s+optional\\s+pam_motd\\.so' "$pam"; then
      sed -i -E '/^session\\s+optional\\s+pam_motd\\.so/d' "$pam"
    fi
    echo "session optional pam_motd.so motd_dynamic=/run/motd.dynamic # {_MARKER}" >> "$pam"
  fi
done

# write installer script
mkdir -p /etc/update-motd.d
cat > /etc/update-motd.d/00-brand-motd <<'MOTD_EOF'
{script}
MOTD_EOF
chmod 755 /etc/update-motd.d/00-brand-motd

# prefill dynamic motd
if mkdir -p /run 2>/dev/null || [ -d /run ]; then
  /etc/update-motd.d/00-brand-motd > /run/motd.dynamic 2>/dev/null || true
  chmod 644 /run/motd.dynamic 2>/dev/null || true
fi

# clear static /etc/motd so PAM dynamic wins
: > /etc/motd 2>/dev/null || true

# drop brand file for tooling
mkdir -p /etc/vexdeploy
echo "{brand_name}" > /etc/vexdeploy/provider

echo "MOTD_OK pam=ok scripts=ok brand={brand_name}"
"""


def encode_installer(installer: str) -> str:
    return base64.b64encode(installer.encode("utf-8")).decode("ascii")


def build_installer_payload(brand: dict[str, Any]) -> str:
    installer = build_installer(brand)
    b64 = encode_installer(installer)
    token = secrets.token_hex(6)
    path = f"/tmp/.vex-motd-{token}.sh"
    return (
        f"echo {b64} | base64 -d > {path} && chmod +x {path} && "
        f"bash {path}; ec=$?; rm -f {path}; exit $ec"
    )


def run_installer(exec_fn, brand: dict[str, Any]) -> tuple[bool, str]:
    payload = build_installer_payload(brand)
    try:
        code, output = exec_fn(payload)
    except Exception as exc:
        return False, str(exc)
    text = (output or "").strip()
    ok = "MOTD_OK" in text or code == 0
    return ok, text


def install_branding_files(exec_fn, brand: dict[str, Any]) -> tuple[bool, str]:
    name = str(brand.get("brand_name") or BRAND_NAME)
    website = str(brand.get("website") or "")
    support = str(brand.get("support_email") or "")
    tagline = str(brand.get("brand_tagline") or "")
    content = f"{name}\n{tagline}\n{website}\n{support}\n"
    b64 = base64.b64encode(content.encode()).decode()
    # brand file + console issue banners
    issue = build_issue_script(brand)
    cmd = (
        f"mkdir -p /etc/vexdeploy && echo {b64} | base64 -d > /etc/vexdeploy/brand && "
        f"chmod 644 /etc/vexdeploy/brand && {issue} && echo BRAND_OK"
    )
    try:
        code, output = exec_fn(cmd)
    except Exception as exc:
        return False, str(exc)
    ok = code == 0 and "BRAND_OK" in (output or "")
    return ok, (output or "")
