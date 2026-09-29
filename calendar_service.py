"""Read Google Calendar and calculate open time in weekly work hours."""

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import re


SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
LOCAL_TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "token.json"
CREDENTIALS_FILE = ROOT / "credentials.json"
DEFAULT_COLOR = "#5b5bd6"


def safe_color(value):
    """Only expose a CSS hex color from Google's palette."""
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else DEFAULT_COLOR


def event_time(value):
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
    # Google's all-day end date is exclusive.
    return datetime.combine(datetime.fromisoformat(value["date"]).date(), time.min, LOCAL_TZ)


def fetch_events(today):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif CREDENTIALS_FILE.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            raise FileNotFoundError("Calendar credentials are missing.")
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    service = build("calendar", "v3", credentials=creds)
    first = datetime.combine(today, time.min, LOCAL_TZ)
    last = datetime.combine(today + timedelta(days=7), time.min, LOCAL_TZ)
    events = []
    page_token = None
    while True:
        page = service.events().list(
            calendarId="primary",
            timeMin=first.isoformat(),
            timeMax=last.isoformat(),
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
        ).execute()
        events.extend(page.get("items", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    # Google's Birthdays and Holidays calendars may be separate from primary.
    # Read them for the event card, but never subtract them from work hours.
    try:
        list_token = None
        while True:
            listing = service.calendarList().list(pageToken=list_token).execute()
            for calendar in listing.get("items", []):
                calendar_id = calendar.get("id")
                calendar_name = calendar.get("summary", "").lower()
                if (not calendar_id or calendar.get("primary") or calendar_id == "primary" or
                        not any(word in calendar_name for word in ("birthday", "holiday"))):
                    continue
                event_token = None
                while True:
                    page = service.events().list(
                        calendarId=calendar_id, timeMin=first.isoformat(),
                        timeMax=last.isoformat(), maxResults=250,
                        singleEvents=True, orderBy="startTime", pageToken=event_token,
                    ).execute()
                    for event in page.get("items", []):
                        event["_special_only"] = True
                        event["_calendar_color"] = calendar.get("backgroundColor")
                        events.append(event)
                    event_token = page.get("nextPageToken")
                    if not event_token:
                        break
            list_token = listing.get("nextPageToken")
            if not list_token:
                break
    except Exception:
        # The primary calendar still works if optional calendars cannot load.
        pass
    # Event color IDs reference Google's event palette. Events without a
    # color ID inherit the primary calendar's own background color.
    try:
        event_colors = service.colors().get().execute().get("event", {})
        primary_color = safe_color(
            service.calendarList().get(calendarId="primary").execute().get("backgroundColor")
        )
    except Exception:
        event_colors = {}
        primary_color = DEFAULT_COLOR
    for event in events:
        color_id = event.get("colorId")
        event["_display_color"] = safe_color(
            event_colors.get(color_id, {}).get("background", event.get("_calendar_color") or primary_color)
        )
    return events


def open_blocks(day, hours, events, now):
    if hours is None:
        return []
    start_text, end_text = hours
    start = datetime.combine(day, time.fromisoformat(start_text), LOCAL_TZ)
    end = datetime.combine(day, time.fromisoformat(end_text), LOCAL_TZ)
    if day == now.date():
        rounded = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        if rounded < now:
            rounded += timedelta(minutes=15)
        start = max(start, rounded)
    blocks = [(start, end)] if start < end else []

    for event in events:
        if event.get("status") == "cancelled" or event.get("_special_only"):
            continue
        if "start" not in event or "end" not in event:
            continue
        # Timed calendar entries are commitments even if Google marks them
        # "free". Keep transparent all-day reminders, such as birthdays,
        # from blocking an entire workday.
        if event.get("transparency") == "transparent" and "dateTime" not in event["start"]:
            continue
        busy_start = event_time(event["start"])
        busy_end = event_time(event["end"])
        updated = []
        for free_start, free_end in blocks:
            if busy_end <= free_start or busy_start >= free_end:
                updated.append((free_start, free_end))
            else:
                if free_start < busy_start:
                    updated.append((free_start, busy_start))
                if busy_end < free_end:
                    updated.append((busy_end, free_end))
        blocks = updated
    return blocks


def week_schedule(work_hours, now=None, events=None):
    """Return display-ready events and free blocks for seven local days.

    Passing events allows calculation without contacting Google.
    """
    now = now or datetime.now(LOCAL_TZ)
    events = fetch_events(now.date()) if events is None else events
    special_events = []
    for event in events:
        if event.get("status") == "cancelled" or "start" not in event or "end" not in event:
            continue
        title = event.get("summary", "Busy")
        if not (event.get("_special_only") or "date" in event["start"] or
                re.search(r"\b(birthday|holiday|anniversary)\b", title, re.I)):
            continue
        start, end = event_time(event["start"]), event_time(event["end"])
        if end > now:
            special_events.append({
                "title": title, "start": start,
                "all_day": "date" in event["start"],
                "calendar": event.get("_special_only", False),
            })
    special_events.sort(key=lambda item: (max(item["start"], now), item["title"]))
    days = []
    for offset in range(7):
        day = now.date() + timedelta(days=offset)
        hours = work_hours.get(day.strftime("%A"))
        blocks = open_blocks(day, hours, events, now)
        total_hours = sum((end - start).total_seconds() for start, end in blocks) / 3600
        days.append({"date": day, "label": day.strftime("%a"), "hours": total_hours, "blocks": blocks})

    for offset, entry in enumerate(days):
        day = entry["date"]
        day_start = datetime.combine(day, time.min, LOCAL_TZ)
        day_end = datetime.combine(day + timedelta(days=1), time.min, LOCAL_TZ)
        day_events = []
        for event in events:
            if (event.get("status") == "cancelled" or event.get("_special_only") or
                    "start" not in event or "end" not in event):
                continue
            start = event_time(event["start"])
            end = event_time(event["end"])
            if start < day_end and end > day_start and end > now:
                day_events.append({
                    "start": max(start, day_start), "end": min(end, day_end),
                    "title": event.get("summary", "Busy"),
                    "detail": event.get("location", ""), "kind": "calendar",
                    "color": safe_color(event.get("_display_color", DEFAULT_COLOR)),
                })
        timeline = day_events + [
            {"start": start, "end": end, "title": "Free for work", "detail": "Within your work hours", "kind": "free"}
            for start, end in entry["blocks"]
        ]
        timeline.sort(key=lambda item: (item["start"], item["kind"] != "calendar"))
        entry["timeline"] = timeline
        entry["schedule_label"] = (
            "Today's schedule" if offset == 0 else
            "Tomorrow's schedule" if offset == 1 else
            f"{day.strftime('%A')}'s schedule"
        )
    return {
        "days": days,
        "today_events": [item for item in days[0]["timeline"] if item["kind"] == "calendar"],
        "today_free_hours": days[0]["hours"],
        "week_free_hours": sum(day["hours"] for day in days),
        "timeline": days[0]["timeline"],
        "next_special_event": special_events[0] if special_events else None,
    }
