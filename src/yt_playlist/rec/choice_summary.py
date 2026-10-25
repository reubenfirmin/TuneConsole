"""Pure descriptive episode outcomes over immutable choice evidence."""
import math


def album_track_days(offers, responses, attribution_seconds):
    """Confirmed tracks on distinct deliberate album-choice days.

    Later tracks in the chosen queue share its choice day. Continuation cannot
    manufacture another return, and unstarted choices supply no track coverage.
    Stable source withdrawals are filtered by the caller before recomputation.
    """
    selected, days = {}, {}
    for event in sorted(responses, key=lambda row: (row['at'], row['kind'] != 'choice', row['id'])):
        offer = offers.get(event['opportunity_id'])
        if not offer or offer['format'] != 'albums':
            continue
        pair = (offer['id'], event['option_id'])
        if event['kind'] == 'choice':
            selected[pair] = event
        elif event['kind'] == 'start':
            choice = selected.get(pair)
            if not choice or not 0 <= event['at'] - choice['at'] <= attribution_seconds:
                continue
            option = next((row for row in offer['options'] if str(row['id']) == event['option_id']), None)
            if not option:
                continue
            tracks = (choice.get('details') or {}).get('tracks')
            tracks = tracks if tracks is not None else option.get('tracks', [])
            video = (event.get('details') or {}).get('video_id')
            keys = {track['key'] for track in tracks
                    if video and track.get('video_id') == video and track.get('key')}
            if len(keys) == 1:
                key = keys.pop()
                days.setdefault(event['option_id'], {}).setdefault(key, set()).add(int(choice['at'] // 86400))
    return {browse: {key: len(values) for key, values in tracks.items()} for browse, tracks in days.items()}


def summarize(offers, responses, formats):
    """Episode/day counts and finite-record coverage, never one trial per generated track.

    A return requires a fresh deliberate choice after a previously confirmed start on an earlier
    UTC day. Continued autoplay alone cannot produce a return. Completion describes the observed
    playhead under the existing exit rule; it does not claim attentive listening or enjoyment.
    """
    from yt_playlist.library.listen_derive import classify_exit
    rows = {format: {'observed_exit_episodes': 0, 'completion_episodes': 0,
                     'early_exit_episodes': 0, 'unknown_ending_episodes': 0,
                     'known_coverage_episodes': 0, 'mean_track_coverage': None, 'return_days': 0}
            for format in formats}
    choices, started, previous, returns, episodes = {}, set(), {}, set(), {}
    for event in responses:
        offer = offers.get(event['opportunity_id'])
        if not offer:
            continue
        pair = (offer['id'], event['option_id'])
        option = next((row for row in offer['options'] if str(row['id']) == event['option_id']), None)
        if not option:
            continue
        if event['kind'] == 'choice':
            choices[pair] = event
        if event['kind'] not in {'start', 'experience'}:
            continue
        unit = (offer['format'], offer['episode_id'], int(event['at'] // 86400))
        state = episodes.setdefault(unit, {'started': False, 'videos': set(), 'expected': set(),
                                          'exits': set(), 'completion': False, 'early': False})
        details = event.get('details') or {}
        if event['kind'] == 'start':
            state['started'] = True
            deliberate = choices.get(pair)
            if deliberate and deliberate['id'] not in started:
                started.add(deliberate['id'])
                identity = (offer['format'], str(option['id']))
                day = int(deliberate['at'] // 86400)
                if identity in previous and previous[identity] < day:
                    returns.add((*identity, day))
                previous[identity] = day
        video = details.get('video_id')
        if video and (event['kind'] == 'start' or details.get('kind') is None):
            state['videos'].add(video)
        selected = choices.get(pair)
        tracks = ((selected.get('details') or {}).get('tracks') if selected else None)
        tracks = tracks if tracks is not None else option.get('tracks', [])
        state['expected'].update(track['video_id'] for track in tracks if track.get('video_id'))
        if event['kind'] == 'experience' and details.get('kind') in {'track_exit', 'ended', 'bye'}:
            position, duration = details.get('position'), details.get('duration')
            if not all(isinstance(value, (int, float)) and math.isfinite(value)
                       for value in (position, duration)) or position < 0:
                continue
            classification = classify_exit(position, duration)
            if classification == 'unknown':
                continue
            state['exits'].add(video)
            state['completion'] |= classification == 'completion'
            # Closing a tab is not an observed rejection or abandonment.
            state['early'] |= details['kind'] == 'track_exit' and classification == 'skip'
    coverage = {format: [] for format in rows}
    for (format, episode, day), state in episodes.items():
        row = rows[format]
        row['observed_exit_episodes'] += bool(state['exits'])
        row['completion_episodes'] += state['completion']
        row['early_exit_episodes'] += state['early']
        row['unknown_ending_episodes'] += state['started'] and not state['exits']
        if format != 'radio' and state['expected']:
            coverage[format].append(len(state['videos'] & state['expected']) / len(state['expected']))
    for format, row in rows.items():
        values = coverage[format]
        row['known_coverage_episodes'] = len(values)
        row['mean_track_coverage'] = sum(values) / len(values) if values else None
        row['return_days'] = sum(identity[0] == format for identity in returns)
    return rows
