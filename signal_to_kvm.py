#!/usr/bin/env python3
"""
signal_to_kvm.py v3 — Signal-to-GLKVM bridge with multi-target routing.

Architecture
------------
One daemon, multiple GLKVM targets (e.g. mac + win). One Signal "Note to Self"
stream. Each target has its own KVM URL, credentials, keymap, and platform-
specific keystroke recipes (Cmd vs Ctrl, layout-cycle combo, etc).

Commands route to a target via:
    1. A `-target` suffix:    /sm-mac "hello"      → typed on the Mac
    2. The current default:   /sm "hello"          → typed on whichever target
                                                     is the active default
Switch the default with:      set-default mac | win

Pending drafts (the send-message / confirm cycle) are per-target. You can have
one pending on Mac and another on Windows simultaneously.

Userscript companions
---------------------
Each KVM web UI tab runs the watcher userscript, which also acts as the daemon's
"eyes" for that target — taking screenshots on demand and reporting state. The
userscript identifies itself by passing ?id=<target> on every poll, and the
daemon routes screenshot requests to the matching userscript only.

Available commands (some short, some with long aliases)
-------------------------------------------------------
  help          /h /?            list every command
  status        /st              show daemon state
  set-default   none             set-default mac|win — set default target
  enable-notifications   /et     resume the watcher's Signal alerts (per default target)
  disable-notifications  /dt     stop the watcher's Signal alerts
  make-screenshot /ms            grab the screen and post it to Signal
  search-contact  /sc            search-contact "Name" — Cmd+G/Ctrl+G in Teams,
                                 type, Enter to open the chat
  send-message    /sm            send-message "text" — type, screenshot, await confirm
  confirm         /c             accept the pending draft (presses Enter)
  cancel          /x             abandon pending draft (text stays in box)
  set-status      /ss            set-status online|busy|away — Teams presence
  change-layout   /cl            cycle macOS input source / Windows layout, screenshot
  show-calendar   /sca           jump to Teams Calendar tab, screenshot, jump back
  focus-teams     /ft            open/focus Teams via Spotlight (Mac) or Start menu (Win)
  set-jiggler     /sj            set-jiggler on [interval]|off — toggle KVM jiggler

All target-routable commands accept the -target suffix. Examples:
    /sm-mac "hi"       /sm-win "hi"
    /sc-mac "Kevin"    /sc-win "Kevin"
    /ss-mac away       /cl-win    /sj-mac on 240

Configuration via env vars
--------------------------
Per-target (replace MAC with WIN for the second target):
    MAC_KVM_BASE        e.g. https://glkvm-mac.local
    MAC_KVM_USER        default: admin
    MAC_KVM_PASSWD      required (no default for security)
    MAC_KVM_KEYMAP      default: en-us
    MAC_KVM_VERIFY_TLS  default: 0

Daemon-wide:
    SIGNAL_API_BASE     default: http://127.0.0.1:8080
    SIGNAL_NUMBER       required, E.164 format e.g. +33XXXXXXXXX
    BRIDGE_HOST         default: 0.0.0.0
    BRIDGE_PORT         default: 8765
    DEFAULT_TARGET      default: mac
    KVM_TARGETS         default: mac,win  (which target names to load)
    JIGGLER_DEFAULT_INTERVAL  default: 240 (seconds)

Dependencies:
    pip3 install websockets httpx aiohttp

Run:
    SIGNAL_NUMBER=+33XXXXXXXXX MAC_KVM_PASSWD=... WIN_KVM_PASSWD=... \\
        python3 signal_to_kvm.py
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

import httpx
import websockets
from aiohttp import web

# ============================================================
# Configuration
# ============================================================

@dataclass
class TargetConfig:
    """Per-target configuration: one of these per remote machine (mac, win, …)."""
    name: str
    display_name: str
    kvm_base: str
    kvm_user: str
    kvm_passwd: str
    kvm_keymap: str
    kvm_verify_tls: bool
    # Platform-specific keystroke recipes:
    # `shortcut_modifier` is the modifier used for Teams shortcuts (Cmd on Mac, Ctrl on Win)
    shortcut_modifier: str
    # `layout_combo` is (list_of_modifier_keys, key) for cycling input layout
    # Mac: (["ControlLeft", "AltLeft"], "Space")  — Ctrl+Option+Space
    # Win: (["MetaLeft"], "Space")                 — Win+Space
    layout_combo: tuple[list[str], str]


@dataclass
class Config:
    # Signal
    signal_api_base: str = os.getenv("SIGNAL_API_BASE", "http://127.0.0.1:8080")
    signal_number:   str = os.getenv("SIGNAL_NUMBER", "")

    # Bridge HTTP server (for userscript polling)
    bridge_host: str = os.getenv("BRIDGE_HOST", "127.0.0.1")
    bridge_port: int = int(os.getenv("BRIDGE_PORT", "8765"))

    # Behaviour
    pending_timeout_seconds:    int = int(os.getenv("PENDING_TIMEOUT_SECONDS", "180"))
    screenshot_timeout_seconds: int = int(os.getenv("SCREENSHOT_TIMEOUT_SECONDS", "8"))
    typing_settle_ms:           int = int(os.getenv("TYPING_SETTLE_MS", "300"))
    search_after_focus_ms:      int = int(os.getenv("SEARCH_AFTER_FOCUS_MS", "1000"))
    search_after_type_ms:       int = int(os.getenv("SEARCH_AFTER_TYPE_MS", "1000"))
    focus_settle_ms:            int = int(os.getenv("FOCUS_SETTLE_MS", "2000"))
    keystroke_delay_ms:         int = int(os.getenv("KEYSTROKE_DELAY_MS", "60"))
    max_message_chars:          int = int(os.getenv("MAX_MESSAGE_CHARS", "1000"))
    jiggler_default_interval:   int = int(os.getenv("JIGGLER_DEFAULT_INTERVAL", "240"))

    # Targets — populated by load_targets() at startup
    targets: dict[str, TargetConfig] = field(default_factory=dict)
    default_target: str = os.getenv("DEFAULT_TARGET", "mac")

    def validate(self) -> None:
        if not self.signal_number:
            raise SystemExit("SIGNAL_NUMBER env var is required (E.164, e.g. +33XXXXXXXXX).")
        if not self.signal_number.startswith("+"):
            raise SystemExit("SIGNAL_NUMBER must start with '+'.")
        if not self.targets:
            raise SystemExit("No targets configured. Set MAC_KVM_BASE / WIN_KVM_BASE etc.")
        if self.default_target not in self.targets:
            raise SystemExit(
                f"DEFAULT_TARGET={self.default_target!r} is not in configured targets "
                f"({list(self.targets)})."
            )


# Recipes for the platforms we know about. Adding a third (e.g. linux) means
# extending this dict, not touching command code.
_PLATFORM_RECIPES: dict[str, dict] = {
    "mac": {
        "display_name": "Mac",
        "shortcut_modifier": "MetaLeft",                            # Cmd
        "layout_combo": (["ControlLeft", "AltLeft"], "Space"),      # Ctrl+Option+Space
    },
    "win": {
        "display_name": "Windows",
        "shortcut_modifier": "ControlLeft",                         # Ctrl
        "layout_combo": (["MetaLeft"], "Space"),                    # Win+Space
    },
}


def load_targets(cfg: Config) -> None:
    """Populate cfg.targets from env vars based on KVM_TARGETS."""
    requested = [t.strip().lower() for t in os.getenv("KVM_TARGETS", "mac,win").split(",") if t.strip()]
    for name in requested:
        if name not in _PLATFORM_RECIPES:
            log.warning("target %r has no platform recipe; skipping", name)
            continue
        prefix = name.upper()
        base = os.getenv(f"{prefix}_KVM_BASE")
        if not base:
            log.info("target %r not configured (no %s_KVM_BASE) — skipping", name, prefix)
            continue
        recipe = _PLATFORM_RECIPES[name]
        cfg.targets[name] = TargetConfig(
            name=name,
            display_name=recipe["display_name"],
            kvm_base=base,
            kvm_user=os.getenv(f"{prefix}_KVM_USER", "admin"),
            kvm_passwd=os.getenv(f"{prefix}_KVM_PASSWD", ""),
            kvm_keymap=os.getenv(f"{prefix}_KVM_KEYMAP", "en-us"),
            kvm_verify_tls=os.getenv(f"{prefix}_KVM_VERIFY_TLS", "0") == "1",
            shortcut_modifier=recipe["shortcut_modifier"],
            layout_combo=recipe["layout_combo"],
        )


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=os.getenv("LOGLEVEL", "INFO").upper(),
)
log = logging.getLogger("bridge")


# ============================================================
# State machine
# ============================================================

class DraftState(Enum):
    IDLE = "idle"
    WAITING_FOR_SCREENSHOT = "waiting_for_screenshot"
    WAITING_FOR_CONFIRM    = "waiting_for_confirm"


@dataclass
class PendingDraft:
    text: str
    started_at: float


@dataclass
class TargetState:
    """Per-target runtime state. Pending drafts and notifications are per-target."""
    target: TargetConfig
    draft_state: DraftState = DraftState.IDLE
    pending: Optional[PendingDraft] = None
    notifications_enabled: bool = True
    last_userscript_poll: float = 0.0


@dataclass
class ScreenshotRequest:
    """A pending screenshot request awaiting fulfilment by a userscript."""
    target_name: str
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class BridgeState:
    config: Config
    targets: dict[str, TargetState] = field(default_factory=dict)

    # Counters/diagnostics
    last_command: str = "none"
    last_command_at: float = 0.0
    commands_total: int = 0

    # Screenshot bridge: dict of req_id -> ScreenshotRequest
    pending_screenshot_requests: dict[str, ScreenshotRequest] = field(default_factory=dict)
    received_screenshots: dict[str, str] = field(default_factory=dict)  # req_id -> data URL

    # Serial command lock — prevents two commands typing at once,
    # even on different targets (the daemon is single-threaded for clarity)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ============================================================
# KVM HID helpers (target-aware)
# ============================================================

async def _kvm_post(t: TargetConfig, path: str, params: dict | None = None,
                    content: bytes | None = None,
                    content_type: str = "text/plain; charset=utf-8") -> tuple[bool, str]:
    url = f"{t.kvm_base.rstrip('/')}{path}"
    headers = {"X-KVMD-User": t.kvm_user, "X-KVMD-Passwd": t.kvm_passwd}
    if content is not None:
        headers["Content-Type"] = content_type
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=t.kvm_verify_tls) as c:
            r = await c.post(url, params=params, headers=headers, content=content)
            if 200 <= r.status_code < 300:
                return True, f"HTTP {r.status_code}"
            return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except httpx.HTTPError as e:
        return False, f"transport: {e}"


async def _kvm_get(t: TargetConfig, path: str) -> tuple[bool, dict | str]:
    url = f"{t.kvm_base.rstrip('/')}{path}"
    headers = {"X-KVMD-User": t.kvm_user, "X-KVMD-Passwd": t.kvm_passwd}
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=t.kvm_verify_tls) as c:
            r = await c.get(url, headers=headers)
            if 200 <= r.status_code < 300:
                try:
                    return True, r.json()
                except Exception:
                    return True, r.text
            return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except httpx.HTTPError as e:
        return False, f"transport: {e}"


# US-International dead-key conversion. The remote may be on US-International
# regardless of platform. If it isn't, set the target's keymap accordingly.
_DEADKEY_MAP: dict[str, str] = {
    "é": "'e", "É": "'E",
    "ç": "'c", "Ç": "'C",
    "è": "`e", "È": "`E",
    "à": "`a", "À": "`A",
    "ù": "`u", "Ù": "`U",
    "ê": "^e", "Ê": "^E",
    "â": "^a", "Â": "^A",
    "î": "^i", "Î": "^I",
    "ô": "^o", "Ô": "^O",
    "û": "^u", "Û": "^U",
    "'": "' ",
    '"': '" ',
}


def apply_deadkey_translation(text: str) -> str:
    return "".join(_DEADKEY_MAP.get(ch, ch) for ch in text)


async def kvm_type_text(t: TargetConfig, text: str, *, translate: bool = True) -> tuple[bool, str]:
    payload = apply_deadkey_translation(text) if translate else text
    if translate and payload != text:
        log.debug("[%s] deadkey-translated %r -> %r", t.name, text, payload)
    return await _kvm_post(
        t,
        "/api/hid/print",
        params={"keymap": t.kvm_keymap},
        content=payload.encode("utf-8"),
    )


async def kvm_send_key(t: TargetConfig, key: str, state: int) -> tuple[bool, str]:
    return await _kvm_post(t, "/api/hid/events/send_key", params={"key": key, "state": str(state)})


async def kvm_tap(t: TargetConfig, key: str, hold_ms: int = 30) -> tuple[bool, str]:
    ok, info = await kvm_send_key(t, key, 1)
    if not ok:
        return False, info
    await asyncio.sleep(hold_ms / 1000.0)
    return await kvm_send_key(t, key, 0)


async def kvm_chord(t: TargetConfig, modifier: str, key: str) -> tuple[bool, str]:
    """Press one modifier, tap key, release modifier. Always releases on failure."""
    ok, info = await kvm_send_key(t, modifier, 1)
    if not ok:
        return False, info
    await asyncio.sleep(0.03)
    ok, info = await kvm_tap(t, key)
    if not ok:
        await kvm_send_key(t, modifier, 0)
        return False, info
    await asyncio.sleep(0.03)
    return await kvm_send_key(t, modifier, 0)


async def kvm_combo(t: TargetConfig, modifiers: list[str], key: str) -> tuple[bool, str]:
    """Press each modifier in order, tap key, release in reverse. Always releases."""
    held: list[str] = []
    try:
        for m in modifiers:
            ok, info = await kvm_send_key(t, m, 1)
            if not ok:
                return False, f"hold {m}: {info}"
            held.append(m)
            await asyncio.sleep(0.03)
        ok, info = await kvm_tap(t, key)
        if not ok:
            return False, f"tap {key}: {info}"
        await asyncio.sleep(0.03)
        return True, "ok"
    finally:
        for m in reversed(held):
            await kvm_send_key(t, m, 0)
            await asyncio.sleep(0.02)


# Convenience: "Teams shortcut" — what would be Cmd+X on Mac, Ctrl+X on Win.
# Picks the right modifier based on the target's platform.
async def kvm_teams_chord(t: TargetConfig, key: str) -> tuple[bool, str]:
    return await kvm_chord(t, t.shortcut_modifier, key)


# ============================================================
# Signal helpers
# ============================================================

async def signal_send(cfg: Config, text: str, png_data_url: Optional[str] = None) -> bool:
    url = f"{cfg.signal_api_base.rstrip('/')}/v2/send"
    payload = {
        "number": cfg.signal_number,
        "recipients": [cfg.signal_number],
        "message": text,
    }
    if png_data_url:
        payload["base64_attachments"] = [png_data_url]
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, json=payload)
            if 200 <= r.status_code < 300:
                return True
            log.warning("signal-send failed: HTTP %d %s", r.status_code, r.text[:200])
            return False
    except httpx.HTTPError as e:
        log.warning("signal-send transport: %s", e)
        return False


# ============================================================
# Screenshot bridge (target-aware)
# ============================================================

async def request_screenshot(state: BridgeState, target_name: str) -> Optional[str]:
    cfg = state.config
    req_id = uuid.uuid4().hex[:12]
    req = ScreenshotRequest(target_name=target_name)
    state.pending_screenshot_requests[req_id] = req
    log.info("screenshot request queued: id=%s target=%s", req_id, target_name)
    try:
        await asyncio.wait_for(req.event.wait(), timeout=cfg.screenshot_timeout_seconds)
    except asyncio.TimeoutError:
        log.warning("screenshot timeout: id=%s target=%s", req_id, target_name)
        state.pending_screenshot_requests.pop(req_id, None)
        return None
    data = state.received_screenshots.pop(req_id, None)
    state.pending_screenshot_requests.pop(req_id, None)
    return data


# ============================================================
# Command registry
# ============================================================

# A command handler signature accepts the bridge state, a target name (which
# the dispatcher resolves from the command suffix or default), and the payload.
# Commands that don't care about a target (help, set-default, status) ignore it.
CommandHandler = Callable[["BridgeState", str, str], Awaitable[None]]
# Each registry entry: (prefix, handler, description, is_alias, accepts_target)
_COMMANDS: list[tuple[str, CommandHandler, str, bool, bool]] = []


def register_command(
    prefix: str,
    handler: CommandHandler,
    description: str = "",
    aliases: list[str] | None = None,
    accepts_target: bool = True,
) -> None:
    """Register a primary command + zero or more aliases sharing the same handler.
    If accepts_target=False, the dispatcher won't try to parse a -target suffix."""
    _COMMANDS.append((prefix.lower(), handler, description, False, accepts_target))
    for alias in (aliases or []):
        _COMMANDS.append((alias.lower(), handler, "", True, accepts_target))
    _COMMANDS.sort(key=lambda kv: len(kv[0]), reverse=True)


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in '"“”' and s[-1] in '"”':
        return s[1:-1]
    return s


