"""Read a user's chosen spreadsheet through their hosted OAuth connection."""
from urllib.parse import quote
import requests
from hosted_calendar import CalendarError

SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'

def fetch_rows(access_token, sheet_id, tab=''):
    range_name = "'" + tab.replace("'", "''") + "'!A:Z" if tab else 'A:Z'
    try:
        response = requests.get(f'https://sheets.googleapis.com/v4/spreadsheets/{quote(sheet_id, safe="")}/values/{quote(range_name, safe="")}',
                                headers={'Authorization': f'Bearer {access_token}'}, timeout=15)
        response.raise_for_status()
        return response.json().get('values', [])
    except (requests.RequestException, ValueError) as exc:
        raise CalendarError('Could not read your sheet. Check Sheets API is enabled, the link and tab are correct, and your connected Google account has access.') from exc
