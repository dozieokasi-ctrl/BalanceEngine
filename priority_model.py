"""Explainable task priority preview; this does not change the scheduler."""

from datetime import timedelta


def ranked_priorities(tasks, now):
    ranked = []
    for task in tasks:
        if task["completed"] or task["earliest_start"] > now:
            continue
        remaining = task["due"] - now
        urgency = 1 - min(max(remaining / timedelta(days=14), 0), 1)
        importance = (task["importance"] - 1) / 4
        difficulty = (task.get("difficulty", 3) - 1) / 4
        workload = min(max(float(task["hours"]) / 8, 0), 1)
        score = round(100 * (
            0.40 * importance + 0.35 * urgency
            + 0.15 * difficulty + 0.10 * workload
        ))
        ranked.append({
            "task": task,
            "score": score,
            "urgency": "Overdue" if remaining.total_seconds() < 0 else
                f"Due in {max(1, int(remaining.total_seconds() / 86400 + 0.9999))} day(s)",
        })
    ranked.sort(key=lambda row: (-row["score"], row["task"]["due"], row["task"]["name"]))
    return ranked