# ============================================================
# Commands
# ============================================================

async def cmd_search_contact(state: BridgeState, target_name: str, payload: str) -> None:
    cfg = state.config
    t = state.targets[target_name].target
    name = _strip_quotes(payload)
    if not name:
        await signal_send(cfg, '⚠️ search-contact: missing name. Usage: search-contact "Name Surname"')
        return
    log.info("[%s] search-contact: %r", target_name, name)

    ok, info = await _focus_teams_impl(t, cfg)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] search-contact: focus-teams failed ({info})")
        return

    # Cmd+G or Ctrl+G — Teams "Go to chat"
    ok, info = await kvm_teams_chord(t, "KeyG")
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] search-contact: shortcut failed ({info})")
        return
    await asyncio.sleep(cfg.search_after_focus_ms / 1000.0)

    ok, info = await kvm_type_text(t, name)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] search-contact: typing failed ({info})")
        return
    await asyncio.sleep(cfg.search_after_type_ms / 1000.0)

    ok, info = await kvm_tap(t, "Enter")
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] search-contact: Enter failed ({info})")
        return

    await signal_send(cfg, f"✅ [{t.display_name}] opened chat for “{name}”.")


register_command(
    "search-contact",
    cmd_search_contact,
    'search-contact "Name Surname" — Teams Go-to-chat (Cmd+G/Ctrl+G), type, Enter.',
    aliases=["/sc"],
)


