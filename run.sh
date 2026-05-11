#!/usr/bin/env bash
# run.sh — launch the Signal→GLKVM bridge in the foreground.
# Run this from the repo directory. Ctrl+C to stop.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${GLKVM_ENV_FILE:-${REPO_DIR}/signal-to-kvm.env}"

# ---------- colors ----------
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'; BLD=$'\033[1m'; OFF=$'\033[0m'

# ---------- env file ----------
if [[ ! -f "$ENV_FILE" ]]; then
    echo "${RED}✗${OFF} env file not found at: ${ENV_FILE}"
    echo "  Copy signal-to-kvm.env.example to signal-to-kvm.env and fill in your values."
    echo "  Or set GLKVM_ENV_FILE to point at it."
    exit 1
fi

# Source env (export every var)
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# ---------- quick sanity ----------
if [[ -z "${SIGNAL_NUMBER:-}" ]]; then
    echo "${RED}✗${OFF} SIGNAL_NUMBER not set in ${ENV_FILE}"; exit 1
fi
if [[ -z "${MAC_KVM_PASSWD:-}${WIN_KVM_PASSWD:-}" ]]; then
    echo "${RED}✗${OFF} Neither MAC_KVM_PASSWD nor WIN_KVM_PASSWD is set"; exit 1
fi

# ---------- KVM reachability probe ----------
# Quick check (~1s each, in parallel). Just probes the base URL responding,
# not auth — that's enough to tell you "is the laptop on, GLKVM reachable".
probe_kvm() {
    local name="$1" url="$2"
    if [[ -z "$url" ]]; then echo "$name|skip|"; return; fi
    local code
    code=$(curl -k -s -o /dev/null -m 2 -w "%{http_code}" "${url}/api/hid" \
        --header "X-KVMD-User: ${name^^}_KVM_USER" 2>/dev/null || echo "000")
    # We don't need auth success; any HTTP response means the KVM is reachable.
    # 401 (unauthorized) is reachable. 000 means connection failed.
    if [[ "$code" == "000" ]]; then
        echo "$name|down|$url"
    else
        echo "$name|up|$url"
    fi
}

MAC_RESULT=$(probe_kvm "mac" "${MAC_KVM_BASE:-}") &
WIN_RESULT=$(probe_kvm "win" "${WIN_KVM_BASE:-}") &
wait

# parse parallel results from the env (subshell didn't update parents)
MAC_LINE=$(probe_kvm "mac" "${MAC_KVM_BASE:-}")
WIN_LINE=$(probe_kvm "win" "${WIN_KVM_BASE:-}")

format_kvm_line() {
    local line="$1"
    local name="${line%%|*}"
    local rest="${line#*|}"
    local state="${rest%%|*}"
    local url="${rest#*|}"
    local marker=""
    case "$state" in
        up)   marker="${GRN}✓${OFF}  reachable    " ;;
        down) marker="${RED}✗${OFF}  unreachable  " ;;
        skip) marker="${YLW}⏭${OFF}  not configured" ;;
    esac
    printf "    %s  KVM '%-3s'  %s  %s\n" "$marker" "$name" "$url" ""
}

# ---------- banner ----------
clear || true
cat <<EOF
${BLD}============================================================${OFF}
   ${BLD}GLKVM Signal Bridge${OFF}
${BLD}============================================================${OFF}

  Signal linked as:  ${CYN}${SIGNAL_NUMBER}${OFF}
  Default target:    ${CYN}${DEFAULT_TARGET:-mac}${OFF}
  Daemon listening:  ${CYN}${BRIDGE_HOST:-127.0.0.1}:${BRIDGE_PORT:-8765}${OFF}

  KVM targets:
$(format_kvm_line "$MAC_LINE")$(format_kvm_line "$WIN_LINE")

${BLD}📋  Checklist before sending commands${OFF}

  ${BLD}1.${OFF} Open the KVM browser window(s). Keep them visible —
     keystrokes don't land if the tab is in deep background.

     ${CYN}# Mac KVM:${OFF}
     /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
       --user-data-dir="\$HOME/Library/Application Support/glkvm-bridge/chrome-mac" \\
       --new-window "${MAC_KVM_BASE:-https://glkvm-mac.local}"

     ${CYN}# Windows KVM:${OFF}
     /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
       --user-data-dir="\$HOME/Library/Application Support/glkvm-bridge/chrome-win" \\
       --new-window "${WIN_KVM_BASE:-https://glkvm-win.local}"

  ${BLD}2.${OFF} If you want audio:
       a. Start the Mumble server on this Mac
            ${CYN}/Applications/Mumble.app/Contents/MacOS/mumble-server${OFF}
       b. Connect the Mumble client on this Mac (your home server)
       c. Connect Mumble client on the target laptop, switch its
          system input/output to Mumble

  ${BLD}3.${OFF} Remote keyboard layout must be ${CYN}"U.S. International - PC"${OFF}
     for accent translation to work (é, ç, è, ...).

  ${BLD}4.${OFF} From your phone, in Signal Note-to-Self, send:
            ${CYN}help${OFF}    — list all commands
            ${CYN}/st${OFF}     — bridge status
            ${CYN}/sm "hi"${OFF}  — send a message via the default target

  Press ${BLD}Ctrl+C${OFF} to stop the bridge.
${BLD}============================================================${OFF}

EOF

# ---------- exec ----------
PYTHON="${REPO_DIR}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "${RED}✗${OFF} .venv not found. Run:"
    echo "    python3 -m venv .venv"
    echo "    .venv/bin/pip install websockets httpx aiohttp"
    exit 1
fi
exec "$PYTHON" "${REPO_DIR}/signal_to_kvm.py"
