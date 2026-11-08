"""Onboarding consumes the feature owners' real serving selection and exclusions."""
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace
from tests.derived_rating_support import seed_dislike

import pytest

from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator
from yt_playlist.core.store import Store
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import album_feedback, growth_readiness, home_prepared, home_vibe, measurement, musical_character, vibe_preparation


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    # These SQL/preparation owners create no web application.
    yield


@pytest.fixture(scope='module', autouse=True)
def worker_template_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('readiness-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        yield


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # These consumers use Jazz candidates. Full taxonomy/bootstrap validation
    # belongs to its independent schema and genre owners.
    with bounded_consumer_schema((('jazz', 'Jazz'),)) as schema:
        yield schema


@pytest.fixture(scope='module')
def readiness_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'),), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def store(tmp_path, readiness_schema, monkeypatch):
    database = Store(tmp_path / 'readiness.db')
    with closing(database.conn):
        # Readiness checks real SQL and published files below mocked snapshot
        # coordination; power-loss durability of this disposable DB is unused.
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, readiness_schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with mock_snapshot_coordinator(database, monkeypatch):
            yield database


@pytest.fixture(autouse=True)
def readiness_policy(monkeypatch):
    # Readiness consumes prepared candidates and current sound/eligibility.
    # Full model/listening fingerprint construction belongs to serving-policy
    # owners; retain real policy records and SQL/file publication here.
    monkeypatch.setattr(measurement, 'serving_policy', lambda *args, **kwargs:
                        ('readiness-fixture', {'ranker': 'readiness-fixture'}))


def fresh_reserve(store, count=48, score=.8):
    tracks = [{'key': f'new {i}|artist {i}', 'video_id': f'new-{i}', 'title': f'New {i}',
               'artist': f'Artist {i}', 'genre': 'Jazz', 'score': score} for i in range(count)]
    bundle = {'all': {'fresh': tracks}}
    existing = store.discovery.discovered_tracks_by_keys([track['key'] for track in tracks])
    missing = [track for track in tracks if track['key'] not in existing]
    seed_publication_subjects(store, missing, discoveries=[track['key'] for track in missing])
    publish_bundles(store, bundle)
    return tracks


def prepared_taste(store):
    # Controlled preparation context, stored exactly as the existing worker does.
    store.rec_proposals.put_proposals(home_prepared.SURFACE, {'choice_context': {
        'policy': {'content_generation': None, 'collaborative_inputs': None},
        'layers': {'saved': {'content': {'titles': ['My existing interest']}}}}}, 1000)


def test_foundation_needs_existing_taste_and_a_sizable_ranked_eligible_reserve(store):
    assert not growth_readiness.foundation(store, 1000)
    tracks = fresh_reserve(store)
    assert not growth_readiness.foundation(store, 1000)  # ingestion is not preference evidence
    prepared_taste(store)
    assert growth_readiness.foundation(store, 1000)
    assert store.rec_query.tracks_total() == 0  # discovered music need not be owned
    seed_dislike(store, tracks[0]['key'], until=None, now=1000)
    assert not growth_readiness.foundation(store, 1000)


def test_unscored_or_small_discovery_pool_cannot_open_foundation(store):
    prepared_taste(store)
    fresh_reserve(store, score=None)
    assert not growth_readiness.foundation(store, 1000)
    fresh_reserve(store, count=47)
    assert not growth_readiness.foundation(store, 1000)


def test_replaced_model_context_does_not_qualify_taste(store):
    fresh_reserve(store)
    prepared_taste(store)
    assert growth_readiness.foundation(store, 1000)
    store.settings.set_setting('rec_content_generation', 'new-generation-not-yet-prepared')
    assert not growth_readiness.foundation(store, 1000)


def test_album_gate_needs_an_actual_choice_and_rechecks_feedback(store):
    items = [{'browse_id': f'MPRE{i}', 'artist': f'Artist {i}', 'title': f'Record {i}',
              'classification': 'discovery', 'score': .9} for i in range(2)]
    store.rec_proposals.put_proposals('album_prepared',
        {'built_at': 1000, 'context': {}, 'items': items[:1]}, 1000)
    assert not growth_readiness.albums(store, 1000)
    store.rec_proposals.put_proposals('album_prepared',
        {'built_at': 1000, 'context': {}, 'items': items}, 1000)
    assert growth_readiness.albums(store, 1000)
    store.accounts.upsert_account('Main', 'credential', None, True)
    album_feedback.set_feedback(store, 'MPRE0', 'reject', now=1000)
    assert not growth_readiness.albums(store, 1000)


def test_small_interludes_need_a_useful_shelf_rather_than_a_cache_row():
    collections = {key: [] for key in ('discovery_tracks', 'discovery_albums', 'rediscover',
                                      'rediscover_albums', 'rediscover_tracks')}
    collections['discovery_tracks'] = [{}]
    collections['rediscover_tracks'] = [{}]
    assert not growth_readiness.discover(collections)
    assert not growth_readiness.revisit(collections)
    collections['discovery_tracks'] *= 10
    collections['rediscover_tracks'] *= 12
    assert growth_readiness.discover(collections)
    assert growth_readiness.revisit(collections)


def test_vibe_input_must_be_compatible_supported_prediction_or_measurement():
    track = {'musical': {'version': musical_character.VERSION, 'vibes': {
        'inputs_version': vibe_preparation.version(), 'fits': {
        'relax': {'score': .8, 'support': .2, 'source': 'artist_genre'}}}}}
    assert growth_readiness._supported(track, 'relax')
    # A model replacement queues new fits; the prepared, supported output
    # retains the same meaning and remains useful during that work.
    track['musical']['vibes']['inputs_version'] = vibe_preparation.serving_prefix() + 'previous-model'
    assert growth_readiness._supported(track, 'relax')
    assert not growth_readiness._supported(track, 'energize')
    for field, value in [('source', 'heuristic'), ('support', 0), ('score', float('nan'))]:
        invalid = deepcopy(track)
        invalid['musical']['vibes']['fits']['relax'][field] = value
        assert not growth_readiness._supported(invalid, 'relax')
    track['musical']['version'] = 'old'
    assert not growth_readiness._supported(track, 'relax')
    track['musical']['version'] = musical_character.VERSION
    track['musical']['vibes']['inputs_version'] = 'old-fit-policy'
    assert not growth_readiness._supported(track, 'relax')


def test_compatible_vibe_evidence_survives_model_replacement_and_still_rechecks_feedback(
        store, monkeypatch, completed_vibe_menus):
    lanes = completed_vibe_menus
    assert growth_readiness.vibes(store, 1000)
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'replacement-model')
    assert vibe_preparation.due(store)
    assert growth_readiness.vibes(store, 1001)
    with atomic_write(store):
        for track in lanes['fresh'][3:]:
            seed_dislike(store, track['key'], until=None, now=1002)
    assert not growth_readiness.vibes(store, 1002)


