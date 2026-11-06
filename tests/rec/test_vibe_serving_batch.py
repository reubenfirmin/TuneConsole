"""Specialist score reads preserve full-profile selection and exact provenance."""
from types import SimpleNamespace

import pytest

from yt_playlist.rec import home_vibe, musical_character, vibe_pools, vibe_serving
from tests.test_growth_readiness import vibe_menu_template, vibe_menus


def test_deep_dive_reuses_pinned_plays_and_rechecks_later_listening(file_backed_store, monkeypatch):
    from yt_playlist.rec import home_cards, journeys
    from yt_playlist.repos.rec_query import RecQueryRepo
    from yt_playlist.util.matching import identity_key

    store = file_backed_store
    account = store.accounts.upsert_account('Listening', 'credential', None, True)
    items = []
    for index in range(12):
        title, artist, video = f'Deep song {index}', f'Deep artist {index}', f'deep-{index}'
        store.tracks.upsert_track(video, title, artist, None, None)
        items.append({'key': identity_key(title, artist), 'video_id': video,
                      'title': title, 'artist': artist, 'genre': 'Rock', 'musical': None})
    counts = {}
    for index, item in enumerate(items[:6]):
        store.history.record_play_event(account, item['key'], item['video_id'], 100000 + index * 86400,
                                        source_timezone='UTC')
        counts[item['key']] = 1
    store.history.record_play_event(account, items[0]['key'], items[0]['video_id'], 100000 + 20 * 86400,
                                    source_timezone='UTC')
    counts[items[0]['key']] = 2
    assert store.rec_query.play_counts() == counts
    catalog = {'journey_catalog': {'genres': {}, 'decades': {}, 'axes': {'Rock': ('rock', None)}}}

    def expected(plays):
        return [item['key'] for item in journeys.journey_order(items, 'deep_dive', 7,
            lambda item: {'artist': item['artist'], 'genre': 'Rock', 'family': 'rock',
                          'energy': .5, 'decade': None, 'plays': plays.get(item['key'], 0), 'recency': 0.})]

    original = RecQueryRepo.play_counts
    calls = []
    def play_counts(repo):
        calls.append(repo)
        return original(repo)
    monkeypatch.setattr(RecQueryRepo, 'play_counts', play_counts)
    first = expected(counts)
    with store.read_view(tracked=True) as reader:
        for _ in range(4):
            actual = home_cards._order_by_journey(reader, [dict(item) for item in items], 'deep_dive', 7, catalog)
            assert [item['key'] for item in actual] == first
    assert len(calls) == 1
    for day in range(4):
        store.history.record_play_event(account, items[-1]['key'], items[-1]['video_id'],
                                        100000 + (30 + day) * 86400, source_timezone='UTC')
    counts[items[-1]['key']] = 4
    second = expected(counts)
    assert second != first
    with store.read_view(tracked=True) as reader:
        actual = home_cards._order_by_journey(reader, [dict(item) for item in items], 'deep_dive', 7, catalog)
        assert [item['key'] for item in actual] == second
    assert len(calls) == 2


