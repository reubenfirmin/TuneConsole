"""Retry deadlines survive batches/restarts without starving other work or repeating successes."""
import io
import logging
import urllib.error
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator,
    native_sql_calls, without_query_timing,
)
from yt_playlist.core import migrations, store as store_module
from yt_playlist.core.store import Store
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.integrations.musicbrainz import MusicBrainzIAO
from yt_playlist.providers import musicbrainz
from yt_playlist.providers.base import EnrichmentResult
from yt_playlist.providers.waterfall import run_waterfall
from yt_playlist.util import net
from yt_playlist.util.matching import identity_key


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Enrichment retries do not construct the HTTP application lifecycle."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('techno', 'Techno'),)) as schema:
        yield schema


@pytest.fixture(scope='module')
def provider_retry_schema():
    # These sequential retries consume snapshot coordination. Snapshot races
    # have separate owners; the complete queue, provider evidence and files
    # below this worker still use private real SQL.
    with bounded_consumer_schema((('techno', 'Techno'),), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture(autouse=True)
def retry_vocabulary(monkeypatch):
    from tests.genre_catalog_support import bound_catalog

    # Keep the provider's real metadata writes and populated admission, with
    # only the genre used by these retry cases in the lower vocabulary input.
    bound_catalog(monkeypatch, entries=(('techno', 'Techno'),))


class Provider:
    def __init__(self, name, fail=None):
        self.name, self.fail, self.calls = name, fail, []

    def available(self, store): return True
    def tripped(self): return False

    def probe(self, track, store):
        self.calls.append(track["title"])
        if self.fail:
            self.fail(track)
        return EnrichmentResult(self.name, {"genre": "techno"})


def worker(store, clock, providers):
    ctx = SimpleNamespace(store=store, now_fn=lambda: clock[0], logger=logging.getLogger(__name__),
                          rec_worker=SimpleNamespace(trigger_content_update=lambda keys: None))
    return EnrichWorker(ctx, waterfall_fn=partial(run_waterfall,
                        registry={p.name: p for p in providers}))


def tracks(store, count):
    store.settings.set_setting("enrich_caught_up_at", "100")
    seed_publication_subjects(store, [dict(key=identity_key(f'Song {i}', 'Artist'),
        video_id=f'v{i}', title=f'Song {i}', artist='Artist', duration_s=200,
        created_at=10-i) for i in range(count)])
    return [row[0] for row in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id')]


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_more_failures_than_one_batch_do_not_starve_healthy_tracks_and_recovery_reuses_success(
        tmp_path, provider_retry_schema, monkeypatch):
    path = tmp_path / "retries.db"
    store = Store(path)
    store.conn.execute('PRAGMA synchronous=OFF')
    initialize_consumer(store, provider_retry_schema, snapshot_guards=False)
    store.conn.execute('PRAGMA wal_autocheckpoint=0')
    clock = [1000.0]
    def fail(track):
        if track["title"] in {f"Song {i}" for i in range(5)}:
            raise ValueError("malformed response")
    broken, healthy = Provider("musicbrainz", fail), Provider("deezer")
    try:
        with mock_snapshot_coordinator(store, monkeypatch):
            monkeypatch.setattr(store.enrichment_queue, 'read_snapshot', store.read_snapshot)
            ids = tracks(store, 7)
            w = worker(store, clock, [broken, healthy])
            for _ in range(4):
                w.drain_once(limit=2)
                clock[0] += 31
            assert {t["id"] for t in store.enrichment_reports.enriched_tracks()} == set(ids[5:])
            assert healthy.calls == [f"Song {i}" for i in range(7)]
            assert store.enrichment_reports.queue_remaining() == 5
            assert "malformed response" in w.activity["warning"]
            key = store.tracks.catalog_subject(ids[0])["identity_key"]
            assert store.enrichment_state.enrichment_attempts(key)["deezer"]["fields"] == {"genre": "techno"}
    finally:
        store.conn.close()
    # A new connection uses saved deadlines and successful provider evidence.
    store = Store(path)
    store.conn.execute('PRAGMA synchronous=OFF')
    store.conn.execute('PRAGMA wal_autocheckpoint=0')
    try:
        # Admission is an independent completed input. Keep the new connection,
        # real saved provider evidence, repair and continuing SQL writes.
        admitted = Mock(return_value=False)
        with monkeypatch.context() as startup:
            startup.setattr(migrations, 'migrate', admitted)
            startup.setattr(store_module, 'Revisions', lambda conn: None)
            store.init_schema()
        admitted.assert_called_once_with(store.conn, defer_validation=False)
        assert store._snapshot_revisions is None
        with mock_snapshot_coordinator(store, monkeypatch):
            monkeypatch.setattr(store.enrichment_queue, 'read_snapshot', store.read_snapshot)
            broken.fail = None
            w = worker(store, clock, [broken, healthy])
            clock[0] += 3600
            for _ in range(3):
                w.drain_once(limit=2)
            assert store._snapshot_revisions is not None
            assert store.enrichment_reports.queue_remaining() == 0
            assert len(healthy.calls) == 7
            assert len(store.enrichment_reports.enriched_tracks()) == 7
    finally:
        store.conn.close()


@pytest.mark.parametrize("retry_after", ["120", "Thu, 01 Jan 1970 00:18:40 GMT"])
def test_throttle_cooldown_honors_retry_after_across_batches(store, monkeypatch, retry_after):
    ids = tracks(store, 4)
    clock = [1000.0]
    requests = []

    def request(req, **kwargs):
        requests.append(req.full_url)
        if clock[0] < 1120:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down",
                                         {"Retry-After": retry_after}, None)
        return io.StringIO('{"recordings": []}')

    monkeypatch.setattr(musicbrainz, '_iao', MusicBrainzIAO(open_fn=request,
        now_fn=lambda: clock[0], monotonic_fn=lambda: clock[0],
        pacer=SimpleNamespace(wait=lambda: None)))
    healthy = Provider("deezer")
    w = worker(store, clock, [musicbrainz, healthy])
    assert w.drain_once(limit=4) == 0
    assert len(requests) == 1 and len(healthy.calls) == 4
    cooldown = store.enrichment_state.integration_cooldown('musicbrainz')
    assert cooldown["retry_at"] == 1120 and cooldown['failures'] == 1
    clock[0] = 1119
    # Enter the real waterfall again, even though the worker queue would suppress
    # this batch. Batch startup and deferred attempts cannot erase the outage.
    assert run_waterfall(store, [store.tracks.track_for_waterfall(tid) for tid in ids],
        [{'name': 'musicbrainz', 'enabled': True}], lambda event: None,
        registry={'musicbrainz': musicbrainz}, now_fn=lambda: clock[0]) == []
    assert store.enrichment_state.integration_cooldown('musicbrainz') == cooldown
    assert w.drain_once(limit=4) == 0
    assert len(requests) == 1
    assert store.enrichment_queue.next_enrich_batch(4, providers=["musicbrainz", "deezer"], now=1119) == []
    assert store.enrichment_reports.queue_remaining() == 4
    clock[0] = 1120
    assert w.drain_once(limit=4) == 4
    assert len(requests) == 5
    assert len(healthy.calls) == 4
    assert store.enrichment_state.enrichment_cooldowns() == {}


def test_backoff_is_bounded_and_partial_provider_evidence_is_retained(store):
    tid = tracks(store, 1)[0]
    track = store.tracks.catalog_subject(tid)
    for i in range(12):
        store.enrichment_state.record_enrichment_attempt(track["identity_key"], "musicbrainz",
            {"mb_recording_id": "mbid"} if i == 0 else {}, retry=True, now=10000*i,
            video_id=track["video_id"], error="invalid JSON")
        state = store.enrichment_state.enrichment_provider_state(track["recording_id"])["musicbrainz"]
        assert 30 <= state["retry_at"] - 10000*i <= 3600
    assert state["failures"] == 12
    assert state["fields"] == {"mb_recording_id": "mbid"}
    # A completed pass starts a new cycle on the next stale sweep.
    store.enrichment_queue.mark_recordings_enriched([track["recording_id"]], 200000)
    assert store.enrichment_state.enrichment_provider_state(track["recording_id"]) == {}


def test_retry_after_invalid_header_uses_default_backoff():
    with urllib.error.HTTPError("u", 503, "busy", {"Retry-After": "nonsense"}, None) as error:
        assert net.retry_after(error, 1000) == 0
