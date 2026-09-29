"""Standalone, Google-free hosted beta. Run with `gunicorn public_beta:app`."""

import hmac
import json
import random
import math
import os
import secrets
import sqlite3
from datetime import datetime, time, timedelta, timezone
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
from hosted_dashboard import week_view
from hosted_sheets import fetch_rows, SCOPE as SHEETS_SCOPE
from assignments_service import spreadsheet_id, parse_rows, preparation_tasks
from task_scheduler import schedule_tasks
from free_time_ideas import IDEAS, recommend


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

        # Additive migrations preserve existing accounts, tasks, and OAuth tokens.
        connection.execute("BEGIN IMMEDIATE")
        for table, columns in {
            "users": {"work_hours": "TEXT NOT NULL DEFAULT '{}'", "timezone_set": "INTEGER NOT NULL DEFAULT 0"},
            "tasks": {"difficulty": "INTEGER NOT NULL DEFAULT 3", "repeat": "TEXT NOT NULL DEFAULT 'once'",
                      "weekday": "TEXT NOT NULL DEFAULT 'Monday'", "due_time": "TEXT NOT NULL DEFAULT '21:00'",
                      "completed_at": "TEXT", "course": "TEXT NOT NULL DEFAULT ''", "source": "TEXT NOT NULL DEFAULT 'manual'"},
        }.items():
            present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute("UPDATE tasks SET completed_at=? WHERE completed=1 AND completed_at IS NULL", (datetime.now(timezone.utc).isoformat(),))
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS suppressed_tasks (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id));
            CREATE TABLE IF NOT EXISTS break_ideas (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL, minutes INTEGER NOT NULL, category TEXT NOT NULL, note TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sheets_connections (user_id TEXT PRIMARY KEY REFERENCES users(id),
                encrypted_refresh_token TEXT NOT NULL, connected_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sheet_settings (user_id TEXT PRIMARY KEY REFERENCES users(id),
                sheet_id TEXT NOT NULL, tab TEXT NOT NULL DEFAULT '');
        """)

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
            difficulty = int(request.form.get("difficulty", "3"))
            frequency = request.form.get("repeat", "once")
            weekday = request.form.get("weekday", "Monday")
            due_time = request.form.get("due_time", "21:00")
            if frequency not in ("once", "weekly"):
                raise ValueError()
            if frequency == "weekly":
                weekdays = ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
                now = datetime.now(local_zone())
                day = now.date() + timedelta(days=(weekdays.index(weekday)-now.weekday()) % 7)
                due_local = datetime.combine(day, time.fromisoformat(due_time))
                if due_local.replace(tzinfo=local_zone()) <= now:
                    due_local += timedelta(days=7)
            else:
                due_local = datetime.fromisoformat(request.form.get("deadline", ""))
            if due_local.tzinfo is not None:
                raise ValueError()
        except ValueError:
            raise ValueError("Enter hours, importance, difficulty, and a valid deadline or weekly time.") from None
        if not math.isfinite(hours) or not 0 < hours <= 100 or importance not in range(1, 6) or difficulty not in range(1, 6):
            raise ValueError("Hours must be between 0 and 100; importance and difficulty must be 1–5.")
        due = due_local.replace(tzinfo=local_zone()).astimezone(timezone.utc)
        if due <= datetime.now(timezone.utc):
            raise ValueError("Choose a future deadline.")
        return name, hours, importance, due.isoformat(), difficulty, frequency, weekday, due_time

    def rows_for(user_id):
        now = datetime.now(local_zone())
        week_start = datetime.combine(now.date()-timedelta(days=now.weekday()), time.min, now.tzinfo)
        rows = []
        with db():
            for row in db().execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY due_utc", (user_id,)).fetchall():
                item = dict(row)
                due = datetime.fromisoformat(item["due_utc"]).astimezone(local_zone())
                if item['repeat'] == 'weekly' and due <= now:
                    # Rebuild in local time so weekly tasks retain their due time across DST.
                    while due <= now:
                        due = datetime.combine(due.date()+timedelta(days=7), time.fromisoformat(item['due_time']), now.tzinfo)
                    db().execute("UPDATE tasks SET due_utc=?,completed=0,completed_at=NULL WHERE id=? AND user_id=?",
                                 (due.astimezone(timezone.utc).isoformat(), item['id'], user_id))
                    item['completed'], item['completed_at'] = 0, None
                if item['completed'] and item['completed_at'] and datetime.fromisoformat(item['completed_at']) < week_start:
                    continue
                item.update(due=due, due_label=due.strftime("%a, %b %d at %I:%M %p"), earliest_start=datetime.combine(due.date(), time.min, now.tzinfo) if item['repeat']=='weekly' else datetime.fromisoformat(item['created_at']).astimezone(local_zone()),
                            repeating=item['repeat']=='weekly', edit_id=item['id'], sheet_task=item['source']=='sheet',
                            priority='High' if item['importance'] >= 4 else 'Medium' if item['importance'] == 3 else 'Low')
                rows.append(item)
        rows.sort(key=lambda item: (item['completed'], item['due'], -item['importance']))
        return rows

    def work_hours():
        return json.loads(g.user['work_hours']) or {day: [g.user['work_start'], g.user['work_end']]
            for day in ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')}

    def dashboard_context(rows, events, now, hours, ideas, **extra):
        calendar = week_view(events, hours, now)
        reservations = schedule_tasks(rows, calendar, now)
        remaining = reservations['remaining_blocks']
        activities = recommend(remaining, ideas)
        random.SystemRandom().shuffle(activities)
        active = [row for row in rows if not row['completed']]
        window = hours.get(now.strftime('%A'))
        return dict(tasks=rows, priorities=ranked_priorities(rows, now), calendar=calendar,
                    suggestions=reservations['suggestions'], next_break=min(remaining) if remaining else None,
                    free_time_ideas=activities, active_count=len(active), completed_count=len(rows)-len(active),
                    due_this_week=sum(now <= row['due'] < now+timedelta(days=7) for row in active),
                    work_window='–'.join(window) if window else 'No work hours', today_label=now.strftime('%A, %B %d'),
                    timezone_name=str(now.tzinfo), calendar_error=None, assignments=[], sheet_config={},
                    sheets_error=None, **extra)

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

    @app.get("/privacy")
    def privacy():
        return render_template("public/privacy.html")

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
        zone = ZoneInfo('America/New_York')
        local_now = now.astimezone(zone)
        day = datetime.combine(local_now.date(), time(18), zone)
        events = [{"title": "Sample class", "start": day, "end": day+timedelta(hours=1), "all_day": False,
                   "location": "Campus", "free": False, "color": "#a4bdfc"}]
        for row in rows:
            row.update(difficulty=3, priority='High' if row['importance'] >= 4 else 'Low', repeating=False,
                       course='', sheet_task=False, edit_id=row['id'])
        hours = {name: ['09:00','21:00'] for name in ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')}
        return render_template("public/dashboard.html", **dashboard_context(rows, events, local_now, hours, IDEAS,
                               demo=True, calendar_enabled=False, calendar_connected=False, sheets_connected=False))

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
                zone_name = request.form.get('timezone', 'UTC')
                try:
                    ZoneInfo(zone_name)
                except (ValueError, ZoneInfoNotFoundError):
                    zone_name = 'UTC'
                try:
                    with db():
                        db().execute("INSERT INTO users (id,email,password_hash,created_at,timezone) VALUES (?,?,?,?,?)",
                                     (user_id, email, generate_password_hash(password), datetime.now(timezone.utc).isoformat(), zone_name))
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
        now = datetime.now(local_zone())
        connection = calendar_connection() if calendar_enabled else None
        events, calendar_error = [], None
        if connection:
            try:
                refresh_token = decrypt_token(calendar_options["TOKEN_ENCRYPTION_KEY"].encode(), connection["encrypted_refresh_token"])
                access_token = refresh_access_token(calendar_options["GOOGLE_CLIENT_ID"], calendar_options["GOOGLE_CLIENT_SECRET"], refresh_token)
                events = upcoming_events(access_token, local_zone())
            except (CalendarError, InvalidToken):
                calendar_error = "Could not load your calendar. Reconnect it in Settings. Availability is hidden until it loads."
        config = db().execute('SELECT * FROM sheet_settings WHERE user_id=?', (g.user['id'],)).fetchone()
        sheets_connection = db().execute('SELECT * FROM sheets_connections WHERE user_id=?', (g.user['id'],)).fetchone()
        assignments, sheets_error = [], None
        if config and sheets_connection and calendar_enabled:
            try:
                token = decrypt_token(calendar_options['TOKEN_ENCRYPTION_KEY'].encode(), sheets_connection['encrypted_refresh_token'])
                access = refresh_access_token(calendar_options['GOOGLE_CLIENT_ID'], calendar_options['GOOGLE_CLIENT_SECRET'], token)
                assignments = parse_rows(fetch_rows(access, config['sheet_id'], config['tab']), config['sheet_id'], config['tab'], now, local_zone())
                with db():
                    suppressed = {row[0] for row in db().execute('SELECT id FROM suppressed_tasks WHERE user_id=?',(g.user['id'],))}
                    for task in preparation_tasks(assignments, now):
                        if g.user['id']+':'+task['id'] in suppressed:
                            continue
                        db().execute("INSERT INTO tasks (id,user_id,name,due_utc,hours,importance,created_at,difficulty,course,source) VALUES (?,?,?,?,?,?,?,?,?,?) "
                                     "ON CONFLICT(id) DO NOTHING",
                                     (g.user['id']+':'+task['id'], g.user['id'],task['name'], task['deadline'], task['hours'],task['importance'],now.isoformat(),task['difficulty'],task['course'],'sheet'))
            except (CalendarError, InvalidToken, ValueError) as exc:
                sheets_error = str(exc) if not isinstance(exc, InvalidToken) else 'Reconnect Google Sheets in Settings.'
        rows = rows_for(g.user['id'])
        # Imported task release dates come from the current sheet, rather than personal defaults.
        assignment_map = {g.user['id']+':'+item['id']: item for item in assignments}
        if config:
            rows = [row for row in rows if not row['sheet_task'] or row['id'] in assignment_map or sheets_error]
        for row in rows:
            if row['id'] in assignment_map:
                row['earliest_start'] = assignment_map[row['id']]['prep_start']
        custom_ideas = [dict(row) for row in db().execute('SELECT * FROM break_ideas WHERE user_id=?', (g.user['id'],))]
        context = dashboard_context(rows, events, now, work_hours(), custom_ideas, demo=False,
                                    calendar_enabled=calendar_enabled, calendar_connected=bool(connection), sheets_connected=bool(sheets_connection))
        context.update(assignments=assignments, sheet_config=dict(config) if config else {}, sheets_error=sheets_error)
        if calendar_error:
            context.update(calendar=None, calendar_error=calendar_error, suggestions=[], free_time_ideas=[], next_break=None)
        return render_template('public/dashboard.html', **context)

    @app.post('/timezone/detect')
    @login_required
    def detect_timezone():
        name = request.form.get('timezone', '')
        try:
            ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            abort(400)
        with db():
            db().execute("UPDATE users SET timezone=?,timezone_set=1 WHERE id=? AND timezone_set=0 AND timezone='UTC'", (name, g.user['id']))
        return ('', 204)

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
                day_hours = {}
                for day in ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'):
                    if request.form.get('daily_hours'):
                        if request.form.get(day+'_off'):
                            day_hours[day] = None
                        else:
                            left, right = request.form.get(day+'_start',start), request.form.get(day+'_end',end)
                            time.fromisoformat(left); time.fromisoformat(right)
                            if left >= right:
                                raise ValueError()
                            day_hours[day] = [left,right]
                if start >= end:
                    raise ValueError()
            except (ValueError, ZoneInfoNotFoundError):
                flash("Use a valid IANA timezone and a work window with start before end.", "error")
            else:
                with db():
                    db().execute("UPDATE users SET timezone=?,work_start=?,work_end=?,work_hours=?,timezone_set=1 WHERE id=?",
                                 (zone_name, start, end, json.dumps(day_hours), g.user["id"]))
                return redirect(url_for("dashboard"))
        return render_template("public/settings.html", calendar_enabled=calendar_enabled,
                               calendar_connected=bool(calendar_connection()) if calendar_enabled else False,
                               sheets_connected=bool(db().execute('SELECT 1 FROM sheets_connections WHERE user_id=?', (g.user['id'],)).fetchone()), work_hours=work_hours())

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
        if connection and not db().execute('SELECT 1 FROM sheets_connections WHERE user_id=?',(g.user['id'],)).fetchone():
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
            name, hours, importance, due, difficulty, frequency, weekday, due_time = task_form()
        except ValueError as error:
            flash(str(error), "error")
        else:
            with db():
                db().execute("INSERT INTO tasks (id,user_id,name,due_utc,hours,importance,created_at,difficulty,repeat,weekday,due_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (str(uuid4()), g.user["id"], name, due, hours, importance,
                              datetime.now(timezone.utc).isoformat(),difficulty,frequency,weekday,due_time))
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
                name, hours, importance, due, difficulty, frequency, weekday, due_time = task_form()
            except ValueError as error:
                flash(str(error), "error")
            else:
                with db():
                    db().execute("UPDATE tasks SET name=?,hours=?,importance=?,due_utc=?,difficulty=?,repeat=?,weekday=?,due_time=? WHERE id=? AND user_id=?",
                                 (name, hours, importance, due, difficulty, frequency, weekday, due_time, task_id, g.user["id"]))
                return redirect(url_for("dashboard"))
        deadline = datetime.fromisoformat(task["due_utc"]).astimezone(local_zone()).strftime("%Y-%m-%dT%H:%M")
        return render_template("public/edit.html", task=task, deadline=deadline)

    @app.post("/tasks/<task_id>/toggle")
    @login_required
    def toggle_task(task_id):
        task = owned_task(task_id)
        with db():
            db().execute("UPDATE tasks SET completed=?,completed_at=? WHERE id=? AND user_id=?",
                         (0 if task["completed"] else 1, None if task["completed"] else datetime.now(timezone.utc).isoformat(), task_id, g.user["id"]))
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<task_id>/delete")
    @login_required
    def delete_task(task_id):
        task = owned_task(task_id)
        with db():
            if task['source'] == 'sheet':
                db().execute('INSERT OR IGNORE INTO suppressed_tasks VALUES (?,?)',(task_id,g.user['id']))
            db().execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, g.user["id"]))
        return redirect(url_for("dashboard"))

    @app.post('/tasks/update')
    @login_required
    def update_task_completion():
        ids = set(request.form.getlist('task_id'))
        checked = set(request.form.getlist('completed'))
        if not checked <= ids:
            abort(400)
        # Validate the whole batch before writing anything.
        owned = {task_id: owned_task(task_id) for task_id in ids}
        with db():
            for task_id, task in owned.items():
                completed = int(task_id in checked)
                if completed != task['completed']:
                    db().execute('UPDATE tasks SET completed=?,completed_at=? WHERE id=? AND user_id=?',
                                 (completed, datetime.now(timezone.utc).isoformat() if completed else None, task_id,g.user['id']))
        flash('Tasks updated.', 'success')
        return redirect(url_for('dashboard'))

    @app.post('/break-ideas')
    @login_required
    def add_break_idea():
        name, note = request.form.get('name','').strip(), request.form.get('note','').strip()
        category = request.form.get('category','Other')
        try:
            minutes = int(request.form.get('minutes',''))
            if not 1 <= minutes <= 240 or not 0 < len(name) <= 100 or len(note) > 200 or category not in ('Wellness','Study','Career','Music','Social','Other'):
                raise ValueError()
        except ValueError:
            flash('Enter an activity, 1–240 minutes, a category, and an optional note up to 200 characters.', 'error')
        else:
            with db():
                db().execute('INSERT INTO break_ideas VALUES (?,?,?,?,?,?)',(str(uuid4()),g.user['id'],name,minutes,category,note))
        return redirect(url_for('dashboard'))

    @app.post('/sheets/configure')
    @login_required
    def configure_sheet():
        try:
            sheet_id = spreadsheet_id(request.form.get('url',''))
            tab = request.form.get('tab','').strip()
            if len(tab)>100:
                raise ValueError('Worksheet name is too long.')
        except ValueError as exc:
            flash(str(exc),'error')
        else:
            with db():
                db().execute('INSERT INTO sheet_settings VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET sheet_id=excluded.sheet_id,tab=excluded.tab', (g.user['id'],sheet_id,tab))
        return redirect(url_for('dashboard'))

    @app.post('/sheets/connect')
    @login_required
    def sheets_connect():
        if not calendar_enabled:
            abort(404)
        state = secrets.token_urlsafe(32)
        session['sheets_oauth'] = dict(state=state,user_id=g.user['id'],created=datetime.now(timezone.utc).timestamp())
        callback = calendar_options['PUBLIC_BASE_URL'].rstrip('/')+url_for('sheets_callback')
        return redirect(authorization_url(calendar_options['GOOGLE_CLIENT_ID'],callback,state,scope=SHEETS_SCOPE))

    @app.get('/sheets/callback')
    @login_required
    def sheets_callback():
        if not calendar_enabled:
            abort(404)
        pending = session.pop('sheets_oauth',None)
        if (not pending or pending.get('user_id') != g.user['id'] or
            datetime.now(timezone.utc).timestamp()-pending.get('created',0)>600 or
            not hmac.compare_digest(request.args.get('state',''),pending['state'])):
            abort(400,'Sheets authorization expired. Connect again.')
        if request.args.get('error'):
            flash('Sheets permission was not granted.','error')
        elif not request.args.get('code'):
            abort(400)
        else:
            try:
                callback = calendar_options['PUBLIC_BASE_URL'].rstrip('/')+url_for('sheets_callback')
                token = exchange_code(calendar_options['GOOGLE_CLIENT_ID'],calendar_options['GOOGLE_CLIENT_SECRET'],callback,request.args['code'],scope=SHEETS_SCOPE)
                encrypted = encrypt_token(calendar_options['TOKEN_ENCRYPTION_KEY'].encode(),token)
                with db():
                    db().execute('INSERT INTO sheets_connections VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET encrypted_refresh_token=excluded.encrypted_refresh_token,connected_at=excluded.connected_at',(g.user['id'],encrypted,datetime.now(timezone.utc).isoformat()))
                flash('Sheets connected. Choose your assignment spreadsheet on the dashboard.','success')
                return redirect(url_for('dashboard'))
            except CalendarError as exc:
                flash(str(exc),'error')
        return redirect(url_for('settings'))

    @app.post('/sheets/disconnect')
    @login_required
    def sheets_disconnect():
        # Google revocation may also revoke Calendar for the same client. Remove this
        # integration locally; the account owner can revoke all access in Google settings.
        with db():
            db().execute('DELETE FROM sheets_connections WHERE user_id=?',(g.user['id'],))
            db().execute('DELETE FROM sheet_settings WHERE user_id=?',(g.user['id'],))
            db().execute("DELETE FROM tasks WHERE user_id=? AND source='sheet'",(g.user['id'],))
        flash('Sheets disconnected and imported preparation tasks removed.','success')
        return redirect(url_for('settings'))

    return app


app = create_app() if os.getenv("BALANCEENGINE_ENV") != "test" else None

if __name__ == "__main__":
    if os.getenv("BALANCEENGINE_ENV") != "development":
        raise RuntimeError("For local development set BALANCEENGINE_ENV=development; production uses Gunicorn.")
    app.run(port=5051, debug=False)
