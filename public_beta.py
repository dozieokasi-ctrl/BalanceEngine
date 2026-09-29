"""Standalone, Google-free hosted beta. Run with `gunicorn public_beta:app`."""

import hmac
import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

from hosted_calendar import (CalendarError, authorization_url, decrypt_token, encrypt_token,
                             exchange_code, refresh_access_token, revoke, upcoming_events)
from priority_model import ranked_priorities


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC', work_start TEXT NOT NULL DEFAULT '09:00',
  work_end TEXT NOT NULL DEFAULT '17:00', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, due_utc TEXT NOT NULL, hours REAL NOT NULL,
  importance INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_user ON tasks(user_id, due_utc);
CREATE TABLE IF NOT EXISTS login_attempts (
  email TEXT PRIMARY KEY, count INTEGER NOT NULL, window_start TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_connections (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  encrypted_refresh_token TEXT NOT NULL, connected_at TEXT NOT NULL
);
"""


def create_app(test_config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(test_config or {})
    calendar_options = {key: app.config.get(key) or os.getenv(key, "") for key in
                        ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_ENCRYPTION_KEY", "PUBLIC_BASE_URL")}
    calendar_enabled = all(calendar_options.values())
    if any(calendar_options.values()) and not calendar_enabled:
        raise RuntimeError("Calendar setup requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, TOKEN_ENCRYPTION_KEY, and PUBLIC_BASE_URL.")
    if calendar_enabled:
        Fernet(calendar_options["TOKEN_ENCRYPTION_KEY"].encode())
        if not calendar_options["PUBLIC_BASE_URL"].startswith(("https://", "http://localhost:", "http://127.0.0.1:")):
            raise RuntimeError("PUBLIC_BASE_URL must be HTTPS outside local development.")
    production = not (app.config.get("TESTING") or os.getenv("BALANCEENGINE_ENV") == "development")
    secret = app.config.get("SECRET_KEY") or os.getenv("SECRET_KEY")
    data_dir = Path(app.config.get("DATA_DIR") or os.getenv("DATA_DIR", "instance")).resolve()
    if production and (not secret or not os.getenv("SIGNUP_CODE") or not os.getenv("DATA_DIR")):
        raise RuntimeError("Production needs SECRET_KEY, SIGNUP_CODE, and persistent DATA_DIR.")
    if not secret:
        secret = secrets.token_hex(32)
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production, MAX_CONTENT_LENGTH=16 * 1024,
        SESSION_COOKIE_NAME="balanceengine_beta_session",
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "public_beta.sqlite3"

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(db_path, timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA busy_timeout = 10000")
        return g.db

    with sqlite3.connect(db_path) as connection:
        connection.executescript(SCHEMA)

    @app.teardown_appcontext
    def close_db(error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.path != "/static/public.css":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def identify_user_and_check_csrf():
        g.user = None
        user_id = session.get("user_id")
        if user_id:
            g.user = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if g.user is None:
                session.clear()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                abort(400, "Invalid form token. Refresh the page and try again.")

    @app.context_processor
    def template_context():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"], "current_user": g.get("user")}

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    def local_zone():
        return ZoneInfo(g.user["timezone"])

    def task_form():
        name = request.form.get("name", "").strip()
        if not name or len(name) > 150:
            raise ValueError("Task name must be 1–150 characters.")
        try:
            hours = float(request.form.get("hours", ""))
            importance = int(request.form.get("importance", ""))
            due_local = datetime.fromisoformat(request.form.get("deadline", ""))
            if due_local.tzinfo is not None:
                raise ValueError()
        except ValueError:
            raise ValueError("Enter hours, importance, and a valid local deadline.") from None
        if not math.isfinite(hours) or not 0 < hours <= 100 or importance not in range(1, 6):
            raise ValueError("Hours must be between 0 and 100; importance must be 1–5.")
        due = due_local.replace(tzinfo=local_zone()).astimezone(timezone.utc)
        if due <= datetime.now(timezone.utc):
            raise ValueError("Choose a future deadline.")
        return name, hours, importance, due.isoformat()

    def rows_for(user_id):
        now = datetime.now(timezone.utc)
        rows = []
        for row in db().execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY due_utc", (user_id,)):
            item = dict(row)
            item["due"] = datetime.fromisoformat(item["due_utc"])
            item["due_label"] = item["due"].astimezone(local_zone()).strftime("%a %b %d, %I:%M %p")
            item["earliest_start"] = now - timedelta(days=1)
            rows.append(item)
        return rows

    def calendar_connection():
        return db().execute("SELECT encrypted_refresh_token FROM calendar_connections WHERE user_id=?",
                            (g.user["id"],)).fetchone()

    def calendar_callback_url():
        return calendar_options["PUBLIC_BASE_URL"].rstrip("/") + url_for("calendar_callback")

    @app.get("/")
    def index():
        return render_template("public/index.html")

    @app.get("/healthz")
    def healthz():
        db().execute("SELECT 1").fetchone()
        return "ok"

    @app.get("/demo")
    def demo():
        now = datetime.now(timezone.utc)
        examples = [
            ("Study for a finance exam", 12, 5, 4),
            ("Apply to two internships", 3, 4, 1),
            ("Stretch for taekwondo", 1, 2, 0.5),
        ]
        rows = [{"id": str(i), "name": name, "hours": hours, "importance": importance,
                 "due": now + timedelta(days=days), "due_label": (now + timedelta(days=days)).strftime("%a %b %d"),
                 "completed": False, "earliest_start": now - timedelta(days=1)}
                for i, (name, days, importance, hours) in enumerate(examples)]
        return render_template("public/dashboard.html", demo=True, tasks=rows,
                               priorities=ranked_priorities(rows, now), timezone_name="Sample schedule")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            required_code = app.config.get("SIGNUP_CODE") or os.getenv("SIGNUP_CODE", "")
            if required_code and not hmac.compare_digest(request.form.get("signup_code", ""), required_code):
                flash("Invalid beta invitation code.", "error")
            elif len(email) > 254 or "@" not in email or not email.partition("@")[2] or len(password) < 12 or len(password) > 256:
                flash("Enter a valid email and a password of 12–256 characters.", "error")
            else:
                user_id = str(uuid4())
                try:
                    with db():
                        db().execute("INSERT INTO users (id,email,password_hash,created_at) VALUES (?,?,?,?)",
                                     (user_id, email, generate_password_hash(password), datetime.now(timezone.utc).isoformat()))
                except sqlite3.IntegrityError:
                    flash("That email is already registered.", "error")
                else:
                    session.clear()
                    session["user_id"] = user_id
                    return redirect(url_for("settings"))
        return render_template("public/auth.html", mode="register", invite_required=bool(app.config.get("SIGNUP_CODE") or os.getenv("SIGNUP_CODE")))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            now = datetime.now(timezone.utc)
            attempt = db().execute("SELECT * FROM login_attempts WHERE email=?", (email,)).fetchone()
            recent = attempt and datetime.fromisoformat(attempt["window_start"]) > now - timedelta(minutes=15)
            if recent and attempt["count"] >= 10:
                flash("Too many attempts. Try again in 15 minutes.", "error")
                return render_template("public/auth.html", mode="login"), 429
            row = db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row is None or not check_password_hash(row["password_hash"], request.form.get("password", "")):
                with db():
                    db().execute("INSERT INTO login_attempts(email,count,window_start) VALUES (?,?,?) "
                                 "ON CONFLICT(email) DO UPDATE SET count=?,window_start=?",
                                 (email, 1, now.isoformat(), (attempt["count"] + 1 if recent else 1),
                                  attempt["window_start"] if recent else now.isoformat()))
                flash("Email or password is incorrect.", "error")
            else:
                with db():
                    db().execute("DELETE FROM login_attempts WHERE email=?", (email,))
                session.clear()
                session["user_id"] = row["id"]
                return redirect(url_for("dashboard"))
        return render_template("public/auth.html", mode="login")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        rows = rows_for(g.user["id"])
        connection = calendar_connection() if calendar_enabled else None
        calendar_events, calendar_error = [], None
        if connection:
            try:
                refresh_token = decrypt_token(calendar_options["TOKEN_ENCRYPTION_KEY"].encode(),
                                              connection["encrypted_refresh_token"])
                access_token = refresh_access_token(calendar_options["GOOGLE_CLIENT_ID"],
                                                    calendar_options["GOOGLE_CLIENT_SECRET"], refresh_token)
                calendar_events = upcoming_events(access_token, local_zone())
                for event in calendar_events:
                    event["start_label"] = event["start"].astimezone(local_zone()).strftime("%a %b %d, %I:%M %p")
                    if not event["all_day"]:
                        event["end_label"] = event["end"].astimezone(local_zone()).strftime("%I:%M %p")
            except (CalendarError, InvalidToken):
                calendar_error = "Could not load your calendar. Try again or reconnect it in Settings."
        return render_template("public/dashboard.html", demo=False, tasks=rows,
                               priorities=ranked_priorities(rows, datetime.now(timezone.utc)),
                               timezone_name=g.user["timezone"], calendar_enabled=calendar_enabled,
                               calendar_connected=bool(connection), calendar_events=calendar_events,
                               calendar_error=calendar_error)

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        if request.method == "POST":
            zone_name = request.form.get("timezone", "").strip()
            start, end = request.form.get("work_start", ""), request.form.get("work_end", "")
            try:
                ZoneInfo(zone_name)
                datetime.strptime(start, "%H:%M")
                datetime.strptime(end, "%H:%M")
                if start >= end:
                    raise ValueError()
            except (ValueError, ZoneInfoNotFoundError):
                flash("Use a valid IANA timezone and a work window with start before end.", "error")
            else:
                with db():
                    db().execute("UPDATE users SET timezone=?,work_start=?,work_end=? WHERE id=?",
                                 (zone_name, start, end, g.user["id"]))
                return redirect(url_for("dashboard"))
        return render_template("public/settings.html", calendar_enabled=calendar_enabled,
                               calendar_connected=bool(calendar_connection()) if calendar_enabled else False)

    @app.post("/calendar/connect")
    @login_required
    def calendar_connect():
        if not calendar_enabled:
            abort(404)
        state = secrets.token_urlsafe(32)
        session["calendar_oauth"] = {"state": state, "user_id": g.user["id"],
                                     "created": datetime.now(timezone.utc).timestamp()}
        return redirect(authorization_url(calendar_options["GOOGLE_CLIENT_ID"], calendar_callback_url(), state))

    @app.get("/calendar/callback")
    @login_required
    def calendar_callback():
        if not calendar_enabled:
            abort(404)
        pending = session.pop("calendar_oauth", None)
        if (not pending or pending.get("user_id") != g.user["id"]
                or datetime.now(timezone.utc).timestamp() - pending.get("created", 0) > 600
                or not hmac.compare_digest(request.args.get("state", ""), pending["state"])):
            abort(400, "Calendar authorization expired or was invalid. Try connecting again.")
        if request.args.get("error"):
            flash("Calendar permission was not granted.", "error")
            return redirect(url_for("settings"))
        code = request.args.get("code", "")
        if not code:
            abort(400, "Google did not return an authorization code.")
        try:
            refresh_token = exchange_code(calendar_options["GOOGLE_CLIENT_ID"],
                                          calendar_options["GOOGLE_CLIENT_SECRET"], calendar_callback_url(), code)
        except CalendarError as exc:
            flash(str(exc), "error")
        else:
            encrypted = encrypt_token(calendar_options["TOKEN_ENCRYPTION_KEY"].encode(), refresh_token)
            with db():
                db().execute("INSERT INTO calendar_connections (user_id,encrypted_refresh_token,connected_at) VALUES (?,?,?) "
                             "ON CONFLICT(user_id) DO UPDATE SET encrypted_refresh_token=excluded.encrypted_refresh_token, "
                             "connected_at=excluded.connected_at",
                             (g.user["id"], encrypted, datetime.now(timezone.utc).isoformat()))
            flash("Calendar connected.", "success")
            return redirect(url_for("dashboard"))
        return redirect(url_for("settings"))

    @app.post("/calendar/disconnect")
    @login_required
    def calendar_disconnect():
        if not calendar_enabled:
            abort(404)
        connection = calendar_connection()
        with db():
            db().execute("DELETE FROM calendar_connections WHERE user_id=?", (g.user["id"],))
        if connection:
            try:
                revoke(decrypt_token(calendar_options["TOKEN_ENCRYPTION_KEY"].encode(),
                                     connection["encrypted_refresh_token"]))
            except InvalidToken:
                pass
        flash("Calendar disconnected from this account.", "success")
        return redirect(url_for("settings"))

    @app.post("/tasks")
    @login_required
    def add_task():
        try:
            name, hours, importance, due = task_form()
        except ValueError as error:
            flash(str(error), "error")
        else:
            with db():
                db().execute("INSERT INTO tasks (id,user_id,name,due_utc,hours,importance,created_at) VALUES (?,?,?,?,?,?,?)",
                             (str(uuid4()), g.user["id"], name, due, hours, importance,
                              datetime.now(timezone.utc).isoformat()))
        return redirect(url_for("dashboard"))

    def owned_task(task_id):
        row = db().execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, g.user["id"])).fetchone()
        if row is None:
            abort(404)
        return row

    @app.route("/tasks/<task_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_task(task_id):
        task = owned_task(task_id)
        if request.method == "POST":
            try:
                name, hours, importance, due = task_form()
            except ValueError as error:
                flash(str(error), "error")
            else:
                with db():
                    db().execute("UPDATE tasks SET name=?,hours=?,importance=?,due_utc=? WHERE id=? AND user_id=?",
                                 (name, hours, importance, due, task_id, g.user["id"]))
                return redirect(url_for("dashboard"))
        deadline = datetime.fromisoformat(task["due_utc"]).astimezone(local_zone()).strftime("%Y-%m-%dT%H:%M")
        return render_template("public/edit.html", task=task, deadline=deadline)

    @app.post("/tasks/<task_id>/toggle")
    @login_required
    def toggle_task(task_id):
        task = owned_task(task_id)
        with db():
            db().execute("UPDATE tasks SET completed=? WHERE id=? AND user_id=?",
                         (0 if task["completed"] else 1, task_id, g.user["id"]))
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<task_id>/delete")
    @login_required
    def delete_task(task_id):
        owned_task(task_id)
        with db():
            db().execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, g.user["id"]))
        return redirect(url_for("dashboard"))

    return app


app = create_app() if os.getenv("BALANCEENGINE_ENV") != "test" else None

if __name__ == "__main__":
    if os.getenv("BALANCEENGINE_ENV") != "development":
        raise RuntimeError("For local development set BALANCEENGINE_ENV=development; production uses Gunicorn.")
    app.run(port=5051, debug=False)
