from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from schedule_settings import WORK_HOURS
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")

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
    print("No events in the next 7 days.")
else:
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))
        title = event.get("summary", "(No title)")
        print(f"{start} to {end} — {title}")

        local_tz = ZoneInfo("America/New_York")
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


print("\nFREE TIME:")

all_free_blocks = []
for offset in range(7):
    day = today + timedelta(days=offset)
    hours = WORK_HOURS[day.strftime("%A")]

    if hours is None:
        print(f"{day}: No work hours")
        continue

    start_text, end_text = hours
    day_start = datetime.combine(day, time.fromisoformat(start_text), local_tz)
    day_end = datetime.combine(day, time.fromisoformat(end_text), local_tz)

    # Don't suggest time that has already passed today.
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


task_name = input("\nTask to schedule: ")
hours_needed = float(input("How many hours will it take? "))

if hours_needed <= 0:
    print("Hours must be greater than zero.")
else:
    remaining = timedelta(hours=hours_needed)
    print(f"\nSuggested times for {task_name}:")

    for free_start, free_end in all_free_blocks:
        if remaining <= timedelta(0):
            break

        session = min(remaining, free_end - free_start)
        if session <= timedelta(0):
            continue

        session_end = free_start + session
        print(
            f"  {free_start.strftime('%a %b %d, %I:%M %p')}–"
            f"{session_end.strftime('%I:%M %p')}"
        )
        remaining -= session

    if remaining > timedelta(0):
        print(f"Not enough free time: {remaining} still unscheduled.")