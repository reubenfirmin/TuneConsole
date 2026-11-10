"""Engineering contracts for shared consumers, not a listening-quality verdict."""
from copy import deepcopy
from contextlib import nullcontext
from tests.derived_rating_support import seed_dislike
import json
import logging
from types import SimpleNamespace

import pytest

from tests.conftest import seed_catalog_track
from tests.genre_catalog_support import bound_catalog
from tests.home_fixture_support import publish_menu
from tests.musical_support import prepare_profiles
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, initialize_consumer, reuse_reporting_reader)
from yt_playlist.core.store import Store
from yt_playlist.core import catalog
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.util import genre_catalog, genre_map
from yt_playlist.rec import (fresh_pool, home_cards, home_prepared, home_vibe, library_pool,
                             mix_tuning, mode_surfaces, musical_character, musical_profiles,
                             musical_preparation, surfaces, vibe_pools, vibe_preparation)
from yt_playlist.controllers.musical_profiles import MusicalProfiles

# These are consumer contracts after unlock. Real startup/readiness is covered
# separately by test_growth_readiness and test_growth_journey.
pytestmark = pytest.mark.usefixtures('unlocked_home_controls')


@pytest.fixture
def controlled_profiles(monkeypatch):
    # These consumers own pool selection, genre reserves and served stamps.
    # Profile inference and its replacement publication remain real in the
    # independent profile-stamp test below and in test_vibe_prediction.
    profiles = {}
    monkeypatch.setattr(MusicalProfiles, 'by_keys',
                        lambda _repo, keys: {key: deepcopy(profiles[key]) for key in keys if key in profiles})

    def add(key, energy):
        profile = musical_profiles.resolve([], selected={'energy': energy})
        profile['vibes'] = {'inputs_version': vibe_preparation.serving_prefix() + 'controlled-provider-profiles', 'fits': {
            preset: {'score': energy if preset in ('energize', 'release') else 1 - energy,
                     'support': .8, 'source': 'provider_measurement'}
            for preset in home_vibe.PRESETS}}
        profiles[key] = profile
        return musical_character.compact(profile)

    return add


@pytest.fixture
def character_consumer(monkeypatch, controlled_profiles):
    # These consumers assemble completed lower-layer results. SQL/file reads,
    # numerical preparation and access admission have independent real owners.
    reader = SimpleNamespace(
        _recommendation_files=True, _read_only=True,
        settings=SimpleNamespace(get_setting=lambda key: None),
        recommendations=SimpleNamespace(framing=lambda *args, **kwargs: {}),
        musical_profiles=MusicalProfiles.__new__(MusicalProfiles),
        rec_query=SimpleNamespace(track_genres=lambda keys: {}, track_decades=lambda keys: {},
            track_audio_features=lambda: {}, library_keys=lambda: set()),
        genres=SimpleNamespace(feature_axes=lambda genres:
            {genre: (genre_map.family(genre), None) for genre in genres}),
        memoized_read=lambda key, read: read())
    # The recipe producer is below card composition. Keep the actual journey,
    # ranker, profile stamp and card construction under test.
    monkeypatch.setattr(home_cards, '_rolled_theme', lambda *args, **kwargs:
                        {'journey': 'warm_up', 'dj': {'seed': 1}, 'facets': {}})
    with home_vibe.authorized(reader, 'relax'):
        yield reader


