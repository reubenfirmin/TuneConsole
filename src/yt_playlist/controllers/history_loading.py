"""Capture library matches, then publish imported history through explicit ports."""
import hashlib

from yt_playlist.library.history_matching import import_identity, spotify_rows, takeout_rows, discovery_seeds


class SpotifyHistory:
    def __init__(self, *, accounts, parse, library_keys, match_recording, history):
        self.accounts, self.parse, self.library_keys = accounts, parse, library_keys
        self.match_recording, self.history = match_recording, history

    def run(self, raw, *, on_progress=None):
        ident = import_identity(self.accounts())
        if ident is None:
            return {'error': 'no identity configured'}
        progress = on_progress or (lambda text: None)
        progress('Reading Spotify history…')
        parsed, quality = self.parse(raw)
        progress(f'Matching {len(parsed):,} history entries to your library…')
        owned = set(self.library_keys())
        matches = {credit: self.match_recording(*credit)
                   for credit in dict.fromkeys((r['title'], r['artist']) for r in parsed)}
        matched, unmatched, listens = spotify_rows(parsed, owned, matches)
        progress(f'Saving {len(matched):,} matched plays…')
        plays, events, added = self.history.import_spotify_listens(ident, listens)
        span = int((parsed[-1]['ts'] - parsed[0]['ts']) // 86400) if parsed else 0
        return {**quality, 'source': 'spotify_export', 'identity_id': ident,
                'matched': len(matched), 'unmatched': sum(unmatched.values()),
                'plays_added': plays, 'events_added': events, 'listens_added': added,
                'span_days': span, 'unmatched_artists': self.history.spotify_unmatched_artists(ident)}


class TakeoutHistory:
    def __init__(self, *, identity, parse, library_keys, video_key, match_recording, history, now):
        self.identity, self.parse, self.library_keys = identity, parse, library_keys
        self.video_key, self.match_recording, self.history, self.now = video_key, match_recording, history, now

    def run(self, raw, *, on_progress=None):
        ident = self.identity()
        if ident is None:
            return {'error': 'no identity configured'}
        progress = on_progress or (lambda text: None)
        progress('Reading Takeout history…')
        parsed, unparsed = self.parse(raw)
        progress(f'Matching {len(parsed):,} history entries to your library…')
        owned = set(self.library_keys())
        videos = {video: self.video_key(video) for video in dict.fromkeys(
            r['video_id'] for r in parsed if r['video_id'])}
        recordings = {credit: self.match_recording(*credit) for credit in dict.fromkeys(
            (r['title'], r['artist']) for r in parsed if not r['video_id'])}
        matched, unmatched = takeout_rows(parsed, owned, videos, recordings)
        progress(f'Saving {len(matched):,} matched plays…')
        plays, events = self.history.import_listening_events(ident, matched)
        data = raw.encode() if isinstance(raw, str) else raw
        import_id = self.history.record_takeout_import(
            hashlib.sha256(data).hexdigest(), ident, self.now(), parsed, unparsed)
        span = int((parsed[-1]['ts'] - parsed[0]['ts']) // 86400) if parsed else 0
        return {'matched': len(matched), 'unmatched': sum(unmatched.values()),
                'plays_added': plays, 'events_added': events, 'span_days': span,
                'unmatched_artists': dict(unmatched), 'unparsed_dates': unparsed, 'import_id': import_id}


def seed_takeout(discovery, unmatched, now, min_plays):
    existing = {a.get('artist', '') for a in (discovery.get_discovered_artists() or [])}
    seeded = 0
    for artist, plays, retained in discovery_seeds(unmatched, existing, min_plays):
        if retained:
            discovery.retain_discovered_artist(artist, plays)
        else:
            discovery.upsert_discovered_artist(artist, plays, None, None, None, now)
            seeded += 1
    return seeded
