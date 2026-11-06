"""Preference refreshes reuse the established models and update saved ranking provenance."""
from contextlib import closing
import sqlite3
import time

import pytest

from tests.prepared_fixture_support import prepared_template, roots
from tests.candidate_timing_support import candidate_costs, candidate_prefix_costs
from tests.rec.test_model_refresh import refresh_costs, seed
from yt_playlist.core import embeddings, model_contract
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, embedding_training as embed, home_prepared, model_refresh, mode_surfaces, rec_params


def refresh(store, now=2000):
    assert model_refresh.rebuild(store, now) is not None
    assert mode_surfaces.prepare_bundles(store, now) is not None


def candidate(store, key):
    return next((row for row in pool(store, 'wheelhouse') if row['key'] == key), None)


def pool(store, lane):
    return store.recommendations.page('home', 'all', lane)


def library(store):
    seed(store)
    refresh(store)
    candidates = pool(store, 'wheelhouse')
    assert candidates
    return candidates[0]['key']


@pytest.fixture(scope='module')
def candidate_library_template(_pristine_current_schema, candidate_prefix_costs):
    """Only the initial real build; every later refresh remains a test subject."""
    started = time.perf_counter()
    with prepared_template(_pristine_current_schema, library) as template:
        candidate_prefix_costs['candidate_cold_template_seconds'] = time.perf_counter() - started
        yield template


