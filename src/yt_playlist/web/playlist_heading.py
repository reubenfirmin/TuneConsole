"""Presentation of recognized generated titles; the stored title remains intact."""
import re
from datetime import datetime


_TYPES = ('More in your wheelhouse', 'From your catalog', 'Comfort listening',
          'Fresh songs', 'Radio Session')
_DATED = re.compile(
    r'^(?P<body>.+) [-·] (?P<date>[A-Za-z]+ \d{1,2} \d{4}|\d{4}-\d{2}-\d{2})'
    r'(?: #(?P<version>[1-9]\d*))?$')


def generated_heading(title, *, is_generated):
    """Separate known type/date/version syntax, preserving custom names verbatim."""
    match = _DATED.fullmatch(title or '') if is_generated else None
    if not match:
        return None
    body, raw_date = match['body'], match['date']
    kind = next((label for label in _TYPES if body == label or body.endswith(' - ' + label)), None)
    if kind is None:
        return None
    try:
        day = datetime.strptime(raw_date, '%Y-%m-%d' if raw_date[:4].isdigit() else '%B %d %Y').date()
    except ValueError:
        return None
    name = body[:-(len(kind) + 3)] if body != kind else kind
    return {'name': name, 'kind': kind, 'date': day.isoformat(),
            'date_label': f'{day:%B} {day.day}, {day.year}', 'version': match['version']}