async def cmd_send_message(state: BridgeState, target_name: str, payload: str) -> None:
    cfg = state.config
    ts = state.targets[target_name]
    t = ts.target

    if ts.draft_state != DraftState.IDLE:
        await signal_send(cfg, f"⚠️ [{t.display_name}] send-message: a draft is already pending ({ts.draft_state.value}). Reply confirm/cancel first.")
        return

    msg = _strip_quotes(payload)
    if not msg:
        await signal_send(cfg, '⚠️ send-message: empty body. Usage: send-message "your text"')
        return
    if len(msg) > cfg.max_message_chars:
        await signal_send(cfg, f"⚠️ send-message: too long ({len(msg)}/{cfg.max_message_chars}).")
        return

    log.info("[%s] send-message: %d chars", target_name, len(msg))
    ok, info = await kvm_type_text(t, msg)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] send-message: typing failed ({info})")
        return

    await asyncio.sleep(cfg.typing_settle_ms / 1000.0)

    ts.draft_state = DraftState.WAITING_FOR_SCREENSHOT
    ts.pending = PendingDraft(text=msg, started_at=time.monotonic())

    data_url = await request_screenshot(state, target_name)
    if not data_url:
        ts.draft_state = DraftState.IDLE
        ts.pending = None
        await signal_send(
            cfg,
            f"❌ [{t.display_name}] typed but no screenshot — is the {t.display_name} KVM tab open?\n"
            f"Reply cancel-{target_name} to abandon, or press Enter on the KVM yourself.",
        )
        return

    ts.draft_state = DraftState.WAITING_FOR_CONFIRM
    await signal_send(
        cfg,
        f"📝 [{t.display_name}] Draft ready ({len(msg)} chars). "
        f"Reply confirm-{target_name} to send, cancel-{target_name} to leave it.\n"
        f"Pending {cfg.pending_timeout_seconds}s.",
        png_data_url=data_url,
    )