def _prepare_opportunities(store, *, controlled=None):
    pools = {}
    # Only the fallback-ranking case needs to cross the full 40-item mode head.
    # Publication/edit/exclusion owners need a full 12-track card and contrasting
    # genres, rather than preparing 128 profiles for the same state transition.
    count, good_start = (64, 40) if controlled is not None else (24, 12)
    # All existing candidates form one input batch. Keep actual metadata and
    # evidence while avoiding a separate transaction for every subject/field.
    with (atomic_write(store) if controlled is None else nullcontext()):
        for lane in ('library', 'fresh'):
            items = []
            for i in range(count):
                key = f'{lane}-{i}'
                good = i >= good_start
                genre = ('Metal' if i % 2 else 'Techno') if good else 'Jazz'
                audio = {'energy': .05 if good else .95, 'mood_relaxed': .95 if good else .05}
                item = dict(key=key, video_id=key, title=key, artist=f'Artist {key}', album='',
                            genre=genre, score=.1 if good else 10, year='2000')
                if controlled:
                    item['musical'] = controlled(key, audio['energy'])
                elif lane == 'library':
                    seed_catalog_track(store, key, key, item['artist'], video_id=key, genre=genre,
                                       **audio)
                else:
                    store.discovery.upsert_discovered_track(key, key, key, item['artist'], '', None,
                                                           genre, '2000', None, 1, audio=audio)
                items.append(item)
            pools[lane] = items
    # Prepare profiles before the sole publication; prepare_vibes would publish
    # these same ordinary pools a second time after their initial publication.
    if controlled is None:
        prepare_profiles(store)
        publish_bundles(store, {
            '0': {'wheelhouse': pools['library'][:40], 'fresh': pools['fresh'][:40]},
            'all': {'wheelhouse': pools['library'], 'explore': pools['library'], 'fresh': pools['fresh']},
            '_meta': {'full_pools': True, 'comfort_pool': 0}}, 1000, vibes=True)
        with mix_tuning.adjusting_vibe(store, 1000):
            store.settings.set_setting(home_vibe.SETTING, 'relax')
    return pools


@pytest.fixture(scope='module')
def opportunities_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, _prepare_opportunities) as template:
        yield template


@pytest.fixture
def opportunities(store, opportunities_template):
    return opportunities_template.restore(store)


@pytest.fixture(scope='module')
def profile_stamp_schema():
    with bounded_consumer_schema((('techno', 'Techno'),)) as schema:
        yield schema


@pytest.fixture(scope='module')
def profile_stamp_template(profile_stamp_schema):
    def build(store):
        item = profile_stamp_input(store)
        # Ranking is a completed lower-layer input to this publication/consumer
        # contract. Keep real typed pools and profile files, without computing
        # six presets over 48 subjects or preparing whole Home menus twice.
        publish_bundles(store, {
            'all': {'wheelhouse': [item]},
            '_meta': {'full_pools': True, 'comfort_pool': 0},
            '_vibes': {'version': home_vibe.VERSION,
                       'pools': {'relax': {'wheelhouse': [item]}}}}, 1000)
        with mix_tuning.adjusting_vibe(store, 1000):
            store.settings.set_setting(home_vibe.SETTING, 'relax')
        return {'library': [item], 'fresh': []}

    with prepared_template(profile_stamp_schema, build) as template:
        yield template


def profile_stamp_input(store):
    key = 'profile-stamp'
    seed_catalog_track(store, key, 'Profile stamp', 'Artist', video_id=key,
                       genre='Techno', energy=.05, mood_relaxed=.95)
    publish_recording_profiles(store, 1000)
    return dict(key=key, video_id=key, title='Profile stamp', artist='Artist',
                album='', genre='Techno', score=1)


@pytest.fixture
def radio_profile_store(tmp_path, profile_stamp_schema, monkeypatch):
    # Radio persists one published profile; it does not consume a Vibe menu or
    # the template's complete SQL/file-copy isolation workflow.
    store = Store(tmp_path / 'radio-profile.db')
    try:
        store.conn.execute('PRAGMA synchronous=NORMAL')
        initialize_consumer(store, profile_stamp_schema)
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        with monkeypatch.context() as patch, reuse_reporting_reader(store, monkeypatch):
            bound_catalog(patch, entries=(('techno', 'Techno'),))
            # This consumer has one named genre, with no ancestry dependency.
            patch.setattr(genre_catalog, '_parents', lambda: {})
            item = profile_stamp_input(store)
            publish_bundles(store, {'all': {'wheelhouse': [item]},
                                   '_meta': {'full_pools': True, 'comfort_pool': 0}}, 1000)
            yield store
    finally:
        store.conn.close()


