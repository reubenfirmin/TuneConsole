"""Saved discovery metadata queues background preparation with the established models."""
import logging
from contextlib import ExitStack, closing
from tests.enrichment_support import complete_entries, complete_discovered
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import FakeClient
from tests.playlist_fixture_support import remote_catalog
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader
from yt_playlist.core.store import Store
from yt_playlist.enrich.enrich_worker import EnrichWorker
from yt_playlist.library import playlist_generated
from yt_playlist.rec import content_model, embedding_training as training, embedding_vectors as embed, fresh_pool, mode_selection, mode_surfaces, model_refresh, recipes, surfaces
from yt_playlist.rec.scoring import PlaylistTaste
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key


@pytest.fixture(scope='module')
def enrichment_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno'), ('folk', 'Folk'))) as schema:
        yield schema


@pytest.fixture
def store(tmp_path, enrichment_schema, monkeypatch):
    database = Store(tmp_path / 'fresh-enrichment.db')
    with closing(database.conn):
        initialize_consumer(database, enrichment_schema)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with reuse_reporting_reader(database, monkeypatch):
            yield database


def _pool(store, surface, mode, lane):
    head = store.recommendations.head(surface)
    assert head is not None
    tracks, after = [], -1
    while True:
        page = store.recommendations.candidate_page(surface, mode, lane,
            generation=head['id'], after=after, limit=128)
        tracks.extend(page['tracks'])
        if page['exhausted']:
            return tracks
        after = page['after']


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
    iid = store.accounts.upsert_account("main", "cred", None, True)
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
        store.conn.execute("UPDATE youtube_items SET mb_year='2000'")
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
        a["audio"] = {"bpm": 172.0, "energy": .92, "danceability": .88}
        b["audio"] = {"bpm": 88.0, "energy": .18, "danceability": .20}

    iid = store.accounts.upsert_account("main", "cred", None, True)
    groups = []
    for group, fields in (("A", a), ("B", b)):
        # The owned library is completed input to the enrichment test. Keep
        # provider delivery and queue writes for the changed discoveries below.
        items = [dict(key=identity_key(f'{group}{i}', group), video_id=f'{group}{i}',
                      title=f'{group}{i}', artist=group, genre=fields['genre'],
                      year=fields['year'], duration_s=200) for i in range(7)]
        seed_publication_subjects(store, items)
        tids = list(store.tracks.track_ids_for_videos([item['video_id'] for item in items]).values())
        if fields['audio']:
            with store.conn:
                store.conn.executemany('UPDATE recordings SET bpm=?,energy=?,danceability=? WHERE id=?',
                    [(fields['audio']['bpm'], fields['audio']['energy'], fields['audio']['danceability'], tid)
                     for tid in tids])
        # Fourteen subjects retain the real eight-dimensional collaborative
        # fit and its minimum vocabulary margin, in two balanced clusters.
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

    _, content, cidx = embed.load_content_vectors(store)
    modes = []
    for mid, keys, fields in zip((1, 2), groups, (a, b)):
        centroid = content[[cidx[k] for k in keys]].mean(0)
        centroid /= np.linalg.norm(centroid)
        modes.append({"mode_id": mid, "label": f"Mode {mid}", "centroid": centroid,
                      "space": embed.content_space_id(store), "size": 10000 if mid == 1 else 10,
                      "families": [[genre_map.family(fields["genre"]), 10]], "rep_keys": keys[:4]})
    store.modes.replace_modes(modes, retired_ids=[], now=200)
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
            store.conn.executemany('UPDATE recordings SET bpm=?,energy=?,danceability=? WHERE identity_key=?',
                [(b['audio']['bpm'], b['audio']['energy'], b['audio']['danceability'], item['key'])
                 for item in discoveries])
    complete_discovered(store, reference, 200)
    return SimpleNamespace(account=iid, preferred=a, reference=reference, changed=changed,
                           discoveries=discoveries, model=store.settings.get_setting('rec_content_model'),
                           taste=taste)


