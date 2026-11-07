"""End-to-end checks against a running local Fieldnote server."""

import datetime as dt
import json
import os
import sqlite3
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app  # noqa: E402

BASE = "http://127.0.0.1:8000"
SESSION_COOKIE = ""


def request(path, method="GET", body=None):
    global SESSION_COOKIE
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if SESSION_COOKIE:
        headers["Cookie"] = SESSION_COOKIE
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            set_cookie = response.headers.get("Set-Cookie")
            if set_cookie:
                SESSION_COOKIE = set_cookie.split(";", 1)[0]
            raw = response.read().decode("utf-8")
            return response.status, raw, json.loads(raw) if "json" in response.headers.get("Content-Type", "") else None
    except urllib.error.HTTPError as error:
        set_cookie = error.headers.get("Set-Cookie")
        if set_cookie:
            SESSION_COOKIE = set_cookie.split(";", 1)[0]
        raw = error.read().decode("utf-8")
        return error.code, raw, json.loads(raw)


class ApplicationEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global SESSION_COOKIE
        SESSION_COOKIE = ""
        status, html, _ = request("/")
        if status != 200 or "entry-form" not in html or "start-voice" not in html:
            raise RuntimeError("The app is not serving its entry page at http://127.0.0.1:8000")
        status, script, _ = request("/app.js")
        for feature in ("SpeechRecognition", "speechSynthesis", "parseSpokenNumber", "setupVoiceButtons", "hi-IN"):
            if status != 200 or feature not in script:
                raise RuntimeError(f"The app JavaScript is missing voice feature {feature!r}")

        status, _, setup = request("/api/setup")
        if status != 200:
            raise RuntimeError("Could not check the app's initial setup state")
        if not setup["initialized"]:
            secret = os.environ.get("NVSENTRY_BOOTSTRAP_SECRET", "")
            if not secret:
                raise unittest.SkipTest("Set NVSENTRY_BOOTSTRAP_SECRET to initialize the local app")
            phone = os.environ.get("NVSENTRY_E2E_PHONE", "+919876543210")
            status, _, _ = request(
                "/api/setup",
                "POST",
                {"phone": phone, "secret": secret},
            )
        else:
            phone = os.environ.get("NVSENTRY_E2E_PHONE", "")
            code = os.environ.get("NVSENTRY_E2E_CODE", "")
            if not phone or not code:
                raise unittest.SkipTest(
                    "Set NVSENTRY_E2E_PHONE and NVSENTRY_E2E_CODE for the running app"
                )
            status, _, _ = request(
                "/api/auth/login",
                "POST",
                {"phone": phone, "code": code},
            )
        if status not in (200, 201):
            raise RuntimeError("Could not authenticate the end-to-end test account")

        status, _, session = request("/api/session")
        if status != 200 or not session["shops"]:
            raise unittest.SkipTest("Register a shop and use an account with shop access")
        cls.shop_id = session["active_shop_id"] or session["shops"][0]["id"]
        status, _, _ = request("/api/active-shop", "POST", {"shop_id": cls.shop_id})
        if status != 200:
            raise RuntimeError("Could not select the test account's active shop")

        start = dt.date(2099, 1, 1)
        cls.test_dates = None
        for offset in range(365 * 5):
            first = (start + dt.timedelta(days=offset)).isoformat()
            second = (start + dt.timedelta(days=offset + 1)).isoformat()
            first_response = request(f"/api/entry?date={first}")
            second_response = request(f"/api/entry?date={second}")
            first_exists = first_response[2]["exists"] if first_response[0] == 200 else True
            second_exists = second_response[2]["exists"] if second_response[0] == 200 else True
            if not first_exists and not second_exists:
                cls.test_dates = (first, second)
                break
        if not cls.test_dates:
            raise RuntimeError("Could not find unused dates for isolated end-to-end checks")

    @classmethod
    def tearDownClass(cls):
        if cls.test_dates:
            with sqlite3.connect(app.DB_PATH) as db:
                db.executemany(
                    "DELETE FROM daily_entries WHERE shop_id = ? AND entry_date = ?",
                    [(cls.shop_id, value) for value in cls.test_dates],
                )

    def test_save_carry_forward_edit_history_summary_and_validation(self):
        first_date, second_date = self.test_dates
        first_before = request(f"/api/entry?date={first_date}")[2]
        original_opening = first_before["fields"]

        fields = {
            "boiler_chicken_bought": "9",
            "desi_chicken_bought": "2",
            "boiler_purchase_cost": "100",
            "desi_purchase_cost": "80",
            "boiler_chicken_chopped": "4",
            "desi_chicken_chopped": "1",
            "closing_boiler_stock": "7",
            "closing_desi_stock": "4",
            "total_weight_sold": "23.8",
            "chicken_wasted": "0",
            "dead_chicken": "0",
            "total_sales": "1400",
            "cash_received": "500",
            "paytm_received": "600",
            "phonepe_received": "300",
            "closing_cash": "800",
            "cash_taken_out_by_bahadur": "50",
            "new_customers": "2",
            "returning_customers": "3",
            "miscellaneous_expense": "25",
        }
        entry = {
            "date": first_date,
            "status": "complete",
            "fields": fields,
            "udhar": [
                {"phone": "9876543210", "amount": "150"},
                {"phone": "+91 9876543211", "amount": "75"},
            ],
            "suppliers": [
                {"phone": "9123456780", "amount": "300"},
                {"phone": "9123456781", "amount": "50"},
            ],
        }

        status, _, saved = request("/api/entry", "POST", entry)
        self.assertEqual(status, 200)
        self.assertEqual(saved["entry"]["status"], "complete")
        self.assertEqual(saved["entry"]["fields"]["opening_boiler_stock"], original_opening.get("opening_boiler_stock", ""))
        self.assertEqual(saved["entry"]["fields"]["opening_cash"], original_opening.get("opening_cash", ""))
        self.assertEqual(len(saved["entry"]["udhar"]), 2)
        self.assertEqual(len(saved["entry"]["suppliers"]), 2)
        self.assertEqual(saved["entry"]["suppliers"][0]["phone"], "9123456780")

        second_before = request(f"/api/entry?date={second_date}")[2]
        self.assertEqual(second_before["fields"]["opening_boiler_stock"], "7")
        self.assertEqual(second_before["fields"]["opening_desi_stock"], "4")
        self.assertEqual(second_before["fields"]["opening_cash"], "800")

        second_entry = {
            "date": second_date,
            "status": "draft",
            "fields": {"closing_boiler_stock": "5", "closing_desi_stock": "3", "closing_cash": "900"},
            "udhar": [],
            "suppliers": [],
        }
        status, _, second_saved = request("/api/entry", "POST", second_entry)
        self.assertEqual(status, 200)
        self.assertEqual(second_saved["entry"]["fields"]["opening_cash"], "800")

        edited = dict(entry)
        edited_fields = dict(fields, closing_boiler_stock="8", closing_desi_stock="6", closing_cash="950")
        edited["fields"] = edited_fields
        status, _, _ = request("/api/entry", "POST", edited)
        self.assertEqual(status, 200)
        recalculated = request(f"/api/entry?date={second_date}")[2]
        self.assertEqual(recalculated["fields"]["opening_boiler_stock"], "8")
        self.assertEqual(recalculated["fields"]["opening_desi_stock"], "6")
        self.assertEqual(recalculated["fields"]["opening_cash"], "950")

        status, _, summary = request(f"/api/summary?date={first_date}")
        self.assertEqual(status, 200)
        self.assertEqual(summary["summary"]["total_sales"], 1400)
        self.assertEqual(summary["summary"]["online_received"], 900)
        self.assertEqual(summary["summary"]["customers"], 5)
        self.assertEqual(summary["summary"]["udhar_total"], 225)
        self.assertTrue(summary["summary"]["payment_check"])

        status, _, history = request("/api/entries")
        self.assertEqual(status, 200)
        records = {item["date"]: item for item in history["entries"]}
        self.assertEqual(records[first_date]["status"], "complete")
        self.assertEqual(records[second_date]["status"], "draft")

        invalid = dict(second_entry, fields={"closing_cash": "-1"})
        status, _, error = request("/api/entry", "POST", invalid)
        self.assertEqual(status, 400)
        self.assertIn("positive", error["error"])
        bad_phone = dict(second_entry, fields={}, udhar=[{"phone": "123", "amount": "40"}])
        status, _, error = request("/api/entry", "POST", bad_phone)
        self.assertEqual(status, 400)
        self.assertIn("phone number", error["error"])
