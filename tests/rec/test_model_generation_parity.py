"""Real numerical and ranked-output parity, with one owned generation journey."""
from contextlib import closing
from copy import deepcopy

import numpy as np
import pytest

from tests import model_support
from tests.genre_catalog_support import bound_catalog
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import model_refresh, mode_surfaces, rec_params


GENRES = (('techno', 'Techno'), ('jazz', 'Jazz'), ('rock', 'Rock'))
SPACES = ('rec_vectors', 'rec_content_vectors', 'rec_artist_vectors', 'rec_artist_content_vectors')


def seed_completed_catalog(store):
    items = [{'key': f'song {i:03}|artist {i}', 'video_id': f'v{i}',
              'title': f'Song {i:03}', 'artist': f'Artist {i}', 'album': 'Album',
              'genre': GENRES[i * 3 // 80][1]} for i in range(80)]
    seed_publication_subjects(store, items)
    with atomic_write(store):
        # These completed native inputs sit below the numerical-model owner.
        # Provider reconciliation and artist inference have their own owners.
        store.conn.executemany('INSERT INTO artists(id,name,provisional) VALUES (?,?,1)',
                               [(i + 1, item['artist']) for i, item in enumerate(items)])
        store.conn.executemany("INSERT INTO artist_aliases VALUES (?,?,'fixture')",
                               [(i + 1, item['artist'].lower()) for i, item in enumerate(items)])
        store.conn.executemany("INSERT INTO entry_artists VALUES (?,0,?,?,'fixture')",
                               [(i + 1, i + 1, item['artist']) for i, item in enumerate(items)])
        store.conn.executemany('UPDATE recordings SET bpm=?,energy=? WHERE id=?',
                               [(80 + i, (i % 10) / 10, i + 1) for i in range(80)])
        account = store.accounts.upsert_account('Listener', 'fixture', None, True)
        tids = [i + 1 for i in range(80)]
        for i in range(8):
            playlist = store.playlists.upsert_playlist(account, f'p{i}', f'Playlist {i}', 20, '', 1)
            store.playlists.set_playlist_tracks(playlist, (tids + tids)[i * 10:i * 10 + 20])
        store.settings.set_setting('rec_dim', '16')
        rec_params.set_param(store, 'modes_k', 3)
        rec_params.set_param(store, 'modes_min_members', 8)
    store.history.import_play_events(account, [(items[i]['key'], f'v{i}', 1000 + i) for i in range(8)])
    return tids


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('model-parity-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch, entries=GENRES)
        yield


@pytest.fixture(scope='module')
def parity_schema(isolated_app_home):
    with bounded_consumer_schema(GENRES) as schema:
        yield schema


def test_consumer_schema_keeps_real_revision_guards_and_foreign_keys(parity_schema):
    assert parity_schema.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert parity_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert parity_schema._revision_script


@pytest.fixture(scope='module', params=['svd', 'item2vec'])
def parity(request, tmp_path_factory, parity_schema):
    store = Store(tmp_path_factory.mktemp('model-parity') / 'models.db')
    with closing(store.conn):
        initialize_consumer(store, parity_schema)
        # Numerical equality and committed generation visibility are the
        # subjects here; this fixture does not simulate power loss.
        store.conn.execute('PRAGMA synchronous=OFF')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        tids = seed_completed_catalog(store)
        store.settings.set_setting('rec_embed_method', request.param)
        yield {'store': store, 'method': request.param, 'tids': tids}


def test_catalog_keeps_eighty_tracks_three_genres_and_eight_overlapping_baskets(parity):
    store = parity['store']
    assert len(parity['tids']) == len(store.rec_query.library_keys()) == 80
    assert store.rec_query.track_audio_features() == {
        f'song {i:03}|artist {i}': {'bpm': 80 + i, 'energy': (i % 10) / 10} for i in range(80)}
    assert {genre for genre, _ in store.rec_query.track_content().values()} == {'Techno', 'Jazz', 'Rock'}
    assert store.conn.execute('SELECT count(*) FROM playlists').fetchone()[0] == 8
    assert [row[0] for row in store.conn.execute(
        'SELECT count(*) FROM playlist_items GROUP BY playlist_id ORDER BY playlist_id')] == [20] * 8
    references = store.artists.track_keys(library=True)
    assert len(references) == len(set().union(*references.values())) == 80
    assert len(store.rec_query.play_counts()) == 8
    assert not store.recommendations.heads()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def established_tracks(parity):
    assert model_support.build_track_model(parity['store']) == 80
    return parity


def test_established_track_fit_has_real_sixteen_dimensional_vectors(established_tracks):
    rows = established_tracks['store'].rec_model.get_rec_vectors()
    assert len(rows) == 80
    assert all(np.frombuffer(blob, np.float32).shape == (16,) for _, blob in rows)


@pytest.fixture(scope='module')
def established_content(established_tracks):
    assert model_support.build_content_model(established_tracks['store']) == 80
    return established_tracks


def test_established_content_fit_is_coherent_and_covers_the_whole_library(established_content):
    generation = established_content['store'].rec_model.content_generation()
    assert generation['coherent'] and generation['generation']
    assert len(generation['library']) == 80


@pytest.fixture(scope='module')
def established_artists(established_content):
    assert model_support.build_collaborative_artist_model(established_content['store']) == 80
    return established_content


def test_established_artist_fit_keeps_all_distinct_artists(established_artists):
    assert len(established_artists['store'].rec_model.get_rec_artist_vectors()) == 80


@pytest.fixture(scope='module')
def established_artist_content(established_artists):
    assert model_support.build_artist_content_model(established_artists['store']) == 80
    return established_artists


def test_established_artist_content_has_real_vectors_for_each_artist(established_artist_content):
    assert len(established_artist_content['store'].rec_model.get_rec_artist_content_vectors()) == 80


@pytest.fixture(scope='module')
def established_modes(established_artist_content):
    store = established_artist_content['store']
    assert model_support.publish_modes(store, 2000) >= 3
    established_artist_content['expected'] = {
        space: dict(getattr(store.rec_model, 'get_' + space)()) for space in SPACES}
    return established_artist_content


def test_established_modes_retain_the_real_eighty_subject_admission(established_modes):
    modes = established_modes['store'].modes.list_modes(active_only=True)
    assert len(modes) >= 3
    assert sum(mode['size'] for mode in modes) == 80


@pytest.fixture(scope='module')
def established_rankings(established_modes):
    established_modes['expected_rankings'] = real_rankings(established_modes['store'])
    return established_modes


def real_rankings(store):
    with store.read_snapshot() as snapshot:
        limit = max(len(snapshot.store.rec_query.library_keys()),
                    int(rec_params.get_param(snapshot.store, 'modes_pool_limit')))
        return mode_surfaces._rank_pools(snapshot.store, 2000, limit)


def test_established_surface_rankers_return_real_playable_candidates(established_rankings):
    rankings = established_rankings['expected_rankings']
    assert set(rankings) == {'wheelhouse', 'explore', 'comfort', 'fresh'}
    assert rankings['wheelhouse']
    assert all(item.key and item.video_id for pool in rankings.values() for item in pool)


def capture_plan(store, rankings):
    # Scoring is independently real immediately above/below. Feed that exact
    # detached lower-layer result to the real bucketing/PPR/policy/file owners.
    def completed_rankings(reader, now, limit):
        assert now == 2000 and limit >= 80
        assert len(reader.rec_query.library_keys()) == 80
        return rankings

    captured = []

    def completed_publication(deferred, target):
        assert target is store
        captured.append(deferred)
        return 'publication-owned-below'

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mode_surfaces, '_rank_pools', completed_rankings)
        patch.setattr(mode_surfaces._DeferredBundles, 'publish', completed_publication)
        result = mode_surfaces.prepare_bundles(store, 2000)
    assert len(captured) == 1 and captured[0].payload is result
    return {'deferred': captured[0], 'heads': store.recommendations.heads()}


def stage_pools(store, publication, group):
    deferred = publication['deferred']
    if 'build' not in publication:
        store.rec_ledger.record_prepared_policies(deferred.rows, deferred.bases)
        publication['build'] = store.recommendations.claim(deferred.prepared)
    build = publication['build']
    selected = [slot for slot in deferred.prepared.slots if (
        'all-home' if slot.surface == 'home' and slot.mode_key == 'all' else
        'mode-home' if slot.surface == 'home' else 'other-surfaces') == group]
    assert selected
    for slot in selected:
        identity = store.recommendations._stage_pool(build, slot.pool)
        assert identity is not None
        store.recommendations.repository.attach_pool(build, slot, identity)
    assert store.recommendations.heads() == publication['heads']


def stage_outputs(store, publication):
    assert store.recommendations.stage(publication['deferred'].prepared, publication['build'])
    assert store.recommendations.heads() == publication['heads']
    row = store.conn.execute('SELECT status FROM recommendation_generations WHERE id=?',
                             (publication['build'].generation_id,)).fetchone()
    assert row[0] == 'ready'


def publish_completed(store, publication):
    deferred, build = publication['deferred'], publication['build']

    def completed_claim(plan):
        assert plan is deferred.prepared
        return build

    # Claim and every SQL/file stage were exercised independently above. The
    # coordinator consumes that exact leased build; staging remains real and
    # idempotent, and the actual captured-snapshot publication owns visibility.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store.recommendations, 'claim', completed_claim)
        assert deferred.publish(store) == build.generation_id
    assert store.recommendations.heads() != publication['heads']
    return deferred.payload


@pytest.fixture(scope='module')
def established_plan(established_rankings):
    state = established_rankings
    state['publication'] = capture_plan(state['store'], state['expected_rankings'])
    return state


def test_established_bucketing_ppr_and_policy_plan_keep_all_ranked_content(established_plan):
    plan = established_plan['publication']['deferred']
    assert plan.payload['all']['wheelhouse']
    assert plan.payload['_ppr']
    assert plan.rows and plan.prepared.slots


@pytest.fixture(scope='module')
def established_full_pools(established_plan):
    stage_pools(established_plan['store'], established_plan['publication'], 'all-home')
    return established_plan


def test_established_full_library_pools_stage_without_exposing_a_generation(established_full_pools):
    assert not established_full_pools['store'].recommendations.heads()


@pytest.fixture(scope='module')
def established_mode_pools(established_full_pools):
    stage_pools(established_full_pools['store'], established_full_pools['publication'], 'mode-home')
    return established_full_pools


def test_established_mode_pools_stage_without_exposing_a_generation(established_mode_pools):
    assert not established_mode_pools['store'].recommendations.heads()


@pytest.fixture(scope='module')
def established_other_pools(established_mode_pools):
    stage_pools(established_mode_pools['store'], established_mode_pools['publication'], 'other-surfaces')
    return established_mode_pools


def test_established_radio_and_vibe_pools_stage_without_exposing_a_generation(established_other_pools):
    assert not established_other_pools['store'].recommendations.heads()


@pytest.fixture(scope='module')
def established_outputs(established_other_pools):
    stage_outputs(established_other_pools['store'], established_other_pools['publication'])
    return established_other_pools


def test_established_real_file_outputs_are_ready_before_visibility_changes(established_outputs):
    assert not established_outputs['store'].conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def established_pools(established_outputs):
    state = established_outputs
    state['expected_pools'] = publish_completed(state['store'], state['publication'])
    state['expected_generation'] = state['store'].rec_model.content_generation()['generation']
    return state


def check_framing(state, pools):
    store = state['store']
    generation = store.rec_model.content_generation()['generation']
    assert pools['_radio']['generation'] == generation
    for surface in ('home', 'radio'):
        head = store.recommendations.head(surface)
        assert head is not None
        assert store.recommendations.framing(surface, generation=head['id'])['radio_generation'] == generation


def test_established_ranked_pools_and_radio_share_the_content_generation(established_pools):
    assert established_pools['expected_pools']['all']['wheelhouse']
    check_framing(established_pools, established_pools['expected_pools'])


@pytest.fixture(scope='module')
def background_generation(established_pools):
    state = established_pools
    state['prepared'] = model_refresh.rebuild(state['store'], 2000)
    assert state['prepared'] is not None
    return state


def test_background_fit_matches_all_four_independently_fitted_vector_spaces(background_generation):
    state = background_generation
    for space, expected in state['expected'].items():
        rows = state['prepared']['vectors'][space]
        assert dict(rows).keys() == expected.keys()
        for key, blob in rows:
            np.testing.assert_allclose(np.frombuffer(blob, np.float32),
                                       np.frombuffer(expected[key], np.float32), atol=1e-6)
    assert not state['store'].rec_refresh.has_pending_changes()


@pytest.fixture(scope='module')
def background_rankings(background_generation):
    state = background_generation
    state['actual_rankings'] = real_rankings(state['store'])
    return state


def test_background_surface_rankers_keep_every_ordered_candidate_and_score(background_rankings):
    state = background_rankings
    for surface, expected in state['expected_rankings'].items():
        assert [mode_surfaces._item_dict(item) for item in state['actual_rankings'][surface]] == [
            mode_surfaces._item_dict(item) for item in expected]


@pytest.fixture(scope='module')
def background_plan(background_rankings):
    state = background_rankings
    state['publication'] = capture_plan(state['store'], state['actual_rankings'])
    return state


def test_background_plan_reuses_the_exact_actual_rankings_and_fresh_receipt(background_plan):
    deferred = background_plan['publication']['deferred']
    assert deferred.payload['_radio']['generation'] != background_plan['expected_generation']
    assert deferred.payload['all']['wheelhouse']


@pytest.fixture(scope='module')
def background_full_pools(background_plan):
    stage_pools(background_plan['store'], background_plan['publication'], 'all-home')
    return background_plan


def test_background_full_library_pools_preserve_the_last_good_heads(background_full_pools):
    state = background_full_pools
    assert state['store'].recommendations.heads() == state['publication']['heads']


@pytest.fixture(scope='module')
def background_mode_pools(background_full_pools):
    stage_pools(background_full_pools['store'], background_full_pools['publication'], 'mode-home')
    return background_full_pools


def test_background_mode_pools_preserve_the_last_good_heads(background_mode_pools):
    state = background_mode_pools
    assert state['store'].recommendations.heads() == state['publication']['heads']


@pytest.fixture(scope='module')
def background_other_pools(background_mode_pools):
    stage_pools(background_mode_pools['store'], background_mode_pools['publication'], 'other-surfaces')
    return background_mode_pools


def test_background_radio_and_vibe_pools_preserve_the_last_good_heads(background_other_pools):
    state = background_other_pools
    assert state['store'].recommendations.heads() == state['publication']['heads']


@pytest.fixture(scope='module')
def background_outputs(background_other_pools):
    stage_outputs(background_other_pools['store'], background_other_pools['publication'])
    return background_other_pools


def test_background_real_file_outputs_are_ready_before_visibility_changes(background_outputs):
    assert not background_outputs['store'].conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def background_pools(background_outputs):
    state = background_outputs
    state['actual_pools'] = publish_completed(state['store'], state['publication'])
    return state


def test_background_publication_keeps_exact_ranked_content_and_new_receipts(background_pools):
    state = background_pools
    assert state['store'].rec_model.content_generation()['generation'] != state['expected_generation']
    check_framing(state, state['actual_pools'])
    expected, actual = deepcopy(state['expected_pools']), deepcopy(state['actual_pools'])
    del expected['_radio']['generation'], actual['_radio']['generation']
    assert actual == expected
    assert state['store'].settings.get_setting('rec_embed_method') == state['method']
    assert state['store'].settings.get_setting('rec_dim') == '16'
    assert not state['store'].rec_refresh.has_pending_changes()
    assert not state['store'].conn.execute('PRAGMA foreign_key_check').fetchone()