def vibe_menu_inputs(store, *, lane_count=8, fresh_count=48, comfort_count=36, artist_count=None):
    lanes = {}
    audio_inputs = {}
    for lane in ('wheelhouse', 'explore', 'fresh', 'comfort'):
        tracks = []
        # The journey producer keeps its actual Fresh and Comfort capacities.
        for index in range(fresh_count if lane == 'fresh' else comfort_count if lane == 'comfort' else lane_count):
            artist = f'Artist {index % artist_count}' if artist_count else f'{lane} artist {index}'
            key = f'{lane} {index}|{artist.lower()}'
            soft = index % 2 == 0
            audio_inputs[key] = {'energy': .1 if soft else .9, 'mood_relaxed': .9 if soft else .1,
                                 'mood_happy': .8 if soft else .2}
            tracks.append({'key': key, 'video_id': key, 'title': f'{lane} {index}',
                           'artist': artist, 'genre': 'Jazz', 'score': .8})
        lanes[lane] = tracks
    seed_publication_subjects(store, [track for tracks in lanes.values() for track in tracks],
                              discoveries=[track['key'] for track in lanes['fresh']])
    return lanes, audio_inputs


def publish_vibe_menus(store, *, lane_count=16, fresh_count=48, comfort_count=36):
    from tests.musical_support import prepare_profiles
    from yt_playlist.core import artists, metadata

    # Worker-update and full serving agreement owners retain actual measured
    # metadata, ordinary profile preparation, and all 116 catalog subjects.
    lanes, audio_inputs = vibe_menu_inputs(store, lane_count=lane_count,
                                          fresh_count=fresh_count, comfort_count=comfort_count)
    entries = dict(store.conn.execute('SELECT r.identity_key,e.id FROM catalog_entries e '
                                     'JOIN recordings r ON r.id=e.recording_id'))
    with atomic_write(store):
        for key, audio in audio_inputs.items():
            artists.observe(store.conn, entries[key], key.split('|', 1)[1], now=1000)
            for field, value in audio.items():
                metadata.select(store.conn, entries[key], field, value, 'fixture')
    prepare_profiles(store)
    bundle = {'all': lanes, '_meta': {'comfort_pool': comfort_count}}
    publish_bundles(store, bundle, vibes=True)
    return lanes


