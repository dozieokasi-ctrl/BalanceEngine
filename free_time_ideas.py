"""Optional ideas that fit after required tasks have reserved time."""

from datetime import timedelta


IDEAS = [
    {"name": "Take a short break", "minutes": 10},
    {"name": "Get some fresh air", "minutes": 15},
    {"name": "Read for fun", "minutes": 20},
]

def recommend(remaining_blocks, ideas=None):
    """Show only ideas that fit the very next unreserved free block."""
    if not remaining_blocks:
        return []

    start, end = min(remaining_blocks)
    return [
        {**idea, "start": start, "end": start + timedelta(minutes=idea["minutes"])}
        for idea in (IDEAS if ideas is None else ideas)
        if start + timedelta(minutes=idea["minutes"]) <= end
    ]
