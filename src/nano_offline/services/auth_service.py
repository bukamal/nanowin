from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from nano_offline.core.database import Database

# Capability catalogue. Keys are stable (stored in permissions_json); the
# Arabic label and group drive the users sheet. "access" keys gate screens and
# menu entries; "sensitive" keys gate actions inside those screens.
CAPABILITIES: dict[str, tuple[str, str]] = {
    "dashboard": ("لوحة التحكم", "access"),
    "invoices_sales": ("المبيعات ونقطة البيع", "access"),
    "invoices_purchases": ("المشتريات", "access"),
    "items": ("المواد والمخزون", "access"),
    "customers": ("العملاء", "access"),
    "suppliers": ("الموردون", "access"),
    "finance": ("المالية (سندات ومصروفات وصندوق)", "access"),
    "reports": ("التقارير", "access"),
    "security": ("الأمان وكلمة المرور", "access"),
    "admin_users": ("إدارة المستخدمين", "sensitive"),
    "admin_backup": ("النسخ الاحتياطي والاستعادة", "sensitive"),
    "view_cost": ("رؤية التكلفة والأرباح", "sensitive"),
    "discounts": ("الخصومات", "sensitive"),
    "delete_invoices": ("حذف/إلغاء الفواتير", "sensitive"),
}

# Backwards-compatible module keys: screens/routes still ask for "invoices" or
# "admin", which now mean "any of these capabilities".
DERIVED_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "invoices": ("invoices_sales", "invoices_purchases"),
    # Dedicated sales/purchases screens: each maps to its own capability so a
    # cashier only sees the sales surface and a warehouse only purchases.
    "sales": ("invoices_sales",),
    "purchases": ("invoices_purchases",),
    "admin": ("admin_users", "admin_backup"),
}

_ALL_CAPABILITIES = frozenset(CAPABILITIES)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": _ALL_CAPABILITIES,
    "accountant": _ALL_CAPABILITIES - {"admin_users", "admin_backup"},
    "sales": frozenset({"dashboard", "invoices_sales", "customers", "security"}),
    "warehouse": frozenset({"dashboard", "invoices_purchases", "items", "suppliers", "reports", "security"}),
    "viewer": frozenset({"dashboard", "reports", "security"}),
}

ROLE_LABELS = {
    "admin": "مدير",
    "accountant": "محاسب",
    "sales": "كاشير",
    "warehouse": "أمين مخزن",
    "viewer": "مشاهدة فقط",
}