@pytest.fixture(scope='module')
def enrichment_templates(enrichment_schema, tmp_path_factory):
    # Fit each distinct metadata comparison once. Consumers restore private
    # SQL, vector/profile files and mutable result values from that exact input.
    templates = {}
    root = tmp_path_factory.mktemp('enrichment-inputs')
    with ExitStack() as resources:
        def template(signal):
            if signal not in templates:
                def seed(store):
                    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch):
                        return _build_enrichment_inputs(store, signal)
                templates[signal] = resources.enter_context(prepared_template(
                    enrichment_schema, seed, database=root / f'{signal}.db'))
            return templates[signal]
        yield template


@pytest.mark.parametrize('signal', ['genre', 'year', 'audio'])
def test_enrichment_input_keeps_real_model_modes_discoveries_and_private_files(enrichment_templates, signal):
    template = enrichment_templates(signal)
    source, inputs = template.source, template.result
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 22
    assert len(source.rec_query.library_keys()) == 14
    assert set(source.discovery.discovered_tracks_by_keys(inputs.changed + inputs.reference)) == set(
        inputs.changed + inputs.reference)
    assert inputs.model and {mode['mode_id'] for mode in source.modes.list_modes()} == {1, 2}
    assert template.files and any(path.is_file() for root in template.files.values() for path in root.rglob('*'))
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def _prepared_enrichment_inputs(store, monkeypatch, signal, enrichment_templates):
    inputs = enrichment_templates(signal).restore(store)
    monkeypatch.setattr(surfaces, 'playlist_taste', lambda s, *, now: inputs.taste)
    return inputs