def publish_recording_profiles(store, now):
    # Keep actual catalog capture, inference, guarded publication and profile
    # files. The progressive worker also runs independent Vibe prediction and
    # content-vector producers, which this menu-stamp contract does not consume.
    with store.read_snapshot() as snapshot:
        pending = snapshot.store.prepared_content.pending()
        rows = snapshot.store.prepared_content.capture(pending)
        profiles = musical_preparation.recordings(snapshot.store, rows)
        assert profiles
        assert snapshot.publish_ordered(lambda: store.musical_profiles.publish_recordings(profiles, now))


@pytest.fixture
def profile_stamp_store(profile_stamp_template):
    # This application contract needs one private consumer. Complete file-copy,
    # inode, payload and two-consumer isolation belong to
    # test_prepared_fixture_support's profile-bearing fixture owners.
    with profile_stamp_template.open(':memory:') as store:
        yield store


def test_fit_precedes_mode_heads_within_explicit_genres_in_every_cached_fallback(
        character_consumer, controlled_profiles, monkeypatch):
    store = character_consumer
    pools = _prepare_opportunities(store, controlled=controlled_profiles)
    # The consumer receives complete lower-layer pool/catalog results. Real
    # typed reads are owned by test_vibe_pool_reads; the reserve test below
    # independently owns preparation before global/mode truncation.
    def stored(_reader, _preset, lanes, _include, _avoid, **_options):
        return {lane: deepcopy(pools['fresh'] if lane == 'fresh' else pools['library']
                              if lane in ('wheelhouse', 'explore') else []) for lane in lanes}

    monkeypatch.setattr(vibe_pools, '_stored_pools', stored)
    _controlled_eligibility(monkeypatch, store, pools['library'], pools['fresh'])
    def unexpected(*args, **kwargs):
        pytest.fail('A control must only assemble prepared opportunities')
    for name in ('for_you', 'explore_for_you', 'comfort_listening'):
        monkeypatch.setattr(surfaces, name, unexpected)
    monkeypatch.setattr(store.rec_query, 'track_audio_features', unexpected)
    monkeypatch.setattr(store.rec_query, 'library_keys', unexpected)
    monkeypatch.setattr(musical_profiles, 'resolve', unexpected)
    parameters = {'include_genres': ['techno'], 'avoid_genres': ['metal'], 'lane': 'wheelhouse', 'epoch': 0}
    cards = mode_surfaces.assemble_cards(store, 1000, 0, include_genres=['techno'], avoid_genres=['metal'])
    assert {card['lane'] for card in cards} == {'wheelhouse', 'fresh'}
    groups = [card['tracks'] for card in cards]
    groups += [fresh_pool.pick(store, 1000, ['techno'], ['metal']), library_pool.pick(store, 1000, parameters)]
    service = home_cards.HomeCards(store, SimpleNamespace(latest=lambda *_: None), logging.getLogger(__name__),
                                   genre_options=[])
    groups += [service.built_library(parameters, 1000, [])['tracks'],
               service.built_fresh(parameters, 1000)['tracks']]
    for tracks in groups:
        assert len(tracks) == 12
        assert all(genre_map.family(track['genre']) == 'techno' for track in tracks)
        assert all(int(track['key'].split('-')[-1]) >= 40 for track in tracks)
        assert all(musical_character.intensity(track) < .5 for track in tracks), [
            (track['key'], musical_character.intensity(track)) for track in tracks]
    assert all(track['serving_context']['vibe'] == 'relax' for card in cards for track in card['tracks'])
    for card in cards:
        for track in card['tracks']:
            context = track['serving_context']
            assert context['musical_version'] == track['musical']['version']
            assert context['musical_digest'] == track['musical']['digest']
            value, support = musical_character.intensity_evidence(track)
            assert context['intensity'] == value
            assert context['intensity_support'] == support
            assert context['intensity_source'] == 'provider_measurement'
            assert context['intensity_vibe'] in ('energize', 'release')
            fit = track['musical']['vibes']['fits'][context['intensity_vibe']]
            assert (value, support) == (fit['score'], fit['support'])


