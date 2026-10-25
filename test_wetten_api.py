import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server
import wetten_core as w


class TestBettingHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_db = w.DB_PATH
        w.DB_PATH = Path(cls.tmp.name) / "test.sqlite3"
        w.init_db()
        cls.old_password = os.environ.get("WETTEN_PASSWORD")
        cls.old_secure = os.environ.get("WETTEN_COOKIE_SECURE")
        os.environ["WETTEN_PASSWORD"] = "test-long-secret-not-used-in-production"
        os.environ["WETTEN_COOKIE_SECURE"] = "0"  # localhost HTTP only
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.origin = "http://127.0.0.1:" + str(cls.httpd.server_port)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        w.DB_PATH = cls.old_db
        cls.tmp.cleanup()
        for k, value in (("WETTEN_PASSWORD", cls.old_password), ("WETTEN_COOKIE_SECURE", cls.old_secure)):
            if value is None: os.environ.pop(k, None)
            else: os.environ[k] = value

    def setUp(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, route, payload=None, opener=None):
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(
            self.origin + route,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers, method=method)
        try:
            with (opener or self.opener).open(request, timeout=4) as result:
                body = result.read().decode()
                return result.status, json.loads(body) if body.startswith("{") else body
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_login_csrf_persist_and_manual_settlement(self):
        code, data = self.request("GET", "/api/wetten/session")
        self.assertEqual(code, 200)
        self.assertFalse(data["authenticated"])
        code, data = self.request("GET", "/api/wetten/data")
        self.assertEqual(code, 401)
        code, data = self.request("POST", "/api/wetten/login", {"password":"test-long-secret-not-used-in-production"})
        self.assertEqual(code, 200)
        csrf = data["csrf"]
        code, data = self.request("POST", "/api/wetten/create", {
            "bookmaker":"bet365","placed_at":"2026-10-25T12:30:00+01:00","stake":"20",
            "legs":[{"sport":"NFL","home":"A","away":"B","market":"moneyline","selection":"home","odds":"1.72"}]
        })
        self.assertEqual(code, 403)
        code, data = self.request("POST", "/api/wetten/create", {
            "csrf":csrf,"bookmaker":"bet365","placed_at":"2026-10-25T12:30:00+01:00","stake":"20",
            "legs":[{"sport":"NFL","home":"A","away":"B","market":"moneyline","selection":"home","odds":"1.72"}]
        })
        self.assertEqual(code, 201, data)
        ident = data["id"]
        code, data = self.request("GET", "/api/wetten/data")
        self.assertEqual(code, 200)
        row = next(t for t in data["tickets"] if t["id"] == ident)
        self.assertEqual(row["potential_cents"], 3440)
        code, data = self.request("POST", "/api/wetten/settle", {
            "csrf":csrf,"id":ident,"status":"won","payout":"34.40","reason":"Buchmacherabrechnung"})
        self.assertEqual(code, 200, data)
        code, data = self.request("GET", "/api/wetten/data")
        row = next(t for t in data["tickets"] if t["id"] == ident)
        self.assertEqual(row["payout_cents"], 3440)
        self.assertEqual(row["status"], "won")

    def test_no_private_journal_visible_anonymously(self):
        anon = urllib.request.build_opener()
        code, data = self.request("GET", "/api/wetten/data", opener=anon)
        self.assertEqual(code, 401)
        code, data = self.request("GET", "/wetten")
        self.assertEqual(code, 200)
        self.assertIn("<title>Meine Wetten", data)


if __name__ == "__main__":
    unittest.main()
