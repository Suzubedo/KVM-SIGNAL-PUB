// ==UserScript==
// @name         GLKVM Watcher + Multi-Target Bridge Companion
// @namespace    https://localhost/glkvm-watcher
// @version      0.9.0
// @description  Watches the KVM video for red-pixel events and acts as the daemon's eyes for one named target (mac, win, …).
// @include      http://glkvm.local/*
// @include      https://glkvm.local/*
// @include      http://192.168.*
// @include      https://192.168.*
// @include      *://*.glkvm.xyz/*
// @include      *://*.glkvm.site/*
// @include      *://*.glkvm.top/*
// @include      *://glkvm.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      127.0.0.1
// @connect      localhost
// @connect      192.168.*
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
    'use strict';
    console.log('[watcher] booting on', location.href);

    // ---------- config ----------
    // Target is derived from the KVM hostname at boot — no manual override needed.
    function detectTargetId() {
        const host = (location.hostname || '').toLowerCase();
        const map = [
            { keys: ['mac', 'macbook', 'mba'],   id: 'mac' },
            { keys: ['win', 'windows', 'pc'],    id: 'win' },
        ];
        for (const m of map) {
            if (m.keys.some(k => host.includes(k))) return m.id;
        }
        return '';
    }

    const KEYS = {
        signalUrl: 'rw_sig_url',
        signalFrom: 'rw_sig_from',
        signalTo: 'rw_sig_to',
        zone: 'rw_zone',
        shotZone: 'rw_shot_zone',
        attachShot: 'rw_attach_shot',
        rMin: 'rw_rmin',
        gMax: 'rw_gmax',
        bMax: 'rw_bmax',
        minPixels: 'rw_min_px',
        intervalMs: 'rw_interval',
        cooldownMs: 'rw_cooldown',
        maxLongEdge: 'rw_max_long_edge',
        bridgeUrl: 'rw_bridge_url',
        bridgeEnabled: 'rw_bridge_enabled',
    };

    const cfg = {
        targetId:      detectTargetId(),
        signalUrl:     GM_getValue(KEYS.signalUrl, 'http://127.0.0.1:8080'),
        signalFrom:    GM_getValue(KEYS.signalFrom, ''),
        signalTo:      GM_getValue(KEYS.signalTo, ''),
        zone:          GM_getValue(KEYS.zone, { x: 80, y: 0, w: 20, h: 15 }),
        shotZone:      GM_getValue(KEYS.shotZone, { x: 0, y: 0, w: 100, h: 100 }),
        attachShot:    GM_getValue(KEYS.attachShot, true),
        rMin:          GM_getValue(KEYS.rMin, 180),
        gMax:          GM_getValue(KEYS.gMax, 80),
        bMax:          GM_getValue(KEYS.bMax, 80),
        minPixels:     GM_getValue(KEYS.minPixels, 25),
        intervalMs:    GM_getValue(KEYS.intervalMs, 1500),
        cooldownMs:    GM_getValue(KEYS.cooldownMs, 30000),
        maxLongEdge:   GM_getValue(KEYS.maxLongEdge, 1280),
        bridgeUrl:     GM_getValue(KEYS.bridgeUrl, 'http://127.0.0.1:8765'),
        bridgeEnabled: GM_getValue(KEYS.bridgeEnabled, true),
    };
    console.log('[watcher] target id from hostname:', cfg.targetId || '(unknown)');

    let watching = false;
    let watchTimer = null;
    let lastFired = 0;
    let detectCanvas = null, detectCtx = null;
    let shotCanvas = null, shotCtx = null;
    let bridgeTimer = null;
    let bridgeFailures = 0;
    const bridgeServedRequests = new Set();

    // ---------- video discovery ----------
    function findVideo(root) {
        root = root || document;
        const v = root.querySelector('video');
        if (v && (v.videoWidth > 0 || v.readyState >= 2)) return v;
        const c = root.querySelector('canvas');
        if (c && c.width > 0) return c;
        return null;
    }

    // ---------- detection ----------
    function sampleOnce() {
        const src = findVideo();
        if (!src) return { ok: false, reason: 'no <video> yet' };
        const isVideo = src.tagName === 'VIDEO';
        const srcW = isVideo ? src.videoWidth : src.width;
        const srcH = isVideo ? src.videoHeight : src.height;
        if (!srcW || !srcH) return { ok: false, reason: 'video not ready (0x0)' };

        const zx = Math.floor((cfg.zone.x / 100) * srcW);
        const zy = Math.floor((cfg.zone.y / 100) * srcH);
        const zw = Math.max(1, Math.floor((cfg.zone.w / 100) * srcW));
        const zh = Math.max(1, Math.floor((cfg.zone.h / 100) * srcH));

        if (!detectCanvas) {
            detectCanvas = document.createElement('canvas');
            detectCtx = detectCanvas.getContext('2d', { willReadFrequently: true });
        }
        detectCanvas.width = zw; detectCanvas.height = zh;
        try { detectCtx.drawImage(src, zx, zy, zw, zh, 0, 0, zw, zh); }
        catch (e) { return { ok: false, reason: 'draw failed: ' + e.message }; }
        let img;
        try { img = detectCtx.getImageData(0, 0, zw, zh); }
        catch (e) { return { ok: false, reason: 'tainted: ' + e.message }; }

        const data = img.data;
        let red = 0;
        for (let i = 0; i < data.length; i += 4) {
            if (data[i] >= cfg.rMin && data[i + 1] <= cfg.gMax && data[i + 2] <= cfg.bMax) red++;
        }
        return { ok: true, redCount: red, total: zw * zh };
    }

    function captureScreenshotDataUrl(zone) {
        const src = findVideo();
        if (!src) return null;
        const isVideo = src.tagName === 'VIDEO';
        const srcW = isVideo ? src.videoWidth : src.width;
        const srcH = isVideo ? src.videoHeight : src.height;
        if (!srcW || !srcH) return null;

        const z = zone || cfg.shotZone;
        const sx = Math.floor((z.x / 100) * srcW);
        const sy = Math.floor((z.y / 100) * srcH);
        const sw = Math.max(1, Math.floor((z.w / 100) * srcW));
        const sh = Math.max(1, Math.floor((z.h / 100) * srcH));

        const longEdge = Math.max(sw, sh);
        const scale = longEdge > cfg.maxLongEdge ? cfg.maxLongEdge / longEdge : 1;
        const outW = Math.max(1, Math.round(sw * scale));
        const outH = Math.max(1, Math.round(sh * scale));

        if (!shotCanvas) { shotCanvas = document.createElement('canvas'); shotCtx = shotCanvas.getContext('2d'); }
        shotCanvas.width = outW; shotCanvas.height = outH;
        try { shotCtx.drawImage(src, sx, sy, sw, sh, 0, 0, outW, outH); }
        catch (e) { return null; }
        try { return shotCanvas.toDataURL('image/png'); }
        catch (e) { return null; }
    }

    // ---------- Signal direct (watcher-side red-detection alerts) ----------
    function signalSendAlert(message, dataUrl) {
        if (!cfg.signalUrl || !cfg.signalFrom || !cfg.signalTo) {
            setStatus('signal config incomplete', '#FFD27C'); return;
        }
        const tagged = (cfg.targetId ? `[${cfg.targetId}] ` : '') + message;
        const payload = { number: cfg.signalFrom, recipients: [cfg.signalTo], message: tagged };
        if (dataUrl) payload.base64_attachments = [dataUrl];
        GM_xmlhttpRequest({
            method: 'POST',
            url: cfg.signalUrl.replace(/\/+$/, '') + '/v2/send',
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify(payload),
            timeout: 30000,
            onload: r => setStatus(r.status >= 200 && r.status < 300
                ? 'alert sent (' + r.status + ')'
                : 'signal HTTP ' + r.status,
                r.status < 300 ? '#9CFF9C' : '#FF9C9C'),
            onerror: () => setStatus('signal error', '#FF9C9C'),
            ontimeout: () => setStatus('signal timeout', '#FF9C9C'),
        });
    }

    function fireAlert(redCount, total) {
        const now = Date.now();
        if (now - lastFired < cfg.cooldownMs) return;
        lastFired = now;
        const msg = '🔴 KVM red-watch trigger — ' + redCount + '/' + total + ' px @ ' + new Date().toLocaleTimeString();
        const data = cfg.attachShot ? captureScreenshotDataUrl() : null;
        signalSendAlert(msg, data);
    }

    function tick() {
        if (!watching) return;
        const r = sampleOnce();
        if (!r.ok) { setStatus(r.reason, '#FFD27C'); return; }
        const triggered = r.redCount >= cfg.minPixels;
        setStatus('red=' + r.redCount + '/' + r.total + (triggered ? ' — TRIGGER' : ''),
                  triggered ? '#FF6464' : '#A9D6FF');
        if (triggered) fireAlert(r.redCount, r.total);
    }

    function startWatching(opts) {
        opts = opts || {};
        if (watching) return;
        if (!cfg.signalUrl || !cfg.signalFrom || !cfg.signalTo) {
            if (opts.skipPushBack) {
                console.warn('[watcher] daemon enabled notifications but Signal config missing');
                pushNotificationsState(false);
                return;
            }
            alert('Set Signal API + numbers first.');
            return;
        }
        watching = true;
        $('#rw-toggle').textContent = 'Notifications: ON';
        $('#rw-toggle').style.background = '#7A1F1F';
        watchTimer = setInterval(tick, cfg.intervalMs);
        tick();
        if (!opts.skipPushBack) pushNotificationsState(true);
    }
    function stopWatching(opts) {
        opts = opts || {};
        watching = false;
        if (watchTimer) clearInterval(watchTimer);
        watchTimer = null;
        $('#rw-toggle').textContent = 'Notifications: OFF';
        $('#rw-toggle').style.background = '#1565C0';
        setStatus('notifications off', '#A9D6FF');
        if (!opts.skipPushBack) pushNotificationsState(false);
    }

    // ---------- Daemon bridge ----------
    function bridgePoll() {
        if (!cfg.bridgeEnabled || !cfg.bridgeUrl) return;
        if (!cfg.targetId) {
            setBridgeStatus('bridge: target id not set', '#FFB347');
            return;
        }
        const url = cfg.bridgeUrl.replace(/\/+$/, '') + '/poll?id=' + encodeURIComponent(cfg.targetId);
        GM_xmlhttpRequest({
            method: 'GET',
            url: url,
            timeout: 5000,
            onload: r => {
                if (r.status === 400) {
                    setBridgeStatus('bridge: target "' + cfg.targetId + '" rejected by daemon', '#FF8A80');
                    return;
                }
                if (r.status !== 200) { onBridgeFailure('HTTP ' + r.status); return; }
                bridgeFailures = 0;
                let body;
                try { body = JSON.parse(r.responseText); } catch { onBridgeFailure('bad JSON'); return; }
                renderBridgeStatus(body);
                (body.pending_screenshots || []).forEach(handleScreenshotRequest);
            },
            onerror: () => onBridgeFailure('network'),
            ontimeout: () => onBridgeFailure('timeout'),
        });
    }

    function onBridgeFailure(reason) {
        bridgeFailures++;
        if (bridgeFailures % 5 === 1) {
            setBridgeStatus('bridge: ' + reason + ' (×' + bridgeFailures + ')', '#FFB347');
        }
    }

    function handleScreenshotRequest(reqId) {
        if (bridgeServedRequests.has(reqId)) return;
        bridgeServedRequests.add(reqId);
        if (bridgeServedRequests.size > 200) {
            const arr = Array.from(bridgeServedRequests);
            bridgeServedRequests.clear();
            arr.slice(-100).forEach(x => bridgeServedRequests.add(x));
        }
        const dataUrl = captureScreenshotDataUrl();
        if (!dataUrl) {
            console.warn('[watcher] capture failed for daemon req', reqId);
            return;
        }
        GM_xmlhttpRequest({
            method: 'POST',
            url: cfg.bridgeUrl.replace(/\/+$/, '') + '/screenshot',
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ request_id: reqId, data_url: dataUrl, target: cfg.targetId }),
            timeout: 15000,
            onload: r => console.log('[watcher][' + cfg.targetId + '] screenshot', reqId, '→', r.status),
            onerror: e => console.warn('[watcher] screenshot post failed', e),
        });
    }

    function renderBridgeStatus(body) {
        setBridgeStatus(
            cfg.targetId + ' · daemon: ' + (body.draft_state || '?') +
            ' · last: ' + (body.last_command || 'none') +
            ' · total: ' + (body.commands_total || 0),
            '#9CFF9C'
        );
        // Reconcile our local watcher state with the daemon's per-target flag.
        if (typeof body.notifications_enabled === 'boolean') {
            if (body.notifications_enabled && !watching) {
                if (cfg.signalUrl && cfg.signalFrom && cfg.signalTo) {
                    console.log('[watcher][' + cfg.targetId + '] daemon flag → enabling watcher');
                    startWatching({ skipPushBack: true });
                }
            } else if (!body.notifications_enabled && watching) {
                console.log('[watcher][' + cfg.targetId + '] daemon flag → disabling watcher');
                stopWatching({ skipPushBack: true });
            }
        }
    }

    function pushNotificationsState(enabled) {
        if (!cfg.bridgeEnabled || !cfg.bridgeUrl || !cfg.targetId) return;
        GM_xmlhttpRequest({
            method: 'POST',
            url: cfg.bridgeUrl.replace(/\/+$/, '') + '/notifications',
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ enabled: enabled, target: cfg.targetId }),
            timeout: 4000,
            onload: r => console.log('[watcher][' + cfg.targetId + '] pushed notif=' + enabled + ' → ' + r.status),
            onerror: () => console.warn('[watcher] failed to push notifications state'),
        });
    }

    function setBridgeStatus(t, color) {
        const el = $('#rw-bridge-status');
        if (!el) return;
        el.textContent = t;
        if (color) el.style.color = color;
    }

    function startBridgePolling() {
        if (bridgeTimer) clearInterval(bridgeTimer);
        bridgeTimer = setInterval(bridgePoll, 2000);
        bridgePoll();
    }

    // ---------- panel ----------
    const panel = document.createElement('div');
    panel.id = 'red-watch-panel';
    panel.style.cssText =
        'position: fixed !important; top: 16px !important; right: 16px !important;' +
        'z-index: 2147483647 !important;' +
        'background: #0D47A1 !important; color: #fff !important;' +
        'font: 13px/1.4 system-ui, sans-serif !important;' +
        'border: 2px solid #42A5F5 !important; border-radius: 10px !important;' +
        'padding: 12px !important; width: 320px !important;' +
        'box-shadow: 0 6px 24px rgba(0,0,0,0.6) !important;';

    panel.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">' +
            '<strong>KVM Watcher + Bridge</strong>' +
            '<span id="rw-collapse" style="cursor:pointer;font-size:18px;">\u2212</span>' +
        '</div>' +
        '<div id="rw-body">' +
            '<button id="rw-toggle" style="width:100%;background:#1565C0;color:#fff;border:0;padding:10px;border-radius:4px;cursor:pointer;font-weight:600;margin-bottom:6px;">Notifications: OFF</button>' +
            '<div id="rw-status" style="font-family:ui-monospace,monospace;color:#A9D6FF;font-size:11px;margin-bottom:8px;word-break:break-word;min-height:14px;">idle</div>' +

            '<details open style="margin-bottom:6px;">' +
                '<summary style="cursor:pointer;font-size:12px;">Daemon bridge</summary>' +
                '<label>Daemon URL</label>' +
                '<input id="rw-bridge-url" type="text" placeholder="http://192.168.x.x:8765">' +
                '<label style="display:flex;align-items:center;gap:6px;margin-top:4px;font-size:12px;">' +
                    '<input id="rw-bridge-enabled" type="checkbox"> Bridge enabled' +
                '</label>' +
                '<div id="rw-bridge-status" style="font-family:ui-monospace,monospace;font-size:11px;color:#A9D6FF;margin-top:6px;">bridge: idle</div>' +
            '</details>' +

            '<details style="margin-bottom:6px;">' +
                '<summary style="cursor:pointer;font-size:12px;">Signal API (for watcher alerts)</summary>' +
                '<label>Signal API base URL</label><input id="rw-sig-url" type="text">' +
                '<label>Sender (your number)</label><input id="rw-sig-from" type="text">' +
                '<label>Recipient</label><input id="rw-sig-to" type="text">' +
            '</details>' +

            '<details style="margin-bottom:6px;">' +
                '<summary style="cursor:pointer;font-size:12px;">Watch zone (red detection)</summary>' +
                '<div style="display:grid;grid-template-columns:auto 1fr;gap:4px 8px;font-size:12px;">' +
                    '<label>x</label><input id="rw-zx" type="number">' +
                    '<label>y</label><input id="rw-zy" type="number">' +
                    '<label>w</label><input id="rw-zw" type="number">' +
                    '<label>h</label><input id="rw-zh" type="number">' +
                '</div>' +
                '<button id="rw-pick-watch" style="margin-top:6px;width:100%;background:#1976D2;color:#fff;border:0;padding:5px;border-radius:4px;cursor:pointer;">Pick watch zone\u2026</button>' +
            '</details>' +

            '<details style="margin-bottom:6px;">' +
                '<summary style="cursor:pointer;font-size:12px;">Screenshot zone</summary>' +
                '<label style="display:flex;align-items:center;gap:6px;font-size:12px;"><input id="rw-attach" type="checkbox"> Attach PNG to alerts</label>' +
                '<div style="display:grid;grid-template-columns:auto 1fr;gap:4px 8px;font-size:12px;">' +
                    '<label>x</label><input id="rw-sx" type="number">' +
                    '<label>y</label><input id="rw-sy" type="number">' +
                    '<label>w</label><input id="rw-sw" type="number">' +
                    '<label>h</label><input id="rw-sh" type="number">' +
                    '<label>Max edge px</label><input id="rw-maxedge" type="number">' +
                '</div>' +
                '<button id="rw-pick-shot" style="margin-top:6px;width:100%;background:#1976D2;color:#fff;border:0;padding:5px;border-radius:4px;cursor:pointer;">Pick screenshot zone\u2026</button>' +
                '<button id="rw-preview" style="margin-top:6px;width:100%;background:#26A69A;color:#fff;border:0;padding:5px;border-radius:4px;cursor:pointer;">Preview screenshot</button>' +
            '</details>' +

            '<details style="margin-bottom:6px;">' +
                '<summary style="cursor:pointer;font-size:12px;">Detection thresholds</summary>' +
                '<div style="display:grid;grid-template-columns:auto 1fr;gap:4px 8px;font-size:12px;">' +
                    '<label>R min</label><input id="rw-rmin" type="number">' +
                    '<label>G max</label><input id="rw-gmax" type="number">' +
                    '<label>B max</label><input id="rw-bmax" type="number">' +
                    '<label>Min px</label><input id="rw-minpx" type="number">' +
                    '<label>Period ms</label><input id="rw-int" type="number">' +
                    '<label>Cooldown ms</label><input id="rw-cd" type="number">' +
                '</div>' +
            '</details>';

    const css = document.createElement('style');
    css.textContent =
        '#red-watch-panel input[type="number"], #red-watch-panel input[type="text"]{' +
            'width:100%;box-sizing:border-box;background:#0A2E6B;color:#fff;' +
            'border:1px solid #42A5F5;border-radius:3px;padding:4px 6px;' +
            'font-size:12px;margin-top:2px;}' +
        '#red-watch-panel summary{color:#A9D6FF;}' +
        '#red-watch-panel label{color:#fff;font-size:11px;opacity:0.85;}';
    document.head.appendChild(css);
    document.documentElement.appendChild(panel);

    const $ = (s) => panel.querySelector(s);
    const statusEl = $('#rw-status');
    function setStatus(t, color) { statusEl.textContent = t; if (color) statusEl.style.color = color; }

    // hydrate
    $('#rw-bridge-url').value = cfg.bridgeUrl;
    $('#rw-bridge-enabled').checked = cfg.bridgeEnabled;
    $('#rw-sig-url').value = cfg.signalUrl;
    $('#rw-sig-from').value = cfg.signalFrom;
    $('#rw-sig-to').value = cfg.signalTo;
    $('#rw-zx').value = cfg.zone.x; $('#rw-zy').value = cfg.zone.y;
    $('#rw-zw').value = cfg.zone.w; $('#rw-zh').value = cfg.zone.h;
    $('#rw-sx').value = cfg.shotZone.x; $('#rw-sy').value = cfg.shotZone.y;
    $('#rw-sw').value = cfg.shotZone.w; $('#rw-sh').value = cfg.shotZone.h;
    $('#rw-attach').checked = cfg.attachShot;
    $('#rw-maxedge').value = cfg.maxLongEdge;
    $('#rw-rmin').value = cfg.rMin; $('#rw-gmax').value = cfg.gMax; $('#rw-bmax').value = cfg.bMax;
    $('#rw-minpx').value = cfg.minPixels;
    $('#rw-int').value = cfg.intervalMs; $('#rw-cd').value = cfg.cooldownMs;

    function persist() {
        cfg.bridgeUrl = $('#rw-bridge-url').value.trim();
        cfg.bridgeEnabled = $('#rw-bridge-enabled').checked;
        cfg.signalUrl = $('#rw-sig-url').value.trim();
        cfg.signalFrom = $('#rw-sig-from').value.trim();
        cfg.signalTo = $('#rw-sig-to').value.trim();
        cfg.zone = { x: +$('#rw-zx').value, y: +$('#rw-zy').value, w: +$('#rw-zw').value, h: +$('#rw-zh').value };
        cfg.shotZone = { x: +$('#rw-sx').value, y: +$('#rw-sy').value, w: +$('#rw-sw').value, h: +$('#rw-sh').value };
        cfg.attachShot = $('#rw-attach').checked;
        cfg.maxLongEdge = Math.max(200, +$('#rw-maxedge').value || 1280);
        cfg.rMin = +$('#rw-rmin').value;
        cfg.gMax = +$('#rw-gmax').value;
        cfg.bMax = +$('#rw-bmax').value;
        cfg.minPixels = +$('#rw-minpx').value;
        cfg.intervalMs = Math.max(200, +$('#rw-int').value);
        cfg.cooldownMs = +$('#rw-cd').value;

        Object.entries(KEYS).forEach(([k, key]) => GM_setValue(key, cfg[k]));

        if (watching) { clearInterval(watchTimer); watchTimer = setInterval(tick, cfg.intervalMs); }
        if (cfg.bridgeEnabled) { startBridgePolling(); }
        else { if (bridgeTimer) clearInterval(bridgeTimer); setBridgeStatus('bridge: disabled', '#888'); }
    }
    panel.querySelectorAll('input').forEach(e => e.addEventListener('change', persist));

    $('#rw-collapse').addEventListener('click', () => {
        const b = $('#rw-body');
        const collapsed = b.style.display === 'none';
        b.style.display = collapsed ? '' : 'none';
        $('#rw-collapse').textContent = collapsed ? '\u2212' : '+';
    });

    $('#rw-toggle').addEventListener('click', () => watching ? stopWatching() : startWatching());

    function pickZone(callback) {
        const v = findVideo();
        if (!v) { alert('No video.'); return; }
        const rect = v.getBoundingClientRect();
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;left:' + rect.left + 'px;top:' + rect.top + 'px;width:' + rect.width + 'px;height:' + rect.height + 'px;background:rgba(0,0,0,0.25);cursor:crosshair;z-index:2147483646;';
        document.body.appendChild(overlay);
        const sel = document.createElement('div');
        sel.style.cssText = 'position:absolute;border:2px dashed #42A5F5;background:rgba(66,165,245,0.2);';
        overlay.appendChild(sel);
        let sx = 0, sy = 0, drag = false;
        overlay.addEventListener('mousedown', e => { drag = true; sx = e.clientX - rect.left; sy = e.clientY - rect.top; sel.style.left = sx + 'px'; sel.style.top = sy + 'px'; sel.style.width = '0px'; sel.style.height = '0px'; });
        overlay.addEventListener('mousemove', e => { if (!drag) return; const cx = e.clientX - rect.left, cy = e.clientY - rect.top; sel.style.left = Math.min(sx, cx) + 'px'; sel.style.top = Math.min(sy, cy) + 'px'; sel.style.width = Math.abs(cx - sx) + 'px'; sel.style.height = Math.abs(cy - sy) + 'px'; });
        overlay.addEventListener('mouseup', () => {
            drag = false;
            const px = parseFloat(sel.style.left), py = parseFloat(sel.style.top);
            const pw = parseFloat(sel.style.width), ph = parseFloat(sel.style.height);
            if (pw > 4 && ph > 4) callback({ x: Math.round(px / rect.width * 100), y: Math.round(py / rect.height * 100), w: Math.round(pw / rect.width * 100), h: Math.round(ph / rect.height * 100) });
            overlay.remove();
        });
        const esc = e => { if (e.key === 'Escape') { overlay.remove(); window.removeEventListener('keydown', esc); } };
        window.addEventListener('keydown', esc);
    }

    $('#rw-pick-watch').addEventListener('click', () => pickZone(z => {
        $('#rw-zx').value = z.x; $('#rw-zy').value = z.y;
        $('#rw-zw').value = z.w; $('#rw-zh').value = z.h;
        persist();
    }));
    $('#rw-pick-shot').addEventListener('click', () => pickZone(z => {
        $('#rw-sx').value = z.x; $('#rw-sy').value = z.y;
        $('#rw-sw').value = z.w; $('#rw-sh').value = z.h;
        persist();
    }));
    $('#rw-preview').addEventListener('click', () => {
        persist();
        const data = captureScreenshotDataUrl();
        if (!data) { alert('Capture failed.'); return; }
        const w = window.open();
        if (w) w.document.write('<img src="' + data + '" style="max-width:100%;">');
    });

    if (!cfg.targetId) {
        setStatus('hostname not recognised — update detectTargetId()', '#FFD27C');
        setBridgeStatus('bridge: unknown target', '#FFB347');
    } else {
        setStatus('ready (' + cfg.targetId + ')');
    }
    if (cfg.bridgeEnabled) startBridgePolling();
    else setBridgeStatus('bridge: disabled', '#888');
})();