def fixed_worker_fit(patch):
    from yt_playlist.rec import vibe_joint_model
    from yt_playlist.repos import proposal_cache

    fit = vibe_joint_model.fit
    patch.setattr(vibe_joint_model, 'fit', lambda rows, vibe, **kwargs:
                  fit(rows, vibe, fixed=vibe_joint_model.INITIAL, **kwargs))
    # These checks observe committed SQL and published files. WAL checkpoint
    # scheduling is below the worker/readiness boundary and has separate owners.
    patch.setattr(proposal_cache, 'checkpoint_wal', lambda *_args, **_kwargs: None)


@pytest.fixture(scope='module')
def worker_vibe_input(_pristine_current_schema, tmp_path_factory):
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core import artists, metadata
    from yt_playlist.core.store import Store
    from yt_playlist.rec import rec_params

    path = tmp_path_factory.mktemp('worker-vibe-inputs') / 'source.db'
    source = Store(path)
    with closing(source.conn):
        initialize_consumer(source, _pristine_current_schema)
        source.conn.execute('PRAGMA synchronous=NORMAL')
        rec_params.set_param(source, 'comfort_min_pool', 4)
        lanes, audio = vibe_menu_inputs(source, lane_count=8, fresh_count=4, comfort_count=4, artist_count=4)
        entries = dict(source.conn.execute('SELECT r.identity_key,e.id FROM catalog_entries e '
                                          'JOIN recordings r ON r.id=e.recording_id'))
        with atomic_write(source):
            for key, fields in audio.items():
                artists.observe(source.conn, entries[key], key.split('|', 1)[1], now=1000)
                for field, value in fields.items():
                    metadata.select(source.conn, entries[key], field, value, 'fixture')
        yield source, lanes


def test_worker_vibe_catalog_keeps_each_real_lane_and_measured_input(worker_vibe_input):
    source, lanes = worker_vibe_input
    assert {lane: len(tracks) for lane, tracks in lanes.items()} == {
        'wheelhouse': 8, 'explore': 8, 'fresh': 4, 'comfort': 4}
    assert source.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 24
    assert source.prepared_content.has_pending()


@pytest.fixture(scope='module')
def worker_vibe_profiles(worker_vibe_input):
    from tests.musical_support import prepare_profiles
    from tests.store_fixture_support import reuse_reporting_reader

    source, lanes = worker_vibe_input
    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(source, patch):
        fixed_worker_fit(patch)
        prepare_profiles(source)
    return source, lanes


def test_normal_worker_publishes_each_measured_profile(worker_vibe_profiles):
    source, lanes = worker_vibe_profiles
    keys = [track['key'] for tracks in lanes.values() for track in tracks]
    assert set(source.musical_profiles.by_keys(keys)) == set(keys)
    assert not source.prepared_content.has_pending()


@pytest.fixture(scope='module')
def worker_vibe_template(worker_vibe_profiles):
    from tests.prepared_fixture_support import capture_template
    from tests.store_fixture_support import reuse_reporting_reader

    source, lanes = worker_vibe_profiles
    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(source, patch):
        fixed_worker_fit(patch)
        patch.setattr(measurement, 'serving_policy', lambda *args, **kwargs:
                      ('readiness-fixture', {'ranker': 'readiness-fixture'}))
        publish_bundles(source, {'all': lanes, '_meta': {'comfort_pool': 4}}, vibes=True)
    return capture_template(source, lanes)


