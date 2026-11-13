from __future__ import annotations

"""Read-only LAN API server (client/server data plane -- phase 1).

One central machine hosts this server; terminal "clients" call it through
``core.transport.RemoteTransport`` using the exact ``(service, method, kwargs)``
contract the in-process ``LocalTransport`` uses -- so the same call works
locally or over the LAN.

Phase 1 accepts only methods listed in :data:`READ_METHODS` (writes arrive in
phase 2), guarded by the shared subnet allow-list and an optional bearer token.
No admin/POS/price endpoints live here.
"""

import dataclasses
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from nano_offline.core import api_settings
from nano_offline.core.logging_config import get_logger
from nano_offline.core.transport import LocalTransport
from nano_offline.version import APP_VERSION

log = get_logger("nano.lan.api")

#: Read-only methods a client may call in phase 1. Grows carefully in phase 2+;
#: anything not listed here is refused (writes stay local/server-only).
READ_METHODS = frozenset({
    # items
    "items.list", "items.get", "items.find_by_barcode", "items.pos_catalog", "items.units",
    "items.activity_summary", "items.movements", "items.list_barcodes", "items.components",
    "items.find_similar_names", "items.find_similar_barcodes",
    "items.purchased_by_party", "items.quiet_item_ids",
    "items.build_item_card", "items.verify_item_card",
    # invoices
    "invoices.list_invoices", "invoices.get_invoice", "invoices.check_purchase_price",
    "invoices.open_sale_orders", "invoices.open_invoices_for_party",
    # parties
    "customers.list", "customers.get", "customers.count", "customers.activity_summary",
    "suppliers.list", "suppliers.get", "suppliers.count", "suppliers.activity_summary",
    # definitions
    "definitions.list_categories", "definitions.list_units",
    # documents (print HTML generated from shared data)
    "documents.invoice_html", "documents.statement_html", "documents.voucher_html",
    "documents.day_close_html", "documents.party_card_html", "documents.item_card_html",
    "documents.barcode_labels_html", "documents.section_ticket_preview_html",
    # statements / reports / dashboard
    "statements.party_statement", "statements.account_statement", "statements.trial_balance",
    "dashboard.summary", "dashboard.today_summary", "dashboard.sales_trend",
    "dashboard.restock_predictions", "dashboard.purchase_list", "dashboard.weekly_owner_summary",
    "reports.income_statement", "reports.top_selling_items", "reports.cash_movement",
    "reports.outstanding_invoices", "reports.collection_priorities",
    "reports.cash_commitments", "reports.expense_anomalies",
    "forecast.seasonal_forecast", "forecast.forecast_accuracy",
    # settings / auth
    "settings.get", "settings.get_all", "auth.has_users", "auth.list_users", "auth.audit_entries",
    # notifications / assistant / pulse
    "notifications.generate_alerts", "notifications.get_config", "notifications.recent",
    "notifications.unread_summary", "notifications.history",
    "smart_assistant.decisions", "smart_assistant.decision_of_the_day",
    "owner_pulse.generate",
    # expenses (reads)
    "expenses.list_expenses", "expenses.list_categories", "expenses.get_expense",
    "expenses.get_expense_receipt",
    # payments / vouchers (reads)
    "payments.list_vouchers", "payments.get_voucher", "payments.payment_source",
    "payments.allocatable_invoices", "payments.open_invoices",
    # stocktake (reads)
    "stocktake.find_resumable_session", "stocktake.lines", "stocktake.diff_summary",
    # cash close / trust / external notifications
    "cash_day_close.book_cash", "cash_day_close.today_cash_movement", "cash_day_close.last_close",
    "party_trust.score",
    "external_notifications.get_config", "external_notifications.delivery_log",
})

