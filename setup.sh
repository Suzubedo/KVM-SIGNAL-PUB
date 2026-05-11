#!/usr/bin/env bash
# setup.sh — first-time setup instructions for the Signal→GLKVM bridge.
# This script doesn't change anything; it just tells you what to do.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RED=$'\033[31m'; GRN=$'\033[32m'; CYN=$'\033[36m'; BLD=$'\033[1m'; OFF=$'\033[0m'

# small live checks so the steps say "done" or "todo"
have_cmd() { command -v "$1" >/dev/null 2>&1; }

check_docker_running() {
    have_cmd docker && docker info >/dev/null 2>&1
}

check_signal_container() {
    docker ps -q -f "name=^signal-api$" 2>/dev/null | grep -q .
}

check_signal_linked() {
    local count
    count=$(curl -sf http://127.0.0.1:8080/v1/accounts 2>/dev/null \
        | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null \
        || echo 0)
    [[ "$count" -gt 0 ]]
}

check_python_deps() {
    "${REPO_DIR}/.venv/bin/python" -c "import websockets, httpx, aiohttp" 2>/dev/null
}

check_env_file() {
    [[ -f "${REPO_DIR}/signal-to-kvm.env" ]]
}

mark() {
    if "$@"; then printf "${GRN}✓${OFF}"; else printf "${RED}○${OFF}"; fi
}

cat <<EOF
${BLD}============================================================${OFF}
   ${BLD}GLKVM Signal Bridge — first-time setup${OFF}
${BLD}============================================================${OFF}

Current state of each step (✓ done, ○ todo):

EOF

# ----- step-by-step status -----
printf "  $(mark have_cmd docker)  Docker installed\n"
printf "  $(mark check_docker_running)  Docker running\n"
printf "  $(mark check_signal_container)  signal-cli-rest-api container exists & running\n"
printf "  $(mark check_signal_linked)  Signal account linked\n"
printf "  $(mark check_python_deps)  Python deps installed (websockets, httpx, aiohttp)\n"
printf "  $(mark check_env_file)  signal-to-kvm.env exists in repo\n"

cat <<EOF

${BLD}============================================================${OFF}
${BLD}Instructions${OFF} — only do the steps that are still ○.
${BLD}============================================================${OFF}

  ${BLD}1.  Install Docker Desktop${OFF}
        ${CYN}brew install --cask docker${OFF}
      Open Docker Desktop once and wait for the whale icon.

  ${BLD}2.  Start the signal-cli-rest-api container${OFF}
        ${CYN}docker run -d --name signal-api --restart=always \\
          -p 127.0.0.1:8080:8080 \\
          -v signal-cli-data:/home/.local/share/signal-cli \\
          -e MODE=json-rpc \\
          bbernhard/signal-cli-rest-api${OFF}

  ${BLD}3.  Link your Signal account${OFF}
        Open in a browser:
            ${CYN}http://127.0.0.1:8080/v1/qrcodelink?device_name=glkvm-bridge${OFF}
        On phone: Signal → Settings → Linked devices → "+" → scan.
        Verify: ${CYN}curl http://127.0.0.1:8080/v1/accounts${OFF}
        (should print a list with your number)

  ${BLD}4.  Create a virtual environment and install dependencies${OFF}
        ${CYN}python3 -m venv .venv${OFF}
        ${CYN}.venv/bin/pip install websockets httpx aiohttp${OFF}

  ${BLD}5.  Create your env file${OFF}
        ${CYN}cp signal-to-kvm.env.example signal-to-kvm.env${OFF}
        Edit it to set MAC_KVM_PASSWD, WIN_KVM_PASSWD, etc.:
        ${CYN}open -a TextEdit signal-to-kvm.env${OFF}

  ${BLD}6.  In each Chrome window you open at the KVMs:${OFF}
        a. Install Tampermonkey from the Chrome Web Store
        b. Install glkvm-watcher.user.js from this repo
        c. Reload the KVM page — the userscript should auto-detect
           its target id from the hostname

${BLD}============================================================${OFF}
${BLD}When all checks above show ✓, run:${OFF}
    ${CYN}./run.sh${OFF}
${BLD}============================================================${OFF}

EOF
