"""Onboarding consumes the feature owners' real serving selection and exclusions."""
from contextlib import closing, contextmanager
from copy import deepcopy
import time
from tests.derived_rating_support import seed_dislike

import pytest

from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, native_sql_calls,
)
from yt_playlist.core.store import Store
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import album_feedback, growth_readiness, home_prepared, home_vibe, measurement, musical_character, vibe_preparation


@contextmanager
def measured(record_property, phase):
    started = time.perf_counter()
    try:
        yield
    finally:
        record_property(phase + '_seconds', time.perf_counter() - started)


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
def readiness_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'),), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def store(tmp_path, readiness_schema, monkeypatch, request):
    if 'measured_readiness_worker' in request.fixturenames:
        import sqlite3

        # This worker consumes fixed-schema SQL below controlled snapshot
        # coordination. Reuse compiled statements on its real private handles.
        connect = sqlite3.connect

        def cached_connection(*args, **kwargs):
            kwargs['cached_statements'] = 512
            return connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, 'connect', cached_connection)
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


def publish_vibe_menus(store, *, lane_count=16, fresh_count=48, comfort_count=36,
                       genre_reserves=True, retain_payload=True):
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
            metadata.apply_provider(store.conn, entries[key], 'fixture', audio, ['fixture'], now=1000)
    prepare_profiles(store)
    bundle = {'all': lanes, '_meta': {'comfort_pool': comfort_count}}
    publish_bundles(store, bundle, vibes=True, genre_reserves=genre_reserves,
                    retain_payload=retain_payload)
    return lanes


@pytest.fixture
def completed_vibe_inputs(store, monkeypatch, native_sql_calls):
    from yt_playlist.rec import home_vibe, musical_profiles, rec_params

    # These consumers inspect completed menus, policy compatibility and live
    # feedback. The real worker/update owners below cover profile production.
    # Retain all 116 original subjects and completed profiles for every lane.
    # Synchronous private reads keep native SQL below the diagnostic clocks.
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'readiness-model')
    lanes, audio_inputs = vibe_menu_inputs(store, lane_count=16, fresh_count=48, comfort_count=36)
    rec_params.set_param(store, 'comfort_min_pool', 36)
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
    # The controlled catalog has two completed profile values. Detach one
    # returned batch, preserving that input sharing instead of copying each
    # identical profile tree again for every recording in every preset read.
    monkeypatch.setattr(musical_character, 'load',
                        lambda _reader, keys: deepcopy({key: by_key[key] for key in keys}))
    store.settings.set_setting('rec_vibe_version', vibe_preparation.version())
    return {'all': lanes, '_meta': {'comfort_pool': 36}}


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


@pytest.fixture
def measured_readiness_worker(store, monkeypatch):
    from functools import lru_cache
    import sqlite3

    from tests.genre_catalog_support import bound_catalog
    from yt_playlist.core import metadata_evidence, metadata_runtime, vibe_versions
    from yt_playlist.core.db_diagnostics import DiagnosticConnection
    from yt_playlist.rec import vibe_joint_preparation, vibe_pools

    # Match the installed fixture's single Jazz identity. Taxonomy curation is
    # below this measured-evidence worker and readiness consumer.
    bound_catalog(monkeypatch, entries=(('jazz', 'Jazz'),))
    # Every preset sees the same artist/album strings. Keep actual normalization
    # once per distinct value; neither worker evidence nor selection is cached.
    monkeypatch.setattr(vibe_pools, 'normalize', lru_cache(maxsize=512)(vibe_pools.normalize))
    # This owner changes a recording's own measured support through the real
    # worker. Admission/training of optional statistical models is a lower
    # collaborator; no model is admitted in this controlled catalog.
    monkeypatch.setattr(vibe_joint_preparation, 'qualified', lambda: False)
    for module in (vibe_preparation.audio_vibe_cruise, vibe_preparation.audio_vibe_release,
                   vibe_preparation.audio_vibe_relax, vibe_preparation.audio_vibe_uplift,
                   vibe_preparation.audio_vibe_energize, vibe_preparation.audio_vibe_focus,
                   vibe_preparation.audio_vibe_model):
        monkeypatch.setattr(module, 'identity', lambda: 'readiness-unconfigured-model')
    # This synchronous private database has no compactor or competing query
    # thread. Keep native SQLite execution, constraints and transactions while
    # excluding per-row timing and handle admission below the worker contract.
    for operation in ('execute', 'executemany'):
        monkeypatch.setattr(DiagnosticConnection, operation, getattr(sqlite3.Connection, operation))
    # This completed consumer schema never migrates or switches storage during
    # the worker updates. Format admission has separate owners; observe this
    # fixture's real format once instead of probing sqlite_schema per field.
    for owner, name in ((metadata_runtime, 'active'), (metadata_evidence, 'available'),
                        (vibe_versions, 'normalized')):
        installed = getattr(owner, name)(store.conn)
        monkeypatch.setattr(owner, name, lambda _conn, installed=installed: installed)
    # Admit the configured version on the empty catalog, within measured setup.
    # Otherwise the first evidence edit also refreshes all 116 already-current
    # profiles through the unrelated model-replacement queue.
    vibe_preparation.step(store, batch=512)
    assert not vibe_preparation.due(store)


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


