# BalanceEngine

BalanceEngine is a local student dashboard that combines tasks, Google Calendar availability, and assignments from Google Sheets. It suggests when to work on tasks and offers short activities for the next open break.

## What it does

- Shows upcoming one-time and weekly tasks. Add, edit, delete, or mark several complete at once.
- Displays seven days of Calendar events and free time within your configured work hours. The schedule has Previous and Next controls; the free-time card highlights today and tomorrow.
- Reads a Google Sheet of assignments and creates preparation tasks for exams, projects, and homework. Econ exams appear in the assignment list without an automatic study task.
- Reserves free blocks for unfinished tasks by earliest deadline, then suggests break activities that fit the next remaining block.
- Shows an **explainable priority preview** in the AI recommendation card. Its 0–100 score uses importance (40%), deadline urgency (35%), difficulty (15%), and workload (10%). **The score does not yet control the schedule, and this feature does not call an AI model.**

## Run locally

Requires Python 3.10+ and an existing `schedule_settings.py` with `WORK_HOURS` and `REPEATING_TASKS`. From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install Flask google-api-python-client google-auth-httplib2 google-auth-oauthlib
python web_app.py
```

Open <http://127.0.0.1:5050> in your browser. If port 5050 is already in use, stop the previous server with Control-C in its terminal before starting another.

For Calendar and Sheets, put the Google OAuth **Desktop app** client file at `credentials.json` in the project directory. Enable both the Google Calendar API and Google Sheets API in the same Google Cloud project. The first request may open a browser to authorize read-only access; the app stores tokens locally in `token.json` and `sheets_token.json`. Paste a Google Sheets URL into the dashboard to select a spreadsheet.

The Sheet needs headers `Assignment` (or `Title`) and `Due Date`. `Course`, `Type`, and `Status` are optional. A `Type` of Exam, Project, or Homework is best; otherwise the app infers type from the title and defaults to Homework. Accepted due dates include `YYYY-MM-DD` and `MM/DD/YYYY`. Date-only deadlines are treated as 11:59 PM in America/New_York. Refresh the page to read changes to the Sheet and Calendar.

## Local data and Git

The dashboard writes your tasks and preferences to JSON files beside `web_app.py`, including `tasks.json`, `weekly_tasks.json`, `completed_tasks.json`, `completion_dates.json`, `break_ideas.json`, and `sheets_config.json`. Back these up if you need to move computers. Keep `credentials.json`, `token.json`, `sheets_token.json`, and personal JSON data out of Git. Before pushing, run `git status --short` and review the staged files with `git diff --cached --name-only`.

## Current limits

- This is a local Flask development app, not a hosted multi-user service.
- Calendar access reads events; suggested task sessions are **not** added to Google Calendar.
- Google Sheets access reads assignments; completing a generated task in the dashboard does not update the Sheet.
- Weekly task completions clear from the visible completed list at the next Monday boundary.
- The priority recommendation is a preview; the scheduling algorithm currently sorts by deadline and then importance.
