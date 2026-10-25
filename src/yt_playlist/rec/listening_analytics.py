"""Personal listening portrait. Descriptive counts never become model training signals."""
from collections import defaultdict
from datetime import date as calendar_date
import hashlib
import json
import math

from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo
from yt_playlist.rec.listening_dates import day, midnight, timezone_name

SCHEMA = 2
FORMATS = ('mixes', 'albums', 'radio', 'liked', 'other')
FIELDS = ('plays', 'sightings', 'new_tracks', 'new_artists', 'artist_plays',
          'liked', 'rated', 'seconds', 'hearts', 'skulls', 'recent_discoveries', 'clears')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def document(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def capture_start(store, identity, video, playlist, status, at, *, album=False):
    """Freeze the first rating readout; later likes must not rewrite 'already liked'.

    Serving only offers a small immutable source record. Durable writes and all
    interpretation run on the existing worker; unavailable capture stays unknown.
    """
    capture = getattr(store, 'choice_capture', None)
    if capture is not None:
        capture.enqueue('listening analytics', store.player_events.record_player_event,
            identity, 'listen_start', video, None, None, playlist,
            json.dumps({'rating': status, 'album': album}), at)


def calculate(reader, now):
    repo = ListeningAnalyticsRepo(reader)
    metadata = {}
    for batch in repo.catalog_rows():
        metadata.update((row['identity_key'], row) for row in batch)
    with setting_reads(reader, ('radio_session_playlist_ids', 'listening_timezone')):
        radio_ids = set(json.loads(reader.settings.get_setting('radio_session_playlist_ids') or '[]'))
        zone = timezone_name(reader.settings.get_setting('listening_timezone'))
    date_of = lambda at: day(at, zone)
    mixes = {row['provider_id'] for batch in repo.batches(
        "SELECT DISTINCT s.provider_id FROM playlist_provenance p JOIN playlist_scopes s ON s.ref=p.ytm "
        "WHERE p.origin='tuneconsole'") for row in batch}
    videos = {row['video_id']: row['identity_key'] for batch in repo.batches(
        'SELECT y.video_id,r.identity_key FROM youtube_items y JOIN recordings r ON r.id=y.recording_id')
        for row in batch}

    def format_of(playlist, album=False):
        if playlist in radio_ids:
            return 'radio'
        if album or (playlist or '').startswith('OLAK5uy_'):
            return 'albums'
        if playlist == 'LM':
            return 'liked'
        return 'mixes' if playlist in mixes else 'other'

    cells = defaultdict(lambda: dict.fromkeys(FIELDS, 0))

    def cell(date, key, format):
        meta = metadata.get(key, {})
        return cells[(date, meta.get('artist') or '', meta.get('genre') or '', format,
                      key or '', str(meta.get('artist_id') or ''))]

    # Only frozen start observations can establish the rating at the first
    # playback report. play_events.like_status changes on subsequent readouts.
    starts, contexts, previous = {}, {}, {}
    for batch in repo.player(now):
        for event in batch:
            payload = document(event['payload'])
            identity = (event['identity_id'], event['video_id'], event['playlist_ytm_id'])
            if event['kind'] == 'listen_start':
                starts[(*identity, event['at'])] = payload
                contexts[identity] = payload
                previous.pop(identity, None)
                continue
            position = event['position']
            if not number(position) or position < 0:
                if event['kind'] in ('ended', 'track_exit', 'bye'):
                    previous.pop(identity, None)
                    contexts.pop(identity, None)
                continue
            before = previous.get(identity)
            if before is not None:
                elapsed, progress = event['at'] - before['at'], position - before['position']
                # A seek, suspended tab or missing heartbeat does not establish
                # elapsed listening. Paused and muted intervals contribute zero.
                if (0 < elapsed <= 45 and 0 <= progress <= elapsed * 1.25 + 2
                        and not before['paused'] and not before['muted']):
                    key = videos.get(event['video_id'])
                    format = format_of(event['playlist_ytm_id'], contexts.get(identity, {}).get('album', False))
                    seconds = min(elapsed, progress)
                    # Split midnight intervals so date filters do not move time
                    # from the previous day into the next one.
                    boundary = midnight(event['at'], zone)
                    tail = min(elapsed, event['at'] - boundary)
                    cell(date_of(event['at']), key, format)['seconds'] += seconds * tail / elapsed
                    if tail < elapsed:
                        cell(date_of(before['at']), key, format)['seconds'] += seconds * (elapsed-tail) / elapsed
            previous[identity] = {'at': event['at'], 'position': position,
                'paused': (payload['state'] == 'paused' if 'state' in payload else
                           False if event['kind'] == 'tick' else (before or {}).get('paused', False)),
                'muted': payload.get('volume', 1) == 0 if event['kind'] == 'volume' else
                    (before or {}).get('muted', False)}
            if event['kind'] in ('ended', 'track_exit', 'bye'):
                previous.pop(identity, None)
                contexts.pop(identity, None)

    # Earlier coarse evidence prevents an imported or live return being called a
    # discovery. Sightings remain separate units and never inflate exact plays.
    first_track, first_artist = {}, {}
    observations = []
    for batch in repo.observations(now):
        for row in batch:
            if row['day'] > date_of(now):
                continue
            observations.append(row)
            key = row['identity_key']
            first_track[key] = min(first_track.get(key, row['day']), row['day'])
            artist = metadata.get(key, {}).get('artist_id')
            if artist:
                first_artist[artist] = min(first_artist.get(artist, row['day']), row['day'])
    exact_days, discovered_tracks, discovered_artists = set(), set(), set()
    subjects = defaultdict(lambda: {'days': set(), 'plays': 0, 'formats': set()})
    for batch in repo.plays(now):
        for play in batch:
            key, date = play['identity_key'], date_of(play['played_at'])
            identity = (play['identity_id'], play['video_id'], play['playlist_ytm_id'])
            # Start captures share the play's timestamp; never borrow a later
            # replay's rating, another account's state or an unrelated playlist.
            start = starts.get((*identity, play['played_at']), {})
            row = cell(date, key, format_of(play['playlist_ytm_id'], start.get('album', False)))
            row['plays'] += 1
            exact_days.add((play['identity_id'], key, date))
            if key not in discovered_tracks and date < first_track.get(key, '9999-12-31'):
                row['new_tracks'] += 1
            discovered_tracks.add(key)
            first_track[key] = min(first_track.get(key, date), date)
            if (calendar_date.fromisoformat(date) - calendar_date.fromisoformat(first_track[key])).days < 28:
                row['recent_discoveries'] += 1
            subject = subjects[key]
            subject['days'].add(date)
            subject['plays'] += 1
            subject['formats'].add(format_of(play['playlist_ytm_id'], start.get('album', False)))
            artist = metadata.get(key, {}).get('artist_id')
            if artist:
                row['artist_plays'] += 1
                if artist not in discovered_artists and date < first_artist.get(artist, '9999-12-31'):
                    row['new_artists'] += 1
                discovered_artists.add(artist)
                first_artist[artist] = min(first_artist.get(artist, date), date)
            rating = start.get('rating')
            if rating in ('LIKE', 'DISLIKE', 'INDIFFERENT'):
                row['rated'] += 1
                row['liked'] += rating == 'LIKE'
    for row in observations:
        if (row['identity_id'], row['identity_key'], row['day']) not in exact_days:
            cell(row['day'], row['identity_key'], 'other')['sightings'] += 1
    # Immutable confirmed transitions survive clears and reversals. Existing
    # retained state has no reliable action date and is reported separately.
    for batch in repo.rating_actions(now):
        for row in batch:
            key = row['identity_key']
            if key not in metadata:
                continue
            kind = {'LIKE': 'hearts', 'DISLIKE': 'skulls', 'INDIFFERENT': 'clears'}[row['status']]
            cell(date_of(row['at']), key, 'other')[kind] += 1
    retained = {'hearts': 0, 'skulls': 0}
    for batch in repo.feedback(now):
        for row in batch:
            retained['hearts' if row['kind'] == 'like' else 'skulls'] += 1
    rows = [[*key[:4], *[round(values[field], 2) for field in FIELDS], *key[4:]]
            for key, values in sorted(cells.items())]
    return {'schema': SCHEMA, 'built_at': now, 'today': date_of(now), 'timezone': zone,
        'columns': ['day', 'artist', 'genre', 'format', *FIELDS, 'track', 'artist_id'], 'rows': rows,
        'catalog': metadata, 'retained': retained, 'first_seen': first_track,
        'subjects': {key: {**value, 'days': sorted(value['days']), 'formats': sorted(value['formats'])}
                     for key, value in subjects.items()}}


def build(store, now):
    """Rebuild privately in 256-row batches and atomically publish a complete generation."""
    with store.read_snapshot() as snapshot:
        payload = calculate(snapshot.store, now)
        from yt_playlist.rec.listening_editions import prepare
        edition = prepare(payload, ListeningAnalyticsRepo(snapshot.store), now)
        chunks, manifest = partition(payload)

        def publish():
            repo = ListeningAnalyticsRepo(store)
            for key, encoded in chunks.items():
                repo.publish(encoded, now, 'listening_chunk:' + key)
            repo.publish(json.dumps(edition, separators=(',', ':'), allow_nan=False), now, 'listening_edition')
            repo.publish(json.dumps(manifest, separators=(',', ':'), allow_nan=False), now)
            store.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('listening_timezone',?)",
                               (payload['timezone'],))
            active = tuple('listening_chunk:' + key for key in chunks)
            # Publication and retirement share one guarded transaction.
            if active:
                store.conn.execute("DELETE FROM rec_proposals WHERE surface LIKE 'listening_chunk:%' "
                    'AND surface NOT IN (' + ','.join('?' for _ in active) + ')', active)
        if snapshot.publish(publish):
            return payload
    return None


def partition(payload):
    """Content-addressed, bounded partitions; the compact manifest has no history rows."""
    chunks, refs, pending = {}, [], []

    def flush():
        if not pending:
            return
        keys = {row[-2] for row in pending}
        data = {'schema': SCHEMA, 'columns': payload['columns'], 'rows': list(pending),
                'catalog': {key: payload['catalog'][key] for key in keys if key in payload['catalog']}}
        encoded = json.dumps(data, separators=(',', ':'), allow_nan=False)
        key = hashlib.sha256(encoded.encode()).hexdigest()[:24]
        chunks[key] = encoded
        refs.append({'key': key, 'from': pending[0][0], 'until': pending[-1][0],
                     'bytes': len(encoded.encode())})
        pending.clear()

    for row in payload['rows']:
        if pending and (row[0][:7] != pending[0][0][:7] or len(pending) >= 256):
            flush()
        pending.append(row)
    flush()
    return chunks, {key: payload[key] for key in ('schema', 'built_at', 'today', 'timezone', 'columns', 'retained')} | {
        'chunks': refs, 'available': bool(payload['rows']),
        'generation': hashlib.sha256(''.join(chunks).encode()).hexdigest()[:24]}