@pytest.mark.usefixtures('measured_readiness_worker')
def test_readiness_follows_normal_worker_updates_between_menu_publications(store, monkeypatch):
    from yt_playlist.core import metadata_evidence
    from yt_playlist.rec import home_vibe, musical_preparation, vibe_pools
    from yt_playlist.rec.progressive import Budget, Preparation
    from tests.musical_support import prepare_profiles

    # Supply completed catalog facts below this worker/update owner. Provider
    # ingestion and artist-credit capture are separate contracts; this workflow
    # owns real metadata decisions, profile workers and SQL/file publication.
    lanes, audio_inputs = vibe_menu_inputs(store, lane_count=16)
    subjects = list(store.conn.execute('SELECT id,identity_key FROM recordings ORDER BY id'))
    assert len(subjects) == 116
    with atomic_write(store):
        store.conn.executemany('UPDATE recordings SET energy=?,mood_relaxed=?,mood_happy=? WHERE id=?',
            [(audio_inputs[key]['energy'], audio_inputs[key]['mood_relaxed'],
              audio_inputs[key]['mood_happy'], rid) for rid, key in subjects])
    prepare_profiles(store)

    def publish():
        # This workflow has no genre controls. Keep taxonomy in the real
        # recording workers, but omit unused genre-specific menu copies.
        previous = store.recommendations.head('home')
        with store.read_view() as reader, monkeypatch.context() as patch:
            patch.setattr(reader.genres, 'feature_axes', lambda _labels: {})
            generation = publish_bundles(store, {'all': lanes, '_meta': {'comfort_pool': 36}},
                                        vibes=True, genre_reserves=False, retain_payload=False)
        assert previous is None or generation != previous['id']
        assert all(store.recommendations.head(surface)['id'] == generation
                   for surface in ('home', *('vibe:' + preset for preset in home_vibe.PRESETS)))

    publish()
    # Keep all 116 published subjects, but change only the Fresh reserve that
    # determines this readiness transition. Three measured songs cannot satisfy
    # any preset's four-song minimum; the other lanes retain usable output.
    entries = [store.conn.execute('SELECT id FROM catalog_tracks WHERE identity_key=?',
                                 (item['key'],)).fetchone()[0]
               for item in lanes['fresh'][3:]]

    def check(expected):
        # Derive the expected answer from the actual current serving output,
        # including ranking changes, rather than from the cached summary.
        # All six references inspect one unchanged read. Share their current
        # catalog/profile inputs within it; every state opens a new cache.
        with store.read_view() as reader, reader.recommendations.planning_read():
            load, profiles = musical_character.load, {}

            def current_profiles(target, keys):
                assert target is reader
                missing = list(dict.fromkeys(key for key in keys if key not in profiles))
                if missing:
                    profiles.update(load(target, missing))
                # These selectors read profiles without mutating them. Keep
                # fresh result mappings without cloning the same evidence tree
                # again for each preset's selected tracks.
                return {key: profiles[key] for key in keys if key in profiles}

            # Each state still reads the actual newly published SQL/file heads.
            # Share those unchanged inputs across its six serving references.
            with monkeypatch.context() as patch:
                patch.setattr(musical_character, 'load', current_profiles)
                menus = [vibe_pools.assemble(reader, 1000, preset) for preset in home_vibe.PRESETS]
            usable = all(len(cards) == 4 and all(sum(growth_readiness._supported(track, preset)
                         for track in card['tracks']) >= 4 for card in cards)
                         for preset, cards in zip(home_vibe.PRESETS, menus))
            distinct = len({tuple(tuple(track['key'] for track in card['tracks']) for card in cards)
                            for cards in menus}) > 1
            assert (usable and distinct) is expected
            assert growth_readiness.vibes(reader, 1000) is expected

    def update_inputs(*, withdraw):
        head = store.recommendations.head('home')['id']
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            for entry in entries:
                for field in ('energy', 'mood_relaxed', 'mood_happy'):
                    metadata_evidence.choose(store.conn, entry, field, None,
                                             withdraw=withdraw, now=1001)
        # Drain this finite input through the ordinary worker without repeating
        # snapshot admission for 32-recording slices. The whole 116-subject
        # workflow, every pending dependency and every publication stay real.
        worker = Preparation(store, budget=Budget(batch=512))
        try:
            for turn in range(1000):
                if (not store.prepared_content.has_pending() and not musical_preparation.due(store)
                        and not vibe_preparation.due(store)):
                    break
                worker.capture(1002 + turn * 5)
            else:
                pytest.fail('Normal profile/Vibe preparation did not drain')
        finally:
            worker.close()
        assert store.recommendations.head('home')['id'] == head

    check(True)
    update_inputs(withdraw=False)
    check(False)
    # Withdrawn support must fail even while the old menu is retained.
    publish()
    check(False)
    update_inputs(withdraw=True)
    # Restored evidence revalidates the retained menu immediately, without
    # waiting for an unrelated complete recommendation publication.
    check(True)
    publish()
    check(True)


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
        store, summary, completed_vibe_inputs, record_property):
    from yt_playlist.rec import home_vibe, vibe_pools, vibe_readiness

    # Each state below publishes its own menu. Start with completed subjects
    # and profiles instead of first publishing an unused, unfiltered menu.
    lanes = completed_vibe_inputs['all']
    bundle = {'all': dict(lanes), '_meta': dict(completed_vibe_inputs['_meta'])}
    bundle['all']['temporal'] = [{**track, 'year': (1980, 1995, 2020)[index % 3]}
                               for index, track in enumerate(lanes['comfort'])]
    bundle['_meta'].update(comfort_pool=0, year_cuts=[1990, 2000])
    jazz = store.genres.feature_axes(['Jazz'])['Jazz'][0]

    state = 0

    def check(expected, **filters):
        nonlocal state
        state += 1
        store.rec_proposals.put_proposals('home_genre_request', {'epoch': 0, **filters}, 1000)
        # Preparation replaces lane lists and creates new preset/genre pools.
        # Copy their input containers, without cloning obsolete prepared pools
        # or retaining another full profile tree solely for this fixture.
        publication = {'all': dict(bundle['all']), '_meta': dict(bundle['_meta'])}
        with measured(record_property, f'state_{state}_publication'):
            publish_bundles(store, publication, vibes=True, retain_vibe_readiness=summary,
                            retain_payload=False)
        published = store.recommendations.output('home', 'ranking-evidence') or {}
        assert ('vibe_readiness' in published) is summary
        # All three rotations read the same unchanged publication and catalog.
        # A new state opens a fresh planning cache after its writes commit.
        with store.read_view() as reader, reader.recommendations.planning_read():
            for epoch in range(3):
                signatures = set()
                usable = True
                for preset in home_vibe.PRESETS:
                    with measured(record_property, f'state_{state}_epoch_{epoch}_{preset}_assembly'):
                        cards = vibe_pools.assemble(reader, 1000, preset, epoch, **filters)
                    usable = usable and len(cards) == 4 and all(
                        sum(growth_readiness._supported(track, preset) for track in card['tracks']) >= 4
                        for card in cards)
                    signatures.add(tuple(tuple(track['key'] for track in card['tracks']) for card in cards))
                assert (usable and len(signatures) > 1) is expected
                with measured(record_property, f'state_{state}_epoch_{epoch}_readiness'):
                    ready = vibe_readiness.ready(reader, 1000, epoch, **filters)
                assert ready is (expected and summary)

    check(True, include_genres=[jazz])
    check(False, avoid_genres=[jazz])
    with measured(record_property, 'dislike_writes'), atomic_write(store):
        for track in lanes['fresh'][3:]:
            seed_dislike(store, track['key'], until=None, now=1000)
    check(False, include_genres=[jazz])