def test_prepared_profile_stamp_survives_a_new_profile_until_menu_publication(
        profile_stamp_store, profile_stamp_template):
    store = profile_stamp_store
    template = profile_stamp_template
    keys = [item['key'] for items in template.result.values() for item in items]
    original_profiles = template.source.musical_profiles.by_keys(keys)
    assert store.musical_profiles.by_keys(keys) == original_profiles

    # Candidate pools resolve current catalog/profile owners (#486). The
    # immutable output is the published Home menu (#487), not a new pick from
    # that pool. Exercise the actual publication and serving boundary.
    def publish(now, previous=None):
        with store.read_view() as reader:
            tracks = vibe_pools.candidates(reader, 'relax', 'wheelhouse')
            tracks = home_cards._order_by_journey(reader, tracks, 'warm_up', 1)
            proto = home_cards._proto(reader, 'wheelhouse', 'Your mix', tracks, now, vibe='relax')
        proto['recipe'] = {'lane': 'wheelhouse', 'vibe': 'relax'}
        result = home_cards.CardsResult([proto], [{'proto': proto, 'pool': None}],
            None, None, [], [], home_cards._epoch(store, 'cards'), 'relax')
        assert home_prepared.remember(store, previous, result, now)
        with store.read_view() as reader:
            return home_prepared.peek(reader, now)

    payload = publish(1000)
    assert payload and payload['menus']
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__), genre_options=[])
    def served():
        with store.read_view() as reader:
            published = home_prepared.peek(reader, 1000)
            return next(p['tracks'] for p in home_prepared.cards(
                reader, published, 1000, service, record=False).protos if p['lane'] == 'wheelhouse')
    before = served()
    track = before[0]
    tid = store.tracks.track_ids_for_videos([track['video_id']])[track['video_id']]
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .99)
    publish_recording_profiles(store, 1001)
    current = musical_character.load(store, [track['key']])[track['key']]
    assert current['digest'] != track['musical']['digest']
    after = served()
    assert after == before
    assert home_prepared.peek(store, 1000)['built_at'] == payload['built_at']
    # A newly assembled offer can use the new input; it must report that input,
    # without retroactively changing the already published menu.
    live = vibe_pools.candidates(store, 'relax', 'wheelhouse')
    assert next(t for t in live if t['key'] == track['key'])['musical']['digest'] == current['digest']
    observed = {}
    from yt_playlist.rec import journeys
    def inspect(items, journey, seed, feat):
        observed.update({item['key']: feat(item)['energy'] for item in items})
        return items
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(journeys, 'journey_order', inspect)
        home_cards._order_by_journey(store, after, 'warm_up', 1)
    assert observed[track['key']] == musical_character.intensity(track)
    replacement = publish(1001, payload)
    assert replacement and replacement['built_at'] != payload['built_at']
    refreshed = served()
    assert refreshed != before
    profiles = musical_character.load(store, [t['key'] for t in refreshed])
    assert all(t['musical']['digest'] == profiles[t['key']]['digest'] for t in refreshed)

    assert template.source.musical_profiles.by_keys(keys) == original_profiles
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def _controlled_eligibility(monkeypatch, store, library, fresh):
    owned = {item['key']: item for item in library}
    discoveries = {item['key']: item for item in fresh}
    metadata = {item['video_id']: item for item in [*library, *fresh]}
    axes = store.genres.feature_axes(item.get('genre') for item in metadata.values())
    # Eligibility/retraction integration remains real in the independent
    # worker-selection test below and test_growth_readiness.
    monkeypatch.setattr(vibe_pools, 'eligibility_inputs', lambda *args, **kwargs:
                        (set(), owned, discoveries, metadata, {}, 3600, set(), axes))


