"""Private genuine enrichment with controlled provider, artwork and worker failures."""
import threading
from types import SimpleNamespace

import pytest

from tests.conftest import FakeClient, only_provider
from yt_playlist.providers import base, musicbrainz, waterfall


@pytest.fixture
def quiet_enrichment(store, monkeypatch, canonical_genre_matcher):
    monkeypatch.delenv('LASTFM_API_KEY', raising=False)
    identity = store.accounts.upsert_account('Main', 'credential', None, True)
    playlist = store.playlists.upsert_playlist(identity, 'QUIET', 'Quiet enrichment', 2, '', 1)
    tracks = [store.tracks.upsert_track(f'quiet-{n}', f'Quiet song {n}', 'Artist', 'Quiet album',
                                      200, album_browse_id='QUIET-ALBUM') for n in range(2)]
    store.playlists.set_playlist_tracks(playlist, tracks)
    store.collection.replace_saved_albums([{'browse': 'QUIET-ALBUM', 'title': 'Quiet album',
        'artist': 'Artist', 'year': '2001', 'type': 'Album', 'thumbnail': None}])
    only_provider(store, 'musicbrainz')
    for track in tracks:
        store.enrichment.apply_provider_fields(track, 'deezer',
            {'bpm': 120, 'energy': .5, 'danceability': .5})
        store.tracks.set_track_art(track, 'https://example.com/quiet-cover.jpg')
    case = SimpleNamespace(identity=identity, playlist=playlist, tracks=tracks,
        clients={identity: FakeClient()}, calls=[], fault=None, now=1000,
        diagnostic='sqlite3.OperationalError: internal-diagnostic-879-enrichment',
        observed=threading.Event(), release=threading.Event(), finished=threading.Event())
    case.release.set()
    probe, resolve_art, run = musicbrainz.probe, waterfall._resolve_art, waterfall.run_waterfall
    monkeypatch.setattr(musicbrainz, 'enrich_full',
        lambda *_args, **_kwargs: ('rock', '2001', None, [], []))

    def controlled_probe(track, owner):
        if track['video_id'] == 'quiet-0':
            if case.fault == 'provider':
                raise RuntimeError(case.diagnostic)
            if case.fault == 'deferred':
                return base.EnrichmentResult('musicbrainz', deferred_reason=case.diagnostic)
        return probe(track, owner)

    def controlled_art(sink, track, results, now_fn):
        if case.fault == 'artwork' and track['video_id'] == 'quiet-0':
            raise RuntimeError(case.diagnostic)
        return resolve_art(sink, track, results, now_fn)

    def controlled_run(owner, pending, config, on_progress, **kwargs):
        case.finished.clear()
        case.calls.append([track['id'] for track in pending])

        def progress(event):
            on_progress(event)
            outer = case.fault == 'outer' and event['type'] == 'track'
            if outer or case.diagnostic in event.get('text', ''):
                case.observed.set()
                assert case.release.wait(10), 'Controlled enrichment was not released'
            if outer:
                raise RuntimeError(case.diagnostic)

        kwargs.setdefault('now_fn', lambda: case.now)
        try:
            return run(owner, pending, config, progress, **kwargs)
        finally:
            case.finished.set()

    monkeypatch.setattr(musicbrainz, 'probe', controlled_probe)
    monkeypatch.setattr(waterfall, '_resolve_art', controlled_art)
    monkeypatch.setattr(waterfall, 'run_waterfall', controlled_run)
    try:
        yield case
    finally:
        case.release.set()