def effective_permissions(role: str, permissions_json: str | None) -> frozenset[str]:
    """Role preset minus revoked capabilities plus granted ones."""
    base = set(ROLE_PERMISSIONS.get(role, frozenset()))
    if not permissions_json:
        return frozenset(base)
    try:
        overrides = json.loads(permissions_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return frozenset(base)
    if not isinstance(overrides, dict):
        return frozenset(base)
    grant = {str(key) for key in overrides.get("grant") or ()}
    revoke = {str(key) for key in overrides.get("revoke") or ()}
    return frozenset((base - revoke) | (grant & _ALL_CAPABILITIES))


def overrides_json(role: str, granted: frozenset[str] | set[str] | None) -> str | None:
    """Serialize the difference between ``granted`` and the role preset."""
    if granted is None:
        return None
    base = set(ROLE_PERMISSIONS.get(role, frozenset()))
    wanted = {key for key in granted if key in _ALL_CAPABILITIES}
    grant = sorted(wanted - base)
    revoke = sorted(base - wanted)
    if not grant and not revoke:
        return None
    return json.dumps({"grant": grant, "revoke": revoke}, ensure_ascii=False)


@dataclass(slots=True, frozen=True)
class UserSession:
    id: int
    username: str
    full_name: str
    role: str
    permissions: frozenset[str] = frozenset()
    must_change_password: bool = False

    def can(self, permission: str) -> bool:
        derived = DERIVED_PERMISSIONS.get(permission)
        if derived is not None:
            return any(key in self.permissions for key in derived)
        return permission in self.permissions


class AuthService:
    MIN_PASSWORD_LENGTH = 8
    MAX_FAILED_ATTEMPTS = 5
    LOCK_MINUTES = 5

    def __init__(self, db: Database):
        self.db = db
        self._session: UserSession | None = None
        self._prefs_path = self.db.path.parent / "login_prefs.json"
        self._quick_key_path = self.db.path.parent / ".nano_quick_auth_key"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        text = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
        salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000, dklen=32)
        return digest.hex(), salt.hex()

    @classmethod
    def _validate_password(cls, password: str) -> None:
        if len(password or "") < cls.MIN_PASSWORD_LENGTH:
            raise ValueError(f"كلمة المرور يجب ألا تقل عن {cls.MIN_PASSWORD_LENGTH} أحرف")

    @staticmethod
    def _normalize_username(username: str) -> str:
        value = (username or "").strip()
        if len(value) < 3:
            raise ValueError("اسم المستخدم يجب ألا يقل عن 3 أحرف")
        if any(ch.isspace() for ch in value):
            raise ValueError("اسم المستخدم لا يجوز أن يحتوي مسافات")
        return value

    def _load_prefs(self) -> dict:
        try:
            data = json.loads(self._prefs_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _save_prefs(self, prefs: dict) -> None:
        self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._prefs_path.with_suffix(".tmp")
        temp.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self._prefs_path)
        try:
            os.chmod(self._prefs_path, 0o600)
        except OSError:
            pass

    def remembered_username(self) -> str:
        return str(self._load_prefs().get("remembered_username") or "")

    def saved_login_enabled(self, username: str | None = None) -> bool:
        prefs = self._load_prefs()
        saved_username = str(prefs.get("saved_username") or "")
        token = str(prefs.get("saved_token") or "")
        if not saved_username or not token:
            return False
        return not username or saved_username.casefold() == (username or "").casefold()

    def set_remembered_username(self, username: str | None) -> None:
        prefs = self._load_prefs()
        if username:
            prefs["remembered_username"] = self._normalize_username(username)
        else:
            prefs.pop("remembered_username", None)
        self._save_prefs(prefs)

    def _quick_key(self) -> bytes:
        try:
            raw = bytes.fromhex(self._quick_key_path.read_text(encoding="ascii").strip())
            if len(raw) == 32:
                return raw
        except (OSError, ValueError):
            pass
        raw = secrets.token_bytes(32)
        self._quick_key_path.write_text(raw.hex(), encoding="ascii")
        try:
            os.chmod(self._quick_key_path, 0o600)
        except OSError:
            pass
        return raw

    def _quick_key_id(self) -> str:
        return hashlib.sha256(self._quick_key()).hexdigest()[:24]

    def _hash_quick(self, secret: str, salt_hex: str | None = None) -> tuple[str, str]:
        salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
        material = self._quick_key() + (secret or "").encode("utf-8")
        digest = hashlib.pbkdf2_hmac("sha256", material, salt, 310_000, dklen=32)
        return digest.hex(), salt.hex()

    @staticmethod
    def _validate_quick(kind: str, secret: str) -> str:
        value = (secret or "").strip()
        if kind == "pin":
            if not value.isdigit() or not 4 <= len(value) <= 8:
                raise ValueError("PIN يجب أن يتكون من 4 إلى 8 أرقام")
        elif kind == "pattern":
            if not 4 <= len(value) <= 9 or any(ch not in "123456789" for ch in value) or len(set(value)) != len(value):
                raise ValueError("النمط يجب أن يحتوي 4 إلى 9 نقاط مختلفة")
        else:
            raise ValueError("نوع الدخول السريع غير صحيح")
        return value

    def has_users(self) -> bool:
        with self.db.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def current(self) -> UserSession | None:
        return self._session

    def require(self, permission: str) -> UserSession:
        if self._session is None:
            raise PermissionError("يجب تسجيل الدخول")
        if not self._session.can(permission):
            raise PermissionError("ليست لديك صلاحية لتنفيذ هذه العملية")
        return self._session

    @contextlib.contextmanager
    def as_actor(self, session: UserSession):
        """Run a block with ``session`` as the process session.

        The LAN API server uses this to run an ``auth.*`` call as the remote
        user, so ``require(...)`` inside (e.g. ``create_user``) sees them.
        """
        previous = self._session
        self._session = session
        try:
            yield
        finally:
            self._session = previous

    def create_initial_admin(self, username: str, full_name: str, password: str) -> int:
        if self.has_users():
            raise ValueError("تم إنشاء المستخدم الأول مسبقًا")
        return self.create_user(username=username, full_name=full_name, password=password, role="admin", _bootstrap=True)

    def create_user(
        self,
        *,
        username: str,
        full_name: str,
        password: str,
        role: str,
        permissions=None,
        must_change_password: bool = False,
        _bootstrap: bool = False,
    ) -> int:
        if not _bootstrap:
            self.require("admin_users")
        username = self._normalize_username(username)
        full_name = (full_name or "").strip()
        if not full_name:
            raise ValueError("الاسم الكامل مطلوب")
        if role not in ROLE_PERMISSIONS:
            raise ValueError("الدور غير صحيح")
        self._validate_password(password)
        password_hash, salt = self._hash_password(password)
        granted = None
        if permissions is not None:
            granted = {str(key) for key in permissions}
        overrides = overrides_json(role, granted)
        with self.db.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO users(username,full_name,password_hash,password_salt,role,
                                     permissions_json,must_change_password,password_changed_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    username,
                    full_name,
                    password_hash,
                    salt,
                    role,
                    overrides,
                    1 if must_change_password else 0,
                    self._iso(self._now()),
                ),
            )
            user_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details,user_id,username) VALUES('create','user',?,?,?,?)",
                (user_id, f"role={role}", self._session.id if self._session else user_id, self._session.username if self._session else username),
            )
            return user_id

    @staticmethod
    def _session_from_row(row) -> UserSession:
        keys = set(row.keys())
        raw = row["permissions_json"] if "permissions_json" in keys else None
        must_change = bool(row["must_change_password"]) if "must_change_password" in keys else False
        return UserSession(
            int(row["id"]),
            str(row["username"]),
            str(row["full_name"]),
            str(row["role"]),
            effective_permissions(str(row["role"]), raw),
            must_change,
        )

    def _active_admin_count(self, conn, *, replace_id: int | None = None, replacement: UserSession | None = None) -> int:
        """Count active users that can still administer users.

        ``replacement`` lets an update/deactivation count the edited user's
        prospective state instead of its stored one, so "last admin" can never
        be removed by a combined role+permission change.
        """
        count = 0
        for row in conn.execute(
            "SELECT id,username,full_name,role,is_active,permissions_json,must_change_password FROM users"
        ).fetchall():
            if int(row["id"]) == replace_id:
                if replacement is not None and replacement.can("admin_users"):
                    count += 1
                continue
            if not int(row["is_active"]):
                continue
            if self._session_from_row(row).can("admin_users"):
                count += 1
        return count

    def _checked_user(self, conn, username: str):
        row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if row is None:
            raise ValueError("بيانات الدخول غير صحيحة")
        if not int(row["is_active"]):
            raise PermissionError("هذا المستخدم معطل")
        locked_until = self._parse_dt(row["locked_until"])
        if locked_until and locked_until > self._now():
            raise PermissionError("الحساب مقفل مؤقتًا بعد محاولات دخول فاشلة")
        return row

    def _failed_attempt(self, conn, row) -> None:
        failed = int(row["failed_attempts"] or 0) + 1
        lock_value = None
        if failed >= self.MAX_FAILED_ATTEMPTS:
            lock_value = self._iso(self._now() + timedelta(minutes=self.LOCK_MINUTES))
            failed = 0
        conn.execute(
            "UPDATE users SET failed_attempts=?,locked_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (failed, lock_value, row["id"]),
        )
        conn.commit()

    def _establish_session(self, row, action: str) -> UserSession:
        session = self._session_from_row(row)
        self._session = session
        self.db.set_actor(session.id, session.username)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES(?, 'user', ?, ?)",
                (action, session.id, f"role={session.role}"),
            )
        return session

    def _set_saved_login(self, session: UserSession, enabled: bool) -> None:
        prefs = self._load_prefs()
        if enabled:
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            with self.db.transaction() as conn:
                conn.execute("UPDATE users SET remember_token_hash=? WHERE id=?", (token_hash, session.id))
            prefs["saved_username"] = session.username
            prefs["saved_token"] = token
        else:
            with self.db.transaction() as conn:
                conn.execute("UPDATE users SET remember_token_hash=NULL WHERE id=?", (session.id,))
            prefs.pop("saved_username", None)
            prefs.pop("saved_token", None)
        self._save_prefs(prefs)

    def login(self, username: str, password: str, *, remember_login: bool = False, remember_username: bool = True) -> UserSession:
        username = self._normalize_username(username)
        conn = self.db.connect()
        try:
            row = self._checked_user(conn, username)
            expected, _ = self._hash_password(password or "", str(row["password_salt"]))
            if not hmac.compare_digest(expected, str(row["password_hash"])):
                self._failed_attempt(conn, row)
                raise ValueError("اسم المستخدم أو كلمة المرور غير صحيحة")
            conn.execute(
                "UPDATE users SET failed_attempts=0,locked_until=NULL,last_login_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (self._iso(self._now()), row["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        session = self._establish_session(row, "login")
        self.set_remembered_username(session.username if (remember_username or remember_login) else None)
        self._set_saved_login(session, bool(remember_login) and not session.must_change_password)
        return session

    def verify_credentials(self, username: str, password: str) -> UserSession | None:
        """Check a username/password without establishing a process session.

        Used by the LAN API server (client/server mode): it must authenticate a
        remote user and learn their identity/role, but must NOT touch the shared
        process session or actor -- many clients share one server process. On
        success returns the ``UserSession``; on failure returns ``None``.
        Lockout / failed-attempt handling mirrors :meth:`login`.
        """
        username = self._normalize_username(username)
        conn = self.db.connect()
        try:
            try:
                row = self._checked_user(conn, username)
            except (ValueError, PermissionError):
                return None
            expected, _ = self._hash_password(password or "", str(row["password_salt"]))
            if not hmac.compare_digest(expected, str(row["password_hash"])):
                self._failed_attempt(conn, row)
                return None
            conn.execute(
                "UPDATE users SET failed_attempts=0,locked_until=NULL,last_login_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (self._iso(self._now()), row["id"]),
            )
            conn.commit()
            return self._session_from_row(row)
        finally:
            conn.close()

    def restore_saved_session(self) -> UserSession | None:
        prefs = self._load_prefs()
        username = str(prefs.get("saved_username") or "")
        token = str(prefs.get("saved_token") or "")
        if not username or not token:
            return None
        conn = self.db.connect()
        try:
            row = self._checked_user(conn, self._normalize_username(username))
            expected = str(row["remember_token_hash"] or "")
            actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not expected or not hmac.compare_digest(expected, actual):
                return None
            # A forced password change must never be bypassed by a saved
            # session: drop the token and require the full login form.
            if bool(row["must_change_password"]):
                conn.execute("UPDATE users SET remember_token_hash=NULL WHERE id=?", (row["id"],))
                conn.commit()
                return None
            conn.execute(
                "UPDATE users SET failed_attempts=0,locked_until=NULL,last_login_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (self._iso(self._now()), row["id"]),
            )
            conn.commit()
        except Exception:
            return None
        finally:
            conn.close()
        return self._establish_session(row, "login_saved")

    def quick_auth_info(self, username: str) -> str | None:
        try:
            username = self._normalize_username(username)
        except ValueError:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT quick_auth_type,quick_auth_key_id,is_active FROM users WHERE username=? COLLATE NOCASE", (username,)
            ).fetchone()
        if not row or not int(row["is_active"]):
            return None
        if str(row["quick_auth_key_id"] or "") != self._quick_key_id():
            return None
        kind = str(row["quick_auth_type"] or "")
        return kind if kind in {"pin", "pattern"} else None

    def verify_current_password(self, password: str) -> None:
        session = self.current()
        if session is None:
            raise PermissionError("يجب تسجيل الدخول")
        with self.db.connect() as conn:
            row = conn.execute("SELECT password_hash,password_salt FROM users WHERE id=?", (session.id,)).fetchone()
        expected, _ = self._hash_password(password or "", str(row["password_salt"]))
        if not hmac.compare_digest(expected, str(row["password_hash"])):
            raise ValueError("كلمة المرور الحالية غير صحيحة")

    def set_quick_auth(self, kind: str, secret: str, current_password: str) -> None:
        session = self.current()
        if session is None:
            raise PermissionError("يجب تسجيل الدخول")
        self.verify_current_password(current_password)
        value = self._validate_quick(kind, secret)
        digest, salt = self._hash_quick(value)
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE users SET quick_auth_type=?,quick_auth_hash=?,quick_auth_salt=?,quick_auth_key_id=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (kind, digest, salt, self._quick_key_id(), session.id),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('quick_auth_set','user',?,?)",
                (session.id, f"type={kind}"),
            )
        self.set_remembered_username(session.username)

    def clear_quick_auth(self, current_password: str) -> None:
        session = self.current()
        if session is None:
            raise PermissionError("يجب تسجيل الدخول")
        self.verify_current_password(current_password)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET quick_auth_type=NULL,quick_auth_hash=NULL,quick_auth_salt=NULL,quick_auth_key_id=NULL WHERE id=?",
                (session.id,),
            )
            conn.execute("INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('quick_auth_clear','user',?,'')", (session.id,))

    def login_quick(self, username: str, kind: str, secret: str) -> UserSession:
        username = self._normalize_username(username)
        value = self._validate_quick(kind, secret)
        conn = self.db.connect()
        try:
            row = self._checked_user(conn, username)
            if str(row["quick_auth_type"] or "") != kind or str(row["quick_auth_key_id"] or "") != self._quick_key_id():
                raise ValueError("الدخول السريع غير مفعّل لهذا المستخدم")
            expected, _ = self._hash_quick(value, str(row["quick_auth_salt"] or ""))
            if not hmac.compare_digest(expected, str(row["quick_auth_hash"] or "")):
                self._failed_attempt(conn, row)
                raise ValueError("PIN أو النمط غير صحيح")
            conn.execute(
                "UPDATE users SET failed_attempts=0,locked_until=NULL,last_login_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (self._iso(self._now()), row["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        session = self._establish_session(row, "login_quick")
        self.set_remembered_username(session.username)
        return session

    def logout(self, *, clear_saved: bool = True) -> None:
        session = self._session
        if session is not None:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('logout','user',?,?)",
                    (session.id, f"role={session.role}"),
                )
        if clear_saved:
            prefs = self._load_prefs()
            saved_username = str(prefs.get("saved_username") or "")
            if saved_username:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE users SET remember_token_hash=NULL WHERE username=? COLLATE NOCASE", (saved_username,))
            prefs.pop("saved_username", None)
            prefs.pop("saved_token", None)
            self._save_prefs(prefs)
        self._session = None
        self.db.set_actor(None, None)

    def list_users(self) -> list[dict]:
        self.require("admin_users")
        with self.db.connect() as conn:
            rows = []
            for row in conn.execute(
                """SELECT id,username,full_name,role,is_active,last_login_at,created_at,
                          permissions_json,must_change_password,password_changed_at,locked_until
                   FROM users ORDER BY id"""
            ).fetchall():
                item = dict(row)
                item["permissions"] = sorted(
                    effective_permissions(str(row["role"]), row["permissions_json"])
                )
                item["role_label"] = ROLE_LABELS.get(str(row["role"]), str(row["role"]))
                rows.append(item)
            return rows

    def set_active(self, user_id: int, active: bool) -> None:
        actor = self.require("admin_users")
        if actor.id == int(user_id) and not active:
            raise ValueError("لا يمكنك تعطيل المستخدم الحالي")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id,username,role,is_active,permissions_json,must_change_password FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("المستخدم غير موجود")
            if not active and self._active_admin_count(conn, replace_id=int(user_id)) <= 0:
                raise ValueError("لا يمكن تعطيل آخر مدير لديه صلاحية إدارة المستخدمين")
            conn.execute("UPDATE users SET is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (1 if active else 0, user_id))
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('update','user',?,?)",
                (user_id, f"is_active={1 if active else 0}"),
            )

    def update_user(
        self,
        user_id: int,
        *,
        full_name: str | None = None,
        role: str | None = None,
        active: bool | None = None,
        permissions=None,
    ) -> None:
        """Edit a user's identity/role/status and (optionally) permissions atomically.

        ``permissions`` may be omitted to keep the stored overrides, or passed
        as an explicit capability set so a role change plus its new switches
        are validated against the *final* state (important for "last admin").
        """
        actor = self.require("admin_users")
        user_id = int(user_id)
        if actor.id == user_id and active is False:
            raise ValueError("لا يمكنك تعطيل المستخدم الحالي")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id,username,full_name,role,is_active,permissions_json,must_change_password "
                "FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("المستخدم غير موجود")
            old_role = str(row["role"])
            new_role = old_role if role is None else str(role)
            if new_role not in ROLE_PERMISSIONS:
                raise ValueError("الدور غير صحيح")
            new_name = str(row["full_name"]) if full_name is None else (full_name or "").strip()
            if not new_name:
                raise ValueError("الاسم الكامل مطلوب")
            new_active = int(row["is_active"]) if active is None else (1 if active else 0)
            if permissions is None:
                new_permissions_json = row["permissions_json"]
            else:
                new_permissions_json = overrides_json(new_role, set(permissions))
            replacement = self._session_from_row(
                {
                    "id": user_id,
                    "username": row["username"],
                    "full_name": new_name,
                    "role": new_role,
                    "is_active": new_active,
                    "permissions_json": new_permissions_json,
                    "must_change_password": row["must_change_password"],
                }
            )
            remaining = self._active_admin_count(
                conn, replace_id=user_id, replacement=replacement if new_active else None
            )
            if remaining <= 0:
                raise ValueError("يجب أن يبقى مدير واحد على الأقل لديه صلاحية إدارة المستخدمين")
            conn.execute(
                "UPDATE users SET full_name=?,role=?,is_active=?,permissions_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_name, new_role, new_active, new_permissions_json, user_id),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('update','user',?,?)",
                (user_id, f"full_name={new_name};role={new_role};is_active={new_active}"),
            )

    def set_permissions(self, user_id: int, granted) -> None:
        """Replace a user's effective capability set (stored as overrides)."""
        self.require("admin_users")
        user_id = int(user_id)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id,username,full_name,role,is_active,permissions_json,must_change_password "
                "FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("المستخدم غير موجود")
            wanted = frozenset(str(key) for key in (granted or ()) if str(key) in CAPABILITIES)
            replacement = UserSession(
                user_id,
                str(row["username"]),
                str(row["full_name"]),
                str(row["role"]),
                wanted,
                bool(row["must_change_password"]),
            )
            if int(row["is_active"]) and self._active_admin_count(conn, replace_id=user_id, replacement=replacement) <= 0:
                raise ValueError("يجب أن يبقى مدير واحد على الأقل لديه صلاحية إدارة المستخدمين")
            payload = overrides_json(str(row["role"]), wanted)
            conn.execute(
                "UPDATE users SET permissions_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (payload, user_id),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('update','user',?,?)",
                (user_id, "permissions=" + ",".join(sorted(wanted))),
            )

    def reset_password(self, user_id: int, new_password: str, *, force_change: bool = True) -> None:
        self.require("admin_users")
        self._validate_password(new_password)
        password_hash, salt = self._hash_password(new_password)
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
                raise ValueError("المستخدم غير موجود")
            conn.execute(
                """UPDATE users SET password_hash=?,password_salt=?,failed_attempts=0,locked_until=NULL,
                   quick_auth_type=NULL,quick_auth_hash=NULL,quick_auth_salt=NULL,quick_auth_key_id=NULL,remember_token_hash=NULL,
                   must_change_password=?,password_changed_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (password_hash, salt, 1 if force_change else 0, self._iso(self._now()), user_id),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('password_reset','user',?,?)",
                (user_id, f"forced={1 if force_change else 0}"),
            )

    def change_own_password(self, current_password: str, new_password: str) -> None:
        session = self.current()
        if session is None:
            raise PermissionError("يجب تسجيل الدخول")
        self.verify_current_password(current_password)
        self._validate_password(new_password)
        if hmac.compare_digest(current_password or "", new_password or ""):
            raise ValueError("كلمة المرور الجديدة يجب أن تختلف عن الحالية")
        password_hash, salt = self._hash_password(new_password)
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE users SET password_hash=?,password_salt=?,failed_attempts=0,locked_until=NULL,
                   quick_auth_type=NULL,quick_auth_hash=NULL,quick_auth_salt=NULL,quick_auth_key_id=NULL,
                   remember_token_hash=NULL,must_change_password=0,password_changed_at=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (password_hash, salt, self._iso(self._now()), session.id),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('password_change','user',?,'self')",
                (session.id,),
            )
        prefs = self._load_prefs()
        prefs.pop("saved_username", None)
        prefs.pop("saved_token", None)
        self._save_prefs(prefs)
        # Keep the in-memory session usable but clear the one-shot flag.
        self._session = UserSession(
            session.id, session.username, session.full_name, session.role,
            session.permissions, False,
        )
        self.db.set_actor(self._session.id, self._session.username)

    def unlock_user(self, user_id: int) -> None:
        self.require("admin_users")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise ValueError("المستخدم غير موجود")
            conn.execute(
                "UPDATE users SET failed_attempts=0,locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES('unlock','user',?,'')",
                (user_id,),
            )

    def delete_user(self, user_id: int) -> None:
        """Permanently remove a user account (audit rows keep the username).

        Guarded like every other user-admin action: you cannot delete the
        account you are signed in with, and the last active user with
        ``admin_users`` can never be removed.
        """
        actor = self.require("admin_users")
        user_id = int(user_id)
        if actor.id == user_id:
            raise ValueError("لا يمكنك حذف المستخدم الحالي")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id,username,role,is_active,permissions_json,must_change_password "
                "FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("المستخدم غير موجود")
            remaining = self._active_admin_count(conn, replace_id=user_id)
            if remaining <= 0:
                raise ValueError("لا يمكن حذف آخر مدير لديه صلاحية إدارة المستخدمين")
            username = str(row["username"])
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.execute(
                "INSERT INTO audit_log(action,entity_type,entity_id,details) "
                "VALUES('delete','user',?,?)",
                (user_id, f"username={username}"),
            )

    def audit_entries(self, limit: int = 200, offset: int = 0) -> list[dict]:
        self.require("admin_users")
        page_size = max(1, min(int(limit), 1000))
        page_offset = max(0, int(offset))
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id,action,entity_type,entity_id,details,user_id,username,created_at
                   FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?""",
                (page_size, page_offset),
            ).fetchall()]
