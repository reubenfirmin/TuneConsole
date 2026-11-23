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
from tests.metadata_provenance_support import metadata_provenance


class Provider:
    def __init__(self, name, available=lambda store: True):
        self.name, self.available, self.calls = name, available, []

    def tripped(self): return False
    def probe(self, track, store):
        self.calls.append(track["id"])
        return EnrichmentResult(self.name, {"genre": "house", "year": "2024", "bpm": 120,
                                          "danceability": .7})


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
    assert store.enrichment_state.provider_coverage(recording) == {"musicbrainz":"found", "deezer":"disabled"}
    store.tracks.set_track_genre(tid, "Selected genre")
    assert w.drain_once() == 0
    config(store, {"musicbrainz", "deezer"})
    store.enrichment_state.reconcile_provider_configuration(enrichment.provider_configuration(store, {a.name:a, b.name:b}))
    assert store.enrichment_reports.queue_remaining() == 1
    assert store.enrichment_reports.coverage_stats()["processed"] == 0
    assert store.enrichment_state.provider_coverage(recording)["deezer"] == "pending"
    assert w.drain_once() == 1
    assert a.calls == [tid] and b.calls == [tid]
    assert store.tracks.get_track_enrichment(tid) == ("Selected genre", "2024")
    assert store.enrichment_reports.queue_remaining() == 0
    assert store.enrichment_reports.coverage_stats()["processed"] == 1
    assert w.drain_once() == 0
    statuses = {r["provider"]:r["status"] for r in store.conn.execute("SELECT * FROM enrichment_provider_config")}
    assert statuses == {"musicbrainz":"available", "deezer":"available"}


def test_reenrichment_keeps_genre_until_later_provider_replacement(store):
    tid = store.tracks.upsert_track('song', 'Song', 'Artist', None, 200)
    order = ['musicbrainz', 'discogs']
    store.enrichment.apply_provider_fields(tid, 'musicbrainz', {'genre': 'Rock'}, providers=order)
    recording = store.conn.execute('SELECT recording_id FROM track_sources WHERE id=?', (tid,)).fetchone()[0]
    with store.conn:
        store.conn.executemany('INSERT INTO enrichment_provider_state '
                               "(recording_id,provider,cycle,at,status,failures,retry_at,error,queued) "
                               "VALUES (?,?,-1,0,'queued',0,0,'',1)",
                               [(recording, name) for name in order])
    store.enrichment_state.record_enrichment_attempt('song|artist', 'musicbrainz', {}, providers=order)
    assert store.tracks.get_track_enrichment(tid)[0] == 'Rock'
    assert metadata_provenance(store, tid)['fields']['genre']['source'] == 'musicbrainz'
    # A subsequent process can replace it using completed answers, without an empty interval.
    store.enrichment.apply_provider_fields(tid, 'discogs', {'genre': 'Soundtrack'}, providers=order)
    store.enrichment_state.record_enrichment_attempt('song|artist', 'discogs', {'genre': 'Soundtrack'}, providers=order)
    assert store.tracks.get_track_enrichment(tid)[0] == 'Soundtrack'
    assert metadata_provenance(store, tid)['fields']['genre']['source'] == 'discogs'


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


def test_saving_lastfm_key_reenriches_the_existing_catalog_across_batches_and_restart(store, monkeypatch):
    monkeypatch.setattr(lastfm, 'api_key', lambda source: source.settings.get_setting('lastfm_api_key') or None)
    owned = [store.tracks.upsert_track(f'owned-{i}', f'Owned {i}', 'Artist', None, 200) for i in range(2)]
    discovered_key = store.discovery.upsert_discovered_track(
        'discovered|artist', 'discovered', 'Discovered', 'Artist', None, None, None, None, 'radio:seed', 1)
    discovered = store.discovery.discovered_tracks_by_keys([discovered_key])[discovered_key]['id']
    entries = set(owned + [discovered])
    original = Provider('musicbrainz')
    added = Provider('lastfm', available=lambda source: lastfm.api_key(source) is not None)
    added.api_key = lastfm.api_key
    config(store, {'musicbrainz', 'lastfm'})
    w = worker(store, [original, added])
    assert w.drain_once() == len(entries)
    assert set(original.calls) == entries and not added.calls
    assert store.enrichment_reports.queue_remaining() == 0
    store.tracks.set_track_genre(owned[0], 'My explicit genre')
    subjects = {entry: store.tracks.catalog_subject(entry)['recording_id'] for entry in entries}
    before = {row['id']: row['first_enriched_at'] for row in store.conn.execute(
        'SELECT id,first_enriched_at FROM recordings')}

    client = _client(store)
    client.app.state.ctx.enrich_worker = w
    assert client.post('/settings/lastfm-key', data={'key': ' new-lastfm-key '}).status_code == 204
    assert w._wake.is_set()
    assert store.settings.get_setting('lastfm_api_key') == 'new-lastfm-key'
    assert store.tracks.get_track_enrichment(owned[0])[0] == 'My explicit genre'
    assert w.drain_once(limit=1) == 1
    assert store.enrichment_reports.queue_remaining() == len(entries) - 1

    # Persisted provider jobs resume without replaying completed recordings or
    # querying the original provider again, even when all metadata was populated.
    w = worker(store, [original, added])
    for _ in range(len(entries) - 1):
        assert w.drain_once(limit=1) == 1
    assert w.drain_once(limit=1) == 0
    assert set(added.calls) == entries and len(added.calls) == len(entries)
    assert set(original.calls) == entries and len(original.calls) == len(entries)
    assert all(store.enrichment_state.provider_coverage(recording)['lastfm'] == 'found'
               for recording in subjects.values())
    assert store.enrichment_reports.queue_remaining() == 0
    assert {row['id']: row['first_enriched_at'] for row in store.conn.execute(
        'SELECT id,first_enriched_at FROM recordings')} == before
    assert store.tracks.get_track_enrichment(owned[0])[0] == 'My explicit genre'
    assert w.ctx.rec_worker.trigger_content_update.call_count == len(entries) - 1


