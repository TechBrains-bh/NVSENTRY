"""Local daily shop entry application using only Python's standard library."""

from __future__ import annotations

import json
import math
import mimetypes
import re
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "daily_entries.sqlite3"
HOST = "127.0.0.1"
PORT = 8000


def connect_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS daily_entries (
            entry_date TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return db


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


def entry_for(db: sqlite3.Connection, entry_date: str) -> dict:
    row = db.execute(
        "SELECT payload FROM daily_entries WHERE entry_date = ?", (entry_date,)
    ).fetchone()
    payload = json.loads(row["payload"]) if row else {"fields": {}, "udhar": [], "suppliers": []}
    previous = db.execute(
        "SELECT payload FROM daily_entries WHERE entry_date < ? ORDER BY entry_date DESC LIMIT 1",
        (entry_date,),
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


def sync_openings(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT entry_date, payload FROM daily_entries ORDER BY entry_date"
    ).fetchall()
    previous_fields: dict = {}
    for row in rows:
        payload = json.loads(row["payload"])
        fields = payload.setdefault("fields", {})
        fields["opening_boiler_stock"] = previous_fields.get("closing_boiler_stock", "")
        fields["opening_desi_stock"] = previous_fields.get("closing_desi_stock", "")
        fields["opening_cash"] = previous_fields.get("closing_cash", "")
        db.execute(
            "UPDATE daily_entries SET payload = ? WHERE entry_date = ?",
            (json.dumps(payload, ensure_ascii=False), row["entry_date"]),
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
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/entry":
            target_date = query.get("date", [""])[0]
            if not valid_date(target_date):
                return self.send_json({"error": "Choose a valid entry date."}, 400)
            with connect_db() as db:
                return self.send_json(entry_for(db, target_date))
        if parsed.path == "/api/entries":
            with connect_db() as db:
                rows = db.execute(
                    "SELECT entry_date, payload FROM daily_entries ORDER BY entry_date DESC LIMIT 500"
                ).fetchall()
                entries = []
                for row in rows:
                    payload = json.loads(row["payload"])
                    entry = {"date": row["entry_date"], **payload}
                    entries.append({"date": row["entry_date"], "status": payload.get("status", "draft"), "summary": summarize(entry)})
                return self.send_json({"entries": entries})
        if parsed.path == "/api/summary":
            target_date = query.get("date", [""])[0]
            if not valid_date(target_date):
                return self.send_json({"error": "Choose a valid entry date."}, 400)
            with connect_db() as db:
                return self.send_json({"date": target_date, "summary": summarize(entry_for(db, target_date))})
        if parsed.path.startswith("/api/"):
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
        if urlparse(self.path).path != "/api/entry":
            return self.send_json({"error": "Not found."}, 404)
        try:
            body = self.read_json()
            if not isinstance(body, dict):
                return self.send_json({"error": "Entry data is invalid."}, 400)
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
                    "INSERT INTO daily_entries(entry_date, payload, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(entry_date) DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP",
                    (target_date, json.dumps(payload, ensure_ascii=False)),
                )
                sync_openings(db)
                db.commit()
                result = entry_for(db, target_date)
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


if __name__ == "__main__":
    with connect_db():
        pass
    print(f"Fieldnote running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