def test_worker_vibe_input_keeps_real_metadata_profiles_and_all_six_presets(worker_vibe_template):
    from yt_playlist.rec import home_vibe, vibe_pools

    source = worker_vibe_template.source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert len(worker_vibe_template.result['fresh']) == 4
    assert source.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 24
    assert all(len(vibe_pools.assemble(source, 1000, preset)) == 4 for preset in home_vibe.PRESETS)


@pytest.fixture
def worker_vibe_store(worker_vibe_template, tmp_path, monkeypatch):
    from tests.store_fixture_support import reuse_reporting_reader
    fixed_worker_fit(monkeypatch)
    with worker_vibe_template.open(tmp_path / 'worker.db') as source, reuse_reporting_reader(source, monkeypatch):
        source.conn.execute('PRAGMA synchronous=NORMAL')
        yield source


@pytest.fixture
def completed_vibe_inputs(store, monkeypatch):
    from yt_playlist.rec import home_vibe, musical_profiles, rec_params

    # These consumers inspect completed menus, policy compatibility and live
    # feedback. The real worker/update owners below cover profile production.
    # Eight candidates per lane and four per temporal band preserve full
    # cards, rotations and contrasting presets. Inventory thresholds retain
    # their real producer owner.
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'readiness-model')
    lanes, audio_inputs = vibe_menu_inputs(store, fresh_count=8, comfort_count=12)
    rec_params.set_param(store, 'comfort_min_pool', 12)
    profiles = {}
    for soft in (True, False):
        attributes = {'energy': .1 if soft else .9, 'mood_relaxed': .9 if soft else .1,
                      'mood_happy': .8 if soft else .2}
        profile = musical_profiles.resolve([], selected=attributes)
        profile['vibes'] = {'inputs_version': vibe_preparation.version(), 'fits': {
            preset: {'score': home_vibe.heuristic_affinity(profile, preset),
                     'support': .8, 'source': 'provider_measurement'}
            for preset in home_vibe.PRESETS}}
        profiles[soft] = musical_character.compact(profile)
    by_key = {key: profiles[attributes['energy'] < .5] for key, attributes in audio_inputs.items()}
    monkeypatch.setattr(musical_character, 'load',
                        lambda _reader, keys: {key: deepcopy(by_key[key]) for key in keys})
    store.settings.set_setting('rec_vibe_version', vibe_preparation.version())
    return {'all': lanes, '_meta': {'comfort_pool': 12}}


@pytest.fixture
def completed_vibe_menus(store, completed_vibe_inputs):
    # These consumers have no genre filter. Keep every preset and lane while
    # omitting unused genre-specific copies. Filter comparisons prepare their
    # complete reserves directly from completed_vibe_inputs below.
    publish_bundles(store, completed_vibe_inputs, vibes=True, genre_reserves=False)
    return completed_vibe_inputs['all']


@pytest.fixture
def vibe_menus(completed_vibe_menus):
    # These consumers own readiness, serving selection and current exclusions.
    # The worker owners below separately produce actual measured profiles.
    return completed_vibe_menus


def test_vibe_gate_checks_real_menus_for_every_preset_and_current_exclusions(store, vibe_menus):
    from yt_playlist.rec import home_vibe, vibe_pools

    lanes = vibe_menus
    assert all(len(vibe_pools.assemble(store, 1000, preset)) == 4 for preset in home_vibe.PRESETS)
    assert growth_readiness.vibes(store, 1000)
    with atomic_write(store):
        for track in lanes['fresh'][3:]:
            seed_dislike(store, track['key'], until=None, now=1001)
    assert not growth_readiness.vibes(store, 1001)


def test_vibe_readiness_resolves_repeated_genres_in_batches(store, vibe_menus):
    queries = []
    with store.read_view() as reader:
        reader.conn.set_trace_callback(queries.append)
        assert growth_readiness.vibes(reader, 1000)
    genre_queries = [query for query in queries
                     if 'genre_catalog' in query or 'genre_redirects' in query]
    # Six presets each inspect four full candidate lanes. Repeated labels must
    # not turn this frequent Home read into thousands of identity lookups.
    assert len(genre_queries) < 500, len(genre_queries)


