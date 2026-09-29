import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from schedule_settings import WORK_HOURS, REPEATING_TASKS


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

PROJECT_DIR = Path(__file__).resolve().parent
TOKEN_FILE = PROJECT_DIR / "token.json"
CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"
TASKS_FILE = PROJECT_DIR / "tasks.json"
COMPLETED_FILE = PROJECT_DIR / "completed_tasks.json"

local_tz = ZoneInfo("America/New_York")


def event_datetime(value):
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"]).astimezone(local_tz)

    # All-day events contain a date instead of a date and time.
    return datetime.combine(
        datetime.fromisoformat(value["date"]).date(),
        time.min,
        tzinfo=local_tz,
    )


# Connect to Google Calendar.
creds = None

if TOKEN_FILE.exists():
    creds = Credentials.from_authorized_user_file(
        str(TOKEN_FILE), SCOPES
    )

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        creds = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

service = build("calendar", "v3", credentials=creds)

now = datetime.now(timezone.utc)
week_later = now + timedelta(days=8)

# Read every page of calendar events.
events = []
page_token = None

while True:
    result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=week_later.isoformat(),
        maxResults=250,
        singleEvents=True,
        orderBy="startTime",
        pageToken=page_token,
    ).execute()

    events.extend(result.get("items", []))
    page_token = result.get("nextPageToken")

    if not page_token:
        break

if not events:
    print("No events found in the requested time range.")
else:
    for event in events:
        start = event["start"].get(
            "dateTime", event["start"].get("date")
        )
        end = event["end"].get(
            "dateTime", event["end"].get("date")
        )
        title = event.get("summary", "(No title)")
        print(f"{start} to {end} — {title}")


# Find free time within each day's work hours.
today = datetime.now(local_tz).date()
all_free_blocks = []

print("\nFREE TIME:")

for offset in range(7):
    day = today + timedelta(days=offset)
    hours = WORK_HOURS[day.strftime("%A")]

    if hours is None:
        print(f"\n{day.strftime('%A, %b %d')}: No work hours")
        continue

    start_text, end_text = hours

    day_start = datetime.combine(
        day, time.fromisoformat(start_text), local_tz
    )
    day_end = datetime.combine(
        day, time.fromisoformat(end_text), local_tz
    )

    # Round the current time up to the next 15-minute mark.
    current_time = datetime.now(local_tz)
    rounded_now = current_time.replace(
        minute=(current_time.minute // 15) * 15,
        second=0,
        microsecond=0,
    )

    if rounded_now < current_time:
        rounded_now += timedelta(minutes=15)

    day_start = max(day_start, rounded_now)
    free_blocks = [(day_start, day_end)] if day_start < day_end else []

    # Remove calendar events from the available time.
    for event in events:
        busy_start = event_datetime(event["start"])
        busy_end = event_datetime(event["end"])
        updated_blocks = []

        for free_start, free_end in free_blocks:
            if busy_end <= free_start or busy_start >= free_end:
                updated_blocks.append((free_start, free_end))
            else:
                if free_start < busy_start:
                    updated_blocks.append((free_start, busy_start))

                if busy_end < free_end:
                    updated_blocks.append((busy_end, free_end))

        free_blocks = updated_blocks

    all_free_blocks.extend(free_blocks)

    print(f"\n{day.strftime('%A, %b %d')}:")

    if not free_blocks:
        print("  No free time")

    for free_start, free_end in free_blocks:
        print(
            f"  {free_start.strftime('%I:%M %p')}–"
            f"{free_end.strftime('%I:%M %p')}"
        )


# Load saved tasks.
tasks = []

if TASKS_FILE.exists():
    saved_tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))

    for saved_task in saved_tasks:
        # Give older tasks an ID if they were saved before IDs existed.
        saved_task.setdefault("id", str(uuid4()))

        deadline = datetime.fromisoformat(saved_task["deadline"])

        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=local_tz)

        saved_task["deadline"] = deadline
        tasks.append(saved_task)

print(f"\nLoaded {len(tasks)} saved task(s).")

for task in tasks:
    print(
        f"  {task['name']} — {task['hours']} hours, "
        f"due {task['deadline'].strftime('%b %d at %I:%M %p')}"
    )


# Add new tasks.
number_of_tasks = int(
    input(
        "\nHow many NEW tasks do you want to add? "
        "Enter 0 to reuse saved tasks: "
    )
)

