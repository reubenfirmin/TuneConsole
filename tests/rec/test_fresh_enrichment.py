"""Saved discovery metadata queues background preparation with the established models."""
import logging
from contextlib import closing
from tests.enrichment_support import complete_entries, complete_discovered
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import FakeClient
from tests.playlist_fixture_support import remote_catalog
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator,
    native_sql_calls, without_query_timing,
)
from yt_playlist.core.store import Store
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.library import playlist_generated
from yt_playlist.rec import (content_model, embedding_training as training, embedding_vectors as embed,
                             fresh_pool, home_cards, home_prepared, mode_surfaces, model_refresh,
                             recipes, surfaces, taste_analysis, vibe_pools)
from yt_playlist.rec.scoring import PlaylistTaste
from yt_playlist.util.matching import identity_key


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """This workflow does not construct the HTTP application."""


@pytest.fixture(scope='module')
def enrichment_schema():
    with bounded_consumer_schema(
            (('house', 'House'), ('techno', 'Techno'), ('folk', 'Folk')), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def store(tmp_path, enrichment_schema, monkeypatch):
    database = Store(tmp_path / 'fresh-enrichment.db')
    with closing(database.conn):
        # The SQL input is disposable and snapshot coordination is mocked.
        # These cases still publish and read actual recommendation files.
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, enrichment_schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with mock_snapshot_coordinator(database, monkeypatch):
            monkeypatch.setattr(database.enrichment_queue, 'read_snapshot', database.read_snapshot)
            yield database


def _pool(store, surface, mode, lane):
    head = store.recommendations.head(surface)
    assert head is not None
    slot = (surface, head['id'], mode, lane)
    candidates, _ = store.recommendations.complete_candidates([slot])
    return candidates.get(slot, [])


def _use_completed_catalog(store, monkeypatch, tracks):
    entries = store.tracks.track_ids_for_videos([track['video_id'] for track in tracks])

    def already_ingested(video, title='', artist='', album=None, duration_s=None, **kwargs):
        assert video in entries, video
        return entries[video]

    # Retain remote confirmation and generated-playlist membership publication;
    # metadata ingestion has its own controller tests.
    monkeypatch.setattr(store.tracks, 'upsert_track', already_ingested)


@pytest.mark.parametrize("with_modes", [False, True])
def test_fresh_waits_for_metadata_then_generates_a_playlist_without_a_model(store, monkeypatch, with_modes):
    iid = store.accounts.upsert_account("main", None, True)
    if with_modes:
        store.modes.replace_modes([
            {"mode_id": 1, "label": "House", "families": [["house", 1]],
             "centroid": np.array([1., 0.], dtype=np.float32), "size": 50, "rep_keys": []},
        ], retired_ids=[], now=1)
    tracks = [dict(key=identity_key(f'Unenriched {i}', f'Unknown artist {i}'),
                   title=f'Unenriched {i}', artist=f'Unknown artist {i}', video_id=f'new{i}')
              for i in range(12)]
    keys = [track['key'] for track in tracks]
    seed_publication_subjects(store, tracks, discoveries=keys, now=100)

    # Discovery alone supplies no ranking evidence. Keep the tracks queued for enrichment.
    mode_surfaces.prepare_bundles(store, 200)
    assert fresh_pool.pick(store, 200) == []
    pending = store.discovery.get_discovered_tracks()
    assert {r["identity_key"] for r in pending} == set(keys)
    assert all(r["last_enriched"] is None for r in pending)

    # Metadata makes an unscored fallback eligible even before a taste/artist model exists.
    with store.conn:
        store.conn.execute("UPDATE track_sources SET mb_year='2000'")
    fresh_pool.prepare(store, 250)
    picked = fresh_pool.pick(store, 250)
    assert {t["key"] for t in picked} == set(keys)
    assert all(not t["genre"] and t["year"] == 2000 and t["score"] is None for t in picked)
    # Unknown genres remain unknown; they must not pretend to satisfy an explicit genre request.
    assert fresh_pool.pick(store, 250, include_genres=["house"]) == []

    client = FakeClient(catalog=remote_catalog(picked))
    _use_completed_catalog(store, monkeypatch, picked)
    result = playlist_generated.create_generated_playlist(store, "Fresh songs", picked, client, 250, iid,
                                                recipe={"model": "fresh"})
    assert result["added"] == 12
    assert set(client.added[0][1]) == {f"new{i}" for i in range(12)}


def _build_enrichment_inputs(store, signal):
    # Fit the real projection on two playlist clusters. Only the named metadata field
    # separates them, so a placement change cannot come from another signal.
    a = {"genre": "Techno", "year": "2024", "audio": {}}
    b = dict(a)
    if signal == "genre":
        b["genre"] = "Folk"
    elif signal == "year":
        b["year"] = "1994"
    else:
        a["audio"] = {"bpm": 172.0, "danceability": .88}
        b["audio"] = {"bpm": 88.0, "danceability": .20}

    iid = store.accounts.upsert_account("main", None, True)
    groups = []
    for group, fields in (("A", a), ("B", b)):
        # The owned library is completed input to the enrichment test. Keep
        # provider delivery and queue writes for the changed discoveries below.
        items = [dict(key=identity_key(f'{group}{i}', group), video_id=f'{group}{i}',
                      title=f'{group}{i}', artist=group, genre=fields['genre'],
                      year=fields['year'], duration_s=200) for i in range(10)]
        seed_publication_subjects(store, items)
        tids = list(store.tracks.track_ids_for_videos([item['video_id'] for item in items]).values())
        if fields['audio']:
            with store.conn:
                store.conn.executemany('UPDATE recordings SET bpm=?,danceability=? WHERE id=?',
                    [(fields['audio']['bpm'], fields['audio']['danceability'], tid)
                     for tid in tids])
        # Preserve both original ten-track clusters for the real
        # eight-dimensional collaborative fit.
        pid = store.playlists.upsert_playlist(iid, group, group, len(items), group, 1)
        store.playlists.set_playlist_tracks(pid, tids)
        complete_entries(store, tids, 200)
        groups.append(sorted(store.playlists.get_playlist_track_keys(pid)))
    store.settings.set_setting("rec_dim", "8")
    assert model_refresh.rebuild(store, 200) is not None
    _, vectors, idx = embed.load_vectors(store)
    preferred = vectors[[idx[k] for k in groups[0]]].mean(0)
    preferred /= np.linalg.norm(preferred)
    taste = PlaylistTaste(["Preferred playlist"], preferred[None, :], np.array([1.0]))

    reference, changed = [], []
    discoveries = []
    for prefix, target in (("Reference", reference), ("Changed", changed)):
        for i in range(4):
            title, artist = f"{prefix} {i}", f"New {prefix} artist {i}"
            key = identity_key(title, artist)
            discoveries.append(dict(key=key, video_id=f'{prefix}{i}', title=title,
                                    artist=artist, genre=b['genre'], year=b['year']))
            target.append(key)
    seed_publication_subjects(store, discoveries, discoveries=[item['key'] for item in discoveries], now=100)
    if b['audio']:
        with store.conn:
            store.conn.executemany('UPDATE recordings SET bpm=?,danceability=? WHERE identity_key=?',
                [(b['audio']['bpm'], b['audio']['danceability'], item['key'])
                 for item in discoveries])
    complete_discovered(store, reference, 200)
    return SimpleNamespace(account=iid, preferred=a, reference=reference, changed=changed,
                           discoveries=discoveries, model=store.rec_model.recording_encoder(),
                           taste=taste)


@pytest.fixture(autouse=True)
def unrelated_recommendation_inputs(monkeypatch):
    for name in ('for_you', 'explore_for_you', 'comfort_listening'):
        monkeypatch.setattr(surfaces, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(vibe_pools, 'prepare', lambda *args, **kwargs: None)


def _forbid_retraining(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('An enrichment update must reuse saved models and rankings')

    monkeypatch.setattr(training, 'build_vectors_from_baskets', forbidden)
    monkeypatch.setattr(content_model, 'build_library_content', forbidden)
    monkeypatch.setattr(fresh_pool, 'prepare', forbidden)


@pytest.mark.parametrize('signal', ['genre', 'year', 'audio'])
def test_enrichment_updates_fresh_ranks_and_generated_playlist(store, monkeypatch, signal):
    inputs = _build_enrichment_inputs(store, signal)
    monkeypatch.setattr(surfaces, 'playlist_taste', lambda s, *, now: inputs.taste)
    assert store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 28
    assert len(store.rec_query.library_keys()) == 20
    assert set(store.discovery.discovered_tracks_by_keys(inputs.changed + inputs.reference)) == set(
        inputs.changed + inputs.reference)
    assert inputs.model
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    a, reference = inputs.preferred, inputs.reference
    # The old, published order is input to this update. Its original ranking
    # pass belongs to Fresh preparation tests; keep real typed publication.
    previous = [{**item, 'year': int(item['year']), 'score': 1.0}
                for item in inputs.discoveries if item['key'] in reference]
    publish_bundles(store, {
        "all": {"wheelhouse": [], "explore": [], "comfort": [], "fresh": previous},
        "_meta": {"full_pools": True, "comfort_pool": 0, "year_cuts": None},
    }, 200, fresh=previous)
    assert [t["key"] for t in fresh_pool.pick(store, 200, size=4)] == reference
    old_order = [t["key"] for t in _pool(store, 'fresh', 'all', 'fresh')]
    _forbid_retraining(monkeypatch)

    def waterfall(s, batch, cfg, on_progress, should_stop=None, sink_for=None, **kwargs):
        for track in batch:
            sink = sink_for(track)
            sink.apply_fields("musicbrainz", {"genre": a["genre"], "year": a["year"]})
            if a["audio"]:
                sink.apply_fields("acousticbrainz", a["audio"])
        return [track['job_id'] for track in batch]

    worker = EnrichWorker(SimpleNamespace(store=store, now_fn=lambda: 500,
                                         logger=logging.getLogger(__name__)), waterfall_fn=waterfall)
    assert worker.drain_once(limit=4) == 4
    assert [t["key"] for t in _pool(store, 'fresh', 'all', 'fresh')] == old_order
    assert store.rec_model.recording_encoder() == inputs.model
    assert [t["key"] for t in fresh_pool.pick(store, 500, size=4)] == reference
    assert store.rec_refresh.has_pending_changes()
    saved = store.discovery.discovered_tracks_by_keys(inputs.changed)
    assert set(saved) == set(inputs.changed)
    assert all(row['genre'] == a['genre'] and row['year'] == a['year'] for row in saved.values())
    if a['audio']:
        audio = store.conn.execute('SELECT bpm,danceability FROM recordings WHERE '
            'identity_key IN (' + ','.join('?' for _ in inputs.changed) + ')', inputs.changed).fetchall()
        assert len(audio) == len(inputs.changed)
        assert all(tuple(row) == tuple(a['audio'][field] for field in ('bpm', 'danceability'))
                  for row in audio)
    changed, reference = inputs.changed, inputs.reference
    # Enrichment only notifies the worker. The background pass reuses the library
    # encoder and original ranker before publishing the updated discovery pools.
    assert model_refresh.rebuild(store, 500) is not None
    assert mode_surfaces.prepare_bundles(store, 500) is not None
    assert store.rec_model.recording_encoder() == inputs.model
    ranked = _pool(store, 'home', 'all', 'fresh')
    assert {t['key'] for t in ranked} == set(changed + reference)
    assert all(t['year'] == 2024 for t in ranked if t['key'] in changed)
    if signal == "year":
        ordered = recipes.theme_filter(store, list(reversed(ranked)), {'eras': ['2020']})
        assert {t["key"] for t in ordered[:4]} == set(changed)
    service = home_cards.HomeCards.live(store, home_prepared._NoJobs(), logging.getLogger(__name__),
                                        taste_analysis.library_summary(store))
    cards = service.cards(home_cards.CardsRequest(500, record=False))
    fresh = next(card for card in cards.protos if card['lane'] == 'fresh')
    picked = [track for track in fresh['tracks'] if track['key'] in changed]
    assert {track['key'] for track in picked} == set(changed)
    client = FakeClient(catalog=remote_catalog(picked))
    _use_completed_catalog(store, monkeypatch, picked)
    result = playlist_generated.create_generated_playlist(store, "Fresh songs", picked, client, 500, inputs.account,
                                                recipe={"model": "fresh"})
    assert result["added"] == 4
    assert set(client.added[0][1]) == {f"Changed{i}" for i in range(4)}