register_command(
    "send-message",
    cmd_send_message,
    'send-message "your text" — types into focused box, screenshots, awaits confirm/cancel.',
    aliases=["/sm"],
)


_TYPE_TEXT_MAX_CHARS = 10_000


async def cmd_type_text(state: BridgeState, target_name: str, payload: str) -> None:
    cfg = state.config
    ts = state.targets[target_name]
    t = ts.target

    msg = _strip_quotes(payload)
    if not msg:
        await signal_send(cfg, '⚠️ type-text: empty body. Usage: type-text "your text"')
        return
    if len(msg) > _TYPE_TEXT_MAX_CHARS:
        await signal_send(cfg, f"⚠️ type-text: too long ({len(msg)}/{_TYPE_TEXT_MAX_CHARS}).")
        return

    log.info("[%s] type-text: %d chars", target_name, len(msg))
    ok, info = await kvm_type_text(t, msg)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] type-text: typing failed ({info})")
        return

    await signal_send(cfg, f"✅ [{t.display_name}] typed {len(msg)} chars.")


register_command(
    "type-text",
    cmd_type_text,
    'type-text "your text" — types into focused box, no screenshot, no confirm. Up to 10 000 chars.',
    aliases=["/tt"],
)


async def cmd_make_screenshot(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    t = state.targets[target_name].target
    log.info("[%s] make-screenshot", target_name)
    data_url = await request_screenshot(state, target_name)
    if not data_url:
        await signal_send(cfg, f"❌ [{t.display_name}] make-screenshot: timeout (KVM tab open?).")
        return
    await signal_send(cfg, f"📸 [{t.display_name}] {time.strftime('%H:%M:%S')}", png_data_url=data_url)


register_command(
    "make-screenshot",
    cmd_make_screenshot,
    "make-screenshot — capture the KVM screen, post to Note-to-Self.",
    aliases=["/ms"],
)


async def cmd_confirm(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    ts = state.targets[target_name]
    t = ts.target
    if ts.draft_state != DraftState.WAITING_FOR_CONFIRM:
        log.debug("[%s] confirm with no pending draft", target_name)
        return
    log.info("[%s] confirming pending draft", target_name)
    ok, info = await kvm_tap(t, "Enter")
    ts.draft_state = DraftState.IDLE
    ts.pending = None
    if ok:
        await signal_send(cfg, f"✅ [{t.display_name}] sent.")
    else:
        await signal_send(cfg, f"❌ [{t.display_name}] confirm: Enter failed ({info}).")


register_command(
    "confirm",
    cmd_confirm,
    "confirm — press Enter on the KVM to send the pending draft.",
    aliases=["/c"],
)


async def cmd_cancel(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    ts = state.targets[target_name]
    t = ts.target
    if ts.draft_state != DraftState.WAITING_FOR_CONFIRM:
        log.debug("[%s] cancel with no pending draft", target_name)
        return
    log.info("[%s] cancelling pending draft", target_name)
    ts.draft_state = DraftState.IDLE
    ts.pending = None
    await signal_send(cfg, f"↩️ [{t.display_name}] cancelled. Text left in box.")


register_command(
    "cancel",
    cmd_cancel,
    "cancel — abandon the pending draft (text stays in the message box).",
    aliases=["/x"],
)


async def cmd_status(state: BridgeState, _target_name: str, _payload: str) -> None:
    cfg = state.config
    age = "n/a"
    if state.last_command_at:
        age = f"{int(time.monotonic() - state.last_command_at)}s ago"

    lines = [
        "🟢 bridge online",
        f"default target: {cfg.default_target}",
        f"last command: {state.last_command} ({age})",
        f"commands handled: {state.commands_total}",
        "",
    ]
    for name, ts in state.targets.items():
        last_poll = "never"
        if ts.last_userscript_poll:
            last_poll = f"{int(time.monotonic() - ts.last_userscript_poll)}s ago"
        lines.append(
            f"[{ts.target.display_name}] draft={ts.draft_state.value} "
            f"notif={'on' if ts.notifications_enabled else 'off'} "
            f"keymap={ts.target.kvm_keymap} "
            f"poll={last_poll}"
        )
    await signal_send(cfg, "\n".join(lines))


register_command(
    "status",
    cmd_status,
    "status — daemon and per-target state.",
    aliases=["/st"],
    accepts_target=False,
)


async def cmd_set_default(state: BridgeState, _target_name: str, payload: str) -> None:
    cfg = state.config
    arg = _strip_quotes(payload).strip().lower()
    if not arg:
        await signal_send(
            cfg,
            f"current default: {cfg.default_target}\n"
            f"available: {', '.join(sorted(cfg.targets))}\n"
            f"Use: set-default <target>",
        )
        return
    if arg not in cfg.targets:
        await signal_send(cfg, f"⚠️ set-default: unknown target '{arg}'. Available: {', '.join(sorted(cfg.targets))}")
        return
    if cfg.default_target == arg:
        await signal_send(cfg, f"already on default {arg}.")
        return
    cfg.default_target = arg
    log.info("default target → %s", arg)
    await signal_send(cfg, f"✅ default target → {state.targets[arg].target.display_name}.")


register_command(
    "set-default",
    cmd_set_default,
    "set-default mac|win — set default target for unsuffixed commands.",
    accepts_target=False,
)


async def cmd_disable_notifications(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    ts = state.targets[target_name]
    if not ts.notifications_enabled:
        await signal_send(cfg, f"🔕 [{ts.target.display_name}] notifications already off.")
        return
    ts.notifications_enabled = False
    log.info("[%s] notifications: disabled", target_name)
    await signal_send(cfg, f"🔕 [{ts.target.display_name}] notifications disabled (within ~2s).")


async def cmd_enable_notifications(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    ts = state.targets[target_name]
    if ts.notifications_enabled:
        await signal_send(cfg, f"🔔 [{ts.target.display_name}] notifications already on.")
        return
    ts.notifications_enabled = True
    log.info("[%s] notifications: enabled", target_name)
    await signal_send(cfg, f"🔔 [{ts.target.display_name}] notifications enabled (within ~2s).")


register_command(
    "disable-notifications",
    cmd_disable_notifications,
    "disable-notifications — stop the watcher's Signal alerts (per target).",
    aliases=["/dt"],
)
register_command(
    "enable-notifications",
    cmd_enable_notifications,
    "enable-notifications — resume the watcher's Signal alerts (per target).",
    aliases=["/et"],
)


# ----- help is special: it lists everything, knows about target suffixes -----

async def cmd_help(state: BridgeState, _target_name: str, _payload: str) -> None:
    cfg = state.config
    primaries = [(p, h, d, at) for (p, h, d, is_alias, at) in _COMMANDS if not is_alias]
    aliases_for: dict[int, list[str]] = {}
    for p, h, _d, is_alias, _at in _COMMANDS:
        if is_alias:
            aliases_for.setdefault(id(h), []).append(p)

    lines = ["📖 Available commands:"]
    for prefix, handler, desc, accepts_target in sorted(primaries, key=lambda kv: kv[0]):
        line = f"• {desc}" if desc else f"• {prefix}"
        alts = aliases_for.get(id(handler))
        if alts:
            line += f"  (alias: {', '.join(sorted(alts))})"
        if accepts_target:
            line += "  ⟨target-routable⟩"
        lines.append(line)
    lines.append("")
    lines.append(f"Targets: {', '.join(sorted(cfg.targets))} (default: {cfg.default_target})")
    lines.append("Suffix any routable command with -<target> to override default, e.g. /sm-win \"hi\".")
    await signal_send(cfg, "\n".join(lines))


register_command(
    "help",
    cmd_help,
    "help — list every command and its usage.",
    aliases=["/h", "/?"],
    accepts_target=False,
)


# ----- set-status, change-layout, show-calendar, set-jiggler -----

_STATUS_TO_SLASH = {
    "online":    "/available",
    "available": "/available",
    "busy":      "/busy",
    "dnd":       "/busy",
    "away":      "/away",
}


async def cmd_set_status(state: BridgeState, target_name: str, payload: str) -> None:
    cfg = state.config
    t = state.targets[target_name].target
    arg = _strip_quotes(payload).strip().lower()
    if not arg:
        await signal_send(cfg, "⚠️ set-status: missing arg. Use: set-status online|busy|away")
        return
    if arg not in _STATUS_TO_SLASH:
        await signal_send(cfg, f"⚠️ set-status: unknown '{arg}'. Valid: {', '.join(sorted(set(_STATUS_TO_SLASH)))}")
        return
    slash = _STATUS_TO_SLASH[arg]
    log.info("[%s] set-status: %s → %s", target_name, arg, slash)

    ok, info = await _focus_teams_impl(t, cfg)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] set-status: focus-teams failed ({info})")
        return

    ok, info = await kvm_teams_chord(t, "KeyE")
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] set-status: shortcut failed ({info})")
        return
    await asyncio.sleep(cfg.search_after_focus_ms / 1000.0)
    ok, info = await kvm_type_text(t, slash, translate=False)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] set-status: typing failed ({info})")
        return
    await asyncio.sleep(cfg.search_after_type_ms / 1000.0)
    ok, info = await kvm_tap(t, "Enter")
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] set-status: Enter failed ({info})")
        return

    canonical = {"/available": "online", "/busy": "busy", "/away": "away"}[slash]
    await signal_send(cfg, f"✅ [{t.display_name}] status set to {canonical}.")


register_command(
    "set-status",
    cmd_set_status,
    "set-status online|busy|away — set Teams presence via slash command.",
    aliases=["/ss"],
)


async def cmd_change_layout(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    t = state.targets[target_name].target
    log.info("[%s] change-layout", target_name)
    modifiers, key = t.layout_combo
    ok, info = await kvm_combo(t, modifiers, key)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] change-layout: combo failed ({info})")
        return
    await asyncio.sleep(1.0)
    data_url = await request_screenshot(state, target_name)
    if data_url:
        await signal_send(cfg, f"🌐 [{t.display_name}] layout cycled.", png_data_url=data_url)
    else:
        await signal_send(cfg, f"🌐 [{t.display_name}] layout cycled (no screenshot — KVM tab not polling).")


register_command(
    "change-layout",
    cmd_change_layout,
    "change-layout — cycle input layout (Mac: Ctrl+Opt+Space, Win: Win+Space) and screenshot.",
    aliases=["/cl"],
)


async def cmd_show_calendar(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    t = state.targets[target_name].target
    log.info("[%s] show-calendar", target_name)

    ok, info = await _focus_teams_impl(t, cfg)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] show-calendar: focus-teams failed ({info})")
        return

    ok, info = await kvm_teams_chord(t, "Digit2")
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] show-calendar: jump failed ({info})")
        return
    await asyncio.sleep(0.8)
    data_url = await request_screenshot(state, target_name)
    # always try to come back to chat
    await kvm_teams_chord(t, "Digit3")

    if data_url:
        await signal_send(cfg, f"📅 [{t.display_name}] calendar.", png_data_url=data_url)
    else:
        await signal_send(cfg, f"📅 [{t.display_name}] calendar (no screenshot).")


