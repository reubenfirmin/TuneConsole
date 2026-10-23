"""Newly available providers revisit completed recordings without repeating other providers."""
import logging
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.providers import enrichment, lastfm
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from tests.test_enrichment_config import _client


class Provider:
    def __init__(self, name, available=lambda store: True):
        self.name, self.available, self.calls = name, available, []

    def reset(self): pass
    def tripped(self): return False
    def probe(self, track, store):
        self.calls.append(track["id"])
        return EnrichmentResult(self.name, {"genre": "house", "year": "2024", "bpm": 120,
                                          "energy": .8, "danceability": .7})


def config(store, enabled):
    enrichment.save_config(store, [{"name": p["name"], "enabled": p["name"] in enabled}
                                    for p in enrichment.PROVIDERS])


def worker(store, providers):
    ctx = SimpleNamespace(store=store, now_fn=lambda: 1000,
        logger=logging.getLogger(__name__), rec_worker=SimpleNamespace(trigger_content_update=Mock()))
    return EnrichWorker(ctx, waterfall_fn=partial(run_waterfall,
                        registry={p.name: p for p in providers}))


def test_enabling_provider_revisits_fully_populated_tracks_and_preserves_user_choices(store):
    tid = store.tracks.upsert_track("v1", "Song", "Artist", None, 200)
    a, b = Provider("musicbrainz"), Provider("deezer")
    config(store, {"musicbrainz"})
    w = worker(store, [a, b])
    assert w.drain_once() == 1
    recording = store.tracks.catalog_subject(tid)["recording_id"]
    assert store.enrichment.provider_coverage(recording) == {"musicbrainz":"found", "deezer":"disabled"}
    store.tracks.set_track_genre(tid, "Selected genre")
    assert w.drain_once() == 0
    config(store, {"musicbrainz", "deezer"})
    store.enrichment.reconcile_provider_configuration(enrichment.provider_configuration(store, {a.name:a, b.name:b}))
    assert store.enrichment.queue_remaining() == 1
    assert store.enrichment.coverage_stats()["processed"] == 0
    assert store.enrichment.provider_coverage(recording)["deezer"] == "pending"
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == [tid]
    assert store.tracks.get_track_enrichment(tid) == ("Selected genre", "2024")
    assert store.enrichment.queue_remaining() == 0
    assert store.enrichment.coverage_stats()["processed"] == 1
    assert w.drain_once() == 0
    statuses = {r["provider"]:r["status"] for r in store.conn.execute("SELECT * FROM enrichment_provider_config")}
    assert statuses == {"musicbrainz":"available", "deezer":"available"}


def test_reenrichment_keeps_genre_until_later_provider_replacement(store):
    tid = store.tracks.upsert_track('song', 'Song', 'Artist', None, 200)
    order = ['musicbrainz', 'discogs']
    store.enrichment.apply_provider_fields(tid, 'musicbrainz', {'genre': 'Rock'}, providers=order)
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (tid,)).fetchone()[0]
    with store.conn:
        store.conn.executemany('INSERT INTO enrichment_provider_jobs VALUES (?,?)',
                               [(recording, name) for name in order])
    store.enrichment.record_enrichment_attempt('song|artist', 'musicbrainz', {}, providers=order)
    assert store.tracks.get_track_enrichment(tid)[0] == 'Rock'
    assert store.enrichment.metadata_provenance(tid)['fields']['genre']['source'] == 'musicbrainz'
    # A subsequent process can replace it using completed answers, without an empty interval.
    store.enrichment.apply_provider_fields(tid, 'discogs', {'genre': 'Soundtrack'}, providers=order)
    store.enrichment.record_enrichment_attempt('song|artist', 'discogs', {'genre': 'Soundtrack'}, providers=order)
    assert store.tracks.get_track_enrichment(tid)[0] == 'Soundtrack'
    assert store.enrichment.metadata_provenance(tid)['fields']['genre']['source'] == 'discogs'


def test_adding_or_changing_effective_key_schedules_only_that_provider(store, monkeypatch):
    tid = store.tracks.upsert_track("v1", "Song", "Artist", None, 200)
    monkeypatch.setattr(lastfm, "api_key", lambda s: s.settings.get_setting("lastfm_api_key") or None)
    a = Provider("musicbrainz")
    b = Provider("lastfm", available=lambda s: lastfm.api_key(s) is not None)
    b.api_key = lastfm.api_key
    config(store, {"musicbrainz", "lastfm"})
    w = worker(store, [a,b])
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == []
    row = store.conn.execute("SELECT status FROM enrichment_provider_config WHERE provider='lastfm'").fetchone()
    assert row[0] == "unavailable"
    store.settings.set_setting("lastfm_api_key", "first-key")
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == [tid]
    assert w.drain_once() == 0
    store.settings.set_setting("lastfm_api_key", "second-key")
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == [tid,tid]
    assert "first-key" not in str(list(map(tuple, store.conn.execute("SELECT * FROM enrichment_provider_config"))))
    assert "second-key" not in str(list(map(tuple, store.conn.execute("SELECT * FROM enrichment_provider_config"))))
    assert w.drain_once() == 0


