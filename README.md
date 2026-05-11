# GLKVM Signal Bridge

Control two GL.iNet Comet PRO KVMs (one Mac, one Windows) from your phone via Signal.

This is a personal, foreground tool: you run `./run.sh` when you want to use it,
Ctrl+C to stop. No services, no auto-start.

## Architecture

```
Mac Mini (always on, static IP)
├── Docker: signal-cli-rest-api  (linked to your Signal account)
├── ./run.sh  →  signal_to_kvm.py daemon (foreground in your terminal)
└── 2 Chrome windows (opened manually) showing 2 KVM streams
        │
        ├── glkvm-mac.local  →  GLKVM connected to MacBook Air
        └── glkvm-win.local  →  GLKVM connected to Windows laptop
```

Daemon binds to `127.0.0.1:8765`. Userscript in each Chrome window polls it
for screenshot requests and reports state.

## First-time setup

```bash
git clone <this-repo> glkvm-bridge
cd glkvm-bridge
./setup.sh           # prints a checklist; do whatever's still ○
```

The setup script doesn't change anything — it just tells you what to do next,
and shows ✓/○ for each prerequisite so you can re-run it any time.

**Python environment** (step 4 in the checklist):

```bash
python3 -m venv .venv
.venv/bin/pip install websockets httpx aiohttp
```

Once everything is ✓, copy `signal-to-kvm.env.example` to `signal-to-kvm.env`
and fill in your KVM passwords.

## Daily use

```bash
./run.sh
```

This prints a checklist and starts the daemon in the foreground. The checklist
includes copy-pasteable commands for opening the KVM browser windows and
starting Mumble for audio.

Ctrl+C stops the daemon. Browsers stay open.

## Signal commands

Send to your own number (Note to Self):

| Long form | Short | What it does |
|---|---|---|
| `help` | `/h` `/?` | List all commands |
| `status` | `/st` | Bridge state, per-target |
| `make-screenshot` | `/ms` | Snap the current KVM screen → Signal |
| `search-contact "Name"` | `/sc` | Teams Go-to-chat → type → Enter |
| `send-message "text"` | `/sm` | Type into focused box, screenshot, await confirm |
| `confirm` | `/c` | Send the pending draft (Enter) |
| `cancel` | `/x` | Abandon pending draft (text stays in box) |
| `set-status online\|busy\|away` | `/ss` | Teams presence |
| `change-layout` | `/cl` | Cycle input layout, screenshot |
| `show-calendar` | `/sca` | Jump to Calendar, screenshot, jump back |
| `set-jiggler on \[interval]\|off` | `/sj` | Toggle the KVM mouse jiggler |
| `set-default mac\|win` | — | Switch the default target |
| `enable-notifications` | `/et` | Resume the watcher's Signal alerts |
| `disable-notifications` | `/dt` | Stop the watcher's Signal alerts |

Every routable command takes an optional `-target` suffix:
```
/sm-mac "hi"     /sm-win "hi"     /ss-mac away     /sj-win on 240
```

Without the suffix, the command goes to the default target.

## Files

| File | Purpose |
|---|---|
| `signal_to_kvm.py` | The daemon |
| `glkvm-watcher.user.js` | Tampermonkey userscript for each Chrome window |
| `run.sh` | Daily launcher (foreground, prints checklist) |
| `setup.sh` | First-time setup instructions |
| `signal-to-kvm.env.example` | Config template |
| `signal-to-kvm.env` | ⚠ Your real config — never commit |
| `.venv/` | Python virtual environment — never commit (git-ignored) |

## Keyboard layout assumption

The remote (Mac or Windows laptop) must be set to **U.S. International - PC**
input source. The daemon translates accented characters to dead-key sequences
that this layout understands. Other layouts will produce wrong characters.

## Stopping everything

```
Ctrl+C in the run.sh terminal      # stops the daemon
# Close Chrome windows when done.
# Docker container keeps running — that's fine; it's idle when no client polls.
# If you want to stop it:
docker stop signal-api
```

## Why no auto-start service?

Earlier versions used launchd. It was overkill for "I sit down, I want to use it"
usage. A foreground script you run gives you:
- Logs you can see in real time
- Ctrl+C to stop cleanly
- No `.plist` files to debug when something breaks
- The script can print a checklist every launch reminding you what to do