register_command(
    "show-calendar",
    cmd_show_calendar,
    "show-calendar — Teams Calendar (Cmd/Ctrl+2), screenshot, back to Chat (Cmd/Ctrl+3).",
    aliases=["/sca"],
)


async def _focus_teams_impl(t: TargetConfig, cfg: Config) -> tuple[bool, str]:
    if t.name == "mac":
        ok, info = await kvm_chord(t, "MetaLeft", "Space")
    else:
        ok, info = await kvm_tap(t, "MetaLeft")
    if not ok:
        return False, f"launcher shortcut failed ({info})"
    await asyncio.sleep(cfg.search_after_focus_ms / 1000.0)
    ok, info = await kvm_type_text(t, "Teams", translate=False)
    if not ok:
        return False, f"typing failed ({info})"
    await asyncio.sleep(cfg.search_after_type_ms / 1000.0)
    ok, info = await kvm_tap(t, "Enter")
    if not ok:
        return False, f"Enter failed ({info})"
    await asyncio.sleep(cfg.focus_settle_ms / 1000.0)
    return True, "Teams focused"


async def cmd_focus_teams(state: BridgeState, target_name: str, _payload: str) -> None:
    cfg = state.config
    t = state.targets[target_name].target
    log.info("[%s] focus-teams", target_name)

    ok, info = await _focus_teams_impl(t, cfg)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] focus-teams: {info}")
        return
    await signal_send(cfg, f"✅ [{t.display_name}] Teams focused.")