def test_worker_readiness_reads_compact_output_and_one_live_eligibility_batch(store, monkeypatch, vibe_menus):
    from yt_playlist.rec import musical_character, vibe_pools

    original, calls = vibe_pools.eligibility_inputs, []

    def inputs(*args):
        calls.append(len(args[1]))
        return original(*args)

    def unexpected(*args, **kwargs):
        raise AssertionError('Readiness must not restore profiles or assemble full Vibe offers')

    monkeypatch.setattr(vibe_pools, 'eligibility_inputs', inputs)
    monkeypatch.setattr(vibe_pools, 'assemble', unexpected)
    monkeypatch.setattr(musical_character, 'load', unexpected)
    assert growth_readiness.vibes(store, 1000)
    assert len(calls) == 1


@pytest.fixture(scope='module', params=[False, True], ids=['without-inference', 'with-inference'])
def worker_update_state(worker_vibe_template, tmp_path_factory, request):
    from yt_playlist.rec.progressive import Preparation

    path = tmp_path_factory.mktemp('worker-readiness') / 'source.db'
    with worker_vibe_template.open(path) as source:
        source.conn.execute('PRAGMA synchronous=NORMAL')
        entries = [source.conn.execute('SELECT id FROM catalog_tracks WHERE identity_key=?',
                                       (track['key'],)).fetchone()[0]
                   for track in worker_vibe_template.result['fresh'][3:]]
        worker = Preparation(source)
        try:
            yield SimpleNamespace(store=source, worker=worker, entries=entries,
                                  inferred=request.param, turn=0, position=0)
        finally:
            worker.close()


@pytest.fixture
def worker_update(worker_update_state, monkeypatch, request):
    from yt_playlist.rec import vibe_joint_preparation
    from yt_playlist.controllers.vibe_predictions import VibePredictions
    from tests.store_fixture_support import reuse_reporting_reader

    fixed_worker_fit(monkeypatch)
    monkeypatch.setattr(measurement, 'serving_policy', lambda *args, **kwargs:
                        ('readiness-fixture', {'ranker': 'readiness-fixture'}))
    if not worker_update_state.inferred:
        # Exercise unavailable support separately from real cohort predictions.
        # Keep real metadata edits, capture, profile
        # publication and readiness reads; only inferred evidence is unavailable.
        monkeypatch.setattr(VibePredictions, 'groups', lambda _, keys: {
            key: {'stats': {}, 'inputs': {}} for key in keys})
        monkeypatch.setattr(vibe_joint_preparation, 'posterior', lambda *_: None)
    with reuse_reporting_reader(worker_update_state.store, monkeypatch):
        state = worker_update_state
        steps = [(test_normal_worker_captures_null_choice_without_republishing_menu, (state, monkeypatch))]
        steps += [(test_normal_worker_publishes_each_null_choice_inference_channel, (state, vibe))
                  for vibe in (*home_vibe.PRESETS, *INTENSITY_CHANNELS)]
        steps += [
            (test_normal_worker_scored_null_choice_changes_readiness_without_republishing_menu, (state,)),
            (test_normal_worker_null_choice_readiness_survives_menu_republication, (state,)),
            (test_normal_worker_captures_withdrawn_choice_without_republishing_menu, (state, monkeypatch))]
        steps += [(test_normal_worker_publishes_each_restored_inference_channel, (state, vibe))
                  for vibe in (*home_vibe.PRESETS, *INTENSITY_CHANNELS)]
        steps += [(test_normal_worker_restored_readiness_is_visible_without_menu_republication, (state,))]
        target = next(index for index, (owner, args) in enumerate(steps)
                      if owner.__name__ == request.node.originalname
                      and ('vibe' not in request.node.callspec.params
                           or args[-1] == request.node.callspec.params['vibe']))
        # Selecting a later channel or readiness owner alone still establishes
        # the actual preceding edits and worker publications.
        while state.position < target:
            owner, args = steps[state.position]
            owner(*args)
            state.position += 1
        yield worker_update_state
        state.position = target + 1