@pytest.mark.parametrize('tracked', [False, True])
def test_rediscovery_batches_cards_and_shadows_without_reusing_changed_history(
        file_backed_store, monkeypatch, tracked):
    from yt_playlist.rec import home_cards, journeys
    from yt_playlist.repos.rec_query import RecQueryRepo

    store = file_backed_store
    account = store.accounts.upsert_account('Listening', 'credential', None, True)
    items = []
    for index in range(24):
        video = f'revisit-{index}'
        store.tracks.upsert_track(video, f'Revisit {index}', f'Artist {index}', None, None)
        items.append({'key': store.tracks.identity_key_for_video(video), 'video_id': video,
                      'title': f'Revisit {index}', 'artist': f'Artist {index}',
                      'genre': 'Rock', 'musical': None})
    for index, item in enumerate(items[:20]):
        store.history.record_play_event(account, item['key'], item['video_id'], 100000 + index * 86400,
                                        source_timezone='UTC')
    cards = [{'tracks': items[:12], 'counterfactual': {'tracks': items[12:]}},
             {'tracks': items[6:18]}]
    calls, query = [], RecQueryRepo.track_last_played

    def last_played(repo, keys):
        calls.append(tuple(keys))
        return query(repo, keys)

    monkeypatch.setattr(RecQueryRepo, 'track_last_played', last_played)

    def check():
        # Derive the ordering from retained evidence, including never-played keys.
        last = dict(store.conn.execute('SELECT identity_key,MAX(at) FROM listening_activity '
                                       'GROUP BY identity_key').fetchall())
        orders = []
        with store.read_view(tracked=tracked) as reader:
            selection = {'journey_catalog': home_cards._completion_catalog(reader, cards)}
            assert set(selection['journey_catalog']['keys']) == {item['key'] for item in items}
            for source in (items[:12], items[12:], items[6:18]):
                expected = journeys.journey_order(source, 'rediscovery', 7,
                    lambda item: {'artist': item['artist'], 'genre': 'Rock', 'family': 'rock',
                        'energy': .5, 'decade': None, 'plays': 0, 'recency': last.get(item['key'], 0.)})
                actual = home_cards._order_by_journey(reader, [dict(item) for item in source],
                                                     'rediscovery', 7, selection)
                orders.append([item['key'] for item in actual])
                assert orders[-1] == [item['key'] for item in expected]
        return orders

    before = check()
    assert len(calls) == 1 and set(calls[0]) == {item['key'] for item in items}
    store.history.record_play_event(account, items[0]['key'], items[0]['video_id'], 4000000,
                                    source_timezone='UTC')
    assert check() != before
    assert len(calls) == 2


@pytest.mark.parametrize('metadata', [[], [{'genre': 'Jazz'}], [{'mb_year': '1997'}],
    [{'genre': 'Jazz'}, {'mb_year': '1997'}, {'genre': 'House', 'mb_year': '2003'},
     {'genre': 'Jazz', 'mb_year': '1998'}, {'mb_year': 'unknown'}, {}]])
