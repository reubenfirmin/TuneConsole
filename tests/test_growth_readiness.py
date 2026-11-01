"""Onboarding consumes the feature owners' real serving selection and exclusions."""
from copy import deepcopy

from tests.recommendation_support import publish_bundles, seed_bundle_subjects
from yt_playlist.rec import album_feedback, growth_readiness, home_prepared, musical_character, vibe_preparation


def fresh_reserve(store, count=48, score=.8):
    tracks = [{'key': f'new {i}|artist {i}', 'video_id': f'new-{i}', 'title': f'New {i}',
               'artist': f'Artist {i}', 'genre': 'Jazz', 'score': score} for i in range(count)]
    bundle = {'all': {'fresh': tracks}}
    seed_bundle_subjects(store, bundle)
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
    store.rec_model.record_dislike(tracks[0]['key'], until=None, now=1000)
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


def test_compatible_vibe_evidence_survives_model_replacement_and_still_rechecks_feedback(store, monkeypatch):
    lanes = publish_vibe_menus(store)
    assert growth_readiness.vibes(store, 1000)
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'replacement-model')
    assert vibe_preparation.due(store)
    assert growth_readiness.vibes(store, 1001)
    for track in lanes['fresh'][3:]:
        store.rec_model.record_dislike(track['key'], until=None, now=1002)
    assert not growth_readiness.vibes(store, 1002)


def publish_vibe_menus(store):
    from tests.conftest import seed_catalog_track
    from tests.musical_support import prepare_profiles
    from yt_playlist.rec import home_vibe, vibe_pools

    # Controlled music with contrasting measured attributes, processed by the
    # ordinary profile worker and selected by the real Vibe assembler.
    lanes = {}
    for lane in ('wheelhouse', 'explore', 'fresh', 'comfort'):
        tracks = []
        for index in range(48 if lane == 'fresh' else 36 if lane == 'comfort' else 16):
            key = f'{lane} {index}|{lane} artist {index}'
            soft = index % 2 == 0
            audio = {'energy': .1 if soft else .9, 'mood_relaxed': .9 if soft else .1,
                     'mood_happy': .8 if soft else .2}
            track = {'key': key, 'video_id': key, 'title': f'{lane} {index}',
                     'artist': f'{lane} artist {index}', 'genre': 'Jazz', 'score': .8}
            if lane == 'fresh':
                store.discovery.upsert_discovered_track(key, key, track['title'], track['artist'],
                    '', None, 'Jazz', None, None, 1, audio=audio)
            else:
                entry = seed_catalog_track(store, key, track['title'], track['artist'],
                                           video_id=key, genre='Jazz')
                from yt_playlist.core import metadata
                with store.conn:
                    for field, value in audio.items():
                        metadata.select(store.conn, entry, field, value, 'fixture')
            tracks.append(track)
        lanes[lane] = tracks
    prepare_profiles(store)
    bundle = {'all': lanes, '_meta': {'comfort_pool': 36}}
    publish_bundles(store, bundle, vibes=True)
    return lanes


def test_vibe_gate_checks_real_menus_for_every_preset_and_current_exclusions(store):
    from yt_playlist.rec import home_vibe, vibe_pools

    lanes = publish_vibe_menus(store)
    assert all(len(vibe_pools.assemble(store, 1000, preset)) == 4 for preset in home_vibe.PRESETS)
    assert growth_readiness.vibes(store, 1000)
    for track in lanes['fresh'][3:]:
        store.rec_model.record_dislike(track['key'], until=None, now=1001)
    assert not growth_readiness.vibes(store, 1001)


def test_vibe_readiness_resolves_repeated_genres_in_batches(store):
    publish_vibe_menus(store)
    queries = []
    with store.read_view() as reader:
        reader.conn.set_trace_callback(queries.append)
        assert growth_readiness.vibes(reader, 1000)
    genre_queries = [query for query in queries
                     if 'genre_catalog' in query or 'genre_redirects' in query]
    # Six presets each inspect four full candidate lanes. Repeated labels must
    # not turn this frequent Home read into thousands of identity lookups.
    assert len(genre_queries) < 500, len(genre_queries)
