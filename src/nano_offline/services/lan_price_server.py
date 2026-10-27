from __future__ import annotations

"""Built-in read-only LAN price-check server.

The main cashier PC hosts this tiny HTTP server (started from ``main.py`` when
the admin enables it). Customer price-check screens on the shop LAN open a
light web page and a barcode scanner (HID keyboard wedge) feeds codes in; the
server answers with the current price.

Security posture: **read-only**, no admin/POS/sale endpoints, and it only
reads existing rows through the normal repositories (SQLite is already WAL, so
concurrent reads never block the cashier). See ``core.lan_settings`` for the
admin-configurable keys.

Endpoints::

    GET /                        -> kiosk web page (responsive, RTL, 3D)
    GET /manifest.webmanifest    -> PWA manifest
    GET /sw.js                   -> service worker (offline shell)
    GET /icon-192.png /icon-512.png
    GET /api/health              -> {"ok": true, ...}
    GET /api/price?barcode=...   -> {"found": bool, "name", "price_number", ...}
    GET /api/search?q=...        -> {"results": [...]}

``lan_price_server_https`` serves the kiosk over HTTPS (self-signed) so the
browser Camera API (``getUserMedia``) and the Wake Lock API -- both blocked in
insecure contexts -- work on phones and Windows terminals.
"""

import base64
import json
import os
import socket
import ssl
import string
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from nano_offline.core import branding, currency, default_brand, lan_settings
from nano_offline.core.logging_config import get_logger
from nano_offline.core.units import is_weighted_unit
from nano_offline.repositories.item_repository import ItemRepository
from nano_offline.version import APP_VERSION

log = get_logger("nano.lan")

_DEFAULT_ACCENT = "#0B5FCC"
_DEFAULT_IDLE_MS = 12000
_MAX_BARCODE_LEN = 64

_SERVICE_WORKER = """const CACHE = 'cashir-price-v1';
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') { return; }
  var url = new URL(req.url);
  if (url.pathname.indexOf('/api/') === 0) {
    e.respondWith(fetch(req).catch(function () { return caches.match(req); }));
    return;
  }
  e.respondWith(caches.open(CACHE).then(function (c) {
    return c.match(req).then(function (r) {
      if (r) { return r; }
      return fetch(req).then(function (resp) { try { c.put(req, resp.clone()); } catch (err) {} return resp; });
    });
  }));
});
"""


def _safe_hex(value, fallback: str = _DEFAULT_ACCENT) -> str:
    """Normalize a user-provided colour to a safe ``#rrggbb`` string."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3 and all(c in "0123456789abcdefABCDEF" for c in text):
        text = "".join(c * 2 for c in text)
    if len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text):
        return "#" + text.lower()
    return fallback


def _rgb_csv(value) -> str:
    text = _safe_hex(value).lstrip("#")
    return f"{int(text[0:2], 16)},{int(text[2:4], 16)},{int(text[4:6], 16)}"


def _darken(value, factor: float = 0.72) -> str:
    text = _safe_hex(value).lstrip("#")
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02x}{:02x}{:02x}".format(int(r * factor), int(g * factor), int(b * factor))


def _ensure_self_signed(cert_dir, lan_ip: str):
    """Generate (once) a self-signed cert/key for the price server; return paths.

    Uses ``cryptography`` (already a dependency). The cert covers ``localhost``
    plus the machine's LAN IP so ``https://<ip>:port`` validates once trusted.
    Returns ``(cert_path, key_path)`` or ``None`` when it can't be created.
    """
    try:
        import datetime as _dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception:
        return None
    directory = Path(cert_dir)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "price_server_cert.pem"
    key_path = directory / "price_server_key.pem"
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Cashir Price Server")])
        sans = [x509.DNSName("localhost")]
        try:
            import ipaddress as _ip

            sans.append(x509.IPAddress(_ip.ip_address(lan_ip)))
        except Exception:
            pass
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return str(cert_path), str(key_path)
    except Exception:
        log.warning("تعذّر إنشاء شهادة TLS", exc_info=True)
        return None


# The kiosk page. Uses string.Template ($name) instead of an f-string so the
# CSS/JS braces stay untouched. No other '$' must appear in the markup.
# Layout is a single flex column (header / main / keypad) sized to 100dvh:
# nothing is position:fixed except the decorative floor, so it never overlaps
# at any size or orientation.
_KIOSK_TEMPLATE = string.Template(
    """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="$accent">