def test_ordering_facets_preserve_independent_genre_and_year_support(store, metadata):
    from tests.conftest import seed_catalog_track
    for index, fields in enumerate(metadata):
        seed_catalog_track(store, f'facet-{index}', f'Facet {index}', 'Artist',
                           video_id=f'facet-{index}', **fields)
    genres, eras = store.rec_query.recipe_facet_keys()
    expected_genres = {family: 1 for family, _ in store.genres.feature_axes(
        fields['genre'] for fields in metadata if fields.get('genre')).values()}
    expected_eras = {str(int(fields['mb_year']) // 10 * 10): 1 for fields in metadata
                     if str(fields.get('mb_year', '')).isdigit()}
    assert genres == expected_genres
    assert eras == expected_eras


def test_all_presets_match_full_profile_selection_and_only_open_selected_profiles(store, monkeypatch, vibe_menus):
    stored, load = vibe_pools._stored_pools, musical_character.load
    for preset in home_vibe.PRESETS:
        with monkeypatch.context() as patch:
            def full(reader, preset, lanes, include, avoid, **options):
                return stored(reader, preset, lanes, include, avoid, ranking_only=False)
            patch.setattr(vibe_pools, '_stored_pools', full)
            expected = vibe_pools.assemble(store, 1000, preset)
        opened = []
        with monkeypatch.context() as patch:
            def selected(reader, keys):
                opened.extend(keys)
                return load(reader, keys)
            patch.setattr(musical_character, 'load', selected)
            actual = vibe_serving.assemble(store, 1000, preset)
        assert actual == expected
        assert len(actual) == 4
        selected_keys = {track['key'] for card in actual for track in card['tracks']}
        assert set(opened) == selected_keys
        assert len(opened) == len(selected_keys)
        assert all(track['musical']['digest'] == track['serving_context']['musical_digest']
                   and track['serving_context']['vibe'] == preset for card in actual for track in card['tracks'])


def test_missing_relational_score_uses_existing_profile_fallback(store, monkeypatch, vibe_menus):
    with store.conn:
        store.conn.execute("DELETE FROM recording_vibes WHERE vibe='relax'")
    stored = vibe_pools._stored_pools
    expected = vibe_serving.assemble(store, 1000, 'relax')
    def full(reader, preset, lanes, include, avoid, **options):
        return stored(reader, preset, lanes, include, avoid, ranking_only=False)
    monkeypatch.setattr(vibe_pools, '_stored_pools', full)
    assert vibe_serving.assemble(store, 1000, 'relax') == expected


def test_retained_menus_follow_current_fits_without_republication(file_backed_store, vibe_menu_template):
    from yt_playlist.core import metadata_evidence
    from yt_playlist.rec import musical_preparation, vibe_preparation, vibe_readiness
    from yt_playlist.rec.progressive import Preparation

    store = file_backed_store
    vibe_menus = vibe_menu_template.restore(store)
    head = store.recommendations.head('home')['id']
    with store.read_view(tracked=True) as reader:
        assert vibe_readiness.ready(reader, 1000, 0)
        original = vibe_serving.assemble(reader, 1000, 'relax')
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        for lane, tracks in vibe_menus.items():
            if lane == 'fresh':
                continue
            for index, track in enumerate(tracks):
                entry = store.conn.execute('SELECT id FROM catalog_tracks WHERE identity_key=?',
                                           (track['key'],)).fetchone()[0]
                for field, value in {'energy': .9 if index % 2 == 0 else .1,
                                     'mood_relaxed': .1 if index % 2 == 0 else .9,
                                     'mood_happy': .2 if index % 2 == 0 else .8}.items():
                    metadata_evidence.choose(store.conn, entry, field, value, now=1001)
    worker = Preparation(store)
    for turn in range(1000):
        if (not store.prepared_content.has_pending() and not musical_preparation.due(store)
                and not vibe_preparation.due(store)):
            break
        worker.capture(1002 + turn * 5)
    else:
        pytest.fail('Normal profile/Vibe preparation did not drain')
    changed = False
    with store.read_view(tracked=True) as reader:
        assert vibe_readiness.ready(reader, 1000, 0)
        for preset in home_vibe.PRESETS:
            actual = vibe_serving.assemble(reader, 1000, preset)
            expected = vibe_pools.assemble(reader, 1000, preset)
            assert actual == expected
            assert len(actual) == 4
            assert all(track['serving_context']['vibe'] == preset
                       and track['musical']['digest'] == track['serving_context']['musical_digest']
                       for card in actual for track in card['tracks'])
            if preset == 'relax':
                changed = actual != original
    assert changed
    assert store.recommendations.head('home')['id'] == head


@pytest.mark.parametrize('minimum', [0, .35])
@pytest.mark.parametrize('distributions', [({}, {}), ({'Techno': .9, 'Rock': .1}, {}),
    ({}, {'1990': .8, '2000': .2}), ({'Techno': .9, 'Rock': .1}, {'1990': .8, '2000': .2})])
def test_completion_retains_original_seeded_journey_and_dj(store, monkeypatch, minimum, distributions):
    from yt_playlist.rec import recipes, rec_params, transient
    genres, eras = distributions
    monkeypatch.setattr(store.rec_query, 'recipe_facet_keys',
        lambda: (dict.fromkeys(genres, 1), dict.fromkeys(eras, 1)))
    monkeypatch.setattr(recipes, 'taste_breadth', lambda _store: {'families': genres})
    monkeypatch.setattr(recipes, 'era_distribution', lambda _store: list(eras.items()))
    monkeypatch.setattr(transient, 'facet_leans', lambda *_: {'genre:Techno': -100, 'era:2000': -100})
    rec_params.set_param(store, 'facet_mult_min', minimum)
    for weights in ({}, {'genre:Techno': 0, 'genre:Rock': 0, 'era:1990': 0, 'era:2000': 0}):
        monkeypatch.setattr(store.rec_model, 'get_weights', lambda **_: weights)
        for lane in ('wheelhouse', 'explore', 'comfort', 'fresh', 'temporal'):
            for seed in range(12):
                original = recipes.roll_recipe(store, lane, seed=seed, now=1000)
                completed = recipes.roll_recipe(store, lane, seed=seed, now=1000, ordering_only=True)
                assert completed == {key: original[key] for key in ('journey', 'dj')}


def test_authorization_is_limited_to_the_guarded_operation_and_store(store, monkeypatch):
    from yt_playlist.rec import mix_tuning
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, '1000')
    monkeypatch.setattr(home_vibe, '_access', lambda *_: False)
    assert home_vibe.current(store, 1000) == ''
    with home_vibe.authorized(store):
        assert home_vibe.current(store, 1000) == 'relax'
        with home_vibe.natural():
            assert home_vibe.current(store, 1000) == ''
        other = SimpleNamespace(settings=store.settings, _read_only=True)
        assert home_vibe.current(other, 1000) == ''
    assert home_vibe.current(store, 1000) == ''