def test_selected_genre_has_reserved_candidates_beyond_the_global_vibe_head(store, controlled_profiles, monkeypatch):
    items = []
    for i in range(vibe_pools.POOL_LIMIT + 24):
        key = f'genre-reserve-{i}'
        genre = 'Techno' if i < vibe_pools.POOL_LIMIT else 'Jazz'
        controlled_profiles(key, .04 if genre == 'Techno' else .2)
        items.append({'key': key, 'video_id': key, 'title': key, 'artist': f'Artist {i}', 'genre': genre})
    bundles = {'all': {'fresh': items}}
    policies = SimpleNamespace(stamp=lambda tracks, *_: list(tracks))
    vibe_pools.prepare(store, bundles, 1000, policies)
    _controlled_eligibility(monkeypatch, store, [], items)
    assert all(t['genre'] == 'Techno' for t in vibe_pools.candidates(store, 'relax', 'fresh', bundles=bundles))
    focused = vibe_pools.pick(store, 1000, 'relax', 'fresh', bundles=bundles, include_genres=['jazz'])
    other = vibe_pools.pick(store, 1000, 'relax', 'fresh', bundles=bundles, avoid_genres=['techno'])
    assert len(focused) == len(other) == 12
    assert all(t['genre'] == 'Jazz' for t in focused + other)
    # A thin or exhausted explicit choice must not silently expand to other genres.
    assert vibe_pools.pick(store, 1000, 'relax', 'fresh', bundles=bundles, include_genres=['metal']) == []


def test_unknown_intensity_stays_neutral_and_weaker_than_a_known_endpoint_fit():
    from yt_playlist.rec import radio_intensity
    unknown, gentle, intense = {'key': 'unknown'}, sound('gentle', energy=.1), sound('intense', energy=.9)
    assert radio_intensity.narrow([unknown, gentle, intense], 0) == [gentle]
    assert radio_intensity.narrow([unknown, gentle, intense], 1) == [intense]
    assert radio_intensity.narrow([unknown, gentle, intense], .5) == [unknown, gentle, intense]
    assert musical_character.provenance(unknown)['intensity'] is None


def test_worker_selection_preserves_current_explicit_exclusions(store, opportunities):
    pool = vibe_pools.candidates(store, 'relax', 'wheelhouse')
    first, second, third = pool[:3]
    seed_dislike(store, first['key'], 2000, 1000)
    store.rec_model.set_weight(f"genre:{genre_map.family('Metal')}", 0, lo=0, now=1000)
    tid = store.tracks.track_ids_for_videos([second['video_id']])[second['video_id']]
    with store.conn:
        catalog.choose_field(store.conn, tid, 'duration_s', 100_000)
    tid = store.tracks.track_ids_for_videos([third['video_id']])[third['video_id']]
    store.tracks.set_track_genre(tid, 'Classical')
    # A newly assembled cross-genre vibe can use a corrected genre. It becomes
    # ineligible when an explicit restriction excludes its *current* genre.
    remaining = vibe_pools.pick(store, 1000, 'relax', 'wheelhouse', avoid_genres=['classical'])
    assert not {first['key'], second['key'], third['key']} & {track['key'] for track in remaining}
    assert all(track['genre'] != 'Metal' for track in remaining)


def sound(key, score=0, **overrides):
    return {'key': key, 'score': score, 'musical': musical_character.compact(
        musical_profiles.resolve([], overrides=overrides))}


@pytest.mark.parametrize('preset,preferred,other', [
    ('relax', {'instrumental': 1, 'mood_relaxed': 1, 'energy': .1},
              {'instrumental': 1, 'mood_aggressive': 1, 'energy': 1}),
    ('concentrate', {'instrumental': 1, 'energy': .3}, {'instrumental': 0, 'energy': .9}),
    ('cruise', {'energy': .5}, {'energy': .05}),
    ('energize', {'mood_happy': .1, 'energy': 1}, {'mood_happy': 1, 'energy': .1}),
    ('uplift', {'mood_happy': 1, 'energy': .1}, {'mood_happy': .1, 'energy': 1}),
    ('release', {'mood_aggressive': 1, 'energy': .1}, {'mood_aggressive': 0, 'energy': 1}),
])
def test_neighboring_presets_use_distinct_character_dimensions(preset, preferred, other):
    assert home_vibe.affinity(sound('preferred', **preferred), preset) > home_vibe.affinity(sound('other', **other), preset)


