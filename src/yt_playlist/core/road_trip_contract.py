"""Persisted Road Trip recipe units and supported user controls."""

DEFAULT_FAMILIARITY_PCT = 50
MIN_TARGET_MINUTES = 15
MAX_TARGET_MINUTES = 12 * 60


def validate_recipe_values(own_pct, familiarity_pct, target_minutes, now):
    for name, value in (('own_pct', own_pct), ('familiarity_pct', familiarity_pct)):
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError(f'{name} must be an integer percentage from 0 through 100')
    if type(target_minutes) is not int or not MIN_TARGET_MINUTES <= target_minutes <= MAX_TARGET_MINUTES:
        raise ValueError(f'target_minutes must be an integer from {MIN_TARGET_MINUTES} through '
                         f'{MAX_TARGET_MINUTES} minutes')
    if type(now) not in (int, float) or not -1.7976931348623157e308 <= now <= 1.7976931348623157e308:
        raise ValueError('Recipe time must be finite epoch seconds')


def validate_selections(*groups):
    for values in groups:
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError('Recipe selections require lists of nonblank labels')


def select_identity(candidates, *, ambiguous=None):
    if len(candidates) == 1:
        return next(iter(candidates))
    if candidates and ambiguous:
        raise ValueError(ambiguous)
    return None


def custom_genre_key(label):
    from yt_playlist.util.genre_catalog import normalize
    if len(label.encode()) > 1024:
        raise ValueError('A custom genre requires 1..1024 bytes of nonblank text')
    key = normalize(label)
    if not key:
        raise ValueError('A custom genre requires a nonempty identity')
    return key


def membership_rows(artists, genres):
    result = {'artists':[], 'genres':[], 'unresolved_artists':[], 'unresolved_genres':[]}
    seen = set()
    for position, (label, identity) in enumerate(artists):
        if identity is None:
            result['unresolved_artists'].append((position, label, 'unknown_or_ambiguous_artist'))
        elif identity in seen:
            raise ValueError('A recipe cannot select the same artist twice')
        else:
            result['artists'].append((identity, position))
            seen.add(identity)
    for role, selections in genres:
        seen = set()
        for position, identity in enumerate(selections):
            if identity in seen:
                raise ValueError('A recipe cannot select the same genre twice in one role')
            result['genres'].append((identity, role, position))
            seen.add(identity)
    return result


def draft_document(state):
    revision = state.get('revision')
    if not isinstance(revision, str) or not revision:
        raise ValueError('A draft requires a nonempty revision')
    candidates = {row['video_id']:row for row in state.get('pool', [])
                  if isinstance(row, dict) and row.get('video_id')}
    candidates.update({row['video_id']:row for row in state.get('picked', [])
                       if isinstance(row, dict) and row.get('video_id')})
    selections = []
    for kind, values in (('picked', state.get('picks', [])), ('excluded', state.get('banned', []))):
        for position, video in enumerate(values):
            if not isinstance(video, str) or not video:
                raise ValueError('A draft selection requires an exact upload ID')
            selections.append((kind, position, video, candidates.get(video, {})))
    recovery = {key:value for key, value in state.items() if key not in ('picks', 'banned')}
    return revision, 'building' if state.get('building') else 'ready', recovery, selections


def restore_draft(state, revision, selections):
    state['revision'] = revision
    state['picks'], state['banned'] = [], []
    for kind, video in selections:
        state['picks' if kind == 'picked' else 'banned'].append(video)
    return state
