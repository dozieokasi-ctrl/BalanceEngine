"""Reserve calendar free blocks for tasks, earliest deadline first."""

from datetime import timedelta


def schedule_tasks(tasks, calendar, now):
    """Return task suggestions and unreserved blocks for optional activities."""
    available = [block for day in calendar["days"] for block in day["blocks"]]
    suggestions = []

    for task in sorted(
        (task for task in tasks if not task["completed"]),
        key=lambda task: (task["due"], -task["importance"]),
    ):
        remaining = timedelta(hours=float(task["hours"]))
        earliest = max(now, task.get("earliest_start", now))
        next_available = []
        sessions = []

        for free_start, free_end in available:
            start = max(free_start, earliest)
            end = min(free_end, task["due"])
            if remaining <= timedelta(0) or start >= end:
                next_available.append((free_start, free_end))
                continue

            if free_start < start:
                next_available.append((free_start, start))

            session_end = start + min(remaining, end - start)
            sessions.append((start, session_end))
            remaining -= session_end - start

            if session_end < free_end:
                next_available.append((session_end, free_end))

        available = next_available
        suggestions.append({
            "task": task,
            "sessions": sessions,
            "hours_short": max(0, remaining.total_seconds() / 3600),
        })

    return {"suggestions": suggestions, "remaining_blocks": available}


def suggested_sessions(tasks, calendar, now):
    """Return one suggestion per unfinished task, with any unscheduled hours."""
    return schedule_tasks(tasks, calendar, now)["suggestions"]
