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

from nano_offline.core import branding, currency, lan_settings
from nano_offline.core.logging_config import get_logger
from nano_offline.repositories.item_repository import ItemRepository
from nano_offline.version import APP_VERSION

log = get_logger("nano.lan")

_DEFAULT_ACCENT = "#0B5FCC"
_DEFAULT_IDLE_MS = 12000
_MAX_BARCODE_LEN = 64

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
:root { --accent: $accent; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; font-family: 'Segoe UI', Tahoma, Arial, sans-serif; background: #0f172a; color: #fff; }
.wrap { min-height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 22px; padding: 32px; text-align: center; }
.brand { font-size: 34px; font-weight: 800; opacity: .96; }
.hint { font-size: 22px; opacity: .72; }
.card { width: min(760px, 92vw); background: #fff; color: #0f172a; border-radius: 24px; padding: 34px; box-shadow: 0 24px 60px rgba(0,0,0,.35); display: none; }
.card.show { display: block; animation: pop .18s ease; }
@keyframes pop { from { transform: scale(.96); opacity: .3; } to { transform: scale(1); opacity: 1; } }
.name { font-size: 34px; font-weight: 800; }
.cat { font-size: 20px; color: #64748b; margin-top: 4px; }
.price { font-size: 62px; font-weight: 900; color: var(--accent); margin: 12px 0; direction: ltr; }
.unit { font-size: 22px; color: #475569; }
.miss { font-size: 30px; font-weight: 800; color: #b42318; }
.hidden-input { position: fixed; top: -100px; left: -100px; width: 1px; height: 1px; opacity: 0; }
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">$company</div>
  <div class="hint" id="hint">امسح باركود المنتج لمعرفة السعر</div>
  <div class="card" id="card">
    <div class="name" id="name"></div>
    <div class="cat" id="cat"></div>
    <div class="price" id="price"></div>
    <div class="unit" id="unit"></div>
  </div>
  <div class="card" id="misscard"><div class="miss">هذا المنتج غير مُدرَج في النظام</div></div>
</div>
<input id="scan" class="hidden-input" autocomplete="off" inputmode="none">
<script>
(function () {
  var input = document.getElementById('scan');
  var card = document.getElementById('card');
  var miss = document.getElementById('misscard');
  var nameEl = document.getElementById('name');
  var catEl = document.getElementById('cat');
  var priceEl = document.getElementById('price');
  var unitEl = document.getElementById('unit');
  var idleTimer = null;

  function beep(ok) {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) { return; }
      var ac = window.__ac || (window.__ac = new Ctx());
      var osc = ac.createOscillator();
      var gain = ac.createGain();
      osc.connect(gain); gain.connect(ac.destination);
      osc.type = 'sine';
      osc.frequency.value = ok ? 880 : 220;
      gain.gain.setValueAtTime(0.001, ac.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.2, ac.currentTime + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + 0.18);
      osc.start(); osc.stop(ac.currentTime + 0.2);
    } catch (e) {}
  }

  function reset() {
    card.classList.remove('show');
    miss.classList.remove('show');
    nameEl.textContent = '';
    catEl.textContent = '';
    priceEl.textContent = '';
    unitEl.textContent = '';
    input.value = '';
    try { input.focus(); } catch (e) {}
  }

  function scheduleReset() {
    if (idleTimer) { clearTimeout(idleTimer); }
    idleTimer = setTimeout(reset, $idle_ms);
  }

  function show(data) {
    if (data && data.found) {
      nameEl.textContent = data.name || '';
      catEl.textContent = data.category || '';
      priceEl.textContent = data.price_text || '';
      unitEl.textContent = data.unit ? ('سعر ' + data.unit) : '';
      miss.classList.remove('show');
      card.classList.add('show');
      beep(true);
    } else {
      card.classList.remove('show');
      miss.classList.add('show');
      beep(false);
    }
    scheduleReset();
  }

  async function lookup(code) {
    code = (code || '').replace(/\\s+/g, '');
    if (!code) { return; }
    try {
      var r = await fetch('/api/price?barcode=' + encodeURIComponent(code), { cache: 'no-store' });
      show(await r.json());
    } catch (e) {
      show({ found: false });
    }
  }

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.keyCode === 13) {
      var v = input.value;
      input.value = '';
      lookup(v);
      e.preventDefault();
    }
  });
  document.addEventListener('click', function () { try { input.focus(); } catch (e) {} });
  window.addEventListener('load', function () { try { input.focus(); } catch (e) {} scheduleReset(); });
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
        idle_ms: int = _DEFAULT_IDLE_MS,
    ):
        self._db = db
        self._settings = settings
        self._items = ItemRepository(db)
        self._host = host
        self._port = int(port)
        self._company = company or branding.APP_NAME_AR
        self._title = title or self._company
        self._accent = accent or _DEFAULT_ACCENT
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
            idle_ms=str(self._idle_ms),
        )

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
        try:
            price_text = currency.format_display_value(
                item.get("selling_price"), self._settings, with_symbol=True
            )
        except Exception:
            price_text = ""
        unit = str(item.get("unit_abbreviation") or item.get("unit_name") or "").strip()
        return {
            "found": True,
            "barcode": code,
            "name": str(item.get("name") or ""),
            "category": str(item.get("category_name") or ""),
            "price_text": price_text,
            "unit": unit,
        }

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
    accent = str(settings.get("invoice_color", "") or "").strip() or _DEFAULT_ACCENT
    return LanPriceServer(
        ctx.db,
        settings,
        host=lan_settings.host(settings),
        port=lan_settings.port(settings),
        company=company,
        title=lan_settings.title(settings, company),
        accent=accent,
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