def test_provider_settings_routes_wake_owned_worker(store):
    c = _client(store)
    wake = Mock()
    c.app.state.ctx.enrich_worker.trigger = wake
    response = c.post("/setup/enrichment", data={"order": [p["name"] for p in enrichment.PROVIDERS],
                                               "enabled": ["musicbrainz"]})
    assert response.status_code == 200
    assert wake.call_count == 1
    assert c.post("/settings/lastfm-key", data={"key":"new"}).status_code == 204
    assert wake.call_count == 2


def test_disabled_provider_retains_retry_evidence_without_reporting_an_active_retry(store):
    tid = store.tracks.upsert_track("v1", "Song", "Artist", None, 200)
    track = store.tracks.catalog_subject(tid)
    store.enrichment.record_enrichment_attempt(track["identity_key"], "deezer", {}, retry=True, now=1000,
                                    error="timeout")
    assert store.enrichment.enrichment_retry_status()["count"] == 1
    config(store, set())
    store.enrichment.reconcile_provider_configuration(enrichment.provider_configuration(store, {"deezer":Provider("deezer")}))
    assert store.enrichment.enrichment_retry_status()["count"] == 0
    assert store.enrichment.provider_coverage(track["recording_id"])["deezer"] == "disabled"
    assert store.enrichment.enrichment_attempts(track["identity_key"])["deezer"]["status"] == "retry"


@pytest.mark.parametrize('change', ['catalog', 'policy', 'vocabulary'])
def test_genre_revision_refreshes_completed_library_in_bounded_resumable_batches(store, monkeypatch, change):
    ids = [store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', None, 200) for i in range(3)]
    genre_provider, audio_provider = Provider('musicbrainz'), Provider('acousticbrainz')
    config(store, {'musicbrainz', 'acousticbrainz'})
    w = worker(store, [genre_provider, audio_provider])
    assert w.drain_once() == 3
    store.tracks.set_track_genre(ids[0], 'My explicit genre')
    if change == 'catalog':
        monkeypatch.setattr(enrichment.genre_catalog, 'REVISION', 'next-catalog')
    elif change == 'policy':
        monkeypatch.setattr(enrichment, 'GENRE_POLICY_VERSION', enrichment.GENRE_POLICY_VERSION + 1)
    else:
        store.genres.add_genre('Dub Techno')

    assert w.drain_once(limit=1) == 1
    assert store.enrichment.queue_remaining() == 2
    assert len(genre_provider.calls) == 4 and len(audio_provider.calls) == 3
    # A replacement worker resumes persisted jobs instead of enqueueing the
    # completed first item again. The same revision eventually becomes idle.
    w = worker(store, [genre_provider, audio_provider])
    assert w.drain_once(limit=1) == 1
    assert w.drain_once(limit=1) == 1
    assert w.drain_once(limit=1) == 0
    assert len(genre_provider.calls) == 6 and len(audio_provider.calls) == 3
    assert store.tracks.get_track_enrichment(ids[0])[0] == 'My explicit genre'


@pytest.mark.parametrize('replacement', [None, 'Soundtrack'])
@pytest.mark.parametrize('retry_first', [False, True])
@pytest.mark.parametrize('explicit', [False, True])
def test_genre_refresh_preserves_existing_value_until_replacement_without_losing_choices_or_retries(
        store, monkeypatch, replacement, retry_first, explicit):
    tid = store.tracks.upsert_track('remix', 'Song (Club Mix)', 'Artist', None, 200)
    original, fallback = Provider('musicbrainz'), Provider('discogs')
    original.probe = Mock(return_value=EnrichmentResult('musicbrainz', {'genre': 'Rock', 'year': '1993'}))
    fallback.probe = Mock(return_value=EnrichmentResult('discogs', {}))
    config(store, {'musicbrainz', 'discogs'})
    w = worker(store, [original, fallback])
    assert w.drain_once() == 1
    if explicit:
        store.tracks.set_track_genre(tid, 'My explicit genre')

    monkeypatch.setattr(enrichment, 'GENRE_POLICY_VERSION', enrichment.GENRE_POLICY_VERSION + 1)
    fresh = EnrichmentResult('musicbrainz', {'year': '1993'})
    original.probe.side_effect = [TimeoutError('temporarily offline'), fresh] if retry_first else [fresh]
    fallback.probe.return_value = EnrichmentResult('discogs', {'genre': replacement} if replacement else {})
    if retry_first:
        assert w.drain_once() == 0
        assert store.tracks.get_track_enrichment(tid) == ('My explicit genre' if explicit else 'Rock', '1993')
        # The alternative provider has already answered. A restarted worker must
        # reuse that answer when the selected provider finally rejects its old tag.
        w = worker(store, [original, fallback])
        w.ctx.now_fn = lambda: 2000
    assert w.drain_once() == 1
    assert store.tracks.get_track_enrichment(tid) == (
        'My explicit genre' if explicit else (replacement or 'Rock'), '1993')
    assert fallback.probe.call_count == 2
    assert store.enrichment.queue_remaining() == 0
