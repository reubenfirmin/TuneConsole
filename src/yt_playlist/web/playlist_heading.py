"""Presentation of recognized generated titles; the stored title remains intact."""
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


def generated_heading(title, *, is_generated, recipe=None):
    """Separate known type/date/version syntax, preserving custom names verbatim."""
    match = _DATED.fullmatch(title or '') if is_generated else None
    if not match:
        return None
    body, raw_date = match['body'], match['date']
    vibe = build_vibe_label(recipe)
    if vibe and body.endswith(' - ' + vibe):
        body = body[:-(len(vibe) + 3)]
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


def generated_title_context(store, playlist, *, is_generated, recipe=None, tracks=None):
    """Present the same current track facts before and after local import."""
    heading = generated_heading(playlist.title, is_generated=is_generated, recipe=recipe)
    if heading and heading['name'] == heading['kind']:
        from yt_playlist.rec import recipes
        lane = {'More in your wheelhouse': 'wheelhouse', 'From your catalog': 'explore',
                'Comfort listening': 'comfort', 'Fresh songs': 'fresh'}.get(heading['kind'])
        if lane:
            if tracks is None:
                tracks = store.charts.playlist_tracks_detail(playlist.id)
            items = [{**track, 'key': track.get('identity_key', ''), 'year': None} for track in tracks]
            heading.update(recipes.theme_heading(store, lane, items))
    return {'generated_heading': heading, 'build_vibe_label': build_vibe_label(recipe)}
