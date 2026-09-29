"""Pure dashboard calculations; no tokens or shared local files."""
import re
from datetime import datetime, time, timedelta


def week_view(events, work_hours, now):
    days = []
    for offset in range(7):
        date = now.date() + timedelta(days=offset)
        midnight = datetime.combine(date, time.min, now.tzinfo)
        day_end = datetime.combine(date + timedelta(days=1), time.min, now.tzinfo)
        hours = work_hours.get(date.strftime('%A'))
        blocks = []
        if hours:
            start = max(datetime.combine(date, time.fromisoformat(hours[0]), now.tzinfo), now)
            end = datetime.combine(date, time.fromisoformat(hours[1]), now.tzinfo)
            if start < end:
                blocks = [(start, end)]
        timeline = []
        for event in events:
            start = event['start'].astimezone(now.tzinfo)
            end = event.get('end', start + timedelta(days=1)).astimezone(now.tzinfo)
            if start >= day_end or end <= midnight:
                continue
            timeline.append({'start': max(start, midnight), 'end': min(end, day_end),
                             'title': event['title'], 'detail': event.get('location', ''),
                             'kind': 'calendar', 'color': event.get('color', '#4285f4'),
                             'all_day': event.get('all_day', False)})
            if event.get('free'):
                continue
            updated = []
            for left, right in blocks:
                if end <= left or start >= right:
                    updated.append((left, right))
                else:
                    if left < start:
                        updated.append((left, start))
                    if end < right:
                        updated.append((end, right))
            blocks = updated
        timeline += [{'start': start, 'end': end, 'title': 'Free for work',
                      'detail': 'Within your work hours', 'kind': 'free'} for start, end in blocks]
        timeline.sort(key=lambda item: (item['start'], item['kind']))
        days.append({'date': date, 'label': date.strftime('%a'), 'blocks': blocks,
                     'hours': sum((end-start).total_seconds()/3600 for start,end in blocks),
                     'timeline': timeline, 'schedule_label': "Today's schedule" if offset == 0 else
                     "Tomorrow's schedule" if offset == 1 else date.strftime("%A's schedule")})
    special = sorted((event for event in events if event.get('end', event['start']+timedelta(days=1)) > now and
                      (event.get('all_day') or re.search(r'\b(birthday|holiday|anniversary)\b', event['title'], re.I))),
                     key=lambda event: event['start'])
    return {'days': days, 'today_free_hours': days[0]['hours'],
            'week_free_hours': sum(day['hours'] for day in days),
            'next_special_event': special[0] if special else None}