def test_cached_readiness_document_is_immutable_and_rechecks_current_profiles(store, vibe_menus):
    from yt_playlist.rec import vibe_readiness
    first = vibe_readiness.load(store, (), ())
    assert first is not None
    with pytest.raises(TypeError):
        first['tracks'][0]['order']['relax'] = (0, 0, 0)
    with store.conn:
        store.conn.execute("DELETE FROM recording_vibes WHERE vibe='relax'")
    current = vibe_readiness.load(store, (), ())
    assert current is not None
    assert all('relax' not in track['supported'] for track in current['tracks'])
    assert not vibe_readiness.ready(store, 1000, 0)
    # Revalidation never mutates the shared publication or its old support.
    assert any('relax' in track['supported'] for track in first['tracks'])
    with pytest.raises(TypeError):
        current['tracks'][0]['order']['relax'] = (0, 0, 0)


def test_retained_menu_checks_each_presets_current_evidence_policy(file_backed_store, vibe_menu_template):
    from yt_playlist.core import vibe_versions
    from yt_playlist.rec import vibe_readiness

    store = file_backed_store
    vibe_menu_template.restore(store)
    head = store.recommendations.head('home')['id']
    column = vibe_versions.column(store.conn, 'recording_vibes')
    original = [tuple(row) for row in store.conn.execute(
        f"SELECT {column},recording_id FROM recording_vibes WHERE vibe='release'")]
    assert original
    with store.read_view(tracked=True) as before:
        assert vibe_readiness.ready(before, 1000, 0)
        saved = vibe_readiness.load(before, (), ())
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            store.conn.execute(f"UPDATE recording_vibes SET {column}=? WHERE vibe='release'",
                               (vibe_versions.stored(store.conn, 'obsolete-fit-policy'),))
        with store.read_view(tracked=True) as obsolete:
            value = vibe_readiness.load(obsolete, (), ())
            assert value is not None
            assert any('relax' in track['supported'] for track in value['tracks'])
            assert all('release' not in track['supported'] for track in value['tracks'])
            # Check the warmed key and a cold key, plus an uncached reader.
            assert not vibe_readiness.ready(obsolete, 1000, 0)
            assert not vibe_readiness.ready(obsolete, 1001, 0)
            with store.read_view() as uncached:
                assert not vibe_readiness.ready(uncached, 1000, 0)
            assert vibe_readiness.ready(before, 1000, 0)
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                store.conn.executemany(
                    f"UPDATE recording_vibes SET {column}=? WHERE recording_id=? AND vibe='release'",
                    original)
            with store.read_view(tracked=True) as restored:
                assert vibe_readiness.ready(restored, 1000, 0)
                assert vibe_readiness.load(restored, (), ()) == saved
                assert not vibe_readiness.ready(obsolete, 1000, 0)
                assert vibe_readiness.ready(before, 1000, 0)
    assert store.recommendations.head('home')['id'] == head


def test_explicit_selection_overrides_only_its_own_pinned_reader(store, monkeypatch):
    from yt_playlist.rec import mix_tuning
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    store.settings.set_setting(mix_tuning.VIBE_UPDATED_AT_SETTING, '1000')
    monkeypatch.setattr(home_vibe, '_access', lambda *_: False)
    with store.read_view() as reader:
        store.settings.set_setting(home_vibe.SETTING, 'concentrate')
        assert reader.settings.get_setting(home_vibe.SETTING) == 'relax'
        with home_vibe.authorized(store, 'concentrate'):
            assert home_vibe.current(reader, 1000) == 'concentrate'
            assert home_vibe.current(SimpleNamespace(settings=reader.settings, _read_only=True), 1000) == ''
            with home_vibe.natural():
                assert home_vibe.current(reader, 1000) == ''
        assert home_vibe.current(reader, 1000) == ''
        with home_vibe.authorized(store, ''):
            assert home_vibe.current(reader, 1000) == ''