def check_worker_readiness(store, expected):
    from yt_playlist.rec import home_vibe, vibe_pools

    # Derive the answer from every actual current serving menu, independently
    # of the compact summary, including the four supported songs per card.
    with store.read_view() as reader:
        menus = [vibe_pools.assemble(reader, 1000, preset) for preset in home_vibe.PRESETS]
        usable = all(len(cards) == 4 and all(sum(growth_readiness._supported(track, preset)
                     for track in card['tracks']) >= 4 for card in cards)
                     for preset, cards in zip(home_vibe.PRESETS, menus))
        distinct = len({tuple(tuple(track['key'] for track in card['tracks']) for card in cards)
                        for cards in menus}) > 1
        assert (usable and distinct) is expected
        assert growth_readiness.vibes(reader, 1000) is expected


def worker_turn(state):
    state.turn += 1
    state.worker.capture(1002 + state.turn * 5)


def change_worker_measurement(state, patch, *, withdraw):
    from yt_playlist.core import metadata_evidence
    from yt_playlist.rec import musical_preparation, vibe_joint_preparation

    store = state.store
    head = store.recommendations.head('home')['id']
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        for entry in state.entries:
            for field in ('energy', 'mood_relaxed', 'mood_happy'):
                metadata_evidence.choose(store.conn, entry, field, None, withdraw=withdraw, now=1001)
    # Separate changed-recording capture from the eight model channel turns.
    # Both use the actual normal worker; numeric fits have named owners below.
    with patch.context() as capture_patch:
        capture_patch.setattr(vibe_joint_preparation, 'admitted', lambda *_args, **_kwargs: False)
        for _ in range(100):
            if not store.prepared_content.has_pending() and not musical_preparation.due(store):
                break
            worker_turn(state)
        else:
            pytest.fail('Normal changed-recording capture did not drain')
    assert store.recommendations.head('home')['id'] == head


def finish_worker_channel(state, vibe):
    from yt_playlist.rec import vibe_joint_preparation

    head = state.store.recommendations.head('home')['id']
    for _ in range(100):
        value = vibe_joint_preparation.state(state.store, vibe)
        if vibe_joint_preparation.current(state.store, value):
            assert value['outcome'] == 'ready'
            break
        worker_turn(state)
    else:
        pytest.fail('Normal worker did not publish channel ' + vibe)
    assert state.store.recommendations.head('home')['id'] == head


def finish_worker_scores(state):
    from yt_playlist.rec import musical_preparation

    for _ in range(100):
        if (not state.store.prepared_content.has_pending() and not musical_preparation.due(state.store)
                and not vibe_preparation.due(state.store)):
            break
        worker_turn(state)
    else:
        pytest.fail('Normal profile/Vibe preparation did not drain')


def test_normal_worker_captures_null_choice_without_republishing_menu(worker_update, monkeypatch):
    check_worker_readiness(worker_update.store, True)
    change_worker_measurement(worker_update, monkeypatch, withdraw=False)


@pytest.mark.parametrize('vibe', (*home_vibe.PRESETS, *INTENSITY_CHANNELS))
def test_normal_worker_publishes_each_null_choice_inference_channel(worker_update, vibe):
    finish_worker_channel(worker_update, vibe)


def test_normal_worker_scored_null_choice_changes_readiness_without_republishing_menu(worker_update):
    finish_worker_scores(worker_update)
    check_worker_readiness(worker_update.store, worker_update.inferred)


def test_normal_worker_null_choice_readiness_survives_menu_republication(worker_update):
    store = worker_update.store
    # Republishing the retained menu cannot invent unavailable support; valid
    # model inference can still support songs whose own measurements are null.
    publish_bundles(store, deepcopy(store._fixture_bundle_payload), vibes=True)
    check_worker_readiness(store, worker_update.inferred)


def test_normal_worker_captures_withdrawn_choice_without_republishing_menu(worker_update, monkeypatch):
    change_worker_measurement(worker_update, monkeypatch, withdraw=True)


@pytest.mark.parametrize('vibe', (*home_vibe.PRESETS, *INTENSITY_CHANNELS))
def test_normal_worker_publishes_each_restored_inference_channel(worker_update, vibe):
    finish_worker_channel(worker_update, vibe)


