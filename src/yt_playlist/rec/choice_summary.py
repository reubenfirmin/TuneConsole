"""Pure descriptive episode outcomes over immutable choice evidence."""
import math

from yt_playlist.rec import choice_attribution


def album_track_days(offers, responses, attribution_seconds, *, album_ids=None, timezone=None, calendars=None):
    decisions = choice_attribution.actions(offers, responses, attribution_seconds)
    calendars = calendars or {}
    choice_days = {d['choice']['id']: choice_attribution.calendar_day(
        d['choice']['at'], choice_attribution.provenance(d), timezone, calendars) for d in decisions}
    return attributed_album_track_days(decisions, choice_days, album_ids=album_ids)


def attributed_album_track_days(decisions, choice_days, *, album_ids=None):
    """Confirmed tracks on distinct deliberate album-choice days.

    Later tracks in the chosen queue share its choice day. Continuation cannot
    manufacture another return, and unstarted choices supply no track coverage.
    Stable source withdrawals are filtered by the caller before recomputation.
    Optional verified identities combine dates before counting, without changing
    the original option IDs used to match each choice and its confirmed start.
    """
    days, episodes = {}, set()
    for decision in decisions:
        if decision['offer']['format'] != 'albums':
            continue
        choice, option = decision['choice'], decision['option']
        tracks = (choice.get('details') or {}).get('tracks')
        tracks = tracks if tracks is not None else option.get('tracks', [])
        for event in decision['outcomes']:
            if event['kind'] != 'start':
                continue
            video = (event.get('details') or {}).get('video_id')
            keys = {track['key'] for track in tracks
                    if video and track.get('video_id') == video and track.get('key')}
            if len(keys) == 1:
                key = keys.pop()
                browse = (album_ids or {}).get(event['option_id'], event['option_id'])
                episode = browse, key, decision['offer'].get('episode_id', decision['offer']['id'])
                if episode not in episodes:
                    episodes.add(episode)
                    days.setdefault(browse, {}).setdefault(key, set()).add(choice_days[choice['id']])
    return {browse: {key: len(values) for key, values in tracks.items()} for browse, tracks in days.items()}


def summarize(decisions, formats, choice_days):
    """Episode/day counts and finite-record coverage, never one trial per generated track.

    A return requires a fresh deliberate choice after a previously confirmed start on an earlier
    local listening day. Continued autoplay alone cannot produce a return. Completion describes the observed
    playhead under the existing exit rule; it does not claim attentive listening or enjoyment.
    """
    from yt_playlist.library.listen_derive import classify_exit
    rows = {format: {'observed_exit_episodes': 0, 'completion_episodes': 0,
                     'early_exit_episodes': 0, 'unknown_ending_episodes': 0,
                     'known_coverage_episodes': 0, 'mean_track_coverage': None, 'return_days': 0}
            for format in formats}
    previous, visited, returns, episodes = {}, set(), set(), {}
    for decision in decisions:
        offer, option, choice = (decision[key] for key in ('offer', 'option', 'choice'))
        playback = [row for row in decision['outcomes'] if row['kind'] in {'start', 'experience'}]
        if not playback:
            continue
        day = choice_days[choice['id']]
        unit = offer['format'], offer['episode_id']
        state = episodes.setdefault(unit, {'started': False, 'videos': set(), 'expected': set(),
                                          'exits': set(), 'completion': False, 'early': False})
        if any(row['kind'] == 'start' for row in playback):
            state['started'] = True
            identity = (offer['format'], str(option['id']))
            visit = (*identity, offer['episode_id'])
            if visit not in visited and previous.get(identity, day) < day:
                returns.add((*identity, day))
            previous.setdefault(identity, day)
            visited.add(visit)
        if offer['format'] != 'mixes':
            tracks = (choice.get('details') or {}).get('tracks', option.get('tracks', []))
            state['expected'].update(track['video_id'] for track in tracks if track.get('video_id'))
        for event in playback:
            details = event.get('details') or {}
            video = details.get('video_id')
            if video and (event['kind'] == 'start' or details.get('kind') is None):
                state['videos'].add(video)
            if event['kind'] != 'experience' or details.get('kind') not in {'track_exit', 'ended', 'bye'}:
                continue
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
    for (format, _episode), state in episodes.items():
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
