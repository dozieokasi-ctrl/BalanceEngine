"""Google Calendar web OAuth and read-only event retrieval for the hosted beta."""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet


SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class CalendarError(Exception):
    """A Google connection needs attention, without exposing token details to the UI."""


def authorization_url(client_id, redirect_uri, state, scope=SCOPE):
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": scope, "access_type": "offline", "prompt": "consent", "state": state,
    })


def exchange_code(client_id, client_secret, redirect_uri, code, scope=SCOPE):
    try:
        response = requests.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri, "code": code, "grant_type": "authorization_code",
        }, timeout=15)
        response.raise_for_status()
        token = response.json()
        if not token.get("refresh_token") or not token.get("access_token"):
            raise CalendarError("Google did not return a reusable connection. Try connecting again.")
        if scope not in token.get("scope", "").split():
            raise CalendarError("Calendar read permission was not granted.")
        return token["refresh_token"]
    except (requests.RequestException, ValueError) as exc:
        raise CalendarError("Could not complete Google authorization. Try again.") from exc


def encrypt_token(key, token):
    return Fernet(key).encrypt(token.encode()).decode()


def decrypt_token(key, ciphertext):
    return Fernet(key).decrypt(ciphertext.encode()).decode()


def refresh_access_token(client_id, client_secret, refresh_token):
    try:
        response = requests.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        }, timeout=15)
        response.raise_for_status()
        return response.json()["access_token"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise CalendarError("Calendar access expired or was revoked. Disconnect and reconnect it.") from exc


def upcoming_events(access_token, zone, now=None):
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    params = {
        "timeMin": now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), "timeMax": end.isoformat(),
        "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
        "fields": "nextPageToken,items(id,summary,start,end,location,status,transparency,colorId)",
    }
    events = []
    colors = {}
    try:
        palette = requests.get("https://www.googleapis.com/calendar/v3/colors",
                               headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        if palette.ok:
            import re
            colors = {key: value["background"] for key, value in palette.json().get("event", {}).items()
                      if re.fullmatch(r"#[0-9a-fA-F]{6}", value.get("background", ""))}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    try:
        for _ in range(10):
            response = requests.get(EVENTS_URL, headers={"Authorization": f"Bearer {access_token}"},
                                    params=params, timeout=15)
            response.raise_for_status()
            result = response.json()
            for event in result.get("items", []):
                if event.get("status") == "cancelled":
                    continue
                start = event.get("start", {})
                end_value = event.get("end", {})
                if "dateTime" in start and "dateTime" in end_value:
                    begins = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
                    ends = datetime.fromisoformat(end_value["dateTime"].replace("Z", "+00:00"))
                    if begins >= end:
                        continue
                    events.append({"title": event.get("summary") or "Busy", "start": begins,
                                   "end": ends, "all_day": False,
                                   "location": event.get("location", ""),
                                   "free": event.get("transparency") == "transparent",
                                   "color": colors.get(event.get("colorId"), "#4285f4")})
                elif "date" in start:
                    begins = datetime.fromisoformat(start["date"]).replace(tzinfo=zone)
                    events.append({"title": event.get("summary") or "All-day event", "start": begins,
                                   "end": datetime.fromisoformat(end_value.get("date", start["date"])).replace(tzinfo=zone),
                                   "all_day": True, "location": event.get("location", ""),
                                   "free": event.get("transparency") == "transparent",
                                   "color": colors.get(event.get("colorId"), "#4285f4")})
            if not result.get("nextPageToken"):
                return sorted(events, key=lambda item: item["start"])
            params["pageToken"] = result["nextPageToken"]
        raise CalendarError("Your calendar has too many events to display this week.")
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise CalendarError("Could not load Calendar events right now. Please retry.") from exc


def revoke(refresh_token):
    try:
        requests.post(REVOKE_URL, data={"token": refresh_token}, timeout=10)
    except requests.RequestException:
        pass  # Local removal always takes effect; Google access can also be revoked in Google settings.