#: Write methods a client may call, each mapped to the permission the user must
#: hold (see ``auth_service.ROLE_PERMISSIONS``). Writes always require a login
#: session; the audit actor is stamped from it (thread-safely -- see
#: ``Database.set_actor``).
WRITE_METHODS: dict[str, str] = {
    "invoices.create_invoice": "invoices",
    "invoices.update_invoice": "invoices",
    "invoices.delete_invoice": "invoices",
    "invoices.merge_sale_orders": "invoices",
    "invoices.split_sale_order": "invoices",
    "invoices.cancel_sale_order": "invoices",
    "invoices.convert_opening_balances": "invoices",
    "items.create": "items",
    "items.update": "items",
    "items.delete": "items",
    "items.add_barcode": "items",
    "items.remove_barcode": "items",
    "items.delete_opening_balance": "items",
    "definitions.create_category": "items",
    "definitions.rename_category": "items",
    "definitions.delete_category": "items",
    "definitions.create_unit": "items",
    "definitions.rename_unit": "items",
    "definitions.delete_unit": "items",
    "customers.create": "customers",
    "customers.update": "customers",
    "customers.delete": "customers",
    "suppliers.create": "suppliers",
    "suppliers.update": "suppliers",
    "suppliers.delete": "suppliers",
    "payments.create_voucher": "finance",
    "payments.update_voucher": "finance",
    "payments.delete_voucher": "finance",
    "payments.register_invoice_payment": "finance",
    "expenses.create_expense": "finance",
    "expenses.update_expense": "finance",
    "expenses.delete_expense": "finance",
    "cash_day_close.close_day": "finance",
    "stocktake.start_session": "items",
    "stocktake.scan": "items",
    "stocktake.set_counted_qty": "items",
    "stocktake.remove_line": "items",
    "stocktake.commit": "items",
    "stocktake.discard_session": "items",
    "auth.create_user": "admin",
    "auth.set_active": "admin",
    "auth.reset_password": "admin",
    "notifications.mark_read": "dashboard",
    "notifications.mark_all_read": "dashboard",
    "notifications.save_config": "admin",
    "external_notifications.save_config": "admin",
    "external_notifications.test_channel": "admin",
}

#: Reads that additionally require a permission (all other READ_METHODS are open).
READ_PERMISSIONS: dict[str, str] = {
    "auth.list_users": "admin",
    "auth.audit_entries": "admin",
    "notifications.history": "admin",
    "external_notifications.get_config": "admin",
    "external_notifications.delivery_log": "admin",
}

_MAX_BODY = 1_000_000


class Unauthorized(Exception):
    """A write arrived without a valid user session (mapped to HTTP 401)."""


