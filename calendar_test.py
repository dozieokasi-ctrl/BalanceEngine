from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from schedule_settings import WORK_HOURS

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")

local_tz = ZoneInfo("America/New_York")

# Connect to Google Calendar.
creds = None

if TOKEN_FILE.exists():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        creds = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(creds.to_json())

service = build("calendar", "v3", credentials=creds)

now = datetime.now(timezone.utc)
week_later = now + timedelta(days=8)

result = service.events().list(
    calendarId="primary",
    timeMin=now.isoformat(),
    timeMax=week_later.isoformat(),
    maxResults=250,
    singleEvents=True,
    orderBy="startTime",
).execute()

events = result.get("items", [])

if not events:
    print("No events found in the requested time range.")
else:
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))
        title = event.get("summary", "(No title)")
        print(f"{start} to {end} — {title}")

today = datetime.now(local_tz).date()


def event_datetime(value):
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"]).astimezone(local_tz)

    # An all-day event uses a date instead of a time.
    return datetime.combine(
        datetime.fromisoformat(value["date"]).date(),
        time.min,
        tzinfo=local_tz,
    )


# Find free time within each day's work hours.
print("\nFREE TIME:")

all_free_blocks = []

for offset in range(7):
    day = today + timedelta(days=offset)
    hours = WORK_HOURS[day.strftime("%A")]

    if hours is None:
        print(f"{day}: No work hours")
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


# Collect tasks before scheduling them.
number_of_tasks = int(input("\nHow many tasks do you want to schedule? "))
tasks = []

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
        "name": task_name,
        "hours": hours_needed,
        "importance": importance,
        "deadline": deadline,
    })


# Earliest deadline first; higher importance breaks ties.
tasks.sort(
    key=lambda task: (task["deadline"], -task["importance"])
)

available_blocks = all_free_blocks.copy()


# Schedule each task using the remaining free time.
for task in tasks:
    task_name = task["name"]
    deadline = task["deadline"]
    remaining = timedelta(hours=task["hours"])

    next_available_blocks = []
    print(f"\nSuggested times for {task_name}:")

    for free_start, free_end in available_blocks:
        # Preserve blocks this task does not need or cannot use.
        if remaining <= timedelta(0) or free_start >= deadline:
            next_available_blocks.append((free_start, free_end))
            continue

        # Only use time before the task's deadline.
        usable_end = min(free_end, deadline)
        session = min(remaining, usable_end - free_start)
        session_end = free_start + session

        print(
            f"  {free_start.strftime('%a %b %d, %I:%M %p')}–"
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