def test_normal_worker_restored_readiness_is_visible_without_menu_republication(worker_update):
    finish_worker_scores(worker_update)
    # Restored evidence revalidates the retained menu immediately, without
    # waiting for an unrelated complete recommendation publication.
    check_worker_readiness(worker_update.store, True)


def test_missing_readiness_waits_for_worker_without_reading_profiles_or_ranked_pools(
        store, monkeypatch, completed_vibe_menus):
    from yt_playlist.core import profile_files
    from yt_playlist.rec import vibe_pools
    from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo

    lanes = completed_vibe_menus
    bundle = deepcopy(store._fixture_bundle_payload)
    bundle['_meta'].pop('vibe_readiness')
    publish_bundles(store, bundle, genre_reserves=False)

    def unexpected(*args, **kwargs):
        raise AssertionError('Missing readiness must wait for workers without reconstructing inputs')

    with monkeypatch.context() as guards:
        guards.setattr(vibe_pools, 'prepare', unexpected)
        guards.setattr(vibe_pools, 'assemble', unexpected)
        guards.setattr(musical_character, 'load', unexpected)
        guards.setattr(profile_files, 'read', unexpected)
        guards.setattr(RecommendationPublicationsRepo, 'candidate_rows', unexpected)
        assert not growth_readiness.vibes(store, 1000)
    publish_bundles(store, bundle, vibes=True, genre_reserves=False)
    assert growth_readiness.vibes(store, 1001)
    with atomic_write(store):
        for track in lanes['fresh'][3:]:
            seed_dislike(store, track['key'], until=None, now=1001)
    assert not growth_readiness.vibes(store, 1001)


def test_oversized_optional_index_does_not_block_useful_menu_publication(
        store, monkeypatch, completed_vibe_menus):
    from yt_playlist.rec import home_vibe, vibe_pools, vibe_readiness_index

    previous = store.recommendations.head('home')['id']
    monkeypatch.setattr(vibe_readiness_index, 'MAX_INDEX_BYTES', 1)
    publish_bundles(store, deepcopy(store._fixture_bundle_payload), vibes=True, genre_reserves=False)
    assert store.recommendations.head('home')['id'] != previous
    assert all(len(vibe_pools.assemble(store, 1000, preset)) == 4 for preset in home_vibe.PRESETS)
    assert not growth_readiness.vibes(store, 1000)


def test_summary_does_not_hide_a_changed_preset_or_incompatible_fit_policy(
        store, monkeypatch, completed_vibe_menus):
    original = store.recommendations.head('home')
    vibes = store._fixture_bundle_payload['_vibes']
    # Publish only the changed preset. The unchanged presets retain their
    # actual original generation rather than being rebuilt and then rewound.
    policy = store.recommendations.pool('vibe:relax', '', 'fresh')['policy_version']
    bundle = {'_meta': {'comfort_pool': 0}, '_vibes': {**vibes,
        'pools': {'relax': {lane: [{**track, 'pool_policy': policy} for track in tracks]
                            for lane, tracks in vibes['pools']['relax'].items()}},
        'genres': {}}}
    plan = store.recommendations.prepare_bundle(bundle, None,
        external_key='readiness-relax-replacement', policy_version=original['policy_version'],
        input_revision=None, created_at=1001)
    build = store.recommendations.claim(plan)
    assert store.recommendations.stage(plan, build)
    store.recommendations.publish(build, 1001)
    # Only this preset has moved on. Its new framing offers three lanes, while
    # Home still owns the earlier four-lane summary.
    with store.conn:
        store.conn.execute("UPDATE recommendation_heads SET generation_id=? WHERE surface!='vibe:relax'",
                           (original['id'],))
    assert not growth_readiness.vibes(store, 1000)
    with store.conn:
        store.conn.execute('UPDATE recommendation_heads SET generation_id=?', (original['id'],))
    assert growth_readiness.vibes(store, 1000)
    monkeypatch.setattr(vibe_preparation, 'serving_prefix', lambda: 'incompatible-fit-policy/')
    assert not growth_readiness.vibes(store, 1000)