def _jsonable(value):
    """Recursively convert service results into plain JSON-friendly values.

    Many services return dataclasses (``Alert``, ``Decision``, ``OwnerPulse``...)
    or lists/dicts of them; the client wraps the resulting dicts back into
    attribute-accessible objects, so field access (``alert.title``) keeps working.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return {str(k): _jsonable(v) for k, v in vars(value).items() if not str(k).startswith("_")}
    return str(value)


def _decode_special(value):
    """Reverse the transport's JSON encoding of non-JSON values (bytes)."""
    if isinstance(value, dict):
        if set(value.keys()) == {"__bytes__"}:
            import base64

            try:
                return base64.b64decode(str(value["__bytes__"]))
            except Exception:
                return b""
        return {k: _decode_special(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_special(v) for v in value]
    return value


def _coerce_args(service: str, method: str, args: dict) -> dict:
    """Rebuild dataclass / non-JSON arguments a client could only send as plain data.

    ``invoices.create_invoice(lines=[...])`` expects ``InvoiceLineInput``
    objects over JSON as dicts; ``payments.*`` ``allocations`` arrive with
    string keys; ``expenses.*`` receipt images arrive base64-encoded.
    """
    if service == "invoices" and method in ("create_invoice", "update_invoice"):
        lines = args.get("lines")
        if isinstance(lines, list):
            from nano_offline.services.invoice_service import InvoiceLineInput

            args = dict(args)
            args["lines"] = [
                line if isinstance(line, InvoiceLineInput) else InvoiceLineInput(**line)
                for line in lines
            ]
    if service == "payments" and isinstance(args.get("allocations"), dict):
        args = dict(args)
        args["allocations"] = {
            int(k): float(v) for k, v in args["allocations"].items() if str(k).lstrip("-").isdigit()
        }
    if service == "expenses" and "receipt_image" in args:
        args = dict(args)
        args["receipt_image"] = _decode_special(args.get("receipt_image"))
    return args


class _Handler(BaseHTTPRequestHandler):
    server_version = "CashirLanApi/1.0"

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        try:
            log.debug("lan-api %s", fmt % args)
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
        body = json.dumps(_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _gateway(self):
        """Return the app after allow-list checks, or ``None`` (already answered)."""
        app = getattr(self.server, "app", None)
        if app is None:
            self._json({"ok": False, "error": "unavailable"}, 503)
            return None
        client = self.client_address[0] if self.client_address else ""
        if not app.client_allowed(client):
            self._json({"ok": False, "error": "forbidden"}, 403)
            return None
        return app

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY:
            self._json({"ok": False, "error": "bad_request"}, 400)
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json({"ok": False, "error": "bad_json"}, 400)
            return None

    def _session_token(self) -> str:
        return str(self.headers.get("X-Session-Token") or "").strip()

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        app = self._gateway()
        if app is None:
            return
        parsed = urlparse(self.path)

        if parsed.path == "/api/login":
            if not app.token_ok(self.headers, parsed.query):
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            body = self._read_json()
            if body is None:
                return
            result = app.login(str(body.get("username") or ""), str(body.get("password") or ""))
            if result is None:
                self._json({"ok": False, "error": "بيانات الدخول غير صحيحة"}, 401)
                return
            token, user = result
            self._json({"ok": True, "session": token, "user": user})
            return

        if parsed.path == "/api/logout":
            app.logout(self._session_token())
            self._json({"ok": True})
            return

        if parsed.path != "/api/rpc":
            self._json({"ok": False, "error": "not_found"}, 404)
            return
        if not app.token_ok(self.headers, parsed.query):
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        payload = self._read_json()
        if payload is None:
            return
        service = str(payload.get("service") or "")
        method = str(payload.get("method") or "")
        args = payload.get("args") or {}
        arglist = payload.get("arglist") or []
        if not isinstance(args, dict):
            self._json({"ok": False, "error": "bad_args"}, 400)
            return
        if not isinstance(arglist, list):
            arglist = []
        try:
            result = app.dispatch(service, method, args, arglist=arglist, session_token=self._session_token())
        except Unauthorized as exc:
            self._json({"ok": False, "error": str(exc)}, 401)
            return
        except PermissionError as exc:
            self._json({"ok": False, "error": str(exc)}, 403)
            return
        except AttributeError as exc:
            self._json({"ok": False, "error": str(exc)}, 404)
            return
        except Exception as exc:
            log.warning("lan-api %s.%s failed", service, method, exc_info=True)
            self._json({"ok": False, "error": str(exc)}, 500)
            return
        self._json({"ok": True, "result": result})

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        app = self._gateway()
        if app is None:
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({"ok": True, "app": "cashir-lan-api", "version": APP_VERSION})
        elif parsed.path == "/api/methods":
            if not app.token_ok(self.headers, parsed.query):
                self._json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._json({"ok": True, "read_methods": sorted(READ_METHODS)})
        else:
            self._json({"ok": False, "error": "not_found"}, 404)


class LanApiServer:
    """Read-only HTTP API dispatching to an ``AppContext`` over the LAN."""

    def __init__(
        self,
        ctx,
        *,
        host: str = api_settings.DEFAULT_HOST,
        port: int = api_settings.DEFAULT_PORT,
        token: str = "",
    ):
        self._ctx = ctx
        self._transport = LocalTransport(ctx)
        self._host = host
        self._port = int(port)
        self._token = str(token or "")
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sessions: dict[str, object] = {}
        self._sessions_lock = threading.Lock()
        # Re-entrant: dispatch() takes it for writes and _run() takes it for the
        # whole call, so concurrent LAN clients can never interleave the shared
        # AuthService session or the audit actor.
        self._write_lock = threading.RLock()

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

    def client_allowed(self, host_ip: str) -> bool:
        try:
            return api_settings.client_allowed(self._ctx.settings, host_ip)
        except Exception:
            return True

    def token_ok(self, headers, query) -> bool:
        if not self._token:
            return True
        supplied = ""
        auth = headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        elif headers.get("X-Api-Token"):
            supplied = str(headers.get("X-Api-Token")).strip()
        else:
            supplied = (parse_qs(query or "").get("token") or [""])[0]
        return supplied == self._token

    def dispatch(self, service: str, method: str, args: dict, *, arglist=(), session_token: str = ""):
        key = f"{service}.{method}"
        if key in READ_METHODS:
            perm = READ_PERMISSIONS.get(key)
            if perm:
                session = self._require_session(session_token, perm)
            else:
                # Attach the caller's session (when logged in) even though the
                # read itself needs no permission: several services call
                # ``auth.require``/``auth.current`` internally (e.g. stocktake).
                session = self._session_for(session_token)
            return self._run(service, method, args, arglist=arglist, session=session)
        perm = WRITE_METHODS.get(key)
        if perm is None:
            raise PermissionError(f"طريقة غير مسموحة: {key}")
        session = self._require_session(session_token, perm)
        # Serialise writes and stamp the audit actor for this thread only, so
        # concurrent clients can never attribute one user's write to another.
        with self._write_lock:
            return self._run(service, method, args, arglist=arglist, session=session, write=True)

    def _require_session(self, token: str, permission: str | None):
        session = self._session_for(token)
        if session is None:
            raise Unauthorized("جلسة غير صالحة — سجّل الدخول أولًا")
        if permission and not session.can(permission):
            raise PermissionError(f"لا تملك صلاحية «{permission}» لهذه العملية")
        return session

    def _run(self, service: str, method: str, args: dict, *, arglist=(), session=None, write: bool = False):
        ctx = self._ctx
        target = getattr(ctx, service, None)
        if target is None:
            raise AttributeError(f"unknown service: {service!r}")
        func = getattr(target, method, None)
        if not callable(func):
            raise AttributeError(f"unknown method: {service}.{method}")
        call_args = _coerce_args(service, method, dict(args or {}))
        # Serialise the whole call: several services (stocktake, user admin)
        # call ``auth.require`` / ``auth.current`` internally, so the remote
        # user's session must be the active one for the duration -- and that
        # state is shared, exactly like the audit actor.
        with self._write_lock:
            # ``as_actor(None)`` clears the shared session for the duration of
            # an unauthenticated call, so a service can never accidentally run
            # under a stale session left by a previous client's login.
            actor = ctx.auth.as_actor(session)
            if write:
                ctx.db.set_actor(session.id, session.username)
            try:
                with actor:
                    if arglist:
                        # Positional arguments arrive as a list; bind them to
                        # the real method's signature (the UI still calls some
                        # methods like ``items.get(item_id)`` positionally).
                        import inspect

                        bound = inspect.signature(func).bind(*arglist, **call_args)
                        return func(*bound.args, **bound.kwargs)
                    return func(**call_args)
            finally:
                if write:
                    ctx.db.set_actor(None, None)

    def login(self, username: str, password: str) -> tuple[str, dict] | None:
        """Authenticate a remote user; returns ``(session_token, user_info)``."""
        session = self._ctx.auth.verify_credentials(username, password)
        if session is None:
            return None
        token = secrets.token_urlsafe(24)
        with self._sessions_lock:
            self._sessions[token] = session
        return token, {
            "id": session.id,
            "username": session.username,
            "full_name": session.full_name,
            "role": session.role,
        }

    def logout(self, token: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(token or "", None)

    def _session_for(self, token: str):
        if not token:
            return None
        with self._sessions_lock:
            return self._sessions.get(token)

    def start(self) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            httpd = ThreadingHTTPServer((self._host, self._port), _Handler)
            httpd.daemon_threads = True
            httpd.app = self  # type: ignore[attr-defined]
            self._httpd = httpd
            self._thread = threading.Thread(
                target=httpd.serve_forever, name="lan-api-server", daemon=True
            )
            self._thread.start()
            log.info("LAN API server listening on %s:%s", self._host, self.port)

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
        import socket

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0])
            finally:
                sock.close()
        except Exception:
            return "127.0.0.1"


_SERVER: LanApiServer | None = None
_MANAGER_LOCK = threading.Lock()


def current() -> LanApiServer | None:
    return _SERVER


def _build(ctx) -> LanApiServer:
    settings = ctx.settings
    return LanApiServer(
        ctx,
        host=api_settings.host(settings),
        port=api_settings.port(settings),
        token=api_settings.token(settings),
    )


def start(ctx) -> LanApiServer | None:
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
            log.warning("تعذّر تشغيل خادم الـAPI", exc_info=True)
            _SERVER = None
            return None


def start_if_enabled(ctx) -> LanApiServer | None:
    if not api_settings.enabled(ctx.settings):
        return None
    return start(ctx)


def stop() -> None:
    global _SERVER
    with _MANAGER_LOCK:
        server = _SERVER
        _SERVER = None
    if server is not None:
        server.stop()


def restart(ctx) -> LanApiServer | None:
    stop()
    return start_if_enabled(ctx)


__all__ = ["LanApiServer", "READ_METHODS", "current", "start", "start_if_enabled", "stop", "restart"]
