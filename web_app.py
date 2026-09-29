"""Local BalanceEngine task dashboard. Run with: python web_app.py"""

import json
import math
import random
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template, request, url_for

from assignments_service import fetch_rows, parse_rows, preparation_tasks, spreadsheet_id
from calendar_service import week_schedule
from free_time_ideas import IDEAS, recommend
from priority_model import ranked_priorities
from schedule_settings import REPEATING_TASKS, WORK_HOURS
from task_scheduler import schedule_tasks


app = Flask(__name__)
ROOT = Path(__file__).resolve().parent
TASKS_FILE = ROOT / "tasks.json"
COMPLETED_FILE = ROOT / "completed_tasks.json"
COMPLETION_DATES_FILE = ROOT / "completion_dates.json"
WEEKLY_FILE = ROOT / "weekly_tasks.json"
CUSTOM_IDEAS_FILE = ROOT / "break_ideas.json"
SHEETS_CONFIG_FILE = ROOT / "sheets_config.json"
LOCAL_TZ = ZoneInfo("America/New_York")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def completion_dates(now, completed):
    """Keep completion history while the visible completed list resets Mondays."""
    dates = read_json(COMPLETION_DATES_FILE, {})
    missing = set(completed) - dates.keys()
    if missing:
        for task_id in missing:
            dates[task_id] = now.isoformat()
        write_json(COMPLETION_DATES_FILE, dates)
    return dates


def all_break_ideas():
    return IDEAS + read_json(CUSTOM_IDEAS_FILE, [])


def sheet_assignments(now):
    config = read_json(SHEETS_CONFIG_FILE, {})
    if not config.get("sheet_id"):
        return []
    rows = fetch_rows(config["sheet_id"], config.get("tab", ""))
    return parse_rows(rows, config["sheet_id"], config.get("tab", ""), now)


def saved_tasks():
    tasks = read_json(TASKS_FILE, [])
    changed = False
    for task in tasks:
        if not task.get("id"):
            task["id"] = str(uuid4())
            changed = True
    if changed:
        write_json(TASKS_FILE, tasks)
    return tasks


def weekly_templates():
    """Combine configured routines with changes made in the dashboard."""
    templates = {
        f"repeat:{item['name']}": {
            **item,
            "id": f"repeat:{item['name']}",
            "due_time": None,  # Existing routines are due at the next midnight.
        }
        for item in REPEATING_TASKS
    }
    for item in read_json(WEEKLY_FILE, []):
        templates[item["id"]] = item
    return [item for item in templates.values() if not item.get("disabled")]


def save_weekly_template(template):
    stored = read_json(WEEKLY_FILE, [])
    stored = [item for item in stored if item["id"] != template["id"]]
    stored.append(template)
    write_json(WEEKLY_FILE, stored)


def remove_weekly_template(template_id):
    if template_id.startswith("repeat:"):
        save_weekly_template({"id": template_id, "disabled": True})
    else:
        stored = read_json(WEEKLY_FILE, [])
        write_json(WEEKLY_FILE, [item for item in stored if item["id"] != template_id])


def repeating_tasks(today):
    tasks = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        for template in weekly_templates():
            if day.strftime("%A") != template["weekday"]:
                continue
            due_time = template.get("due_time")
            deadline = (
                datetime.combine(day, time.fromisoformat(due_time), LOCAL_TZ)
                if due_time else datetime.combine(day + timedelta(days=1), time.min, LOCAL_TZ)
            )
            tasks.append({
                "id": f"{template['id']}:{day.isoformat()}",
                "template_id": template["id"],
                "name": f"{template['name']} ({day:%b %d})",
                "hours": template["hours"],
                "importance": template["importance"],
                "difficulty": template.get("difficulty", 3),
                "earliest_start": datetime.combine(
                    day, time.min, LOCAL_TZ
                ).isoformat(),
                "deadline": deadline.isoformat(),
                "repeating": True,
            })
    return tasks


def task_rows(now, extra_tasks=()):
    completed = set(read_json(COMPLETED_FILE, []))
    dates = completion_dates(now, completed)
    week_start = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min, LOCAL_TZ)
    all_tasks = saved_tasks() + repeating_tasks(now.date()) + list(extra_tasks)
    rows = []
    for task in all_tasks:
        if task["id"] in completed and datetime.fromisoformat(dates[task["id"]]) < week_start:
            continue
        due = datetime.fromisoformat(task["deadline"])
        if due.tzinfo is None:
            due = due.replace(tzinfo=LOCAL_TZ)
        importance = int(task["importance"])
        rows.append({
            "id": task["id"],
            "name": task["name"],
            "course": task.get("course", ""),
            "hours": task["hours"],
            "importance": importance,
            "difficulty": int(task.get("difficulty", 3)),
            "priority": "High" if importance >= 4 else "Medium" if importance >= 3 else "Low",
            "due": due,
            "earliest_start": (
                datetime.fromisoformat(task["earliest_start"])
                if task.get("earliest_start") else now
            ),
            "due_label": due.strftime("%a, %b %d at %I:%M %p"),
            "completed": task["id"] in completed,
            "repeating": task.get("repeating", False),
            "sheet_task": task.get("sheet_task", False),
            "edit_id": task.get("template_id", task["id"]),
        })
    rows.sort(key=lambda task: (task["completed"], task["due"], -task["importance"]))
    return rows