def test_legacy_publication_without_summary_does_not_restore_full_bundle(store, monkeypatch, vibe_menus):
    from yt_playlist.core import json_cache
    from yt_playlist.rec import vibe_readiness

    bundle = deepcopy(store._fixture_bundle_payload)
    bundle['_meta'].pop('vibe_readiness')
    encoded = json_cache.encode(bundle, group_pools=True)
    assert json_cache.MARKER in encoded
    with store.read_view() as reader:
        reader._recommendation_files = False
        monkeypatch.setattr(reader.rec_proposals, '_proposal_json', lambda surface: encoded)
        def unexpected(*args, **kwargs):
            raise AssertionError('Home must not restore full legacy profiles')
        monkeypatch.setattr(musical_character, 'load', unexpected)
        assert not vibe_readiness.ready(reader, 1000, 0)


@pytest.mark.parametrize('summary', [True, False])
def test_compact_gate_agrees_with_served_menus_for_genres_rotation_and_feedback(
        store, summary, completed_vibe_inputs, monkeypatch):
    from yt_playlist.core import json_cache
    from yt_playlist.rec import home_vibe, vibe_pools, vibe_readiness, vibe_readiness_index
    from yt_playlist.controllers.recommendation_publications import RecommendationPublications

    lanes = completed_vibe_inputs['all']
    bundle = deepcopy(completed_vibe_inputs)
    bundle['all']['temporal'] = [{**track, 'year': (1980, 1995, 2020)[index % 3]}
                               for index, track in enumerate(lanes['comfort'])]
    bundle['_meta'].update(comfort_pool=0, year_cuts=[1990, 2000])
    jazz = store.genres.feature_axes(['Jazz'])['Jazz'][0]
    # Filters and dislikes do not change the underlying measured candidates.
    # Prepare their real pools once, then project the actual index for each
    # request below. No unchanged model/profile work belongs in that loop.
    prepared = vibe_pools.prepare(store, bundle, 1000, measurement.PoolPolicies(store, 1000))
    profile_state = json_cache.decode(prepared['_meta']['vibe_readiness'])['profile_state']

    # This comparison owns compact selection versus full selection. Typed
    # publication and generation coherence have their real SQL/file owners in
    # test_vibe_pool_reads and the other readiness cases above. Supply the
    # worker's real prepared output at the controller boundary rather than
    # staging the same candidate ranks again for every exclusion scenario.
    metadata = {}
    output = RecommendationPublications.output

    def prepared_output(repo, surface, kind, **options):
        if (surface, kind) == ('home', 'ranking-evidence'):
            return metadata
        return output(repo, surface, kind, **options)

    monkeypatch.setattr(RecommendationPublications, 'output', prepared_output)

    def check(expected, **filters):
        store.rec_proposals.put_proposals('home_genre_request', {'epoch': 0, **filters}, 1000)
        metadata.clear()
        if summary:
            index = vibe_readiness_index.prepare(prepared, include_genres=True, genre_request=(
                filters.get('include_genres', ()), filters.get('avoid_genres', ())))
            metadata['vibe_readiness'] = json_cache.encode({**index, 'profile_state': profile_state})
        # All rotations use the same current eligibility snapshot. The next
        # filter/feedback scenario opens a new reader after its actual writes.
        with store.read_view() as reader:
            for epoch in range(3):
                signatures = set()
                usable = True
                for preset in home_vibe.PRESETS:
                    cards = vibe_pools.assemble(reader, 1000, preset, epoch, bundles=prepared, **filters)
                    usable = usable and len(cards) == 4 and all(
                        sum(growth_readiness._supported(track, preset) for track in card['tracks']) >= 4
                        for card in cards)
                    signatures.add(tuple(tuple(track['key'] for track in card['tracks']) for card in cards))
                assert (usable and len(signatures) > 1) is expected
                ready = vibe_readiness.ready(reader, 1000, epoch, **filters)
                assert ready is (expected and summary)

    check(True, include_genres=[jazz])
    check(False, avoid_genres=[jazz])
    with atomic_write(store):
        for track in lanes['fresh'][3:]:
            seed_dislike(store, track['key'], until=None, now=1000)
    check(False, include_genres=[jazz])