register_command(
    "focus-teams",
    cmd_focus_teams,
    "focus-teams — open/focus Teams via Spotlight (Mac: Cmd+Space) or Start menu (Win: Win key).",
    aliases=["/ft"],
)


_JIGGLER_TO_BOOL = {
    "on": True, "enable": True, "enabled": True, "true": True, "1": True,
    "off": False, "disable": False, "disabled": False, "false": False, "0": False,
}


async def cmd_set_jiggler(state: BridgeState, target_name: str, payload: str) -> None:
    cfg = state.config
    t = state.targets[target_name].target
    raw = _strip_quotes(payload).strip()

    if not raw:
        ok, body = await _kvm_get(t, "/api/hid")
        if not ok:
            await signal_send(cfg, f"❌ [{t.display_name}] set-jiggler: read state failed ({body})")
            return
        try:
            j = body["result"]["jiggler"]
            await signal_send(
                cfg,
                f"🪄 [{t.display_name}] jiggler: {'on' if j.get('enabled') else 'off'} "
                f"(active={j.get('active')}, interval={j.get('interval')}s)\n"
                f"Use: set-jiggler on [interval_s] | off",
            )
        except (KeyError, TypeError):
            await signal_send(cfg, f"❌ [{t.display_name}] set-jiggler: unexpected /api/hid shape.")
        return

    parts = raw.split(maxsplit=1)
    arg = parts[0].lower()
    interval_arg: Optional[int] = None
    if len(parts) == 2:
        try:
            interval_arg = int(parts[1].strip())
            if interval_arg < 5 or interval_arg > 3600:
                raise ValueError
        except ValueError:
            await signal_send(cfg, "⚠️ set-jiggler: interval must be 5..3600 seconds.")
            return

    if arg not in _JIGGLER_TO_BOOL:
        await signal_send(cfg, "⚠️ set-jiggler: use on [interval_s] | off.")
        return

    desired = _JIGGLER_TO_BOOL[arg]
    params: dict[str, str] = {"jiggler": "true" if desired else "false"}
    interval_used: Optional[int] = None
    if desired:
        interval_used = interval_arg if interval_arg is not None else cfg.jiggler_default_interval
        params["jiggler_interval"] = str(interval_used)

    log.info("[%s] set-jiggler: %s (interval=%s)", target_name, desired, interval_used)
    ok, info = await _kvm_post(t, "/api/hid/set_params", params=params)
    if not ok:
        await signal_send(cfg, f"❌ [{t.display_name}] set-jiggler: API failed ({info})")
        return
    if desired:
        await signal_send(cfg, f"🪄 [{t.display_name}] jiggler on (interval {interval_used}s).")
    else:
        await signal_send(cfg, f"🪄 [{t.display_name}] jiggler off.")


