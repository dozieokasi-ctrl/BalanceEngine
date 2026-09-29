"""Optional ideas that fit after required tasks have reserved time."""

from datetime import timedelta


IDEAS = [
    {"name": "Stretch for taekwondo", "minutes": 15},
    {"name": "Listen to a new album", "minutes": 45},
    {"name": "Journal for a few minutes", "minutes": 10},
    {"name": "Review notes for your next class", "minutes": 20},
    {"name": "Scout summer internship openings", "minutes": 25},
    {"name": "Grab a snack", "minutes": 10},
    {"name": "Tidy your room", "minutes": 20},
    {"name": "Practice guitar", "minutes": 20},
    {"name": "Review finance flashcards", "minutes": 15},
    {"name": "Take a walk outside", "minutes": 15},
    {"name": "Read something for fun", "minutes": 20},
    {"name": "Send a networking follow-up", "minutes": 15},
    {"name": "Practice Python", "minutes": 25},
    {"name": "Write a few song lyrics", "minutes": 15},
    {"name": "Plan tomorrow's priorities", "minutes": 10},
    {"name": "Review SIE material", "minutes": 20},
    {"name": "Do a quick room reset", "minutes": 10},
    {"name": "Try a taekwondo technique drill", "minutes": 20},
    {"name": "Work through a practice problem", "minutes": 20},
    {"name": "Take an actual break", "minutes": 15},
    {"name": "Relax", "minutes": 20},
    {"name": "Go socialize", "minutes": 60},
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