def test_tracked_catalog_reuse_rechecks_changed_membership_and_live_feedback(store, monkeypatch, vibe_menus):
    from tests.derived_rating_support import seed_dislike
    from yt_playlist.repos.rec_query import RecQueryRepo
    original_read = RecQueryRepo.group_candidates
    catalog_reads = []
    def read(repo, keys):
        catalog_reads.append(tuple(keys))
        return original_read(repo, keys)
    monkeypatch.setattr(RecQueryRepo, 'group_candidates', read)
    def select(now):
        with store.read_view(tracked=True) as reader:
            return vibe_serving.assemble(reader, now, 'relax')
    original = select(1000)
    first = original[0]['tracks'][0]
    assert select(1001) == original
    catalog_keys = catalog_reads[0]
    catalog_count = catalog_reads.count(catalog_keys)
    with store.conn:
        store.conn.execute('UPDATE recordings SET last_enriched_at=1002')
    assert select(1002) == original
    # The eligibility catalog remains reusable when only enrichment's
    # bookkeeping advances; feedback and availability still stay live below.
    assert catalog_reads.count(catalog_keys) == catalog_count
    seed_dislike(store, first['key'], until=1003, now=1001)
    assert first['key'] not in {item['key'] for card in select(1002) for item in card['tracks']}
    assert first['key'] in {item['key'] for card in select(1004) for item in card['tracks']}
    with store.conn:
        store.conn.execute('UPDATE youtube_items SET available=0 WHERE video_id=?', (first['video_id'],))
    assert first['key'] not in {item['key'] for card in select(1005) for item in card['tracks']}


def test_vibe_menu_copies_keep_scores_profiles_and_payloads_private(
        store, file_backed_store, vibe_menu_template):
    from tests.prepared_fixture_support import roots

    template = vibe_menu_template
    first, second = template.restore(store), template.restore(file_backed_store)
    assert first == second == template.result
    keys = [track['key'] for lane in second.values() for track in lane]
    assert len(keys) == 116
    profiles = template.source.musical_profiles.by_keys(keys)
    assert len(profiles) == 116
    assert store.musical_profiles.by_keys(keys) == file_backed_store.musical_profiles.by_keys(keys) == profiles
    namespaces = [set(roots(target.conn).values()) for target in (template.source, store, file_backed_store)]
    assert all(left.isdisjoint(right) for index, left in enumerate(namespaces) for right in namespaces[index + 1:])
    scores = template.source.conn.execute('SELECT * FROM recording_vibes ORDER BY recording_id,vibe').fetchall()
    expected = vibe_serving.assemble(file_backed_store, 1000, 'relax')
    assert len(expected) == 4
    first['fresh'].clear()
    store._fixture_bundle_payload['all']['fresh'].clear()
    with store.conn:
        store.conn.execute("DELETE FROM recording_vibes WHERE vibe='relax'")
    assert second == template.result and len(second['fresh']) == 48
    for other in (template.source, file_backed_store):
        assert other.conn.execute('SELECT * FROM recording_vibes ORDER BY recording_id,vibe').fetchall() == scores
        assert len(other._fixture_bundle_payload['all']['fresh']) == 48
    assert vibe_serving.assemble(file_backed_store, 1000, 'relax') == expected


def test_ordered_selection_stops_after_eligible_diverse_tracks_fill_the_card():
    tracks = [{'key': str(index), 'artist': 'Repeated' if index < 20 else str(index),
               'album': str(index), 'genre': 'Jazz', 'video_id': str(index)} for index in range(160)]
    checked = []
    class Owned(dict):
        def get(self, key):
            checked.append(key)
            return super().get(key)
    owned = Owned((item['key'], item) for item in tracks)
    inputs = ({'0'}, owned, {}, {}, {}, 3600, set(), {'Jazz': ('Jazz', None)})
    selected = vibe_pools.diversify(vibe_pools.iter_eligible(None, tracks, 0, 'wheelhouse', inputs=inputs), 2, 2)
    assert [item['key'] for item in selected] == ['1', '2', *map(str, range(20, 30))]
    assert checked == list(map(str, range(30)))
    assert selected == vibe_pools.diversify(vibe_pools.eligible(None, tracks, 0, 'wheelhouse', inputs=inputs), 2, 2)