@app.get("/")
def home():
    now = datetime.now(LOCAL_TZ)
    assignments = []
    sheet_error = None
    sheet_config = read_json(SHEETS_CONFIG_FILE, {})
    if sheet_config.get("sheet_id"):
        try:
            assignments = sheet_assignments(now)
        except (FileNotFoundError, ImportError):
            sheet_error = "Google Sheets setup is needed in this Python environment."
        except ValueError as exc:
            sheet_error = str(exc)
        except Exception as exc:
            app.logger.exception("Could not load assignments from Google Sheets")
            # The dashboard runs locally, so show Google's reason instead of
            # hiding actionable errors such as API disabled or no access.
            reason = getattr(exc, "reason", None) or str(exc)
            sheet_error = f"Google Sheets could not load ({type(exc).__name__}): {reason[:500]}"
    tasks = task_rows(now, preparation_tasks(assignments, now))
    priorities = ranked_priorities(tasks, now)
    active = [task for task in tasks if not task["completed"]]
    due_this_week = [
        task for task in active
        if now <= task["due"] < now + timedelta(days=7)
    ]
    hours = WORK_HOURS.get(now.strftime("%A"))
    work_window = f"{hours[0]}–{hours[1]}" if hours else "No work hours"
    calendar = None
    calendar_error = None
    suggestions = []
    free_time_ideas = []
    next_break = None
    try:
        calendar = week_schedule(WORK_HOURS, now)
        plan = schedule_tasks(tasks, calendar, now)
        suggestions = plan["suggestions"]
        free_time_ideas = recommend(plan["remaining_blocks"], all_break_ideas())
        random.shuffle(free_time_ideas)
        if plan["remaining_blocks"]:
            next_break = min(plan["remaining_blocks"])
    except (FileNotFoundError, ImportError):
        calendar_error = "Google Calendar setup is needed on this Python environment."
    except Exception:
        app.logger.exception("Could not load Google Calendar")
        calendar_error = "Google Calendar could not load. Your tasks are still available."
    return render_template(
        "dashboard.html",
        tasks=tasks,
        priorities=priorities,
        active_count=len(active),
        completed_count=len(tasks) - len(active),
        due_this_week=len(due_this_week),
        work_window=work_window,
        calendar=calendar,
        calendar_error=calendar_error,
        suggestions=suggestions,
        free_time_ideas=free_time_ideas,
        next_break=next_break,
        assignments=assignments,
        sheet_config=sheet_config,
        sheet_error=sheet_error,
        today_label=now.strftime("%A, %B %d"),
        error=request.args.get("error"),
    )


@app.post("/sheets/configure")
def configure_sheet():
    try:
        url = request.form["url"].strip()
        tab = request.form.get("tab", "").strip()
        sheet_id = spreadsheet_id(url)
        if len(tab) > 100:
            raise ValueError("Keep the tab name under 100 characters.")
    except (KeyError, ValueError) as exc:
        return redirect(url_for("home", error=str(exc)))
    write_json(SHEETS_CONFIG_FILE, {"sheet_id": sheet_id, "tab": tab})
    return redirect(url_for("home"))


@app.post("/break-ideas")
def add_break_idea():
    try:
        name = request.form["name"].strip()
        minutes = int(request.form["minutes"])
        category = request.form["category"]
        note = request.form.get("note", "").strip()
        if not name or len(name) > 100 or not 5 <= minutes <= 240:
            raise ValueError("Enter a name and a duration from 5 to 240 minutes.")
        if category not in ("Study", "Career", "Wellness", "Music", "Social", "Other"):
            raise ValueError("Choose a category.")
        if len(note) > 200:
            raise ValueError("Keep the note under 200 characters.")
    except (KeyError, ValueError) as exc:
        return redirect(url_for("home", error=str(exc)))

    ideas = read_json(CUSTOM_IDEAS_FILE, [])
    ideas.append({"id": str(uuid4()), "name": name, "minutes": minutes,
                  "category": category, "note": note})
    write_json(CUSTOM_IDEAS_FILE, ideas)
    return redirect(url_for("home"))


@app.post("/tasks")
def add_task():
    try:
        task = task_from_form()
    except (KeyError, ValueError) as exc:
        return redirect(url_for("home", error=str(exc)))

    if task.get("repeat") == "weekly":
        task["id"] = f"weekly:{uuid4()}"
        save_weekly_template(task)
    else:
        task["id"] = str(uuid4())
        tasks = saved_tasks()
        tasks.append(task)
        write_json(TASKS_FILE, tasks)
    return redirect(url_for("home"))