register_command(
    "set-jiggler",
    cmd_set_jiggler,
    "set-jiggler on [interval_s] | off — toggle the KVM jiggler (no arg = report state).",
    aliases=["/sj"],
)


# ============================================================
# Dispatcher
# ============================================================

def _parse_target_suffix(prefix_matched: str, rest: str, valid_targets: set[str]) -> tuple[Optional[str], str]:
    """Given that a command prefix matched, look for a -target suffix at the start of rest.
    Returns (target_or_None, remaining_payload). If rest starts with '-X' where X is a known
    target, strip it and return X. Otherwise return None and the original rest."""
    if not rest.startswith("-"):
        return None, rest
    # extract token up to next space or end
    after_dash = rest[1:]
    space = after_dash.find(" ")
    if space < 0:
        token, payload = after_dash, ""
    else:
        token, payload = after_dash[:space], after_dash[space + 1:]
    if token.lower() in valid_targets:
        return token.lower(), payload
    return None, rest


async def dispatch(state: BridgeState, text: str) -> bool:
    cfg = state.config
    valid_targets = set(cfg.targets)
    lowered = text.lower()

    for prefix, handler, _desc, _is_alias, accepts_target in _COMMANDS:
        # Three forms of match in priority order:
        #   1. Exact   : "/sm" exactly
        #   2. Suffixed: "/sm-mac ..." or "/sm-mac"
        #   3. Spaced  : "/sm hello"
        # Plus a defensive case for /sm"hi" (no space, quoted arg).

        if lowered == prefix:
            target, payload = None, ""
        elif lowered.startswith(prefix):
            rest = text[len(prefix):]
            if accepts_target:
                target, payload = _parse_target_suffix(prefix, rest, valid_targets)
            else:
                target, payload = None, rest

            # Past the optional -target suffix, we need either a space, EOL, or a quote.
            if target is None:
                # No target suffix matched; require space or EOL or quoted arg.
                if rest == "":
                    pass
                elif rest.startswith(" "):
                    payload = rest[1:]
                elif rest.lstrip().startswith(('"', '“')):
                    payload = rest
                else:
                    # e.g. "/sm-foo ..." with foo not a target — fail to match
                    continue
            else:
                # We did parse a target. Payload as already determined.
                pass
        else:
            continue

        # Resolve target
        chosen = target or (cfg.default_target if accepts_target else "")
        if accepts_target and chosen not in cfg.targets:
            await signal_send(cfg, f"⚠️ {prefix}: target '{chosen}' not configured.")
            return True

        async with state.command_lock:
            label = f"{prefix}" + (f"-{chosen}" if chosen else "")
            state.last_command = label
            state.last_command_at = time.monotonic()
            state.commands_total += 1
            try:
                await handler(state, chosen, payload)
            except Exception as e:
                log.exception("command handler error")
                await signal_send(cfg, f"❌ {label}: internal error: {e}")
        return True
    return False


# ============================================================
# Pending-draft watchdog (per target)
# ============================================================

async def pending_watchdog(state: BridgeState) -> None:
    cfg = state.config
    while True:
        await asyncio.sleep(5)
        for name, ts in state.targets.items():
            if (
                ts.draft_state == DraftState.WAITING_FOR_CONFIRM
                and ts.pending
                and (time.monotonic() - ts.pending.started_at) > cfg.pending_timeout_seconds
            ):
                log.info("[%s] pending draft timed out", name)
                ts.draft_state = DraftState.IDLE
                ts.pending = None
                await signal_send(cfg, f"⌛ [{ts.target.display_name}] draft timed out, discarded.")


# ============================================================
# Signal receive
# ============================================================

def extract_text_message(envelope: dict) -> tuple[Optional[str], Optional[str]]:
    env = envelope.get("envelope") or envelope
    src = env.get("source") or env.get("sourceNumber")
    msg = env.get("dataMessage")
    if not msg:
        sync = env.get("syncMessage") or {}
        msg = sync.get("sentMessage")
    if not msg:
        return None, None
    text = msg.get("message")
    if not isinstance(text, str):
        return None, None
    return src, text