def _assert_library_matches(store, original):
    assert len(store.rec_query.library_keys()) == 96
    for name in ('library_keys', 'track_content', 'track_audio_features', 'play_counts'):
        assert getattr(store.rec_query, name)() == getattr(original.rec_query, name)()
    assert store.rec_evidence.rec_baskets() == original.rec_evidence.rec_baskets()
    assert store.history.recent_plays_with_ts(limit=128) == original.history.recent_plays_with_ts(limit=128)
    for name in embeddings.SPACES:
        assert embeddings.rows(store.conn, name) == embeddings.rows(original.conn, name)
    for name in {*model_contract.FIELDS, 'rec_dim', 'rec_refresh_version', 'rec_mode_fit_inputs'} - {
            'rec_content_generation'}:
        assert store.settings.get_setting(name) == original.settings.get_setting(name)
    assert list(map(tuple, store.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id'))) == list(
        map(tuple, original.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id')))
    ranks = ('SELECT h.surface,m.mode_key,m.purpose,c.recording_id,c.rank,c.score,p.policy_version,'
             'b.intensity_band,b.genre_key FROM recommendation_heads h '
             'JOIN recommendation_surface_pools m ON m.generation_id=h.generation_id AND m.surface=h.surface '
             'JOIN recommendation_pools p ON p.id=m.pool_id '
             'JOIN recommendation_candidates c ON c.pool_id=p.id '
             'LEFT JOIN recommendation_radio_bands b ON b.pool_id=p.id AND b.recording_id=c.recording_id '
             'ORDER BY h.surface,m.mode_key,m.purpose,c.rank')
    assert list(map(tuple, store.conn.execute(ranks))) == list(map(tuple, original.conn.execute(ranks)))
    assert not store.rec_refresh.has_pending_changes()
    assert not candidate_refresh.due(store, 2000)
    assert store.conn.execute('PRAGMA foreign_key_check').fetchall() == []


def no_fit(*args, **kwargs):
    raise AssertionError('Preference or clock changes must reuse the completed model')


@pytest.mark.parametrize('projection', ['vibe', 'profile'])
def test_projection_upgrade_refreshes_legacy_pools_on_worker_without_refitting(
        store, monkeypatch, projection, candidate_library_template, file_backed_store, tmp_path):
    from yt_playlist.rec import home_vibe, musical_character

    template = candidate_library_template
    key = template.restore(store)
    if projection == 'vibe':
        # Keep one independent fresh producer and compare its full inputs/ranks.
        producer = Store(tmp_path / 'producer.db')
        with closing(producer.conn):
            producer.init_schema()
            assert library(producer) == key
            _assert_library_matches(store, producer)
        assert template.restore(file_backed_store) == key
        for current in (store, file_backed_store):
            _assert_library_matches(current, template.source)
            assert current.recommendations.heads() == template.source.recommendations.heads()
            assert current.rec_model.content_generation() == template.source.rec_model.content_generation()
        assert len({current._evidence_key for current in (template.source, store, file_backed_store)}) == 3
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            template.source.conn.execute("INSERT INTO settings VALUES ('private_candidate_input','forbidden')")
        destinations = [roots(current.conn) for current in (store, file_backed_store)]
        copied = 0
        for name, source in template.files.items():
            assert len({source, *(owned[name] for owned in destinations)}) == 3
            for path in source.rglob('*'):
                if path.is_file():
                    paths = [path, *(owned[name] / path.relative_to(source) for owned in destinations)]
                    assert all(private.read_bytes() == path.read_bytes() for private in paths)
                    assert len({(private.stat().st_dev, private.stat().st_ino) for private in paths}) == 3
                    copied += 1
        assert copied
        file_backed_store.rec_model.set_weight('genre:techno', 0.5, now=2000)
        assert file_backed_store.rec_model.weight_records()
        assert not store.rec_model.weight_records() and not template.source.rec_model.weight_records()
    vectors = store.rec_model.get_rec_vectors()
    previous = store.recommendations.head('home')['id']
    # A published generation and its preference stamp can be current before
    # an upgrade changes the projection used by the controls.
    assert not candidate_refresh.due(store, 2000)
    module = home_vibe if projection == 'vibe' else musical_character
    monkeypatch.setattr(module, 'VERSION', module.VERSION + '-upgrade')
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    assert candidate_refresh.due(store, 2000)

    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert store.recommendations.head('home')['id'] != previous
    refreshed = store.recommendations.framing('home')
    assert refreshed['vibe_version'] == home_vibe.VERSION
    assert refreshed['vibe_profile_version'] == musical_character.VERSION
    assert {row['surface'] for row in store.recommendations.heads()
            if row['surface'].startswith('vibe:')} == {'vibe:' + key for key in home_vibe.PRESETS}
    assert store.recommendations.page('vibe:relax', '', 'explore')
    assert not candidate_refresh.due(store, 2001)
    assert store.rec_model.get_rec_vectors() == vectors


def test_genre_edits_reset_and_restore_update_scores_and_served_provenance(
        store, monkeypatch, candidate_library_template):
    key = candidate_library_template.restore(store)
    original = candidate(store, key)
    genre = store.rec_query.track_genres([key])[key]
    from yt_playlist.util import genre_map
    axis = 'genre:' + genre_map.family(genre)
    vectors = store.rec_model.get_rec_vectors()
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    for weight in (0.5, 1.5, 0):
        store.rec_model.set_weight(axis, weight, lo=0, now=2000)
        assert candidate_refresh.due(store, 2000)
        refresh(store)
        row = candidate(store, key)
        if weight == 0:
            assert row is None
        else:
            assert (row['score'] < original['score']) == (weight < 1)
            policy = store.rec_ledger.policy_configuration(row['pool_policy'])
            assert policy['weights'][axis] == weight
        assert store.rec_model.get_rec_vectors() == vectors
    store.rec_model.reset_weights()
    refresh(store)
    assert candidate(store, key)['score'] == original['score']
    store.rec_model.set_weight(axis, 0.5, now=2000)
    store.rec_model.save_taste_settings('taste_settings_checkpoint_v1', [], 2000)
    refresh(store)
    saved_score = candidate(store, key)['score']
    store.rec_model.set_weight(axis, 1.5, now=2000)
    refresh(store)
    assert store.rec_model.restore_taste_settings('taste_settings_checkpoint_v1', [])
    refresh(store)
    assert candidate(store, key)['score'] == saved_score


def test_decay_and_half_life_changes_refresh_without_authoritative_edits(
        store, monkeypatch, candidate_library_template):
    key = candidate_library_template.restore(store)
    store.rec_model.set_weight('genre:techno', 0.5, now=2000)
    refresh(store)
    before = home_prepared.tuning_key(store, 2000)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    now = 2000 + 60 * 86400
    assert candidate_refresh.due(store, now)
    assert home_prepared.tuning_key(store, now) != before
    refresh(store, now)
    assert not candidate_refresh.due(store, now + 1)
    assert candidate_refresh.due(store, now + candidate_refresh.DECAY_TICK_S)
    rec_params.set_param(store, 'weight_revert_halflife_d', 30)
    assert candidate_refresh.due(store, now)
    refresh(store, now)
    assert not candidate_refresh.due(store, now + 1)


def test_completed_ranking_retains_captured_preferences_and_leaves_later_edits_due(
        store, monkeypatch, candidate_library_template):
    candidate_library_template.restore(store)
    store.rec_model.set_weight('genre:techno', 0.5, now=2000)
    original = mode_surfaces._calculate_bundles
    def superseded(reader, now, policies):
        result = original(reader, now, policies)
        store.rec_model.set_weight('genre:techno', 1.5, now=now)
        return result
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', superseded)
    before = store.recommendations.head('home')['id']
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert store.recommendations.head('home')['id'] != before
    policy = store.rec_ledger.policy_configuration(store.recommendations.head('home')['policy_version'])
    assert policy['weights']['genre:techno'] == .5
    assert candidate_refresh.due(store, 2000)


def test_taste_controls_rerank_saved_pools_after_edits_resets_and_checkpoint_restore(store, monkeypatch):
    from tests.taste_support import _client

    seed(store)
    iid = store.accounts.get_accounts()[0].id
    store.history.import_play_events(iid, [
        (store.tracks.identity_key_for_video(f'v{i}'), f'v{i}', 1000 - day * 86400)
        for i in range(8) for day in range(1, 6)])
    refresh(store)
    client = _client(store, now=2000)
    worker = client.app.state.ctx.rec_worker
    requests = []

    def rerank(**kwargs):
        requests.append(kwargs)
        assert kwargs == {'full': False, 'bundles': True}
        assert candidate_refresh.due(store, 2000)
        assert mode_surfaces.prepare_bundles(store, 2000) is not None

    monkeypatch.setattr(worker, '_schedule', rerank)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    vectors = store.rec_model.get_rec_vectors()

    def served(surface):
        return pool(store, surface)

    original = served('wheelhouse')
    assert served('comfort')
    tuning = home_prepared.tuning_key(store, 2000)
    assert client.post('/taste/checkpoint').status_code == 200
    assert client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12}).status_code == 200
    assert served('comfort') == []
    assert home_prepared.tuning_key(store, 2000) != tuning
    assert client.post('/taste/reset-param', data={'name': 'comfort_min_plays'}).status_code == 200
    assert served('comfort')
    assert client.post('/taste/weight', data={'axis': 'lane:deep_cut', 'weight': 3}).status_code == 200
    changed = served('wheelhouse')
    assert [r['key'] for r in changed] != [r['key'] for r in original]
    policy = store.rec_ledger.policy_configuration(changed[0]['pool_policy'])
    assert policy['weights']['lane:deep_cut'] == 3
    assert policy['params']['comfort_min_plays'] == 4
    assert client.post('/taste/reset-weights').status_code == 200
    assert served('wheelhouse') == original
    client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12})
    client.post('/taste/weight', data={'axis': 'lane:deep_cut', 'weight': 3})
    assert client.post('/taste/checkpoint/restore').status_code == 200
    assert served('wheelhouse') == original
    assert served('comfort')
    client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12})
    assert client.post('/taste/reset-all').status_code == 200
    assert served('comfort')
    assert len(requests) == 9
    assert store.rec_model.get_rec_vectors() == vectors
    assert not candidate_refresh.due(store, 2000)


def test_lane_decay_invalidates_prepared_rankings(store, monkeypatch, candidate_library_template):
    candidate_library_template.restore(store)
    store.rec_model.set_weight('lane:deep_cut', 3, now=2000)
    refresh(store)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    now = 2000 + candidate_refresh.DECAY_TICK_S
    assert candidate_refresh.due(store, now)
    refresh(store, now)
    assert not candidate_refresh.due(store, now)