def test_batched_catalog_matches_separate_upload_metadata_after_corrections(store):
    from tests.conftest import seed_catalog_track
    from yt_playlist.core import metadata

    entry = seed_catalog_track(store, 'owned|artist', 'Owned', 'Artist', video_id='owned',
                               genre='Jazz', duration_s=120)
    alternative = store.tracks.upsert_track('alternative', 'Owned', 'Artist', '', 200)
    with store.conn:
        metadata.select(store.conn, alternative, 'genre', 'Rock', 'fixture')
    store.discovery.upsert_discovered_track('new|other', 'new', 'New', 'Other', '', None,
                                            'Soul', None, None, 1)
    items = [{'key': 'owned|artist', 'genre': 'Jazz'}, {'key': 'new|other', 'genre': 'Soul'}]
    keys = [item['key'] for item in items]
    for change in ('original', 'genre', 'availability'):
        with store.conn:
            if change == 'genre':
                metadata.select(store.conn, entry, 'genre', 'Ambient', 'correction')
            elif change == 'availability':
                store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='owned'")
        owned = store.rec_query.tracks_by_keys(keys)
        discoveries = store.discovery.candidate_tracks_by_keys(keys)
        separate = store.rec_eligibility.offer_metadata(
            [row['video_id'] for row in [*owned.values(), *discoveries.values()]])
        with store.read_view(tracked=True) as reader:
            actual = vibe_pools.eligibility_inputs(reader, items, 1000)
        assert actual[3] == separate
        assert {key: {field: row[field] for field in owned[key]} for key, row in actual[1].items()} == owned
        assert actual[2] == discoveries


def test_cached_catalog_cannot_be_changed_by_a_selection_reader(store):
    from tests.conftest import seed_catalog_track
    seed_catalog_track(store, 'owned|artist', 'Owned', 'Artist', video_id='owned',
                       genre='Jazz', duration_s=120)
    items = [{'key': 'owned|artist', 'genre': 'Jazz'}]
    with store.read_view(tracked=True) as reader:
        first = vibe_pools.eligibility_inputs(reader, items, 1000)
        with pytest.raises(TypeError):
            first[1]['owned|artist']['title'] = 'Changed'
        with pytest.raises(TypeError):
            first[3]['owned'] = {}
    with store.read_view(tracked=True) as reader:
        second = vibe_pools.eligibility_inputs(reader, items, 1001)
        assert second[1]['owned|artist']['title'] == 'Owned'
        assert second[3]['owned']['duration'] == 120


def test_owned_recordings_skip_discovery_hydration_until_ownership_is_unavailable(store, monkeypatch):
    from tests.conftest import seed_catalog_track
    from yt_playlist.core import metadata
    from yt_playlist.repos.discovery import DiscoveryRepo

    key = 'shared|artist'
    rid = seed_catalog_track(store, key, 'Shared', 'Artist', video_id='owned-upload',
                             genre='Jazz', duration_s=120)
    # Two uploads of one explicitly established recording, with independent
    # owned and discovery membership. A title match alone is not identity.
    with store.conn:
        store.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist) '
                           "VALUES ('discovery-upload',?,'Shared','Artist')", (rid,))
        entry = store.conn.execute('INSERT INTO catalog_entries(recording_id,video_id) '
                                   "VALUES (?,'discovery-upload')", (rid,)).lastrowid
        store.conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,1)',
                           (rid, entry))
        metadata.select(store.conn, entry, 'genre', 'Rock', 'fixture')
    items = [{'key': key}]
    calls, original = [], DiscoveryRepo.candidate_tracks_by_keys

    def read(repo, keys):
        calls.append(list(keys))
        return original(repo, keys)

    monkeypatch.setattr(DiscoveryRepo, 'candidate_tracks_by_keys', read)
    with store.read_view() as reader:
        inputs = vibe_pools.eligibility_inputs(reader, items, 1000)
        assert inputs[1][key]['video_id'] == 'owned-upload'
        assert inputs[2] == {}
        assert list(vibe_pools.iter_eligible(reader, items, 1000, 'fresh', inputs=inputs)) == []
        assert [item['key'] for item in vibe_pools.iter_eligible(
            reader, items, 1000, 'wheelhouse', inputs=inputs)] == [key]
    assert calls == [[]]
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='owned-upload'")
    with store.read_view() as reader:
        inputs = vibe_pools.eligibility_inputs(reader, items, 1001)
        assert inputs[1] == {}
        assert inputs[2][key]['video_id'] == 'discovery-upload'
        # A current discovery entry remains usable after its owned upload is
        # lost; normal current-genre checking still applies to the fresh lane.
        current_items = [{'key': key, 'genre': inputs[2][key]['genre']}]
        assert [item['key'] for item in vibe_pools.iter_eligible(
            reader, current_items, 1001, 'fresh', inputs=inputs)] == [key]
    assert calls == [[], [key]]


