import io
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app


class WSGIApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        self.original_local_auth_path = app.LOCAL_AUTH_PATH
        self.original_secret = os.environ.get("NVSENTRY_BOOTSTRAP_SECRET")
        os.environ["NVSENTRY_BOOTSTRAP_SECRET"] = "test-bootstrap-secret"
        app.DB_PATH = Path(self.temp_dir.name) / "entries.sqlite3"
        app.LOCAL_AUTH_PATH = Path(self.temp_dir.name) / "local_auth.json"
        self.cookie = ""

    def tearDown(self):
        app.DB_PATH = self.original_db_path
        app.LOCAL_AUTH_PATH = self.original_local_auth_path
        if self.original_secret is None:
            os.environ.pop("NVSENTRY_BOOTSTRAP_SECRET", None)
        else:
            os.environ["NVSENTRY_BOOTSTRAP_SECRET"] = self.original_secret
        self.temp_dir.cleanup()

    def request(self, path, method="GET", body=b"", cookie=None):
        parsed_path, _, query = path.partition("?")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parsed_path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_COOKIE": self.cookie if cookie is None else cookie,
        }
        response = {}

        def start_response(status, headers):
            response["status"] = status
            response["headers"] = dict(headers)

        response["body"] = b"".join(app.application(environ, start_response))
        return response

    def json_request(self, path, method="GET", values=None, cookie=None):
        body = json.dumps(values or {}).encode("utf-8") if method == "POST" else b""
        return self.request(path, method=method, body=body, cookie=cookie)

    @staticmethod
    def response_cookie(response):
        return response["headers"].get("Set-Cookie", "").split(";", 1)[0]

    def setup_owner(self):
        response = self.json_request(
            "/api/setup",
            "POST",
            {"phone": "+919876543210", "secret": "test-bootstrap-secret"},
        )
        self.assertEqual(response["status"], "201 Created")
        self.cookie = self.response_cookie(response)
        return self.cookie

    def create_shop(self, name="Test Shop"):
        response = self.json_request("/api/shops", "POST", {"name": name})
        self.assertEqual(response["status"], "201 Created")
        return json.loads(response["body"])["shop"]

    def test_serves_static_page(self):
        response = self.request("/")

        self.assertEqual(response["status"], "200 OK")
        self.assertIn(b'id="entry-form"', response["body"])
        self.assertIn("text/html", response["headers"]["Content-Type"])

    def test_saves_and_reads_entry(self):
        self.setup_owner()
        self.create_shop()
        body = json.dumps({
            "date": "2099-12-30",
            "status": "complete",
            "fields": {"closing_cash": "125"},
            "udhar": [],
            "suppliers": [],
        }).encode("utf-8")

        saved = self.request("/api/entry", method="POST", body=body)
        loaded = self.request("/api/entry?date=2099-12-30")

        self.assertEqual(saved["status"], "200 OK")
        self.assertEqual(json.loads(saved["body"])["entry"]["status"], "complete")
        self.assertEqual(json.loads(loaded["body"])["fields"]["closing_cash"], "125")

    def test_requires_login_and_limits_session_to_24_hours(self):
        unauthorized = self.json_request("/api/entry?date=2099-12-30")
        self.assertEqual(unauthorized["status"], "401 Unauthorized")

        cookie = self.setup_owner()
        session = self.json_request("/api/session")
        self.assertEqual(session["status"], "200 OK")
        self.assertEqual(json.loads(session["body"])["user"]["platform_role"], "platform_owner")

        token = cookie.split("=", 1)[1]
        token_hash = hashlib.sha256(token.encode("ascii")).digest()
        db = sqlite3.connect(app.DB_PATH)
        try:
            db.execute("UPDATE sessions SET expires_at = 0 WHERE token_hash = ?", (token_hash,))
            db.commit()
        finally:
            db.close()
        expired = self.json_request("/api/session")
        self.assertEqual(expired["status"], "401 Unauthorized")

    def test_logout_clears_session_cookie(self):
        self.setup_owner()
        logged_out = self.request("/api/auth/logout", method="POST")

        self.assertEqual(logged_out["status"], "200 OK")
        self.assertIn("Max-Age=0", logged_out["headers"]["Set-Cookie"])
        session = self.json_request("/api/session")
        self.assertEqual(session["status"], "401 Unauthorized")

    def test_shop_entries_are_isolated_and_legacy_entries_move_to_first_shop(self):
        self.setup_owner()
        db = sqlite3.connect(app.DB_PATH)
        try:
            db.execute(
                "INSERT INTO legacy_daily_entries(entry_date, payload) VALUES(?, ?)",
                ("2099-12-29", json.dumps({"fields": {"closing_cash": "42"}})),
            )
            db.commit()
        finally:
            db.close()
        first_shop = self.create_shop("First Shop")
        migrated = self.json_request("/api/entry?date=2099-12-29")
        self.assertEqual(json.loads(migrated["body"])["fields"]["closing_cash"], "42")

        first_entry = {
            "date": "2099-12-30",
            "status": "complete",
            "fields": {"closing_cash": "125"},
        }
        saved_first = self.json_request("/api/entry", "POST", first_entry)
        self.assertEqual(saved_first["status"], "200 OK")
        second_shop = self.create_shop("Second Shop")
        saved_second = self.json_request(
            "/api/entry",
            "POST",
            dict(first_entry, fields={"closing_cash": "900"}),
        )
        self.assertEqual(saved_second["status"], "200 OK")

        self.json_request("/api/active-shop", "POST", {"shop_id": first_shop["id"]})
        first_loaded = self.json_request("/api/entry?date=2099-12-30")
        self.assertEqual(json.loads(first_loaded["body"])["fields"]["closing_cash"], "125")

        self.json_request("/api/active-shop", "POST", {"shop_id": second_shop["id"]})
        second_loaded = self.json_request("/api/entry?date=2099-12-30")
        self.assertEqual(json.loads(second_loaded["body"])["fields"]["closing_cash"], "900")

    def test_invitation_code_is_single_use_and_staff_is_shop_scoped(self):
        self.setup_owner()
        shop = self.create_shop()
        issued = self.json_request(
            "/api/auth/codes",
            "POST",
            {"phone": "+919876543211", "role": "staff", "shop_id": shop["id"]},
        )
        self.assertEqual(issued["status"], "201 Created")
        code = json.loads(issued["body"])["code"]

        login = self.json_request(
            "/api/auth/login",
            "POST",
            {"phone": "+919876543211", "code": code},
            cookie="",
        )
        self.assertEqual(login["status"], "200 OK")
        staff_cookie = self.response_cookie(login)
        session = self.json_request("/api/session", cookie=staff_cookie)
        self.assertEqual(json.loads(session["body"])["shops"][0]["role"], "staff")
        forbidden = self.json_request("/api/staff", cookie=staff_cookie)
        self.assertEqual(forbidden["status"], "403 Forbidden")

        repeated_login = self.json_request(
            "/api/auth/login",
            "POST",
            {"phone": "+919876543211", "code": code},
            cookie="",
        )
        self.assertEqual(repeated_login["status"], "401 Unauthorized")

    def test_login_codes_expire_and_lock_after_five_wrong_attempts(self):
        self.setup_owner()
        shop = self.create_shop()
        phone = "+919876543212"
        expired_issue = self.json_request(
            "/api/auth/codes",
            "POST",
            {"phone": phone, "role": "staff", "shop_id": shop["id"]},
        )
        expired_code = json.loads(expired_issue["body"])["code"]
        db = sqlite3.connect(app.DB_PATH)
        try:
            db.execute("UPDATE login_codes SET expires_at = 0")
            db.commit()
        finally:
            db.close()
        expired_login = self.json_request(
            "/api/auth/login",
            "POST",
            {"phone": phone, "code": expired_code},
            cookie="",
        )
        self.assertEqual(expired_login["status"], "401 Unauthorized")

        valid_issue = self.json_request(
            "/api/auth/codes",
            "POST",
            {"phone": phone, "role": "staff", "shop_id": shop["id"]},
        )
        valid_code = json.loads(valid_issue["body"])["code"]
        wrong_code = f"{(int(valid_code) + 1) % 1_000_000:06d}"
        for _ in range(5):
            rejected = self.json_request(
                "/api/auth/login",
                "POST",
                {"phone": phone, "code": wrong_code},
                cookie="",
            )
            self.assertEqual(rejected["status"], "401 Unauthorized")
        locked_out = self.json_request(
            "/api/auth/login",
            "POST",
            {"phone": phone, "code": valid_code},
            cookie="",
        )
        self.assertEqual(locked_out["status"], "401 Unauthorized")

    def test_configured_password_is_phone_scoped_hashed_and_rate_limited(self):
        password = "test-password-for-unit-tests"
        salt = b"test-salt-123456"
        bootstrap = self.json_request(
            "/api/setup",
            "POST",
            {"phone": f"+91{app.LOCAL_PASSWORD_PHONE}", "secret": "test-bootstrap-secret"},
        )
        self.assertEqual(bootstrap["status"], "201 Created")
        self.cookie = self.response_cookie(bootstrap)
        first_shop = self.create_shop("First master shop")
        second_shop = self.create_shop("Second master shop")
        db = sqlite3.connect(app.DB_PATH)
        try:
            db.execute(
                "DELETE FROM shop_memberships WHERE shop_id = ?",
                (second_shop["id"],),
            )
            db.execute(
                "UPDATE users SET platform_role = NULL, can_own_shop = 0 WHERE phone = ?",
                (f"+91{app.LOCAL_PASSWORD_PHONE}",),
            )
            db.commit()
        finally:
            db.close()
        self.cookie = ""
        password_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, app.LOCAL_PASSWORD_ITERATIONS, dklen=32
        )
        app.LOCAL_AUTH_PATH.write_text(
            json.dumps({
                "phone": app.LOCAL_PASSWORD_PHONE,
                "salt": salt.hex(),
                "password_hash": password_hash.hex(),
            }),
            encoding="utf-8",
        )
        self.assertTrue(json.loads(self.json_request("/api/setup")["body"])["password_login_available"])

        wrong_user = self.json_request(
            "/api/auth/password",
            "POST",
            {"phone": "+919350025161", "password": password},
            cookie="",
        )
        self.assertEqual(wrong_user["status"], "401 Unauthorized")
        wrong_password = self.json_request(
            "/api/auth/password",
            "POST",
            {"phone": app.LOCAL_PASSWORD_PHONE, "password": "not-the-test-password"},
            cookie="",
        )
        self.assertEqual(wrong_password["status"], "401 Unauthorized")

        signed_in = self.json_request(
            "/api/auth/password",
            "POST",
            {"phone": app.LOCAL_PASSWORD_PHONE, "password": password},
            cookie="",
        )
        self.assertEqual(signed_in["status"], "200 OK")
        self.cookie = self.response_cookie(signed_in)
        self.assertTrue(self.cookie.startswith("nvsentry_session="))
        signed_in_session = self.json_request("/api/session")
        self.assertEqual(
            json.loads(signed_in_session["body"])["user"]["phone"],
            f"+91{app.LOCAL_PASSWORD_PHONE}",
        )
        session_data = json.loads(signed_in_session["body"])
        self.assertEqual(session_data["user"]["platform_role"], "platform_owner")
        self.assertEqual(
            {shop["id"] for shop in session_data["shops"]},
            {first_shop["id"], second_shop["id"]},
        )
        active_shop = self.json_request(
            "/api/active-shop",
            "POST",
            {"shop_id": second_shop["id"]},
        )
        self.assertEqual(active_shop["status"], "200 OK")
        ledger = self.json_request("/api/entry?date=2099-12-30")
        self.assertEqual(ledger["status"], "200 OK")
        staff = self.json_request("/api/staff")
        self.assertEqual(staff["status"], "200 OK")
        created_shop = self.json_request("/api/shops", "POST", {"name": "Master created"})
        self.assertEqual(created_shop["status"], "201 Created")
        self.assertNotIn(password, app.LOCAL_AUTH_PATH.read_text(encoding="utf-8"))

        for _ in range(5):
            rejected = self.json_request(
                "/api/auth/password",
                "POST",
                {"phone": app.LOCAL_PASSWORD_PHONE, "password": "incorrect-password"},
                cookie="",
            )
        self.assertEqual(rejected["status"], "429 Too Many Requests")
        locked_out = self.json_request(
            "/api/auth/password",
            "POST",
            {"phone": app.LOCAL_PASSWORD_PHONE, "password": password},
            cookie="",
        )
        self.assertEqual(locked_out["status"], "429 Too Many Requests")

    def test_rejects_unknown_route_and_method(self):
        self.setup_owner()
        self.create_shop()
        missing = self.request("/api/missing")
        method = self.request("/api/entry", method="DELETE")

        self.assertEqual(missing["status"], "404 Not Found")
        self.assertEqual(method["status"], "405 Method Not Allowed")
        self.assertEqual(method["headers"]["Allow"], "GET, POST")


if __name__ == "__main__":
    unittest.main()
