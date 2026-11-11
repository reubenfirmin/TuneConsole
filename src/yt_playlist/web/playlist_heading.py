"""Separate recognized mixtape metadata without inventing a replacement name."""
import re
from datetime import datetime

from yt_playlist.rec import home_vibe

_TYPES = ('More in your wheelhouse', 'From your catalog', 'Comfort listening',
          'Fresh songs', 'Radio Session')
_DATED = re.compile(
    r'^(?P<body>.+) [-·] (?P<date>[A-Za-z]+ \d{1,2} \d{4}|\d{4}-\d{2}-\d{2})'
    r'(?: #(?P<version>[1-9]\d*))?$')


def build_vibe_label(recipe):
    """Saved provenance alone identifies the sound used to build this mix."""
    preset = recipe.get('vibe') if isinstance(recipe, dict) else None
    return home_vibe.PRESETS.get(preset, {}).get('label') if isinstance(preset, str) else None


def title_context(title, *, is_generated, recipe=None):
    """Custom names stay plain; saved provenance supplies a recognized mix's Vibe."""
    match = _DATED.fullmatch(title or '') if is_generated or recipe is not None else None
    if not match:
        return {'generated_heading': None, 'build_vibe_label': None}
    body = match['body']
    vibe = build_vibe_label(recipe)
    identity = body
    # Recognize historical title syntax without inferring missing provenance.
    for label in (preset['label'] for preset in home_vibe.PRESETS.values()):
        if identity.endswith(' - ' + label):
            identity = identity[:-(len(label) + 3)]
            break
    kind = next((label for label in _TYPES if identity == label or identity.endswith(' - ' + label)), None)
    try:
        day = datetime.strptime(match['date'], '%Y-%m-%d' if match['date'][:4].isdigit() else '%B %d %Y').date()
    except ValueError:
        kind = None
    if kind is None:
        return {'generated_heading': None, 'build_vibe_label': None}
    # All displayed text comes from the saved name. Date/version and a known
    # recorded Vibe can move into metadata; unknown historical text stays intact.
    name = body
    if vibe and name.endswith(' - ' + vibe):
        name = name[:-(len(vibe) + 3)]
    return {'generated_heading': {'name': name, 'kind': kind, 'date': day.isoformat(),
            'date_label': f'{day:%B} {day.day}, {day.year}', 'version': match['version']},
            'build_vibe_label': vibe}
