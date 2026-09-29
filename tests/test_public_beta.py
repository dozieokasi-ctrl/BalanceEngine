"""Run with: BALANCEENGINE_ENV=test python -m unittest discover -s tests."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from cryptography.fernet import Fernet

os.environ["BALANCEENGINE_ENV"] = "test"
from public_beta import create_app  # noqa: E402


class PublicBetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-only-key", "DATA_DIR": self.temp.name, "SIGNUP_CODE": "invite"})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, data, client=None, token=True):
        client = client or self.client
        if token:
            client.get("/register")
            with client.session_transaction() as state:
                data = {**data, "csrf_token": state["csrf_token"]}
        return client.post(path, data=data)

    def register(self, email, client=None):
        return self.post("/register", {"email": email, "password": "long-test-password", "signup_code": "invite"}, client)

    def test_demo_is_public_and_read_only(self):
        self.assertIn(b"Sample tasks only", self.client.get("/demo").data)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.assertEqual(self.post("/tasks", {"name": "Nope"}).status_code, 302)

    def test_csrf_required_and_password_hashed(self):
        self.client.get("/register")
        response = self.post("/register", {"email": "one@example.com", "password": "long-test-password", "signup_code": "invite"}, token=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.register("one@example.com").status_code, 302)
        with self.app.app_context():
            import sqlite3
            with sqlite3.connect(os.path.join(self.temp.name, "public_beta.sqlite3")) as db:
                stored = db.execute("SELECT password_hash FROM users").fetchone()[0]
            self.assertNotIn("long-test-password", stored)

    def test_tasks_are_private_even_with_direct_url(self):
        self.register("one@example.com")
        due = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        self.post("/tasks", {"name": "Private assignment", "hours": "2", "importance": "5", "deadline": due})
        import sqlite3
        with sqlite3.connect(os.path.join(self.temp.name, "public_beta.sqlite3")) as db:
            task_id = db.execute("SELECT id FROM tasks").fetchone()[0]
        self.assertIn(b"Private assignment", self.client.get("/dashboard").data)
        other = self.app.test_client()
        self.register("two@example.com", other)
        self.assertNotIn(b"Private assignment", other.get("/dashboard").data)
        self.assertEqual(other.get(f"/tasks/{task_id}/edit").status_code, 404)
        self.assertEqual(self.post(f"/tasks/{task_id}/toggle", {}, other).status_code, 404)
        self.assertEqual(self.post(f"/tasks/{task_id}/delete", {}, other).status_code, 404)
        self.assertIn(b"Private assignment", self.client.get("/dashboard").data)
        self.post(f"/tasks/{task_id}/toggle", {})
        with sqlite3.connect(os.path.join(self.temp.name, "public_beta.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT completed FROM tasks WHERE id=?", (task_id,)).fetchone()[0], 1)
        self.assertIn(b'class="task done"', self.client.get("/dashboard").data)
        self.post(f"/tasks/{task_id}/delete", {})
        self.assertNotIn(b"Private assignment", self.client.get("/dashboard").data)

    def test_invite_code_and_login(self):
        bad = self.post("/register", {"email": "one@example.com", "password": "long-test-password", "signup_code": "wrong"})
        self.assertIn(b"Invalid beta invitation code", bad.data)
        self.register("one@example.com")
        self.post("/logout", {})
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.post("/login", {"email": "one@example.com", "password": "long-test-password"})
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_login_attempts_are_limited(self):
        self.register("one@example.com")
        self.post("/logout", {})
        for _ in range(10):
            result = self.post("/login", {"email": "one@example.com", "password": "incorrect-password"})
            self.assertEqual(result.status_code, 200)
        locked = self.post("/login", {"email": "one@example.com", "password": "long-test-password"})
        self.assertEqual(locked.status_code, 429)

    def test_calendar_oauth_state_and_account_isolation(self):
        self.temp.cleanup()
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-only-key", "DATA_DIR": self.temp.name,
                               "SIGNUP_CODE": "invite", "GOOGLE_CLIENT_ID": "client-id",
                               "GOOGLE_CLIENT_SECRET": "client-secret", "PUBLIC_BASE_URL": "https://example.com",
                               "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()})
        self.client = self.app.test_client()
        self.register("one@example.com")
        self.assertIn(b"Connect Calendar", self.client.get("/settings").data)
        start = self.post("/calendar/connect", {})
        self.assertEqual(start.status_code, 302)
        self.assertIn("accounts.google.com", start.location)
        self.assertIn("calendar.events.readonly", start.location)
        with self.client.session_transaction() as state:
            oauth_state = state["calendar_oauth"]["state"]
        invalid = self.client.get("/calendar/callback?state=wrong&code=secret")
        self.assertEqual(invalid.status_code, 400)
        with patch("public_beta.exchange_code", return_value="private-refresh-token") as exchange:
            self.assertEqual(self.client.get("/calendar/callback?state=" + oauth_state + "&code=secret").status_code, 400)
            self.post("/calendar/connect", {})
            with self.client.session_transaction() as state:
                oauth_state = state["calendar_oauth"]["state"]
            result = self.client.get("/calendar/callback?state=" + oauth_state + "&code=secret")
            self.assertEqual(result.status_code, 302)
            exchange.assert_called_once()
        import sqlite3
        with sqlite3.connect(os.path.join(self.temp.name, "public_beta.sqlite3")) as connection:
            encrypted = connection.execute("SELECT encrypted_refresh_token FROM calendar_connections").fetchone()[0]
        self.assertNotIn("private-refresh-token", encrypted)
        second = self.app.test_client()
        self.register("two@example.com", second)
        self.assertIn(b"Connect Calendar", second.get("/settings").data)
        with patch("public_beta.refresh_access_token", return_value="access"), \
             patch("public_beta.upcoming_events", return_value=[{"title": "Private meeting", "start": datetime.now(timezone.utc),
                                                                  "end": datetime.now(timezone.utc) + timedelta(hours=1),
                                                                  "all_day": False, "location": "", "free": False}]):
            self.assertIn(b"Private meeting", self.client.get("/dashboard").data)
            self.assertNotIn(b"Private meeting", second.get("/dashboard").data)
        with patch("public_beta.revoke") as revoke:
            self.post("/calendar/disconnect", {})
            revoke.assert_called_once_with("private-refresh-token")
        self.assertIn(b"Connect Calendar", self.client.get("/settings").data)


if __name__ == "__main__":
    unittest.main()