def _forbid_retraining(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('An enrichment update must reuse saved models and rankings')

    monkeypatch.setattr(training, 'build_vectors_from_baskets', forbidden)
    monkeypatch.setattr(content_model, 'build_library_content', forbidden)
    monkeypatch.setattr(fresh_pool, 'prepare', forbidden)


@pytest.mark.parametrize('signal', ['genre', 'year', 'audio'])
def test_enrichment_updates_metadata_and_queues_refresh_without_changing_published_order(
        store, monkeypatch, signal, enrichment_templates):
    inputs = _prepared_enrichment_inputs(store, monkeypatch, signal, enrichment_templates)
    a, reference = inputs.preferred, inputs.reference
    # The old, published order is input to this update. Its original ranking
    # pass belongs to Fresh preparation tests; keep real typed publication.
    previous = [{**item, 'year': int(item['year']), 'score': 1.0}
                for item in inputs.discoveries if item['key'] in reference]
    publish_bundles(store, {
        "1": {"wheelhouse": [], "explore": [], "comfort": [], "fresh": []},
        "2": {"wheelhouse": [], "explore": [], "comfort": [], "fresh": previous},
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
    assert store.settings.get_setting("rec_content_model") == inputs.model
    assert [t["key"] for t in fresh_pool.pick(store, 500, size=4)] == reference
    assert store.rec_refresh.has_pending_changes()
    saved = store.discovery.discovered_tracks_by_keys(inputs.changed)
    assert set(saved) == set(inputs.changed)
    assert all(row['genre'] == a['genre'] and row['year'] == a['year'] for row in saved.values())
    if a['audio']:
        audio = store.conn.execute('SELECT bpm,energy,danceability FROM recordings WHERE '
            'identity_key IN (' + ','.join('?' for _ in inputs.changed) + ')', inputs.changed).fetchall()
        assert len(audio) == len(inputs.changed)
        assert all(tuple(row) == tuple(a['audio'][field] for field in ('bpm', 'energy', 'danceability'))
                   for row in audio)


def _completed_enrichment(store, inputs):
    a, changed = inputs.preferred, inputs.changed
    # This owner consumes completed enrichment. The preceding owner verifies
    # actual provider writes, notification and preservation of the old menu.
    genre = store.genres.resolve(a['genre'])[0]
    with store.conn:
        store.conn.executemany('UPDATE recordings SET genre_key=? WHERE identity_key=?',
                               [(genre, key) for key in changed])
        store.conn.executemany('UPDATE youtube_items SET mb_year=? WHERE recording_id='
                               '(SELECT id FROM recordings WHERE identity_key=?)',
                               [(a['year'], key) for key in changed])
        if a['audio']:
            store.conn.executemany('UPDATE recordings SET bpm=?,energy=?,danceability=? WHERE identity_key=?',
                [(a['audio']['bpm'], a['audio']['energy'], a['audio']['danceability'], key) for key in changed])
    complete_discovered(store, changed, 500)


@pytest.mark.parametrize('signal', ['genre', 'year', 'audio'])
def test_background_metadata_refresh_reuses_encoder_and_reassigns_fresh_modes(
        store, monkeypatch, signal, enrichment_templates):
    inputs = _prepared_enrichment_inputs(store, monkeypatch, signal, enrichment_templates)
    changed, reference = inputs.changed, inputs.reference
    _completed_enrichment(store, inputs)
    _forbid_retraining(monkeypatch)
    # Enrichment only notifies the worker. The background pass reuses the library
    # encoder and original ranker before publishing the updated discovery pools.
    assert model_refresh.rebuild(store, 500) is not None
    assert mode_surfaces.prepare_bundles(store, 500) is not None
    assert store.settings.get_setting("rec_content_model") == inputs.model
    preferred_fresh = _pool(store, 'home', '1', 'fresh')
    reference_fresh = _pool(store, 'home', '2', 'fresh')
    assert {t["key"] for t in preferred_fresh} == set(changed)
    assert {t["key"] for t in reference_fresh} == set(reference)
    assert all(t["year"] == 2024 for t in preferred_fresh)
    if signal == "year":
        ordered = recipes.theme_filter(store, list(reversed(_pool(store, 'home', 'all', 'fresh'))), {"eras": ["2020"]})
        assert {t["key"] for t in ordered[:4]} == set(changed)


@pytest.mark.parametrize('signal', ['genre', 'year', 'audio'])
def test_enriched_fresh_cards_create_the_preferred_modes_generated_playlist(
        store, monkeypatch, signal, enrichment_templates):
    inputs = _prepared_enrichment_inputs(store, monkeypatch, signal, enrichment_templates)
    _completed_enrichment(store, inputs)
    _forbid_retraining(monkeypatch)
    changed = inputs.changed
    # Ranking and mode assignment have their own owner above. This consumer
    # reads a completed typed worker output and keeps real card/playlist writes.
    selected = [{**item, 'genre': inputs.preferred['genre'], 'year': 2024, 'score': 1.0}
                for item in inputs.discoveries if item['key'] in changed]
    publish_bundles(store, {
        '1': {'wheelhouse': [], 'explore': [], 'comfort': [], 'fresh': selected},
        'all': {'wheelhouse': [], 'explore': [], 'comfort': [], 'fresh': selected},
        '_meta': {'full_pools': True, 'comfort_pool': 0, 'year_cuts': None},
    }, 500, fresh=selected)
    # Focus this preview on the preferred mode; mode rotation is tested separately.
    monkeypatch.setattr(mode_selection, "select_modes", lambda *args, **kwargs: [1])
    cards = mode_surfaces.assemble_cards(store, 500, epoch=0)
    fresh = next(c for c in cards if c["lane"] == "fresh")
    assert {t["key"] for t in fresh["tracks"][:4]} == set(changed)
    picked = fresh["tracks"][:4]
    client = FakeClient(catalog=remote_catalog(picked))
    _use_completed_catalog(store, monkeypatch, picked)
    result = playlist_generated.create_generated_playlist(store, "Fresh songs", picked, client, 500, inputs.account,
                                                recipe={"model": "fresh"})
    assert result["added"] == 4
    assert set(client.added[0][1]) == {f"Changed{i}" for i in range(4)}