@pytest.mark.parametrize('newer_status', [None, 'found', 'retry'])
def test_provider_refresh_retains_durable_evidence_and_newer_state(store, newer_status):
    from yt_playlist.core import metadata_attempts

    tid = store.tracks.upsert_track('refresh', 'Song', 'Artist', None, 200)
    subject = store.tracks.catalog_subject(tid)
    recording = subject['recording_id']
    repo = store.enrichment_state
    repo._configure_providers({'a': {'fingerprint': 'first', 'status': 'available'}})
    for provider, genre in [('a', 'Jazz'), ('b', 'Rock')]:
        store.enrichment.apply_provider_fields(tid, provider, {'genre': genre}, providers=('a', 'b'))
        repo.record_enrichment_attempt(subject['identity_key'], provider,
                                       {'genre': genre, 'bpm': 120}, now=10, run_id=provider + '-old')
    store.tracks.set_track_genre(tid, 'My explicit choice')
    with store.conn:
        store.conn.execute('UPDATE recordings SET first_enriched_at=10 WHERE id=?', (recording,))
    current = {'fingerprint': 'second', 'status': 'available'}
    repo._configure_providers({'a': current})
    if newer_status is not None:
        repo.record_enrichment_attempt(subject['identity_key'], 'a', {'bpm': 130}, now=20,
            retry=newer_status == 'retry', run_id='a-new')
        if newer_status == 'retry':
            repo.defer_integration('a', now=20, retry_after=50, error='new outage')
    protected = ('metadata_deliveries', 'metadata_observations',
                 'current_metadata_selections', 'metadata_conflicts',
                 'provider_attempts')

    def retained():
        return {**{table: list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY 1,2')))
                   for table in protected},
                'choices': list(map(tuple, store.conn.execute('SELECT id,choice_observations FROM track_sources ORDER BY id')))}

    before = retained()
    states = repo.enrichment_provider_state(recording, include_previous=True)
    cooldowns = repo.enrichment_cooldowns()
    assert repo._advance_provider_refresh('a', current)
    assert not repo._advance_provider_refresh('a', current)
    assert retained() == before
    assert store.tracks.get_track_enrichment(tid)[0] == 'My explicit choice'
    after = repo.enrichment_provider_state(recording, include_previous=True)
    assert after['b'] == states['b']
    if newer_status is None:
        assert 'a' not in after
        assert metadata_attempts.state_fields(store.conn, recording, 'a') == {}
    else:
        assert after['a'] == states['a']
    assert repo.enrichment_cooldowns() == cooldowns
    queued = store.conn.execute('SELECT provider FROM enrichment_provider_state WHERE recording_id=? AND queued=1',
                                 (recording,)).fetchall()
    assert [row[0] for row in queued] == ([] if newer_status == 'found' else ['a'])
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_provider_refresh_rolls_back_operational_fields_state_jobs_and_frontier_together(store):
    import sqlite3

    tid = store.tracks.upsert_track('rollback', 'Rollback', 'Artist', None, 200)
    subject = store.tracks.catalog_subject(tid)
    repo = store.enrichment_state
    repo._configure_providers({'a': {'fingerprint': 'first', 'status': 'available'}})
    repo.record_enrichment_attempt(subject['identity_key'], 'a', {'genre': 'Jazz'}, now=10)
    current = {'fingerprint': 'second', 'status': 'available'}
    repo._configure_providers({'a': current})
    with store.conn:
        store.conn.execute("CREATE TRIGGER interrupt_refresh BEFORE DELETE ON enrichment_provider_state "
                           "BEGIN SELECT RAISE(ABORT,'Controlled refresh interruption'); END")
    before = list(store.conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='Controlled refresh interruption'):
        repo._advance_provider_refresh('a', current)
    assert list(store.conn.iterdump()) == before
    with store.conn:
        store.conn.execute('DROP TRIGGER interrupt_refresh')
    assert repo._advance_provider_refresh('a', current)
    assert not store.conn.execute("SELECT 1 FROM enrichment_provider_state WHERE field_links<>'{}'").fetchone()
    assert not store.conn.execute("SELECT 1 FROM enrichment_provider_state WHERE status<>'queued'").fetchone()


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
    store.enrichment_state.record_enrichment_attempt(track["identity_key"], "deezer", {}, retry=True, now=1000,
                                    error="timeout")
    assert store.enrichment_state.enrichment_retry_status()["count"] == 1
    config(store, set())
    store.enrichment_state.reconcile_provider_configuration(enrichment.provider_configuration(store, {"deezer":Provider("deezer")}))
    assert store.enrichment_state.enrichment_retry_status()["count"] == 0
    assert store.enrichment_state.provider_coverage(track["recording_id"])["deezer"] == "disabled"
    assert store.enrichment_state.enrichment_attempts(track["identity_key"])["deezer"]["status"] == "retry"


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
    assert store.enrichment_reports.queue_remaining() == 2
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
    assert store.enrichment_reports.queue_remaining() == 0