def task_from_form():
    name = request.form["name"].strip()
    hours = float(request.form["hours"])
    importance = int(request.form["importance"])
    difficulty = int(request.form.get("difficulty", "3"))
    repeat = request.form.get("repeat", "once")
    if not name or not math.isfinite(hours) or hours <= 0:
        raise ValueError("Enter a task name and a positive number of hours.")
    if importance not in range(1, 6):
        raise ValueError("Choose importance from 1 to 5.")
    if difficulty not in range(1, 6):
        raise ValueError("Choose difficulty from 1 to 5.")
    if repeat == "weekly":
        weekday = request.form["weekday"]
        if weekday not in WEEKDAYS:
            raise ValueError("Choose a weekday.")
        due_time = time.fromisoformat(request.form["due_time"]).strftime("%H:%M")
        return {"name": name, "hours": hours, "importance": importance, "difficulty": difficulty,
                "repeat": "weekly", "weekday": weekday, "due_time": due_time}
    if repeat != "once":
        raise ValueError("Choose one-time or weekly.")
    due = datetime.fromisoformat(request.form["deadline"]).replace(tzinfo=LOCAL_TZ)
    if due <= datetime.now(LOCAL_TZ):
        raise ValueError("Choose a future deadline.")
    return {"name": name, "hours": hours, "importance": importance, "difficulty": difficulty,
            "deadline": due.isoformat()}


@app.route("/tasks/<task_id>/edit", methods=["GET", "POST"])
def edit_task(task_id):
    one_time = next((task for task in saved_tasks() if task["id"] == task_id), None)
    weekly = next((task for task in weekly_templates() if task["id"] == task_id), None)
    original = one_time or weekly
    if original is None:
        return redirect(url_for("home", error="Task not found."))

    if request.method == "GET":
        return render_template(
            "edit_task.html", task=original, is_weekly=weekly is not None,
            weekdays=WEEKDAYS, error=request.args.get("error"),
        )

    try:
        updated = task_from_form()
    except (KeyError, ValueError) as exc:
        return redirect(url_for("edit_task", task_id=task_id, error=str(exc)))

    if one_time:
        tasks = [task for task in saved_tasks() if task["id"] != task_id]
        if updated.get("repeat") == "weekly":
            updated["id"] = f"weekly:{task_id}"
            save_weekly_template(updated)
        else:
            updated["id"] = task_id
            tasks.append(updated)
        write_json(TASKS_FILE, tasks)
    else:
        if updated.get("repeat") == "weekly":
            updated["id"] = task_id
            save_weekly_template(updated)
        else:
            remove_weekly_template(task_id)
            updated["id"] = str(uuid4())
            tasks = saved_tasks()
            tasks.append(updated)
            write_json(TASKS_FILE, tasks)
    return redirect(url_for("home"))


@app.post("/tasks/<task_id>/delete")
def delete_task(task_id):
    tasks = saved_tasks()
    if any(task["id"] == task_id for task in tasks):
        write_json(TASKS_FILE, [task for task in tasks if task["id"] != task_id])
        removed_ids = {task_id}
    elif any(task["id"] == task_id for task in weekly_templates()):
        remove_weekly_template(task_id)
        history = set(read_json(COMPLETED_FILE, [])) | set(read_json(COMPLETION_DATES_FILE, {}))
        removed_ids = {item for item in history if item.startswith(f"{task_id}:")}
    else:
        return redirect(url_for("home", error="Task not found."))

    if removed_ids:
        completed = set(read_json(COMPLETED_FILE, []))
        write_json(COMPLETED_FILE, sorted(completed - removed_ids))
        dates = read_json(COMPLETION_DATES_FILE, {})
        write_json(COMPLETION_DATES_FILE, {
            key: value for key, value in dates.items() if key not in removed_ids
        })
    return redirect(url_for("home"))


@app.post("/tasks/completion")
def update_task_completion():
    now = datetime.now(LOCAL_TZ)
    valid_ids = {task["id"] for task in saved_tasks() + repeating_tasks(now.date())}
    submitted_ids = set(request.form.getlist("task_id"))
    checked_ids = set(request.form.getlist("completed"))
    if not checked_ids <= submitted_ids:
        return redirect(url_for("home", error="Invalid task selection."))
    if any(task_id.startswith("sheet:") for task_id in submitted_ids):
        try:
            valid_ids.update(task["id"] for task in preparation_tasks(sheet_assignments(now), now))
        except Exception:
            return redirect(url_for("home", error="Could not verify tasks from your sheet."))
    if not submitted_ids <= valid_ids:
        return redirect(url_for("home", error="The task list changed. Refresh and try again."))
    completed = set(read_json(COMPLETED_FILE, []))
    dates = completion_dates(now, completed)
    newly_completed = checked_ids - completed
    reopened = submitted_ids - checked_ids
    completed.difference_update(submitted_ids)
    completed.update(checked_ids)
    for task_id in newly_completed:
        dates[task_id] = now.isoformat()
    for task_id in reopened:
        dates.pop(task_id, None)
    write_json(COMPLETED_FILE, sorted(completed))
    write_json(COMPLETION_DATES_FILE, dates)
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(port=5050, debug=True)