for task_number in range(number_of_tasks):
    task_name = input(f"\nTask {task_number + 1} name: ")
    hours_needed = float(input("How many hours will it take? "))
    importance = int(input("Importance from 1 to 5: "))

    deadline_text = input(
        "Deadline (YYYY-MM-DD HH:MM, 24-hour time): "
    )

    try:
        deadline = datetime.strptime(
            deadline_text, "%Y-%m-%d %H:%M"
        ).replace(tzinfo=local_tz)
    except ValueError:
        print("Invalid date format. Skipping this task.")
        continue

    if deadline <= datetime.now(local_tz):
        print("Deadline must be in the future. Skipping this task.")
        continue

    if hours_needed <= 0 or not 1 <= importance <= 5:
        print("Invalid hours or importance. Skipping this task.")
        continue

    tasks.append({
        "id": str(uuid4()),
        "name": task_name,
        "hours": hours_needed,
        "importance": importance,
        "deadline": deadline,
    })


# Save one-time tasks. Repeating tasks are generated separately each run.
tasks_to_save = []

for task in tasks:
    saved_task = task.copy()
    saved_task["deadline"] = task["deadline"].isoformat()
    tasks_to_save.append(saved_task)

TASKS_FILE.write_text(
    json.dumps(tasks_to_save, indent=2),
    encoding="utf-8",
)

print(f"\nSaved {len(tasks)} one-time task(s).")


# Generate repeating tasks for the next seven days.
for offset in range(7):
    occurrence_day = today + timedelta(days=offset)

    for template in REPEATING_TASKS:
        if occurrence_day.strftime("%A") != template["weekday"]:
            continue

        earliest_start = datetime.combine(
            occurrence_day,
            time.min,
            tzinfo=local_tz,
        )
        deadline = datetime.combine(
            occurrence_day + timedelta(days=1),
            time.min,
            tzinfo=local_tz,
        )

        tasks.append({
            "id": (
                f"repeat:{template['name']}:"
                f"{occurrence_day.isoformat()}"
            ),
            "name": (
                f"{template['name']} "
                f"({occurrence_day:%b %d})"
            ),
            "hours": template["hours"],
            "importance": template["importance"],
            "earliest_start": earliest_start,
            "deadline": deadline,
        })


# Load completed task IDs and remove those tasks from this run.
completed_ids = set()

if COMPLETED_FILE.exists():
    completed_ids = set(
        json.loads(COMPLETED_FILE.read_text(encoding="utf-8"))
    )

tasks = [
    task for task in tasks
    if task["id"] not in completed_ids
]


# Optionally mark one task complete.
if tasks:
    print("\nTasks you can mark complete:")

    for number, task in enumerate(tasks, start=1):
        print(f"  {number}. {task['name']}")

    choice = input(
        "Enter a task number to mark complete, "
        "or press Enter to skip: "
    ).strip()

    if choice:
        if choice.isdigit() and 1 <= int(choice) <= len(tasks):
            completed_task = tasks.pop(int(choice) - 1)
            completed_ids.add(completed_task["id"])

            COMPLETED_FILE.write_text(
                json.dumps(sorted(completed_ids), indent=2),
                encoding="utf-8",
            )

            print(f"Completed: {completed_task['name']}")
        else:
            print("Invalid task number. No task was marked complete.")
else:
    print("\nNo incomplete tasks to schedule.")


# Schedule earlier deadlines first. Importance breaks deadline ties.
tasks.sort(
    key=lambda task: (
        task["deadline"],
        -task["importance"],
    )
)

available_blocks = all_free_blocks.copy()

for task in tasks:
    task_name = task["name"]
    deadline = task["deadline"]
    earliest_start = task.get("earliest_start", now)
    remaining = timedelta(hours=task["hours"])

    next_available_blocks = []
    print(f"\nSuggested times for {task_name}:")

    for free_start, free_end in available_blocks:
        # Preserve blocks this task does not need or cannot use.
        if remaining <= timedelta(0) or free_start >= deadline:
            next_available_blocks.append((free_start, free_end))
            continue

        usable_start = max(free_start, earliest_start)
        usable_end = min(free_end, deadline)

        # Preserve blocks outside this task's allowed dates.
        if usable_start >= usable_end:
            next_available_blocks.append((free_start, free_end))
            continue

        # Preserve any time before this task is allowed to start.
        if free_start < usable_start:
            next_available_blocks.append((free_start, usable_start))

        session = min(remaining, usable_end - usable_start)
        session_end = usable_start + session

        print(
            f"  {usable_start.strftime('%a %b %d, %I:%M %p')}–"
            f"{session_end.strftime('%I:%M %p')}"
        )

        remaining -= session

        # Preserve unused time for the next task.
        if session_end < free_end:
            next_available_blocks.append((session_end, free_end))

    available_blocks = next_available_blocks

    if remaining > timedelta(0):
        hours_short = remaining.total_seconds() / 3600
        print(
            f"Could not fit {hours_short:.2f} hours before the deadline "
            "within the next 7 days."
        )