def test_shared_completion_metadata_matches_separate_cards_and_invalidates_after_corrections(store, monkeypatch):
    from copy import deepcopy
    from tests.conftest import seed_catalog_track
    from yt_playlist.core import metadata
    from yt_playlist.rec import home_cards, journeys, recipes
    from yt_playlist.repos.rec_query import RecQueryRepo

    items, entries = [], []
    for index in range(48):
        title, artist = f'Song {index}', f'Artist {index % 5}'
        key = f'song {index}|artist {index % 5}'
        entries.append(seed_catalog_track(store, key, title, artist, video_id=f'video-{index}',
            genre='Jazz' if index % 2 else 'Ambient', mb_year=str(1980 + index)))
        items.append({'key': key, 'title': title, 'artist': artist, 'video_id': f'video-{index}',
                      'genre': 'Old tag', 'year': 2020 if index % 3 else None, 'musical': None})
    # An alternate upload's metadata and an out-of-corpus artist fallback must
    # keep the same interpretation as the original per-card queries.
    alternate = store.tracks.upsert_track('alternate', 'Song 0', 'Artist 0', '', 180)
    with store.conn:
        metadata.select(store.conn, alternate, 'genre', 'Rock', 'fixture')
    items[15] = {'key': 'fresh|artist 0', 'title': 'Fresh', 'artist': 'Artist 0',
                 'video_id': 'fresh', 'genre': '', 'musical': None}
    cards = [{'lane': lane, 'label': lane, 'mode_id': index, 'ranker': 'fixture',
              'tracks': items[index * 12:(index + 1) * 12],
              'counterfactual': {'tracks': list(reversed(items[index * 12:(index + 1) * 12]))}}
             for index, lane in enumerate(('wheelhouse', 'explore', 'comfort', 'fresh'))]
    monkeypatch.setattr(RecQueryRepo, 'play_counts', lambda _: {item['key']: index for index, item in enumerate(items)})
    monkeypatch.setattr(RecQueryRepo, 'track_last_played', lambda _, keys: {key: index for index, key in enumerate(sorted(keys))})
    reads, original = [], RecQueryRepo.track_genres
    def counted(repo, keys):
        reads.append(tuple(keys))
        return original(repo, keys)
    monkeypatch.setattr(RecQueryRepo, 'track_genres', counted)
    for correction in (False, True):
        if correction:
            with store.conn:
                metadata.select(store.conn, entries[25], 'genre', 'Soul', 'correction')
                metadata.select(store.conn, entries[25], 'mb_year', '1951', 'correction')
        with store.read_view(tracked=True) as reader:
            catalog_reads = []
            for journey in journeys.JOURNEYS:
                monkeypatch.setattr(recipes, 'roll_recipe', lambda *_, **__: {
                    'journey': journey, 'dj': {'seed': 31, 'stickiness': .5}})
                expected = [home_cards._complete_card(reader, deepcopy(card), 1000, 0, None, None)
                            for card in cards]
                before = len(reads)
                actual = []
                for card in cards:
                    def complete():
                        return home_cards._complete_card(reader, deepcopy(card), 1000, 0, None,
                            {'journey_catalog': home_cards._completion_catalog(reader, cards)})
                    actual.append(reader.memoized_read(('completion-parity', journey, card['lane']), complete))
                catalog_reads.extend(reads[before:])
                assert actual == expected
            assert len(catalog_reads) == 1
            assert len(catalog_reads[0]) == 48