def test_actual_taste_scores_break_close_fit_ties_independently_of_pool_order_and_size():
    weak = sound('weak', score=100, energy=1)
    low = sound('low', score=-2, energy=.1)
    high = sound('high', score=2, energy=.11)
    assert sorted([weak, low, high], key=lambda item: home_vibe.order_key(item, 'relax')) == [high, low, weak]
    assert home_vibe.taste_score(high) == home_vibe.taste_score({**high, 'key': 'in-another-pool'})
    assert home_vibe.taste_score({'key': 'unknown'}) == .5


def test_clicked_library_fallback_prepares_the_shared_pools(store, monkeypatch):
    store.settings.set_setting(home_vibe.SETTING, 'release')
    calls = []
    monkeypatch.setattr(mode_surfaces, 'prepare_bundles', lambda s, now: calls.append((s, now)))
    library_pool.prepare(store, 1000, {'lane': 'explore', 'include_genres': ['jazz'], 'avoid_genres': []})
    assert calls == [(store, 1000)]


@pytest.fixture
def preference_store(tmp_path, monkeypatch):
    # Preference serving consumes completed cards. Genre import and snapshot
    # conflict detection have independent owners; retain real settings/files.
    with bounded_consumer_schema((('house', 'House'),), snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as database:
            with reuse_reporting_reader(database, monkeypatch):
                yield database


def test_prepared_set_from_another_vibe_is_served_with_a_preference_notice(preference_store):
    store = preference_store
    # The consumer receives a completed worker menu. Profile inference and
    # ranking are independent of retaining that menu after a preference change.
    with mix_tuning.adjusting_vibe(store, 1000):
        store.settings.set_setting(home_vibe.SETTING, 'relax')
    payload = publish_menu(store, vibe='relax', tracks_per_card=1)
    assert payload and payload['menus']
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__), genre_options=[])
    assert home_prepared.cards(store, payload, 1000, service, record=False).protos
    with mix_tuning.adjusting_vibe(store, 1000):
        store.settings.set_setting(home_vibe.SETTING, 'energize')
    scheduled = []
    fallback = home_prepared.cards(store, payload, 1000, service, lambda: scheduled.append(True), record=False)
    assert fallback.protos and fallback.preparing
    assert fallback.preference_notice == "Heads up - we're unable to create energize mix tapes right now."
    assert all(p['recipe']['vibe'] == 'relax' for p in fallback.protos)
    assert scheduled  # Partial menus and stale tuning can each request the same coalesced preparation.
    with mix_tuning.adjusting_vibe(store, 1000):
        store.settings.delete_setting(home_vibe.SETTING)
    assert home_prepared.cards(store, payload, 1000, service, record=False).protos


def test_radio_midpoint_is_neutral_and_saved_session_preserves_profile_stamps(radio_profile_store):
    from yt_playlist.rec import radio_tuning
    from yt_playlist.rec.radio_candidates import RadioCandidates
    from yt_playlist.web import radio_persistence
    from yt_playlist.web.radio_state import Phase, Session
    from yt_playlist.rec.radio_directions import RadioDirections
    store = radio_profile_store
    c = RadioCandidates(store, 1000, radio_tuning.DEFAULTS)
    track = c._load('local')[0]
    genres = SimpleNamespace(breadth=.5, bonus=lambda _: 0)
    low, high = sound('low', energy=0), sound('high', energy=1)
    assert c._score({**track, 'musical': low['musical']}, [], None, {}, genres) == c._score(
        {**track, 'musical': high['musical']}, [], None, {}, genres)
    s = Session(object(), c, 1, 'On Air', phase=Phase.PLAYING, playlist='PL', tracks=[track],
                position=0, furthest=0, heard=[track])
    s.directions = RadioDirections(c, track)
    s.directions.update([track], [], 0)
    state = json.loads(radio_persistence.encode(s))
    before = deepcopy(state)
    newer = RadioCandidates(store, 1001, radio_tuning.DEFAULTS, now_source=c)
    restored = radio_persistence.restore(state, object(), 1, newer)
    assert restored.tracks == before['tracks']
    assert restored.directions.slots == before['directions']['slots']
    assert restored.tracks[0]['musical']['version'] == musical_character.VERSION
    assert restored.position == 0 and restored.playlist == 'PL'