async def receive_loop(state: BridgeState) -> None:
    cfg = state.config
    base = cfg.signal_api_base
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base
    ws_url = f"{ws_base.rstrip('/')}/v1/receive/{cfg.signal_number}"
    log.info("Signal WS: %s", ws_url)

    backoff = 2.0
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=15) as ws:
                log.info("Signal WebSocket connected")
                backoff = 2.0
                async for raw in ws:
                    try:
                        envelope = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    src, text = extract_text_message(envelope)
                    if text is None:
                        continue
                    if src and src != cfg.signal_number:
                        log.debug("ignoring message from non-self %s", src)
                        continue
                    log.info("incoming: %s%s", text[:80], "…" if len(text) > 80 else "")
                    await dispatch(state, text)
        except (websockets.WebSocketException, OSError) as e:
            log.warning("Signal WS error: %s — reconnect in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 60.0)


# ============================================================
# HTTP server (userscript bridge)
# ============================================================

async def http_poll(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    target_name = request.query.get("id", "").lower()
    if target_name not in state.targets:
        return web.json_response({"ok": False, "error": f"unknown target id '{target_name}'"}, status=400)

    ts = state.targets[target_name]
    ts.last_userscript_poll = time.monotonic()

    # Only return screenshot requests for THIS target.
    pending = [
        rid for rid, req in state.pending_screenshot_requests.items()
        if req.target_name == target_name
    ]
    return web.json_response({
        "target": target_name,
        "pending_screenshots": pending,
        "draft_state": ts.draft_state.value,
        "notifications_enabled": ts.notifications_enabled,
        "last_command": state.last_command,
        "commands_total": state.commands_total,
    })


async def http_screenshot(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    body = await request.json()
    req_id = body.get("request_id")
    data_url = body.get("data_url")
    target_name = (body.get("target") or "").lower()
    if not req_id or not data_url:
        return web.json_response({"ok": False, "error": "missing fields"}, status=400)
    req = state.pending_screenshot_requests.get(req_id)
    if not req:
        return web.json_response({"ok": False, "error": "unknown request_id"}, status=404)
    if target_name and target_name != req.target_name:
        return web.json_response({"ok": False, "error": "target mismatch"}, status=400)
    state.received_screenshots[req_id] = data_url
    req.event.set()
    log.info("screenshot received: id=%s target=%s (%d bytes)", req_id, req.target_name, len(data_url))
    return web.json_response({"ok": True})


async def http_notifications(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    body = await request.json()
    target_name = (body.get("target") or "").lower()
    enabled = bool(body.get("enabled"))
    if target_name not in state.targets:
        return web.json_response({"ok": False, "error": "unknown target"}, status=400)
    ts = state.targets[target_name]
    if ts.notifications_enabled != enabled:
        ts.notifications_enabled = enabled
        log.info("[%s] notifications: %s (via userscript)", target_name, "enabled" if enabled else "disabled")
    return web.json_response({"ok": True, "notifications_enabled": ts.notifications_enabled})


async def http_status(request: web.Request) -> web.Response:
    state: BridgeState = request.app["state"]
    cfg = state.config
    return web.json_response({
        "default_target": cfg.default_target,
        "targets": {
            name: {
                "display_name": ts.target.display_name,
                "draft_state": ts.draft_state.value,
                "notifications_enabled": ts.notifications_enabled,
                "kvm_base": ts.target.kvm_base,
                "keymap": ts.target.kvm_keymap,
            } for name, ts in state.targets.items()
        },
        "last_command": state.last_command,
        "commands_total": state.commands_total,
    })


async def start_http_server(state: BridgeState) -> web.AppRunner:
    cfg = state.config
    app = web.Application()
    app["state"] = state
    app.router.add_get("/poll", http_poll)
    app.router.add_post("/screenshot", http_screenshot)
    app.router.add_post("/notifications", http_notifications)
    app.router.add_get("/status", http_status)

    async def cors_mw(app_, handler_):
        async def mw(request):
            if request.method == "OPTIONS":
                return web.Response(headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                })
            r = await handler_(request)
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r
        return mw
    app.middlewares.append(cors_mw)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.bridge_host, cfg.bridge_port)
    await site.start()
    log.info("HTTP bridge listening on http://%s:%d", cfg.bridge_host, cfg.bridge_port)
    return runner


# ============================================================
# Main
# ============================================================

async def main() -> None:
    cfg = Config()
    load_targets(cfg)
    cfg.validate()

    state = BridgeState(config=cfg)
    for name, tcfg in cfg.targets.items():
        state.targets[name] = TargetState(target=tcfg)

    log.info("signal_to_kvm v3 starting")
    log.info("  signal: %s as %s", cfg.signal_api_base, cfg.signal_number)
    for name, t in cfg.targets.items():
        log.info("  target %s: %s (keymap=%s, verify_tls=%s)", name, t.kvm_base, t.kvm_keymap, t.kvm_verify_tls)
    log.info("  default target: %s", cfg.default_target)
    log.info("  bridge: %s:%d", cfg.bridge_host, cfg.bridge_port)
    log.info("  commands: %s", [p for p, _h, _d, _a, _at in _COMMANDS])

    targets_list = ", ".join(t.display_name for t in cfg.targets.values())
    await signal_send(
        cfg,
        f"🟢 KVM bridge v3 online. Targets: {targets_list}. Default: {cfg.default_target}. Send `help` for commands.",
    )

    runner = await start_http_server(state)
    try:
        await asyncio.gather(receive_loop(state), pending_watchdog(state))
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutdown")
        sys.exit(0)
