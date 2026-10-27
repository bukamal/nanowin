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

Phase 1 (MVP) endpoints::

    GET /                -> kiosk web page (RTL, big fonts)
    GET /api/health      -> {"ok": true, ...}
    GET /api/price?barcode=...  -> {"found": bool, "name", "price_text", "unit"}
"""

import json
import socket
import string
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


# The kiosk page. Uses string.Template ($name) instead of an f-string so the
# CSS/JS braces stay untouched. No other '$' must appear in the markup.
_KIOSK_TEMPLATE = string.Template(
    """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
:root { --accent: $accent; --accent-deep: $accent_deep; --accent-rgb: $accent_rgb; --ink:#0f172a; --paper:rgba(255,255,255,.96); }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin:0; height:100%; font-family:'Cairo','Segoe UI',Tahoma,Arial,sans-serif; }
body {
  color:#eaf1ff; overflow:hidden;
  background:
    radial-gradient(1100px 620px at 84% -12%, rgba(var(--accent-rgb),.55), transparent 60%),
    radial-gradient(900px 560px at -8% 116%, rgba(var(--accent-rgb),.35), transparent 62%),
    linear-gradient(155deg,#0a1020 0%,#111c33 52%,#0a1020 100%);
}
body::before {
  content:''; position:fixed; left:-25%; right:-25%; bottom:-32%; height:72%; pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.06) 1px,transparent 1px);
  background-size:46px 46px; transform:perspective(620px) rotateX(62deg); transform-origin:bottom center;
  -webkit-mask-image:linear-gradient(to top,rgba(0,0,0,.55),transparent 82%);
  mask-image:linear-gradient(to top,rgba(0,0,0,.55),transparent 82%);
}
.stage { position:relative; min-height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:26px; padding:9vh 4vw 12vh; perspective:1500px; }
.topbar { position:fixed; top:0; left:0; right:0; display:flex; align-items:center; justify-content:space-between; gap:14px; padding:16px 24px; z-index:5; }
.brand { display:flex; align-items:center; gap:14px; transform-style:preserve-3d; }
.logo-badge { width:62px; height:62px; border-radius:18px; display:grid; place-items:center; overflow:hidden;
  background:linear-gradient(150deg,#ffffff,#e9f0ff);
  box-shadow:0 18px 30px -12px rgba(0,0,0,.65), inset 0 2px 0 rgba(255,255,255,.9), inset 0 -6px 14px rgba(var(--accent-rgb),.20);
  transform:translateZ(40px) rotateX(6deg); }
.logo-badge img { width:100%; height:100%; object-fit:contain; }
.wordmark { transform:translateZ(22px); }
.wordmark .ar { font-size:26px; font-weight:800; line-height:1; }
.wordmark .en { font-size:12px; letter-spacing:3px; opacity:.78; text-transform:uppercase; margin-top:2px; }
.tools { display:flex; align-items:center; gap:10px; }
.pill { border:1px solid rgba(255,255,255,.20); background:rgba(255,255,255,.08); color:#fff; border-radius:999px;
  padding:9px 15px; font-size:14px; font-weight:700; cursor:pointer; -webkit-backdrop-filter:blur(6px); backdrop-filter:blur(6px); }
.pill.on { background:var(--accent); border-color:transparent; box-shadow:0 12px 26px -12px var(--accent-deep); }
.dot { width:9px; height:9px; border-radius:50%; background:#22c55e; box-shadow:0 0 12px #22c55e; }
.idle { text-align:center; transform-style:preserve-3d; animation:float 5.5s ease-in-out infinite; }
.idle .glyph { font-size:104px; transform:translateZ(60px); filter:drop-shadow(0 26px 32px rgba(0,0,0,.5)); }
.idle h1 { font-size:30px; margin:16px 0 8px; }
.idle p { font-size:18px; opacity:.75; margin:0; }
@keyframes float { 0%,100% { transform:translateY(0); } 50% { transform:translateY(-12px); } }
.plate { position:relative; width:min(840px,94vw); transform-style:preserve-3d; display:none; }
.plate.show { display:block; animation:rise .40s cubic-bezier(.2,.8,.2,1); }
@keyframes rise { from { opacity:0; transform:translateY(28px) rotateX(16deg) scale(.96); } to { opacity:1; transform:translateY(0) rotateX(0) scale(1); } }
.plate .glow { position:absolute; inset:auto 8% -28px 8%; height:64px; background:radial-gradient(closest-side, rgba(0,0,0,.55), transparent); filter:blur(6px); transform:translateZ(-40px); }
.card { position:relative; background:var(--paper); color:var(--ink); border-radius:30px; padding:40px 44px; text-align:center;
  box-shadow:0 44px 74px -32px rgba(0,0,0,.72), 0 18px 30px -20px rgba(0,0,0,.5), inset 0 2px 0 #fff; transform:rotateX(5deg); }
.card::after { content:''; position:absolute; inset:0; border-radius:30px; background:linear-gradient(180deg,rgba(var(--accent-rgb),.07),transparent 42%); pointer-events:none; }
.cat { display:inline-block; font-size:14px; font-weight:700; color:var(--accent-deep); background:rgba(var(--accent-rgb),.13); padding:6px 15px; border-radius:999px; margin-bottom:12px; }
.name { font-size:34px; font-weight:800; line-height:1.25; margin-bottom:16px; }
.price { display:flex; align-items:baseline; justify-content:center; gap:14px; direction:rtl; }
.price .num { font-size:clamp(52px,12vw,108px); font-weight:900; direction:ltr; unicode-bidi:isolate; font-variant-numeric:tabular-nums; letter-spacing:1px;
  background:linear-gradient(180deg, var(--accent), var(--accent-deep)); -webkit-background-clip:text; background-clip:text; color:transparent;
  text-shadow:0 2px 0 rgba(255,255,255,.55), 0 18px 26px rgba(var(--accent-rgb),.28); }
.price .cur { font-size:26px; font-weight:800; color:#64748b; }
.unit { margin-top:12px; font-size:20px; color:#475569; }
.badge { display:inline-block; margin-top:16px; font-size:14px; font-weight:700; color:#b45309; background:#fff7ed; padding:6px 15px; border-radius:999px; }
.miss { text-align:center; padding:40px; }
.miss .x { font-size:72px; filter:drop-shadow(0 18px 24px rgba(239,68,68,.35)); }
.miss h2 { font-size:28px; margin:12px 0 4px; }
.miss p { opacity:.75; margin:0; }
.results { width:min(840px,94vw); display:none; }
.results.show { display:block; }
.rrow { display:flex; justify-content:space-between; align-items:center; gap:16px; background:var(--paper); color:var(--ink);
  border-radius:18px; padding:16px 22px; margin-bottom:12px; box-shadow:0 26px 42px -26px rgba(0,0,0,.6); }
.rrow .rn { font-size:20px; font-weight:700; }
.rrow .rp { font-size:24px; font-weight:900; color:var(--accent-deep); direction:rtl; white-space:nowrap; }
.rrow .rp .cur { font-size:14px; color:#64748b; margin-inline-start:6px; }
.pad { position:fixed; left:0; right:0; bottom:0; padding:14px 16px 18px; display:none; z-index:6; background:linear-gradient(0deg,rgba(6,10,20,.92),transparent); }
.pad.show { display:block; }
.pad .grid { max-width:430px; margin:0 auto; display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
.key { background:rgba(255,255,255,.10); color:#fff; border:1px solid rgba(255,255,255,.18); border-radius:16px; padding:16px;
  font-size:22px; font-weight:800; cursor:pointer; -webkit-backdrop-filter:blur(6px); backdrop-filter:blur(6px); }
.key:active { transform:translateY(2px); }
.key.acc { background:var(--accent); border-color:transparent; }
.manual { position:fixed; bottom:82px; left:0; right:0; text-align:center; font-size:22px; font-weight:900; letter-spacing:3px; min-height:28px; z-index:6; color:#fff; }
.taskbar { position:fixed; bottom:20px; left:0; right:0; display:flex; justify-content:center; gap:10px; z-index:6; }
.hidden-input { position:fixed; top:-120px; left:-120px; width:1px; height:1px; opacity:0; border:0; }
@media (max-width:560px){ .name{font-size:26px} .wordmark .ar{font-size:20px} .logo-badge{width:50px;height:50px} .card{padding:28px 22px} }
</style>
</head>
<body>
<input id="scan" class="hidden-input" autocomplete="off" inputmode="none">

<div class="topbar">
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
    <div class="pill" style="pointer-events:none"><span class="dot"></span></div>
  </div>
</div>

<div class="stage">
  <div class="idle" id="idle">
    <div class="glyph">🛒</div>
    <h1 id="idleTitle">امسح باركود المنتج</h1>
    <p id="hint">مرّر الباركود أمام القارئ لمعرفة السعر</p>
  </div>

  <div class="plate" id="pricePlate">
    <div class="glow"></div>
    <div class="card">
      <div class="cat" id="pCat"></div>
      <div class="name" id="pName"></div>
      <div class="price"><span class="num" id="pNum">0</span><span class="cur" id="pCur"></span></div>
      <div class="unit" id="pUnit"></div>
      <div class="badge" id="pBadge" style="display:none"></div>
    </div>
  </div>

  <div class="plate" id="missPlate">
    <div class="glow"></div>
    <div class="card miss">
      <div class="x">⚠️</div>
      <h2 id="missTitle">هذا المنتج غير مُدرَج في النظام</h2>
      <p id="missText">جرّب مسحًا آخر أو استخدم البحث بالاسم</p>
    </div>
  </div>

  <div class="results" id="results"></div>
</div>

<div class="manual" id="manualView"></div>
<div class="pad" id="pad"><div class="grid" id="padGrid"></div></div>
<div class="taskbar"><div class="pill" id="enterBtn">استعلام</div><div class="pill" id="clearBtn">مسح</div></div>

<script>
(function () {
  var LANG = 'ar';
  var TEXT = {
    ar: { hint:'مرّر الباركود أمام القارئ لمعرفة السعر', manual:'إدخال يدوي', enter:'استعلام', clear:'مسح',
          missT:'هذا المنتج غير مُدرَج في النظام', missP:'جرّب مسحًا آخر أو ابحث بالاسم', idleT:'امسح باركود المنتج',
          weigh:'يُوزن', perUnit:'السعر لكل', price:'سعر', results:'نتائج البحث', notFound:'لا نتائج' },
    en: { hint:'Pass the barcode in front of the reader to see the price', manual:'Keypad', enter:'Check', clear:'Clear',
          missT:'Item not found', missP:'Try another scan or search by name', idleT:'Scan a product barcode',
          weigh:'Sold by weight', perUnit:'Price per', price:'Price', results:'Search results', notFound:'No results' }
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
  var idleTimer = null, buffer = '';

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
    document.getElementById('enterBtn').textContent = t('enter');
    document.getElementById('clearBtn').textContent = t('clear');
    document.getElementById('missTitle').textContent = t('missT');
    document.getElementById('missText').textContent = t('missP');
    document.getElementById('langBtn').textContent = (LANG === 'ar') ? 'EN' : 'ع';
    revealBuffer();
  }

  function hideAll() { idle.style.display = 'none'; pricePlate.classList.remove('show'); missPlate.classList.remove('show'); resultsEl.classList.remove('show'); }
  function reset() { hideAll(); idle.style.display = ''; buffer = ''; revealBuffer(); try { input.focus(); } catch (e) {} scheduleReset(); }
  function scheduleReset() { if (idleTimer) { clearTimeout(idleTimer); } idleTimer = setTimeout(reset, $idle_ms); }

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
    if (e.key === 'Enter' || e.keyCode === 13) { var v = input.value; input.value = ''; dispatch(v); e.preventDefault(); }
  });
  document.addEventListener('click', function () { try { input.focus(); } catch (e) {} });
  window.addEventListener('load', function () { try { input.focus(); } catch (e) {} scheduleReset(); });

  // on-screen numeric keypad (kiosks without a scanner)
  function press(d) { if (buffer.length < 24) { buffer += d; revealBuffer(); } }
  function revealBuffer() { manualView.textContent = buffer; }
  var keys = ['1','2','3','4','5','6','7','8','9','C','0','OK'];
  for (var k = 0; k < keys.length; k++) {
    (function (label) {
      var el = document.createElement('div');
      el.className = 'key' + ((label === 'OK') ? ' acc' : '');
      el.textContent = label;
      el.onclick = function () {
        if (label === 'C') { buffer = ''; revealBuffer(); }
        else if (label === 'OK') { var v = buffer; buffer = ''; revealBuffer(); dispatch(v); }
        else { press(label); }
      };
      document.getElementById('padGrid').appendChild(el);
    })(keys[k]);
  }
  document.getElementById('manualBtn').onclick = function () { pad.classList.toggle('show'); try { input.focus(); } catch (e) {} };
  document.getElementById('enterBtn').onclick = function () { var v = buffer; buffer = ''; revealBuffer(); dispatch(v); };
  document.getElementById('clearBtn').onclick = function () { buffer = ''; revealBuffer(); reset(); };
  document.getElementById('langBtn').onclick = function () { LANG = (LANG === 'ar') ? 'en' : 'ar'; applyLang(); };

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
        return f"http://{self._lan_ip()}:{self.port}/"

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

    def _price_fields(self, value) -> dict:
        """Number + symbol separately so the kiosk can place the symbol itself.

        ``format_amount`` converts a *stored USD* price into the display
        currency (the previous code used ``format_display_value``, which does
        not convert -- a 11000 SYP item therefore showed as "1").
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
            self._httpd = httpd
            self._thread = threading.Thread(
                target=httpd.serve_forever, name="lan-price-server", daemon=True
            )
            self._thread.start()
            log.info("LAN price server listening on %s:%s", self._host, self.port)

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
