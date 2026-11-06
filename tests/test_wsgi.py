import io
import json
import tempfile
import unittest
from pathlib import Path

import app


class WSGIApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        app.DB_PATH = Path(self.temp_dir.name) / "entries.sqlite3"

    def tearDown(self):
        app.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def request(self, path, method="GET", body=b""):
        parsed_path, _, query = path.partition("?")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parsed_path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        response = {}

        def start_response(status, headers):
            response["status"] = status
            response["headers"] = dict(headers)

        response["body"] = b"".join(app.application(environ, start_response))
        return response

    def test_serves_static_page(self):
        response = self.request("/")

        self.assertEqual(response["status"], "200 OK")
        self.assertIn(b'id="entry-form"', response["body"])
        self.assertIn("text/html", response["headers"]["Content-Type"])

    def test_saves_and_reads_entry(self):
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

    def test_rejects_unknown_route_and_method(self):
        missing = self.request("/api/missing")
        method = self.request("/api/entry", method="DELETE")

        self.assertEqual(missing["status"], "404 Not Found")
        self.assertEqual(method["status"], "405 Method Not Allowed")
        self.assertEqual(method["headers"]["Allow"], "GET, POST")


if __name__ == "__main__":
    unittest.main()
