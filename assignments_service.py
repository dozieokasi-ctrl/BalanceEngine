"""Read assignments from a Google Sheet and create preparation tasks."""

import hashlib
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
TOKEN_FILE = ROOT / "sheets_token.json"
CREDENTIALS_FILE = ROOT / "credentials.json"
LEAD_DAYS = {"Exam": 14, "Project": 7, "Homework": 2}
PREP_HOURS = {"Exam": 4.0, "Project": 3.0, "Homework": 1.0}
PRIORITY = {"Exam": 5, "Project": 4, "Homework": 3}


def spreadsheet_id(url):
    match = re.fullmatch(
        r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?", url.strip()
    )
    if not match:
        raise ValueError("Paste a Google Sheets link ending in /spreadsheets/d/...")
    return match.group(1)


def fetch_rows(sheet_id, tab=""):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid or not creds.has_scopes(SCOPES):
        if creds and creds.expired and creds.refresh_token and creds.has_scopes(SCOPES):
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    # An unqualified range reads the first worksheet. Quote named tabs safely.
    range_name = f"'{tab.replace(chr(39), chr(39) * 2)}'!A:Z" if tab else "A:Z"
    result = build("sheets", "v4", credentials=creds).spreadsheets().values().get(
        spreadsheetId=sheet_id, range=range_name
    ).execute()
    return result.get("values", [])


def due_datetime(raw, zone=LOCAL_TZ):
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if len(text) == 10:
            parsed = datetime.combine(parsed.date(), time(23, 59))
        return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m/%d/%y"):
        try:
            parsed = datetime.strptime(text, pattern)
            if pattern in ("%m/%d/%Y", "%m/%d/%y"):
                parsed = datetime.combine(parsed.date(), time(23, 59))
            return parsed.replace(tzinfo=zone)
        except ValueError:
            continue
    raise ValueError(f"Unsupported due date: {text}")


def assignment_type(label, title):
    for text in ((label or "").lower(), title.lower()):
        if re.search(r"\b(exam|test|midterm|final|quiz)\b", text):
            return "Exam"
        if re.search(r"\b(project|presentation|paper|report)\b", text):
            return "Project"
        if re.search(r"\b(homework|hw|assignment|problem set|reading)\b", text):
            return "Homework"
    return "Homework"


def is_econ_course(course):
    """Identify ECON course codes and Economics names, without matching words like economy."""
    return bool(re.search(r"\b(?:economics|econ(?:[\s-]*\d{3,4})?)\b", course, re.I))


def parse_rows(rows, sheet_id, tab="", now=None, zone=LOCAL_TZ):
    """Map a header row and return dated, incomplete assignments."""
    if not rows:
        return []
    now = now or datetime.now(zone)
    headers = [re.sub(r"[^a-z0-9]", "", str(cell).lower()) for cell in rows[0]]
    aliases = {
        "title": {"assignment", "assignmentname", "title", "name", "task"},
        "due": {"due", "duedate", "deadline", "date", "duetime"},
        "course": {"course", "class", "subject"},
        "type": {"type", "category", "assignmenttype"},
        "status": {"status", "completed", "done"},
    }
    positions = {
        key: next((i for i, header in enumerate(headers) if header in names), None)
        for key, names in aliases.items()
    }
    if positions["title"] is None or positions["due"] is None:
        raise ValueError("The first row needs Assignment (or Title) and Due Date columns.")

    assignments = []
    for row_number, row in enumerate(rows[1:], 2):
        def value(key):
            index = positions[key]
            return str(row[index]).strip() if index is not None and index < len(row) else ""

        title, due_text = value("title"), value("due")
        if not title or not due_text:
            continue
        if value("status").lower() in ("done", "complete", "completed", "yes", "true", "submitted"):
            continue
        try:
            due = due_datetime(due_text, zone)
        except ValueError as exc:
            raise ValueError(f"Row {row_number}: {exc}") from exc
        if due <= now:
            continue
        kind = assignment_type(value("type"), title)
        course = value("course")
        prep_required = not (kind == "Exam" and is_econ_course(course or title))
        # A row can move when the sheet is sorted; keep the task's completion
        # attached to the assignment's identifying fields instead.
        digest = hashlib.sha256(
            f"{sheet_id}:{tab}:{course}:{title}:{due.isoformat()}".encode()
        ).hexdigest()[:16]
        assignments.append({
            "id": f"sheet:{digest}", "title": title, "course": course,
            "type": kind, "due": due, "prep_required": prep_required,
            "prep_start": datetime.combine(
                due.date() - timedelta(days=LEAD_DAYS[kind]), time.min, zone
            ),
            "prep_hours": PREP_HOURS[kind], "importance": PRIORITY[kind],
        })
    return sorted(assignments, key=lambda item: item["due"])


def preparation_tasks(assignments, now):
    tasks = []
    for item in assignments:
        if not item["prep_required"] or item["prep_start"] > now:
            continue
        action = {"Exam": "Study for", "Project": "Start", "Homework": "Work on"}[item["type"]]
        tasks.append({
            "id": item["id"], "name": f"{action} {item['title']}",
            "course": item["course"],
            "hours": item["prep_hours"], "importance": item["importance"],
            "difficulty": {"Exam": 4, "Project": 4, "Homework": 2}[item["type"]],
            "deadline": item["due"].isoformat(),
            "earliest_start": max(now, item["prep_start"]).isoformat(),
            "sheet_task": True,
        })
    return tasks