<title>$title</title>
<link rel="manifest" href="/manifest.webmanifest">
<style>
:root { --accent: $accent; --accent-deep: $accent_deep; --accent-rgb: $accent_rgb; --ink:#0f172a; --paper:rgba(255,255,255,.96); }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html { height: 100%; }
body {
  margin: 0; color: #eaf1ff; overflow: hidden;
  min-height: 100vh; min-height: 100dvh;
  display: flex; flex-direction: column;
  overscroll-behavior: none;
  font-family: 'Cairo','Segoe UI',Tahoma,Arial,sans-serif;
  background:
    radial-gradient(1100px 620px at 84% -12%, rgba(var(--accent-rgb),.55), transparent 60%),
    radial-gradient(900px 560px at -8% 116%, rgba(var(--accent-rgb),.35), transparent 62%),
    linear-gradient(155deg,#0a1020 0%,#111c33 52%,#0a1020 100%);
}
body.idle-cursor { cursor: none; }
body::before {
  content:''; position:fixed; left:-25%; right:-25%; bottom:-32%; height:70%; pointer-events:none; z-index:0;
  background-image:linear-gradient(rgba(255,255,255,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.06) 1px,transparent 1px);
  background-size:46px 46px; transform:perspective(620px) rotateX(62deg); transform-origin:bottom center;
  -webkit-mask-image:linear-gradient(to top,rgba(0,0,0,.5),transparent 82%);
  mask-image:linear-gradient(to top,rgba(0,0,0,.5),transparent 82%);
}
.topbar {
  flex: 0 0 auto; position: relative; z-index: 2;
  display: flex; align-items: center; justify-content: space-between; gap: clamp(8px,2vw,16px);
  padding: clamp(10px,2.2vh,20px) clamp(12px,3vw,28px);
}
.brand { display: flex; align-items: center; gap: clamp(8px,1.6vw,14px); min-width: 0; transform-style: preserve-3d; }
.logo-badge {
  width: clamp(42px,7vmin,62px); height: clamp(42px,7vmin,62px); flex: 0 0 auto; border-radius: clamp(12px,2vmin,18px);
  display: grid; place-items: center; overflow: hidden;
  background: linear-gradient(150deg,#ffffff,#e9f0ff);
  box-shadow: 0 16px 28px -12px rgba(0,0,0,.65), inset 0 2px 0 rgba(255,255,255,.9), inset 0 -6px 14px rgba(var(--accent-rgb),.20);
  transform: translateZ(30px) rotateX(6deg);
}
.logo-badge img { width: 100%; height: 100%; object-fit: contain; }
.wordmark { min-width: 0; transform: translateZ(18px); }
.wordmark .ar { font-size: clamp(18px,3.2vmin,26px); font-weight: 800; line-height: 1; white-space: nowrap; }
.wordmark .en { font-size: clamp(9px,1.5vmin,12px); letter-spacing: 3px; opacity: .78; text-transform: uppercase; margin-top: 2px; white-space: nowrap; }
.tools { display: flex; align-items: center; gap: clamp(6px,1.4vw,10px); flex: 0 0 auto; }
.pill {
  border: 1px solid rgba(255,255,255,.20); background: rgba(255,255,255,.08); color: #fff; border-radius: 999px;
  padding: clamp(7px,1.5vmin,10px) clamp(11px,2.2vmin,15px); font-size: clamp(12px,1.9vmin,14px); font-weight: 700; cursor: pointer;
  -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px); white-space: nowrap;
}
.pill.on { background: var(--accent); border-color: transparent; box-shadow: 0 12px 26px -12px var(--accent-deep); }
.dot { width: 9px; height: 9px; border-radius: 50%; background: #22c55e; box-shadow: 0 0 12px #22c55e; }
.main {
  flex: 1 1 auto; min-height: 0; position: relative; z-index: 1;
  display: flex; align-items: center; justify-content: center;
  padding: clamp(8px,2.4vh,28px) clamp(12px,4vw,40px); overflow: auto;
}
.idle { text-align: center; transform-style: preserve-3d; animation: float 5.5s ease-in-out infinite; }
.idle .glyph { font-size: clamp(48px,13vmin,104px); transform: translateZ(50px); filter: drop-shadow(0 24px 30px rgba(0,0,0,.5)); }
.idle h1 { font-size: clamp(18px,3.4vmin,30px); margin: clamp(8px,2vh,16px) 0 8px; }
.idle p { font-size: clamp(13px,2.2vmin,18px); opacity: .75; margin: 0; }
@keyframes float { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-12px); } }
.plate { width: min(840px,94%); transform-style: preserve-3d; display: none; }
.plate.show { display: block; animation: rise .40s cubic-bezier(.2,.8,.2,1); }
@keyframes rise { from { opacity:0; transform: translateY(24px) rotateX(14deg) scale(.96); } to { opacity:1; transform: translateY(0) rotateX(0) scale(1); } }
.plate .glow { position: absolute; inset: auto 8% -24px 8%; height: 56px; background: radial-gradient(closest-side, rgba(0,0,0,.5), transparent); filter: blur(6px); transform: translateZ(-40px); }
.card {
  position: relative; background: var(--paper); color: var(--ink); text-align: center;
  border-radius: clamp(18px,3vmin,30px); padding: clamp(20px,4vmin,44px) clamp(20px,5vmin,44px);
  box-shadow: 0 44px 74px -32px rgba(0,0,0,.72), 0 18px 30px -20px rgba(0,0,0,.5), inset 0 2px 0 #fff;
  transform: rotateX(4deg); max-width: 100%;
}
.card::after { content:''; position:absolute; inset:0; border-radius: inherit; background: linear-gradient(180deg,rgba(var(--accent-rgb),.07),transparent 42%); pointer-events:none; }
.cat { display: inline-block; font-size: clamp(11px,1.8vmin,14px); font-weight: 700; color: var(--accent-deep); background: rgba(var(--accent-rgb),.13); padding: 5px 14px; border-radius: 999px; margin-bottom: clamp(6px,1.4vmin,12px); }
.name { font-size: clamp(20px,4.4vmin,34px); font-weight: 800; line-height: 1.25; margin-bottom: clamp(8px,1.8vmin,16px); overflow-wrap: anywhere; }
.price { display: flex; align-items: baseline; justify-content: center; gap: clamp(8px,2vmin,14px); direction: rtl; flex-wrap: wrap; }
.price .num {
  font-size: clamp(40px,12vmin,108px); font-weight: 900; direction: ltr; unicode-bidi: isolate; font-variant-numeric: tabular-nums; letter-spacing: 1px; line-height: 1;
  background: linear-gradient(180deg, var(--accent), var(--accent-deep)); -webkit-background-clip: text; background-clip: text; color: transparent;
  text-shadow: 0 2px 0 rgba(255,255,255,.55), 0 18px 26px rgba(var(--accent-rgb),.28);
}
.price .cur { font-size: clamp(16px,3vmin,26px); font-weight: 800; color: #64748b; }
.unit { margin-top: clamp(6px,1.6vmin,12px); font-size: clamp(13px,2.2vmin,20px); color: #475569; }
.badge { display: inline-block; margin-top: clamp(8px,1.8vmin,16px); font-size: clamp(11px,1.8vmin,14px); font-weight: 700; color: #b45309; background: #fff7ed; padding: 5px 14px; border-radius: 999px; }
.miss { padding: clamp(20px,4vmin,40px); }
.miss .x { font-size: clamp(40px,9vmin,72px); filter: drop-shadow(0 16px 22px rgba(239,68,68,.35)); }
.miss h2 { font-size: clamp(18px,3.4vmin,28px); margin: 10px 0 4px; }
.miss p { opacity: .75; margin: 0; font-size: clamp(12px,2vmin,16px); }
.results { width: min(840px,94%); display: none; }
.results.show { display: block; }
.rrow { display: flex; justify-content: space-between; align-items: center; gap: clamp(8px,2vw,16px); background: var(--paper); color: var(--ink);
  border-radius: clamp(12px,2vmin,18px); padding: clamp(10px,2vmin,16px) clamp(14px,3vmin,22px); margin-bottom: clamp(8px,1.6vmin,12px);
  box-shadow: 0 26px 42px -26px rgba(0,0,0,.6); }
.rrow .rn { font-size: clamp(14px,2.4vmin,20px); font-weight: 700; overflow-wrap: anywhere; }
.rrow .rp { font-size: clamp(16px,2.8vmin,24px); font-weight: 900; color: var(--accent-deep); direction: rtl; white-space: nowrap; }
.rrow .rp .cur { font-size: clamp(10px,1.7vmin,14px); color: #64748b; margin-inline-start: 6px; }
.pad {
  flex: 0 0 auto; display: none; position: relative; z-index: 2;
  padding: clamp(8px,1.8vh,16px) clamp(12px,4vw,24px) calc(clamp(8px,1.8vh,16px) + env(safe-area-inset-bottom, 0px));
  background: linear-gradient(0deg, rgba(6,10,20,.92), transparent);
}
.pad.show { display: block; }
.manual { display: flex; align-items: center; justify-content: center; gap: 12px; min-height: 1.7em; margin-bottom: 8px; color: #fff; }
.manual #manualView { font-size: clamp(16px,3vmin,22px); font-weight: 900; letter-spacing: 3px; direction: ltr; min-width: 2ch; }
.manual #manualView.empty { opacity: .45; font-weight: 700; letter-spacing: 1px; }
.mini { background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.2); color: #fff; border-radius: 999px; padding: 4px 12px; font-size: clamp(11px,1.8vmin,13px); font-weight: 700; cursor: pointer; }
.pad .grid { max-width: 460px; margin: 0 auto; display: grid; grid-template-columns: repeat(3, 1fr); gap: clamp(6px,1.4vmin,10px); }
.key {
  min-height: clamp(38px,7vmin,58px); background: rgba(255,255,255,.10); color: #fff; border: 1px solid rgba(255,255,255,.18);
  border-radius: clamp(10px,2vmin,16px); font-size: clamp(16px,3vmin,22px); font-weight: 800; cursor: pointer;
  display: grid; place-items: center; -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px);
}
.key:active { transform: translateY(2px); }
.key.acc { background: var(--accent); border-color: transparent; }
.hidden-input { position: fixed; top: -120px; left: -120px; width: 1px; height: 1px; opacity: 0; border: 0; }
.cam { position: fixed; inset: 0; z-index: 20; display: none; background: #05070d; }
.cam.show { display: block; }
.cam video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.cam .frame { position: absolute; left: 8%; right: 8%; top: 26%; height: 30%; border: 3px solid rgba(255,255,255,.9); border-radius: 18px; box-shadow: 0 0 0 100vmax rgba(0,0,0,.35); }
.cam .scanline { position: absolute; left: 8%; right: 8%; height: 3px; background: var(--accent); box-shadow: 0 0 18px 4px rgba(var(--accent-rgb),.9); top: 27%; animation: scan 2.2s ease-in-out infinite; }
@keyframes scan { 0%,100% { top: 27%; } 50% { top: 55%; } }
.cam .cambar { position: absolute; left: 0; right: 0; bottom: calc(18px + env(safe-area-inset-bottom, 0px)); display: flex; justify-content: center; gap: 12px; }
.cam .cambar .pill { background: rgba(255,255,255,.16); }
.cam .msg { position: absolute; left: 0; right: 0; top: 50%; transform: translateY(-50%); text-align: center; font-size: clamp(14px,2.6vmin,20px); padding: 0 24px; }
@media (max-height: 520px) {
  .idle .glyph { display: none; }
  .logo-badge { width: clamp(36px,9vmin,48px); height: clamp(36px,9vmin,48px); }
  .price .num { font-size: clamp(34px,15vmin,72px); }
  .name { font-size: clamp(18px,4.4vmin,26px); }
  .main { padding: clamp(6px,1.6vh,14px) clamp(10px,3vw,24px); }
  .cam .frame { top: 18%; height: 40%; }
}
</style>
</head>
<body>
<input id="scan" class="hidden-input" autocomplete="off" inputmode="none">

<header class="topbar">
  <div class="brand">
    <div class="logo-badge"><img id="logo" src="$logo" alt=""></div>
    <div class="wordmark">
      <div class="ar">$app_ar</div>
      <div class="en">$app_en</div>
    </div>
  </div>
  <div class="tools">
    <div class="pill" id="langBtn">EN</div>
    <div class="pill" id="manualBtn">إدخال يدوي</div>
    <div class="pill" id="camBtn">الكاميرا</div>
    <div class="pill" id="fullBtn">⛶</div>
    <div class="pill" style="pointer-events:none"><span class="dot"></span></div>
  </div>
</header>

<main class="main">
  <section class="idle" id="idle">
    <div class="glyph">🛒</div>
    <h1 id="idleTitle">امسح باركود المنتج</h1>
    <p id="hint">مرّر الباركود أمام القارئ لمعرفة السعر</p>
  </section>

  <section class="plate" id="pricePlate">
    <div class="glow"></div>
    <div class="card">
      <div class="cat" id="pCat" style="display:none"></div>
      <div class="name" id="pName"></div>
      <div class="price"><span class="num" id="pNum">0</span><span class="cur" id="pCur"></span></div>
      <div class="unit" id="pUnit"></div>
      <div class="badge" id="pBadge" style="display:none"></div>
    </div>
  </section>

  <section class="plate" id="missPlate">
    <div class="glow"></div>
    <div class="card miss">
      <div class="x">⚠️</div>
      <h2 id="missTitle">هذا المنتج غير مُدرَج في النظام</h2>
      <p id="missText">جرّب مسحًا آخر أو ابحث بالاسم</p>
    </div>
  </section>

  <section class="results" id="results"></section>
</main>

<div class="pad" id="pad">
  <div class="manual"><span id="manualView" class="empty">—</span><button type="button" class="mini" id="padClear">مسح</button></div>
  <div class="grid" id="padGrid"></div>
</div>

<div class="cam" id="cam">
  <video id="camVideo" playsinline muted></video>
  <div class="frame"></div>
  <div class="scanline"></div>
  <div class="msg" id="camMsg"></div>
  <div class="cambar">
    <div class="pill" id="camSwitch">تبديل الكاميرا</div>
    <div class="pill" id="camTorch">الفلاش</div>
    <div class="pill" id="camClose">إغلاق</div>
  </div>
</div>

<script>
(function () {
  var LANG = 'ar';
  var TEXT = {
    ar: { hint:'مرّر الباركود أمام القارئ لمعرفة السعر', manual:'إدخال يدوي', enter:'استعلام', clear:'مسح',
          missT:'هذا المنتج غير مُدرَج في النظام', missP:'جرّب مسحًا آخر أو ابحث بالاسم', idleT:'امسح باركود المنتج',
          weigh:'يُوزن', perUnit:'السعر لكل', price:'سعر', cam:'الكاميرا', camNoHttps:'الكاميرا تتطلّب تشغيل الخادم عبر HTTPS',
          camUnsupported:'قراءة الباركود بالكاميرا غير مدعومة في هذا المتصفح', camDenied:'تعذّر الوصول للكاميرا أو رُفض الإذن',
          camSwitch:'تبديل الكاميرا', camTorch:'الفلاش', camClose:'إغلاق' },
    en: { hint:'Pass the barcode in front of the reader to see the price', manual:'Keypad', enter:'Check', clear:'Clear',
          missT:'Item not found', missP:'Try another scan or search by name', idleT:'Scan a product barcode',
          weigh:'Sold by weight', perUnit:'Price per', price:'Price', cam:'Camera', camNoHttps:'The camera needs the server served over HTTPS',
          camUnsupported:'Camera barcode scanning is not supported in this browser', camDenied:'Camera unavailable or permission denied',
          camSwitch:'Switch camera', camTorch:'Flash', camClose:'Close' }
  };
  var input = document.getElementById('scan');
  var idle = document.getElementById('idle');
  var pricePlate = document.getElementById('pricePlate');
  var missPlate = document.getElementById('missPlate');
  var resultsEl = document.getElementById('results');
  var pad = document.getElementById('pad');
  var manualView = document.getElementById('manualView');
  var pNum = document.getElementById('pNum'), pCur = document.getElementById('pCur');
  var pName = document.getElementById('pName'), pCat = document.getElementById('pCat');
  var pUnit = document.getElementById('pUnit'), pBadge = document.getElementById('pBadge');
  var idleTimer = null, buffer = '', cursorTimer = null;
  var IDLE_MS = $idle_ms;
  var IDLE_MS_MANUAL = Math.max(30000, IDLE_MS * 3);

  function t(k) { return TEXT[LANG][k] || ''; }
  function isDigits(s) { if (!s) { return false; } for (var i = 0; i < s.length; i++) { var c = s.charCodeAt(i); if (c < 48 || c > 57) { return false; } } return true; }

  function beep(ok) {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) { return; }
      var ac = window.__ac || (window.__ac = new Ctx());
      var osc = ac.createOscillator(), g = ac.createGain();
      osc.connect(g); g.connect(ac.destination);
      osc.type = 'sine'; osc.frequency.value = ok ? 900 : 200;
      g.gain.setValueAtTime(0.001, ac.currentTime);
      g.gain.exponentialRampToValueAtTime(0.22, ac.currentTime + 0.01);
      g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + 0.2);
      osc.start(); osc.stop(ac.currentTime + 0.22);
    } catch (e) {}
  }

  function applyLang() {
    document.documentElement.lang = LANG;
    document.documentElement.dir = (LANG === 'ar') ? 'rtl' : 'ltr';
    document.getElementById('hint').textContent = t('hint');
    document.getElementById('idleTitle').textContent = t('idleT');
    document.getElementById('manualBtn').textContent = t('manual');
    document.getElementById('camBtn').textContent = t('cam');
    document.getElementById('camSwitch').textContent = t('camSwitch');
    document.getElementById('camTorch').textContent = t('camTorch');
    document.getElementById('camClose').textContent = t('camClose');
    document.getElementById('missTitle').textContent = t('missT');
    document.getElementById('missText').textContent = t('missP');
    document.getElementById('langBtn').textContent = (LANG === 'ar') ? 'EN' : 'ع';
    document.getElementById('padClear').textContent = t('clear');
    revealBuffer();
  }

  function hideAll() { idle.style.display = 'none'; pricePlate.classList.remove('show'); missPlate.classList.remove('show'); resultsEl.classList.remove('show'); }
  function scheduleReset() {
    if (idleTimer) { clearTimeout(idleTimer); }
    var composing = pad.classList.contains('show') && buffer.length > 0;
    idleTimer = setTimeout(reset, composing ? IDLE_MS_MANUAL : IDLE_MS);
  }
  function reset() {
    hideAll(); idle.style.display = '';
    buffer = ''; revealBuffer();
    pad.classList.remove('show');
    try { input.focus(); } catch (e) {}
    scheduleReset();
  }

  function unitLabel(data) {
    var u = data.unit || '';
    if (data.weighted) { return t('weigh') + (u ? (' — ' + t('perUnit') + ' ' + u) : ''); }
    return u ? (t('price') + ' / ' + u) : '';
  }

  function showPrice(data) {
    hideAll();
    pCat.textContent = data.category || '';
    pCat.style.display = data.category ? '' : 'none';
    pName.textContent = data.name || '';
    pNum.textContent = data.price_number || data.price_text || '0';
    pCur.textContent = data.price_symbol || '';
    pUnit.textContent = unitLabel(data);
    pBadge.style.display = data.weighted ? '' : 'none';
    pBadge.textContent = data.weighted ? t('weigh') : '';
    pricePlate.classList.add('show');
    beep(true);
    scheduleReset();
  }

  function showMiss() { hideAll(); missPlate.classList.add('show'); beep(false); scheduleReset(); }

  function renderResults(rows) {
    hideAll();
    if (!rows.length) { showMiss(); return; }
    resultsEl.innerHTML = '';
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var el = document.createElement('div'); el.className = 'rrow';
      var rn = document.createElement('span'); rn.className = 'rn';
      rn.textContent = row.name + (row.weighted ? (' · ' + t('weigh')) : '');
      var rp = document.createElement('span'); rp.className = 'rp';
      var b = document.createElement('span'); b.textContent = row.price_number || row.price_text || '';
      var c = document.createElement('span'); c.className = 'cur'; c.textContent = row.price_symbol || '';
      rp.appendChild(b); rp.appendChild(c);
      el.appendChild(rn); el.appendChild(rp);
      resultsEl.appendChild(el);
    }
    resultsEl.classList.add('show');
    beep(true);
    scheduleReset();
  }

  function lookup(code) {
    code = (code || '').replace(/[^0-9A-Za-z]/g, '');
    if (!code) { return; }
    fetch('/api/price?barcode=' + encodeURIComponent(code), { cache: 'no-store' })
      .then(function (r) { return r.json(); }).then(showPrice).catch(function () { showMiss(); });
  }
  function search(q) {
    q = (q || '').trim();
    if (!q) { return; }
    fetch('/api/search?q=' + encodeURIComponent(q), { cache: 'no-store' })
      .then(function (r) { return r.json(); }).then(function (d) { renderResults(d.results || []); }).catch(function () { showMiss(); });
  }
  function dispatch(v) {
    v = (v || '').trim();
    if (!v) { return; }
    if (isDigits(v) && v.length >= 4) { lookup(v); } else { search(v); }
  }

  input.addEventListener('keydown', function (e) {
    scheduleReset();
    if (e.key === 'Enter' || e.keyCode === 13) { var v = input.value; input.value = ''; dispatch(v); e.preventDefault(); }
  });
  document.addEventListener('click', function (e) {
    if (cam.classList.contains('show')) { return; }
    try { input.focus(); } catch (err) {}
  });
  window.addEventListener('load', function () { try { input.focus(); } catch (e) {} scheduleReset(); });

  // on-screen numeric keypad (kiosks without a scanner) -- in normal flow, so
  // it shrinks the main area instead of covering it, on any screen size.
  function press(d) { if (buffer.length < 24) { buffer += d; revealBuffer(); scheduleReset(); } }
  function backspace() { buffer = buffer.slice(0, -1); revealBuffer(); scheduleReset(); }
  function clearBuffer() { buffer = ''; revealBuffer(); scheduleReset(); }
  function revealBuffer() {
    if (buffer) { manualView.textContent = buffer; manualView.classList.remove('empty'); }
    else { manualView.textContent = '—'; manualView.classList.add('empty'); }
  }
  var keys = ['1','2','3','4','5','6','7','8','9','⌫','0','OK'];
  for (var k = 0; k < keys.length; k++) {
    (function (label) {
      var el = document.createElement('div');
      el.className = 'key' + ((label === 'OK') ? ' acc' : '');
      el.textContent = (label === 'OK') ? t('enter') : label;
      el.setAttribute('data-key', label);
      el.onclick = function () {
        if (label === 'OK') { var v = buffer; buffer = ''; revealBuffer(); dispatch(v); }
        else if (label === '⌫') { backspace(); }
        else { press(label); }
      };
      document.getElementById('padGrid').appendChild(el);
    })(keys[k]);
  }
  function relabelKeys() {
    var nodes = document.querySelectorAll('#padGrid .key');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute('data-key') === 'OK') { nodes[i].textContent = t('enter'); }
    }
  }
  document.getElementById('padClear').onclick = clearBuffer;
  document.getElementById('manualBtn').onclick = function () {
    pad.classList.toggle('show');
    revealBuffer();
    scheduleReset();
    try { input.focus(); } catch (e) {}
  };
  document.getElementById('langBtn').onclick = function () { LANG = (LANG === 'ar') ? 'en' : 'ar'; applyLang(); relabelKeys(); };

  // fullscreen + wake lock (appliance mode)
  function toggleFull() {
    try {
      if (document.fullscreenElement) { document.exitFullscreen(); }
      else if (document.documentElement.requestFullscreen) { document.documentElement.requestFullscreen(); }
    } catch (e) {}
  }
  document.getElementById('fullBtn').onclick = toggleFull;
  var wakeLock = null;
  function requestWake() {
    try {
      if (navigator.wakeLock && navigator.wakeLock.request) {
        navigator.wakeLock.request('screen').then(function (wl) { wakeLock = wl; }).catch(function () {});
      }
    } catch (e) {}
  }
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { requestWake(); } });
  document.addEventListener('click', requestWake, { once: true });

  // hide the cursor when idle (kiosk)
  function markCursor() {
    document.body.classList.remove('idle-cursor');
    if (cursorTimer) { clearTimeout(cursorTimer); }
    cursorTimer = setTimeout(function () { document.body.classList.add('idle-cursor'); }, 4000);
  }
  document.addEventListener('mousemove', markCursor);
  document.addEventListener('touchstart', markCursor, { passive: true });
  markCursor();

  // camera barcode scanning (BarcodeDetector + getUserMedia; needs HTTPS)
  var cam = document.getElementById('cam');
  var video = document.getElementById('camVideo');
  var camMsg = document.getElementById('camMsg');
  var camState = { stream: null, detector: null, timer: null, track: null, facing: 'environment', last: '', lastAt: 0 };
  function camMessage(m) { camMsg.textContent = m || ''; }
  function camTick() {
    if (!camState.stream) { return; }
    camState.timer = setTimeout(function () {
      if (video.readyState === 4 && camState.detector) {
        camState.detector.detect(video).then(function (codes) {
          if (codes && codes.length) {
            var v = String(codes[0].rawValue || '');
            var now = Date.now();
            if (v && !(v === camState.last && (now - camState.lastAt) < 2500)) {
              camState.last = v; camState.lastAt = now;
              closeCamera();
              dispatch(v);
              return;
            }
          }
          camTick();
        }).catch(function () { camTick(); });
      } else { camTick(); }
    }, 220);
  }
  function openCamera() {
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) { cam.classList.add('show'); camMessage(t('camNoHttps')); return; }
    if (!('BarcodeDetector' in window)) { cam.classList.add('show'); camMessage(t('camUnsupported')); return; }
    cam.classList.add('show');
    camMessage('');
    camState.last = ''; camState.lastAt = 0;
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: camState.facing } }, audio: false })
      .then(function (s) {
        camState.stream = s;
        camState.track = s.getVideoTracks ? s.getVideoTracks()[0] : null;
        video.srcObject = s;
        try { video.play(); } catch (e) {}
        try { camState.detector = new BarcodeDetector({ formats: ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128', 'code_39', 'itf', 'qr_code'] }); }
        catch (e) { camState.detector = new BarcodeDetector(); }
        camTick();
      })
      .catch(function () { camMessage(t('camDenied')); });
  }
  function closeCamera() {
    if (camState.timer) { clearTimeout(camState.timer); camState.timer = null; }
    if (camState.stream) { try { camState.stream.getTracks().forEach(function (tr) { tr.stop(); }); } catch (e) {} }
    camState.stream = null; camState.detector = null; camState.track = null;
    cam.classList.remove('show');
    camMessage('');
  }
  document.getElementById('camBtn').onclick = openCamera;
  document.getElementById('camClose').onclick = closeCamera;
  document.getElementById('camSwitch').onclick = function () {
    camState.facing = (camState.facing === 'environment') ? 'user' : 'environment';
    closeCamera(); openCamera();
  };
  document.getElementById('camTorch').onclick = function () {
    try {
      if (camState.track && camState.track.applyConstraints) {
        var on = !(camState.torchOn);
        camState.torchOn = on;
        camState.track.applyConstraints({ advanced: [{ torch: on }] });
      }
    } catch (e) {}
  };

  // PWA: register the service worker for offline resilience
  if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(function () {}); }

  applyLang();
})();
</script>
</body>
</html>
"""
)


class _Handler(BaseHTTPRequestHandler):
    server_version = "CashirPriceCheck/1.0"

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        try:
            log.debug("lan %s", fmt % args)
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        app = getattr(self.server, "app", None)
        if app is None:
            self._json({"ok": False, "error": "unavailable"}, 503)
            return
        client = self.client_address[0] if self.client_address else ""
        if not app.client_allowed(client):
            self._json({"ok": False, "error": "forbidden"}, 403)
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, app.kiosk_html().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/manifest.webmanifest":
            self._send(200, app.manifest().encode("utf-8"), "application/manifest+json; charset=utf-8")
        elif path == "/sw.js":
            self._send(200, app.service_worker().encode("utf-8"), "application/javascript; charset=utf-8")
        elif path in ("/icon-192.png", "/icon-512.png", "/icon.png"):
            self._send(200, app.icon_png(), "image/png")
        elif path == "/api/health":
            self._json({"ok": True, "app": "cashir-price-check", "version": APP_VERSION})
        elif path == "/api/price":
            code = (parse_qs(parsed.query).get("barcode") or [""])[0]
            self._json(app.lookup(code))
        elif path == "/api/search":
            query = (parse_qs(parsed.query).get("q") or [""])[0]
            self._json(app.search(query))
        else:
            self._json({"ok": False, "error": "not_found"}, 404)


class LanPriceServer:
    """Read-only HTTP server that answers barcode -> price over the LAN."""

    def __init__(
        self,
        db,
        settings,
        *,
        host: str = lan_settings.DEFAULT_HOST,
        port: int = lan_settings.DEFAULT_PORT,
        company: str = "",
        title: str = "",
        accent: str = _DEFAULT_ACCENT,
        logo: str = "",
        app_ar: str = branding.APP_NAME_AR,
        app_en: str = branding.APP_NAME_EN,
        idle_ms: int = _DEFAULT_IDLE_MS,
        https: bool = False,
        cert_dir=None,
    ):
        self._db = db
        self._settings = settings
        self._items = ItemRepository(db)
        self._host = host
        self._port = int(port)
        self._company = company or branding.APP_NAME_AR
        self._title = title or self._company
        self._accent = _safe_hex(accent)
        self._logo = logo or default_brand.DEFAULT_COMPANY_LOGO
        self._app_ar = app_ar or branding.APP_NAME_AR
        self._app_en = app_en or branding.APP_NAME_EN
        self._idle_ms = int(idle_ms)
        self._https = bool(https)
        self._cert_dir = cert_dir
        self._scheme = "https" if self._https else "http"
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            try:
                return int(self._httpd.server_address[1])
            except Exception:
                pass
        return self._port

    @property
    def url(self) -> str:
        return f"{self._scheme}://{self._lan_ip()}:{self.port}/"

    def client_allowed(self, host: str) -> bool:
        try:
            return lan_settings.client_allowed(self._settings, host)
        except Exception:
            return True

    def kiosk_html(self) -> str:
        return _KIOSK_TEMPLATE.substitute(
            company=self._company,
            title=self._title,
            accent=self._accent,
            accent_rgb=_rgb_csv(self._accent),
            accent_deep=_darken(self._accent),
            logo=self._logo,
            app_ar=self._app_ar,
            app_en=self._app_en,
            idle_ms=str(self._idle_ms),
        )

    def manifest(self) -> str:
        return json.dumps(
            {
                "name": f"{self._app_ar} · {self._app_en}",
                "short_name": self._app_ar,
                "description": branding.APP_TAGLINE,
                "start_url": "/",
                "scope": "/",
                "display": "fullscreen",
                "orientation": "any",
                "background_color": "#0a1020",
                "theme_color": self._accent,
                "lang": "ar",
                "dir": "rtl",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                ],
            },
            ensure_ascii=False,
        )

    def service_worker(self) -> str:
        return _SERVICE_WORKER

    def icon_png(self) -> bytes:
        try:
            return base64.b64decode(str(default_brand.DEFAULT_COMPANY_LOGO).split(",", 1)[1])
        except Exception:
            return b""

    def _price_fields(self, value) -> dict:
        """Number + symbol separately so the kiosk can place the symbol itself.

        ``format_amount`` converts a *stored USD* price into the display
        currency (``format_display_value`` does not convert).
        """
        number = ""
        symbol = ""
        try:
            number = currency.format_amount(value, self._settings, with_symbol=False)
        except Exception:
            number = ""
        try:
            symbol = currency.get_display_symbol(self._settings)
        except Exception:
            symbol = ""
        return {
            "price_number": number,
            "price_symbol": symbol,
            "price_text": (number + " " + symbol).strip(),
        }

    def lookup(self, barcode: str) -> dict:
        code = (barcode or "").strip()[:_MAX_BARCODE_LEN]
        if not code:
            return {"found": False}
        try:
            item = self._items.find_by_barcode(code)
        except Exception:
            log.warning("LAN price lookup failed for %r", code, exc_info=True)
            return {"found": False}
        if not item:
            return {"found": False}
        unit = str(item.get("unit_abbreviation") or item.get("unit_name") or "").strip()
        result = {
            "found": True,
            "barcode": code,
            "name": str(item.get("name") or ""),
            "category": str(item.get("category_name") or ""),
            "unit": unit,
            "weighted": is_weighted_unit(item.get("unit_abbreviation"), item.get("unit_name")),
        }
        result.update(self._price_fields(item.get("selling_price")))
        return result

    def search(self, query: str, limit: int = 8) -> dict:
        text = (query or "").strip()[:64]
        if not text:
            return {"results": []}
        try:
            rows = self._items.list(search=text, limit=limit)
        except Exception:
            log.warning("LAN price search failed for %r", text, exc_info=True)
            return {"results": []}
        results = []
        for item in rows:
            unit = str(item.get("unit_abbreviation") or item.get("unit_name") or "").strip()
            row = {
                "name": str(item.get("name") or ""),
                "unit": unit,
                "weighted": is_weighted_unit(item.get("unit_abbreviation"), item.get("unit_name")),
            }
            row.update(self._price_fields(item.get("selling_price")))
            results.append(row)
        return {"results": results}

    def start(self) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
            httpd.daemon_threads = True
            httpd.app = self  # type: ignore[attr-defined]
            if self._https:
                pair = _ensure_self_signed(self._cert_dir, self._lan_ip())
                if pair:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(pair[0], pair[1])
                    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
                    self._scheme = "https"
                else:
                    log.warning("HTTPS مطلوب لكن تعذّر إنشاء الشهادة؛ سيُستخدم HTTP")
                    self._scheme = "http"
            self._httpd = httpd
            self._thread = threading.Thread(
                target=httpd.serve_forever, name="lan-price-server", daemon=True
            )
            self._thread.start()
            log.info("LAN price server listening on %s:%s (%s)", self._host, self.port, self._scheme)

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            self._httpd = None
            self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass

    @staticmethod
    def _lan_ip() -> str:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0])
            finally:
                sock.close()
        except Exception:
            return "127.0.0.1"


# --------------------------------------------------------------------------- #
# Process-wide manager (started from main.py, restarted from the admin tab).   #
# --------------------------------------------------------------------------- #
_SERVER: LanPriceServer | None = None
_MANAGER_LOCK = threading.Lock()


def current() -> LanPriceServer | None:
    return _SERVER


def _build(ctx) -> LanPriceServer:
    settings = ctx.settings
    company = str(settings.get("company_name", "") or "").strip() or branding.APP_NAME_AR
    accent = _safe_hex(settings.get("invoice_color"))
    logo = str(settings.get("company_logo", "") or "").strip() or default_brand.DEFAULT_COMPANY_LOGO
    cert_dir = None
    try:
        cert_dir = os.path.dirname(str(ctx.db.path))
    except Exception:
        cert_dir = None
    return LanPriceServer(
        ctx.db,
        settings,
        host=lan_settings.host(settings),
        port=lan_settings.port(settings),
        company=company,
        title=lan_settings.title(settings, company),
        accent=accent,
        logo=logo,
        app_ar=branding.APP_NAME_AR,
        app_en=branding.APP_NAME_EN,
        idle_ms=lan_settings.idle_seconds(settings) * 1000,
        https=lan_settings.https_enabled(settings),
        cert_dir=cert_dir,
    )


def start(ctx) -> LanPriceServer | None:
    """Start the server regardless of the enabled flag (used by the test)."""
    global _SERVER
    with _MANAGER_LOCK:
        if _SERVER is not None and _SERVER.running:
            return _SERVER
        try:
            server = _build(ctx)
            server.start()
            _SERVER = server
            return server
        except Exception:
            log.warning("تعذّر تشغيل خادم الأسعار", exc_info=True)
            _SERVER = None
            return None


def start_if_enabled(ctx) -> LanPriceServer | None:
    if not lan_settings.enabled(ctx.settings):
        return None
    return start(ctx)


def stop() -> None:
    global _SERVER
    with _MANAGER_LOCK:
        server = _SERVER
        _SERVER = None
    if server is not None:
        server.stop()


def restart(ctx) -> LanPriceServer | None:
    stop()
    return start_if_enabled(ctx)


__all__ = ["LanPriceServer", "current", "start", "start_if_enabled", "stop", "restart"]
