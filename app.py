"""Local daily shop entry application using only Python's standard library."""

from __future__ import annotations

import io
import hashlib
import hmac
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import string
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie
from typing import Iterator
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NVSENTRY_DB_PATH", ROOT / "daily_entries.sqlite3")).expanduser().resolve()
LOCAL_AUTH_PATH = ROOT / "local_auth.json"
LOCAL_PASSWORD_PHONE = "9350025160"
LOCAL_PASSWORD_ITERATIONS = 600_000
HOST = "127.0.0.1"
PORT = 8000
SESSION_SECONDS = 24 * 60 * 60
LOGIN_CODE_SECONDS = 10 * 60


@contextmanager
def connect_db() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    try:
        db.row_factory = sqlite3.Row
        db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                platform_role TEXT,
                can_own_shop INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )"""
        )
        user_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
        }
        if "can_own_shop" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN can_own_shop INTEGER NOT NULL DEFAULT 0")
        db.execute(
            """CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at INTEGER NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS shop_memberships (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('owner', 'staff')),
                created_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, shop_id)
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS login_codes (
                id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL,
                code_hash BLOB NOT NULL,
                code_salt BLOB NOT NULL,
                role TEXT NOT NULL,
                shop_id INTEGER REFERENCES shops(id),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                consumed_at INTEGER
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                token_hash BLOB PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                active_shop_id INTEGER REFERENCES shops(id),
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS password_login_attempts (
                phone TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                window_started_at INTEGER NOT NULL,
                locked_until INTEGER NOT NULL DEFAULT 0
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS legacy_daily_entries (
                entry_date TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(daily_entries)").fetchall()
        }
        if columns and "shop_id" not in columns:
            db.execute(
                """INSERT OR IGNORE INTO legacy_daily_entries(entry_date, payload, updated_at)
                   SELECT entry_date, payload, updated_at FROM daily_entries"""
            )
            db.execute("DROP TABLE daily_entries")
            columns = set()
        if not columns:
            db.execute(
                """CREATE TABLE daily_entries (
                    shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
                    entry_date TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(shop_id, entry_date)
                )"""
            )
        db.execute("CREATE INDEX IF NOT EXISTS idx_login_codes_phone ON login_codes(phone, expires_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at)")
        with db:
            yield db
    finally:
        db.close()


def valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    except ValueError:
        return False


def number(value: object) -> float:
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def clean_phone(value: object) -> str:
    return re.sub(r"[^+\d]", "", str(value or ""))[:20]


def valid_phone(value: str) -> bool:
    return bool(re.fullmatch(r"\+?[1-9]\d{9,14}", value))


def hash_login_code(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode("ascii"), salt=salt, n=2**14, r=8, p=1)


def load_local_password_config() -> dict | None:
    if not LOCAL_AUTH_PATH.is_file():
        return None
    try:
        config = json.loads(LOCAL_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("The local password configuration could not be read.") from error
    if (
        not isinstance(config, dict)
        or config.get("phone") != LOCAL_PASSWORD_PHONE
        or not isinstance(config.get("salt"), str)
        or not isinstance(config.get("password_hash"), str)
    ):
        raise ValueError("The local password configuration is invalid.")
    try:
        salt = bytes.fromhex(config["salt"])
        password_hash = bytes.fromhex(config["password_hash"])
    except ValueError as error:
        raise ValueError("The local password configuration is invalid.") from error
    if len(salt) != 16 or len(password_hash) != 32:
        raise ValueError("The local password configuration is invalid.")
    return {"salt": salt, "password_hash": password_hash}


def platform_role(session: sqlite3.Row) -> str | None:
    return session["platform_role"]


def new_session(db: sqlite3.Connection, user_id: int, active_shop_id: int | None) -> str:
    token = secrets.token_urlsafe(32)
    now = int(datetime.now(timezone.utc).timestamp())
    db.execute(
        "INSERT INTO sessions(token_hash, user_id, active_shop_id, created_at, expires_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (hashlib.sha256(token.encode("ascii")).digest(), user_id, active_shop_id, now, now + SESSION_SECONDS),
    )
    return token


def active_session(token: str) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("ascii", "ignore")).digest()
    now = int(datetime.now(timezone.utc).timestamp())
    with connect_db() as db:
        row = db.execute(
            """SELECT s.token_hash, s.user_id, s.active_shop_id, s.expires_at,
                      u.phone, u.platform_role, u.can_own_shop
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (token_hash, now),
        ).fetchone()
        return dict(row) if row else None


def session_shops(db: sqlite3.Connection, user_id: int) -> list[dict]:
    user = db.execute(
        "SELECT platform_role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if user and user["platform_role"] == "platform_owner":
        return [
            dict(row)
            for row in db.execute(
                "SELECT id, name, 'owner' AS role FROM shops ORDER BY name COLLATE NOCASE"
            ).fetchall()
        ]
    return [
        dict(row)
        for row in db.execute(
            """SELECT s.id, s.name, m.role FROM shops s
               JOIN shop_memberships m ON m.shop_id = s.id
               WHERE m.user_id = ? ORDER BY s.name COLLATE NOCASE""",
            (user_id,),
        ).fetchall()
    ]


def entry_for(db: sqlite3.Connection, shop_id: int, entry_date: str) -> dict:
    row = db.execute(
        "SELECT payload FROM daily_entries WHERE shop_id = ? AND entry_date = ?",
        (shop_id, entry_date),
    ).fetchone()
    payload = json.loads(row["payload"]) if row else {"fields": {}, "udhar": [], "suppliers": []}
    previous = db.execute(
        "SELECT payload FROM daily_entries WHERE shop_id = ? AND entry_date < ? "
        "ORDER BY entry_date DESC LIMIT 1",
        (shop_id, entry_date),
    ).fetchone()
    prev_fields = json.loads(previous["payload"]).get("fields", {}) if previous else {}
    fields = payload.setdefault("fields", {})
    if row:
        # Persisted opening values are kept in sync whenever an earlier entry changes.
        opening = {
            "opening_boiler_stock": fields.get("opening_boiler_stock", ""),
            "opening_desi_stock": fields.get("opening_desi_stock", ""),
            "opening_cash": fields.get("opening_cash", ""),
        }
    else:
        opening = {
            "opening_boiler_stock": prev_fields.get("closing_boiler_stock", ""),
            "opening_desi_stock": prev_fields.get("closing_desi_stock", ""),
            "opening_cash": prev_fields.get("closing_cash", ""),
        }
    fields.update(opening)
    return {"date": entry_date, "exists": bool(row), "status": payload.get("status", "draft"), **payload}


def sync_openings(db: sqlite3.Connection, shop_id: int) -> None:
    rows = db.execute(
        "SELECT entry_date, payload FROM daily_entries WHERE shop_id = ? ORDER BY entry_date",
        (shop_id,),
    ).fetchall()
    previous_fields: dict = {}
    for row in rows:
        payload = json.loads(row["payload"])
        fields = payload.setdefault("fields", {})
        fields["opening_boiler_stock"] = previous_fields.get("closing_boiler_stock", "")
        fields["opening_desi_stock"] = previous_fields.get("closing_desi_stock", "")
        fields["opening_cash"] = previous_fields.get("closing_cash", "")
        db.execute(
            "UPDATE daily_entries SET payload = ? WHERE shop_id = ? AND entry_date = ?",
            (json.dumps(payload, ensure_ascii=False), shop_id, row["entry_date"]),
        )
        previous_fields = fields


def summarize(entry: dict) -> dict:
    fields = entry.get("fields", {})
    sales = number(fields.get("total_sales"))
    cash = number(fields.get("cash_received"))
    paytm = number(fields.get("paytm_received"))
    phonepe = number(fields.get("phonepe_received"))
    expenses = sum(
        number(fields.get(key))
        for key in (
            "miscellaneous_expense", "dalia", "dead_chicken_cost", "police",
            "bahadur_salary", "shop_rent", "electricity_bill", "ap", "ag",
            "boiler_purchase_cost", "desi_purchase_cost",
        )
    ) + sum(number(item.get("amount")) for item in entry.get("suppliers", []))
    return {
        "total_sales": sales,
        "cash_received": cash,
        "online_received": paytm + phonepe,
        "paytm_received": paytm,
        "phonepe_received": phonepe,
        "customers": number(fields.get("new_customers")) + number(fields.get("returning_customers")),
        "new_customers": number(fields.get("new_customers")),
        "returning_customers": number(fields.get("returning_customers")),
        "expenses": expenses,
        "udhar_total": sum(number(item.get("amount")) for item in entry.get("udhar", [])),
        "closing_cash": number(fields.get("closing_cash")),
        "closing_boiler_stock": fields.get("closing_boiler_stock", ""),
        "closing_desi_stock": fields.get("closing_desi_stock", ""),
        "weight_sold": number(fields.get("total_weight_sold")),
        "payment_check": abs(sales - cash - paytm - phonepe) < 0.01,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Fieldnote/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, value: object, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        session_cookie = getattr(self, "_pending_session_cookie", None)
        if session_cookie:
            self.send_header("Set-Cookie", session_cookie)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}

    def request_session(self) -> dict | None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get("nvsentry_session")
        return active_session(morsel.value if morsel else "")

    def set_session_cookie(self, token: str, max_age: int = SESSION_SECONDS) -> None:
        secure = getattr(self, "secure_request", False)
        value = f"nvsentry_session={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
        if secure:
            value += "; Secure"
        self._pending_session_cookie = value

    @staticmethod
    def valid_session_for_shop(db: sqlite3.Connection, session: dict) -> int | None:
        shop_id = session.get("active_shop_id")
        if shop_id is None:
            return None
        if session.get("platform_role") == "platform_owner":
            shop = db.execute("SELECT 1 FROM shops WHERE id = ?", (shop_id,)).fetchone()
            return shop_id if shop else None
        member = db.execute(
            "SELECT 1 FROM shop_memberships WHERE user_id = ? AND shop_id = ?",
            (session["user_id"], shop_id),
        ).fetchone()
        return shop_id if member else None

    def issue_code(
        self,
        db: sqlite3.Connection,
        issuer: dict,
        phone_value: object,
        role: str,
        shop_id: int | None,
    ) -> tuple[str | None, str | None]:
        phone = clean_phone(phone_value)
        if not valid_phone(phone):
            return None, "Enter a mobile number with country code and 10–15 digits."

        platform = issuer["platform_role"]
        allowed_platform_roles = {"platform_admin", "support_admin", "support"}
        if role == "shop_owner":
            if platform not in {"platform_owner", "platform_admin"}:
                return None, "Only platform administrators can invite a shop owner."
        elif role == "staff":
            if not shop_id:
                return None, "Choose a shop for this staff invitation."
            membership = db.execute(
                "SELECT role FROM shop_memberships WHERE user_id = ? AND shop_id = ?",
                (issuer["user_id"], shop_id),
            ).fetchone()
            if platform not in {"platform_owner", "platform_admin"} and (
                not membership or membership["role"] != "owner"
            ):
                return None, "Only this shop's owner can invite its staff."
        elif role in allowed_platform_roles:
            if platform != "platform_owner":
                return None, "Only the platform owner can invite platform administrators or support."
        else:
            return None, "That invitation role is not allowed."

        now = int(datetime.now(timezone.utc).timestamp())
        recent = db.execute(
            "SELECT COUNT(*) AS count FROM login_codes WHERE created_by = ? AND created_at > ?",
            (issuer["user_id"], now - 3600),
        ).fetchone()["count"]
        if recent >= 20:
            return None, "Invitation limit reached. Try again later."

        db.execute(
            "UPDATE login_codes SET consumed_at = ? WHERE phone = ? AND consumed_at IS NULL",
            (now, phone),
        )
        code = "".join(secrets.choice(string.digits) for _ in range(6))
        salt = secrets.token_bytes(16)
        db.execute(
            """INSERT INTO login_codes
               (phone, code_hash, code_salt, role, shop_id, created_by, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                phone,
                hash_login_code(code, salt),
                salt,
                role,
                shop_id,
                issuer["user_id"],
                now,
                now + LOGIN_CODE_SECONDS,
            ),
        )
        return code, None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/setup":
            with connect_db() as db:
                initialized = db.execute(
                    "SELECT 1 FROM users WHERE platform_role = 'platform_owner' LIMIT 1"
                ).fetchone() is not None
            return self.send_json(
                {
                    "initialized": initialized,
                    "password_login_available": LOCAL_AUTH_PATH.is_file(),
                }
            )
        if parsed.path == "/api/session":
            session = self.request_session()
            if not session:
                return self.send_json({"authenticated": False}, 401)
            with connect_db() as db:
                shops = session_shops(db, session["user_id"])
            return self.send_json(
                {
                    "authenticated": True,
                    "user": {
                        "id": session["user_id"],
                        "phone": session["phone"],
                        "platform_role": session["platform_role"],
                        "can_own_shop": bool(session["can_own_shop"]),
                    },
                    "shops": shops,
                    "active_shop_id": session["active_shop_id"],
                    "expires_at": session["expires_at"],
                }
            )
        if parsed.path.startswith("/api/"):
            session = self.request_session()
            if not session:
                return self.send_json({"error": "Sign in to continue."}, 401)
            if parsed.path == "/api/shops":
                with connect_db() as db:
                    return self.send_json(
                        {
                            "shops": session_shops(db, session["user_id"]),
                            "can_create_shop": bool(session["can_own_shop"])
                            or session["platform_role"] == "platform_owner",
                        }
                    )
            if parsed.path == "/api/staff":
                with connect_db() as db:
                    shop_id = self.valid_session_for_shop(db, session)
                    membership = db.execute(
                        "SELECT role FROM shop_memberships WHERE user_id = ? AND shop_id = ?",
                        (session["user_id"], shop_id),
                    ).fetchone() if shop_id else None
                    if (
                        session["platform_role"] != "platform_owner"
                        and (not membership or membership["role"] != "owner")
                    ):
                        return self.send_json({"error": "Only the shop owner can view staff."}, 403)
                    rows = db.execute(
                        """SELECT u.phone, m.role FROM shop_memberships m
                           JOIN users u ON u.id = m.user_id WHERE m.shop_id = ?
                           ORDER BY m.role, u.phone""",
                        (shop_id,),
                    ).fetchall()
                    return self.send_json({"staff": [dict(row) for row in rows]})
            if parsed.path in {"/api/entry", "/api/entries", "/api/summary"}:
                with connect_db() as db:
                    shop_id = self.valid_session_for_shop(db, session)
                    if not shop_id:
                        return self.send_json(
                            {"error": "Select a shop before opening its ledger."}, 409
                        )
                    if parsed.path == "/api/entry":
                        target_date = query.get("date", [""])[0]
                        if not valid_date(target_date):
                            return self.send_json({"error": "Choose a valid entry date."}, 400)
                        return self.send_json(entry_for(db, shop_id, target_date))
                    if parsed.path == "/api/entries":
                        rows = db.execute(
                            "SELECT entry_date, payload FROM daily_entries "
                            "WHERE shop_id = ? ORDER BY entry_date DESC LIMIT 500",
                            (shop_id,),
                        ).fetchall()
                        entries = []
                        for row in rows:
                            payload = json.loads(row["payload"])
                            entry = {"date": row["entry_date"], **payload}
                            entries.append(
                                {
                                    "date": row["entry_date"],
                                    "status": payload.get("status", "draft"),
                                    "summary": summarize(entry),
                                }
                            )
                        return self.send_json({"entries": entries})
                    target_date = query.get("date", [""])[0]
                    if not valid_date(target_date):
                        return self.send_json({"error": "Choose a valid entry date."}, 400)
                    return self.send_json(
                        {
                            "date": target_date,
                            "summary": summarize(entry_for(db, shop_id, target_date)),
                        }
                    )
            return self.send_json({"error": "Not found."}, 404)
        relative = "index.html" if parsed.path in ("/", "") else parsed.path.lstrip("/")
        file_path = (ROOT / relative).resolve()
        if ROOT not in file_path.parents or not file_path.is_file():
            self.send_error(404)
            return
        content = file_path.read_bytes()
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "application/json"):
            mime += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if not isinstance(body, dict):
                return self.send_json({"error": "Request data is invalid."}, 400)
            if path == "/api/setup":
                configured_secret = os.environ.get("NVSENTRY_BOOTSTRAP_SECRET", "")
                supplied_secret = str(body.get("secret", ""))
                phone = clean_phone(body.get("phone"))
                if not configured_secret:
                    return self.send_json(
                        {"error": "Platform setup is not configured by the host."}, 503
                    )
                if not hmac.compare_digest(supplied_secret, configured_secret):
                    return self.send_json({"error": "Setup secret is incorrect."}, 401)
                if not valid_phone(phone):
                    return self.send_json(
                        {"error": "Enter a mobile number with country code and 10–15 digits."},
                        400,
                    )
                with connect_db() as db:
                    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                        return self.send_json({"error": "Initial setup has already been used."}, 409)
                    now = int(datetime.now(timezone.utc).timestamp())
                    cursor = db.execute(
                        """INSERT INTO users(phone, platform_role, can_own_shop, created_at)
                           VALUES(?, 'platform_owner', 1, ?)""",
                        (phone, now),
                    )
                    token = new_session(db, cursor.lastrowid, None)
                    self.set_session_cookie(token)
                return self.send_json({"authenticated": True}, 201)
            if path == "/api/auth/login":
                phone = clean_phone(body.get("phone"))
                code = str(body.get("code", "")).strip()
                if not valid_phone(phone) or not re.fullmatch(r"\d{6}", code):
                    return self.send_json({"error": "Enter your mobile number and six-digit code."}, 400)
                now = int(datetime.now(timezone.utc).timestamp())
                with connect_db() as db:
                    issued = db.execute(
                        """SELECT * FROM login_codes WHERE phone = ? AND consumed_at IS NULL
                           AND expires_at > ? ORDER BY id DESC LIMIT 1""",
                        (phone, now),
                    ).fetchone()
                    if not issued or issued["attempts"] >= 5:
                        return self.send_json({"error": "Code is invalid, expired, or already used."}, 401)
                    if not hmac.compare_digest(
                        hash_login_code(code, issued["code_salt"]), issued["code_hash"]
                    ):
                        db.execute(
                            "UPDATE login_codes SET attempts = attempts + 1 WHERE id = ?",
                            (issued["id"],),
                        )
                        return self.send_json({"error": "Code is invalid, expired, or already used."}, 401)

                    db.execute(
                        "UPDATE login_codes SET consumed_at = ? WHERE id = ?",
                        (now, issued["id"]),
                    )
                    user = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
                    if user:
                        user_id = user["id"]
                    else:
                        cursor = db.execute(
                            "INSERT INTO users(phone, created_at) VALUES(?, ?)", (phone, now)
                        )
                        user_id = cursor.lastrowid

                    role = issued["role"]
                    if role == "shop_owner":
                        db.execute("UPDATE users SET can_own_shop = 1 WHERE id = ?", (user_id,))
                    elif role in {"platform_admin", "support_admin", "support"}:
                        db.execute(
                            "UPDATE users SET platform_role = ? WHERE id = ? AND platform_role != 'platform_owner'",
                            (role, user_id),
                        )
                    elif role == "staff":
                        db.execute(
                            """INSERT INTO shop_memberships(user_id, shop_id, role, created_at)
                               VALUES(?, ?, 'staff', ?)
                               ON CONFLICT(user_id, shop_id) DO NOTHING""",
                            (user_id, issued["shop_id"], now),
                        )

                    memberships = session_shops(db, user_id)
                    active_shop_id = issued["shop_id"] if role == "staff" else (
                        memberships[0]["id"] if memberships else None
                    )
                    token = new_session(db, user_id, active_shop_id)
                    self.set_session_cookie(token)
                return self.send_json({"authenticated": True})
            if path == "/api/auth/password":
                phone = clean_phone(body.get("phone"))
                password = body.get("password")
                if (
                    not valid_phone(phone)
                    or not isinstance(password, str)
                    or not password
                    or len(password) > 1024
                ):
                    return self.send_json({"error": "Enter your mobile number and password."}, 400)
                try:
                    config = load_local_password_config()
                except ValueError as error:
                    logging.getLogger(__name__).exception("Invalid local password configuration")
                    return self.send_json({"error": str(error)}, 503)
                if config is None:
                    return self.send_json(
                        {"error": "Password sign-in is not configured. Use a one-time code."},
                        503,
                    )

                now = int(datetime.now(timezone.utc).timestamp())
                designated_phone = phone in (
                    LOCAL_PASSWORD_PHONE,
                    f"+91{LOCAL_PASSWORD_PHONE}",
                )
                attempt_phone = LOCAL_PASSWORD_PHONE if designated_phone else phone
                candidate = hashlib.pbkdf2_hmac(
                    "sha256",
                    password.encode("utf-8"),
                    config["salt"],
                    LOCAL_PASSWORD_ITERATIONS,
                    dklen=32,
                )
                with connect_db() as db:
                    attempt = db.execute(
                        "SELECT attempts, window_started_at, locked_until "
                        "FROM password_login_attempts WHERE phone = ?",
                        (attempt_phone,),
                    ).fetchone()
                    if attempt and attempt["locked_until"] > now:
                        return self.send_json(
                            {"error": "Too many sign-in attempts. Try again in 15 minutes."},
                            429,
                        )

                    if (
                        attempt is None
                        or now - attempt["window_started_at"] >= 900
                        or attempt["locked_until"] > 0
                    ):
                        attempts = 0
                        window_started_at = now
                    else:
                        attempts = attempt["attempts"]
                        window_started_at = attempt["window_started_at"]

                    if designated_phone:
                        user = db.execute(
                            "SELECT id FROM users WHERE phone IN (?, ?)",
                            (LOCAL_PASSWORD_PHONE, f"+91{LOCAL_PASSWORD_PHONE}"),
                        ).fetchone()
                    else:
                        user = db.execute(
                            "SELECT id FROM users WHERE phone = ?", (phone,)
                        ).fetchone()
                    password_matches = hmac.compare_digest(
                        candidate, config["password_hash"]
                    )
                    if (
                        designated_phone
                        and user
                        and password_matches
                    ):
                        db.execute(
                            """UPDATE users SET platform_role = 'platform_owner',
                               can_own_shop = 1 WHERE id = ?""",
                            (user["id"],),
                        )
                        db.execute(
                            "DELETE FROM password_login_attempts WHERE phone = ?",
                            (attempt_phone,),
                        )
                        memberships = session_shops(db, user["id"])
                        active_shop_id = memberships[0]["id"] if memberships else None
                        token = new_session(db, user["id"], active_shop_id)
                        self.set_session_cookie(token)
                        return self.send_json({"authenticated": True})

                    attempts += 1
                    locked_until = now + 900 if attempts >= 5 else 0
                    db.execute(
                        """INSERT INTO password_login_attempts
                           (phone, attempts, window_started_at, locked_until)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(phone) DO UPDATE SET
                             attempts=excluded.attempts,
                             window_started_at=excluded.window_started_at,
                             locked_until=excluded.locked_until""",
                        (attempt_phone, attempts, window_started_at, locked_until),
                    )
                    if locked_until:
                        return self.send_json(
                            {"error": "Too many sign-in attempts. Try again in 15 minutes."},
                            429,
                        )
                return self.send_json({"error": "Mobile number or password is incorrect."}, 401)
            if path == "/api/auth/logout":
                session = self.request_session()
                if session:
                    with connect_db() as db:
                        db.execute(
                            "DELETE FROM sessions WHERE token_hash = ?",
                            (
                                hashlib.sha256(
                                    self._cookie_token().encode("ascii", "ignore")
                                ).digest(),
                            ),
                        )
                self.set_session_cookie("", 0)
                return self.send_json({"authenticated": False})

            session = self.request_session()
            if not session:
                return self.send_json({"error": "Sign in to continue."}, 401)
            if path == "/api/active-shop":
                try:
                    shop_id = int(body.get("shop_id"))
                except (TypeError, ValueError):
                    return self.send_json({"error": "Choose a valid shop."}, 400)
                with connect_db() as db:
                    has_membership = db.execute(
                        "SELECT 1 FROM shop_memberships WHERE user_id = ? AND shop_id = ?",
                        (session["user_id"], shop_id),
                    ).fetchone()
                    is_platform_owner = (
                        session["platform_role"] == "platform_owner"
                        and db.execute("SELECT 1 FROM shops WHERE id = ?", (shop_id,)).fetchone()
                    )
                    if not has_membership and not is_platform_owner:
                        return self.send_json({"error": "You do not have access to that shop."}, 403)
                    db.execute(
                        "UPDATE sessions SET active_shop_id = ? WHERE token_hash = ?",
                        (
                            shop_id,
                            hashlib.sha256(self._cookie_token().encode("ascii", "ignore")).digest(),
                        ),
                    )
                return self.send_json({"active_shop_id": shop_id})
            if path == "/api/shops":
                name = str(body.get("name", "")).strip()
                if not 2 <= len(name) <= 80:
                    return self.send_json(
                        {"error": "Shop name must be between 2 and 80 characters."}, 400
                    )
                if not (session["can_own_shop"] or session["platform_role"] == "platform_owner"):
                    return self.send_json({"error": "Only a shop owner can register a shop."}, 403)
                now = int(datetime.now(timezone.utc).timestamp())
                with connect_db() as db:
                    previous_shop_count = db.execute("SELECT COUNT(*) AS count FROM shops").fetchone()["count"]
                    cursor = db.execute(
                        "INSERT INTO shops(name, created_by, created_at) VALUES(?, ?, ?)",
                        (name, session["user_id"], now),
                    )
                    shop_id = cursor.lastrowid
                    db.execute(
                        """INSERT INTO shop_memberships(user_id, shop_id, role, created_at)
                           VALUES(?, ?, 'owner', ?)""",
                        (session["user_id"], shop_id, now),
                    )
                    if previous_shop_count == 0:
                        db.execute(
                            """INSERT INTO daily_entries(shop_id, entry_date, payload, updated_at)
                               SELECT ?, entry_date, payload, updated_at FROM legacy_daily_entries""",
                            (shop_id,),
                        )
                        db.execute("DELETE FROM legacy_daily_entries")
                    token_hash = hashlib.sha256(self._cookie_token().encode("ascii", "ignore")).digest()
                    db.execute(
                        "UPDATE sessions SET active_shop_id = ? WHERE token_hash = ?",
                        (shop_id, token_hash),
                    )
                    shop = db.execute("SELECT id, name FROM shops WHERE id = ?", (shop_id,)).fetchone()
                return self.send_json({"shop": dict(shop)}, 201)
            if path == "/api/auth/codes":
                role = str(body.get("role", ""))
                raw_shop_id = body.get("shop_id")
                try:
                    shop_id = int(raw_shop_id) if raw_shop_id is not None else None
                except (TypeError, ValueError):
                    return self.send_json({"error": "Choose a valid shop."}, 400)
                with connect_db() as db:
                    code, error = self.issue_code(
                        db, session, body.get("phone"), role, shop_id
                    )
                    if error:
                        return self.send_json({"error": error}, 400)
                return self.send_json(
                    {
                        "code": code,
                        "expires_in_seconds": LOGIN_CODE_SECONDS,
                        "delivery": "Show this one-time code to the invited user using a private channel.",
                    },
                    201,
                )
            if path == "/api/entry":
                with connect_db() as db:
                    shop_id = self.valid_session_for_shop(db, session)
                    if not shop_id:
                        return self.send_json({"error": "Select a shop before saving its ledger."}, 409)
            else:
                return self.send_json({"error": "Not found."}, 404)
            target_date = str(body.get("date", ""))
            if not valid_date(target_date):
                return self.send_json({"error": "Choose a valid entry date."}, 400)
            fields = body.get("fields", {})
            if not isinstance(fields, dict):
                return self.send_json({"error": "Entry fields are invalid."}, 400)
            for key, value in fields.items():
                if str(value).strip() == "":
                    continue
                try:
                    parsed = float(str(value).replace(",", "").strip())
                except ValueError:
                    return self.send_json({"error": f"Enter a number for {str(key).replace('_', ' ')}."}, 400)
                if not math.isfinite(parsed) or parsed < 0:
                    return self.send_json({"error": f"Use zero or a positive number for {str(key).replace('_', ' ')}."}, 400)
            for group_label, raw_items in (("Udhar", body.get("udhar", [])), ("Supplier payment", body.get("suppliers", []))):
                if isinstance(raw_items, list):
                    for item in raw_items:
                        if not isinstance(item, dict):
                            continue
                        if str(item.get("amount", "")).strip():
                            try:
                                parsed = float(str(item["amount"]).replace(",", "").strip())
                            except ValueError:
                                return self.send_json({"error": f"Enter a valid {group_label.lower()} amount."}, 400)
                            if not math.isfinite(parsed) or parsed < 0:
                                return self.send_json({"error": f"{group_label} amount must be zero or more."}, 400)
                        phone = clean_phone(item.get("phone"))
                        if phone and not 10 <= len(phone.lstrip("+")) <= 15:
                            return self.send_json({"error": f"Enter a valid phone number for {group_label.lower()} (10–15 digits)."}, 400)
            payload = {
                "fields": {str(key): str(value or "")[:200] for key, value in fields.items()},
                "udhar": self.clean_repeat(body.get("udhar", [])),
                "suppliers": self.clean_repeat(body.get("suppliers", [])),
                "status": "complete" if body.get("status") == "complete" else "draft",
            }
            with connect_db() as db:
                db.execute(
                    "INSERT INTO daily_entries(shop_id, entry_date, payload, updated_at) "
                    "VALUES(?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(shop_id, entry_date) "
                    "DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
                    (shop_id, target_date, json.dumps(payload, ensure_ascii=False)),
                )
                sync_openings(db, shop_id)
                db.commit()
                result = entry_for(db, shop_id, target_date)
            return self.send_json({"entry": result, "summary": summarize(result)})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return self.send_json({"error": "The entry could not be read. Please check the values and try again."}, 400)

    @staticmethod
    def clean_repeat(value: object) -> list:
        if not isinstance(value, list):
            return []
        result = []
        for item in value[:100]:
            if not isinstance(item, dict):
                continue
            phone = clean_phone(item.get("phone"))
            amount = str(item.get("amount", "")).strip()[:30]
            if phone or amount:
                result.append({"phone": phone, "amount": amount})
        return result

    def _cookie_token(self) -> str:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get("nvsentry_session")
        return morsel.value if morsel else ""


class WSGIHandler(Handler):
    def send_response(self, code: int, message: str | None = None) -> None:
        self._status = code

    def send_header(self, keyword: str, value: str) -> None:
        self._response_headers.append((keyword, value))

    def end_headers(self) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        body = f"{HTTPStatus(code).phrase}\n".encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def application(environ: dict, start_response) -> list[bytes]:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    request = WSGIHandler.__new__(WSGIHandler)
    request._status = 200
    request._response_headers = []
    request.wfile = io.BytesIO()
    request.rfile = environ["wsgi.input"]
    request.headers = {
        "Content-Length": environ.get("CONTENT_LENGTH") or "0",
        "Cookie": environ.get("HTTP_COOKIE", ""),
    }
    request.secure_request = environ.get("wsgi.url_scheme") == "https"
    request.path = environ.get("PATH_INFO", "/")
    query = environ.get("QUERY_STRING", "")
    if query:
        request.path += f"?{query}"

    if method == "GET":
        request.do_GET()
    elif method == "POST":
        request.do_POST()
    else:
        request._status = 405
        request._response_headers.extend(
            [("Content-Type", "text/plain; charset=utf-8"), ("Allow", "GET, POST")]
        )
        body = b"Method not allowed\n"
        request._response_headers.append(("Content-Length", str(len(body))))
        request.wfile.write(body)

    status = request._status
    start_response(f"{status} {HTTPStatus(status).phrase}", request._response_headers)
    return [request.wfile.getvalue()]


if __name__ == "__main__":
    with connect_db():
        pass
    print(f"Fieldnote running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
