"""Ordinary Home cards preserve complete opportunities without a bundle read."""
from copy import deepcopy
from tests.derived_rating_support import seed_dislike
import numpy as np
import pytest

from tests.legacy_recommendation_fixture_support import recommendations
from tests.enrichment_support import seed_discovered_enrichment
from tests.repos.test_typed_recommendation_publication import publish, plan
from tests.rec.test_stored_fresh import fresh, forbid_legacy
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, mock_snapshot_coordinator,
    native_sql_calls, reuse_reporting_reader, without_query_timing,
)
from yt_playlist.rec import bundle_storage, mode_surfaces, rec_params
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.core.recording_selection import READY
from yt_playlist.core.recommendation_custody import ROOTS as RECOMMENDATION_ROOTS
from yt_playlist.core.worker_settings import save


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Synchronous Home assembly does not construct the web app."""


@pytest.fixture
def home_assembly_reads(fresh, monkeypatch):
    """Compare both assemblers on one actual, independent reporting reader."""
    store, _ = fresh
    with reuse_reporting_reader(store, monkeypatch):
        yield


def modes(store, families):
    store.modes.replace_modes([{'mode_id': index, 'label': f'Mode {index}', 'families': [(family, 1)],
        'centroid': [1., 0.], 'size': 100, 'rep_keys': []} for index, family in families], [], 10)


def candidates(store):
    tracks, after = [], -1
    while True:
        page = store.recommendations.candidate_page('fresh', 'all', 'fresh', after=after, limit=128)
        tracks.extend({key: value for key, value in track.items() if key in bundle_storage.ITEM_FIELDS}
                      for track in page['tracks'])
        after = page['after']
        if page['exhausted']:
            return tracks


def publish_bundle(store, value, *, key='home-test'):
    policy = plan(store).policy_version
    prepared = bundle_storage.prepare(store.conn, value, [], external_key=key,
        policy_version=policy, input_revision=store.prepared_content.serial(), created_at=10)
    return publish(store, prepared).generation_id


def keys(cards):
    return [(card['lane'], card['mode_id'], card['ranker'], [track['key'] for track in card['tracks']])
            for card in cards]


@pytest.fixture
def initial_import_store(tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    # This workflow imports 24 actual catalog entries with no genre metadata.
    # Schema admission, taxonomy curation and snapshot races have other owners.
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as store:
            with mock_snapshot_coordinator(store, monkeypatch):
                yield store


def test_real_initial_import_prepares_and_serves_home_before_modes_without_legacy_documents(
        initial_import_store, monkeypatch):
    store = initial_import_store
    from tests.home_fixture_support import service
    from yt_playlist.rec import home_prepared, surfaces
    for index in range(24):
        store.tracks.upsert_track(f'import-{index}', f'Song {index}', f'Artist {index}', None, None)
    original = RecommendationProposals._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool'), 'Decoded an obsolete candidate bundle'
        return original(self, surface)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)
    bundles = mode_surfaces.prepare_bundles(store, 1000)
    assert bundles['all']['explore'] and not store.modes.list_modes(active_only=True)
    def forbidden(*args, **kwargs):
        pytest.fail('Home attempted foreground ranking')
    monkeypatch.setattr(surfaces, 'for_you', forbidden)
    monkeypatch.setattr(surfaces, 'explore_for_you', forbidden)
    prepared = home_prepared.prepare(store, 1001)
    assert home_prepared._valid(prepared)
    result = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    catalog = next(proto for proto in result.protos if proto['lane'] == 'explore')
    assert len(catalog['tracks']) >= 4 and catalog.get('mode_id') is None
    disliked = catalog['tracks'][0]['key']
    seed_dislike(store, disliked, until=None, now=1002)
    refreshed = home_prepared.cards(store, prepared, 1002, service(store), record=False)
    assert refreshed.protos[0]['tracks'] == [track for track in catalog['tracks'] if track['key'] != disliked]


def test_unmodeled_genre_selection_reads_all_lanes_once(fresh, monkeypatch):
    from yt_playlist.rec import stored_home
    store, _ = fresh
    for index in range(254, 260):
        seed_discovered_enrichment(store, f'fresh-{index}', 'jazz', None)
    with store.conn:
        store.conn.execute("UPDATE track_sources SET library_origin='import',library_added_at=5 "
                           "WHERE id IN (SELECT discovery_entry_id FROM recordings WHERE discovery_entry_id IS NOT NULL)")
    tracks = candidates(store)
    generation = publish_bundle(store, {'all': {'explore': tracks}, '_meta': {'unmodeled': True}})
    original, reads = RecommendationPublications.complete_ranks, []
    def ranks(self, slots, **kwargs):
        result = original(self, slots, **kwargs)
        reads.append(([slot[-1] for slot in slots], len(result)))
        return result
    monkeypatch.setattr(RecommendationPublications, 'complete_ranks', ranks)
    original_read = RecommendationProposals._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool')
        return original_read(self, surface)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)
    from yt_playlist.rec.home_cards import _carded
    cards = stored_home.unmodeled_cards(store, 10, include=('jazz',), card_builder=_carded)
    assert len(cards) == 1 and cards[0]['lane'] == 'explore'
    assert {track['key'] for track in cards[0]['tracks']} == {f'fresh-{index}' for index in range(254, 260)}
    assert {track['serving_context']['recommendation_generation_id'] for track in cards[0]['tracks']} == {generation}
    assert reads == [(['wheelhouse', 'explore', 'comfort'], 260)]
    assert not store.conn.execute(f"SELECT 1 FROM {RECOMMENDATION_ROOTS} AND root.value->>0='pending'").fetchone()


def test_mix_batch_shares_selection_metadata_and_reads_display_only_for_selected_tracks(
        fresh, home_assembly_reads, monkeypatch):
    from tests.home_fixture_support import service
    from yt_playlist.rec.home_cards import CardsRequest, HomeCards
    from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo

    store, _ = fresh
    tracks = candidates(store)
    with store.conn:
        store.conn.execute("UPDATE track_sources SET library_origin='import',library_added_at=5 WHERE id IN "
                           "(SELECT discovery_entry_id FROM recordings WHERE id BETWEEN 3 AND 20)")
        store.conn.execute("UPDATE track_sources SET orig_title=title || ' (Original Edit)', "
                           "artist_browse_id='channel-' || recording_id "
                           "WHERE recording_id BETWEEN 3 AND 262 AND recording_id % 2 = 0")
    publish_bundle(store, {'all': {'wheelhouse': tracks[:18], 'fresh': tracks}, '_meta': {'unmodeled': True}})
    requests = [CardsRequest(10, epoch=epoch, record=False) for epoch in (0, 1)]
    with store.read_view() as reader:
        legacy = service(reader)
        expected = [legacy.cards(request).protos for request in requests]
        for protos in expected:
            for proto in protos:
                for track in proto['tracks']:
                    track.pop('plays', None)

        selection_reads, display_reads, explanation_reads = [], [], []
        repo = RecommendationPublicationsRepo
        selection, details = repo.complete_rank_rows, repo.mix_playback_details
        explanations = repo.mix_display_references

        def read_selection(self, slots, **options):
            if not options.get('include_metadata'):
                return selection(self, slots, **options)
            ranks, facts = selection(self, slots, **options)
            assert all('video_id' not in track for track in facts.values())
            assert all('orig_title' not in track and 'artist_browse_id' not in track for track in facts.values())
            fresh_start = options['fresh_start']
            seen_fresh = set()
            expected = [[] for _ in slots]
            for row in selection(self, slots):
                recording_id = row['recording_id']
                if recording_id not in facts:
                    continue
                if row['slot'] >= fresh_start:
                    if recording_id in seen_fresh:
                        continue
                    seen_fresh.add(recording_id)
                expected[row['slot']].append(tuple(row[name] for name in (
                    'generation_id', 'mode_key', 'pool_id', 'pool_policy', 'rank', 'score', 'recording_id')))
            assert ranks == expected
            selection_reads.append(sorted(facts))
            return ranks, facts

        def read_display(self, recordings):
            display_reads.extend(dict.fromkeys(recordings))
            return details(self, recordings)

        def read_explanations(self, sources):
            explanation_reads.append(sum(map(len, sources.values())))
            return explanations(self, sources)

        monkeypatch.setattr(repo, 'complete_rank_rows', read_selection)
        monkeypatch.setattr(repo, 'mix_playback_details', read_display)
        monkeypatch.setattr(repo, 'mix_display_references', read_explanations)
        batch = HomeCards(reader, legacy.jobs, legacy.logger, reuse_mix_candidates=True)
        actual = [batch.cards(request).protos for request in requests]

    assert actual == expected
    lanes = [{proto['lane'] for proto in protos} for protos in actual]
    assert all({'wheelhouse', 'fresh'} <= offered for offered in lanes), lanes
    for protos in actual:
        for proto in protos:
            observed = set()
            for track in proto['tracks']:
                recording = track['recording_id']
                populated = recording % 2 == 0
                assert track['orig_title'] == (f"{track['title']} (Original Edit)" if populated else None)
                assert track['artist_browse_id'] == (f'channel-{recording}' if populated else None)
                observed.add(populated)
            assert observed == {False, True}
    assert selection_reads == [list(range(3, 263))]
    selected = {track['recording_id'] for protos in actual for proto in protos for track in proto['tracks']}
    assert set(display_reads) == selected
    assert len(display_reads) == len(selected)
    assert explanation_reads == [sum(len(proto['tracks']) for proto in protos) for protos in actual]


def test_mix_candidates_prefer_a_saved_playable_upload_then_fall_back(fresh):
    from yt_playlist.core import recording_selection

    store, _ = fresh
    recording = 3
    slots = (('fresh', None, 'all', 'fresh'),)

    def chosen_video():
        candidates = store.recommendations.mix_candidates((), slots)
        fact = candidates.facts[recording]
        pick = dict(fact, _mix_selection='fresh')
        assert not candidates.load_display([pick])
        return pick['video_id'], fact['_radio_fresh'], fact['duration']

    original = chosen_video()
    assert original[:2] == ('fresh-upload-0', True)
    with store.conn:
        store.conn.execute('INSERT INTO track_sources(video_id,recording_id,title,artist,library_origin,duration_s) '
                           'VALUES (?,?,?,?,?,?)', ('saved-upload', recording, 'Song', 'Artist', 'import', 240))
        save(store.conn, READY, 0)
        store.conn.execute('UPDATE recordings SET effective_duration_s=NULL,has_playable_source=0 WHERE id=?',
                           (recording,))
    assert chosen_video() == ('saved-upload', False, 240)
    assert recording_selection.prepare(store) is True
    assert chosen_video() == ('saved-upload', False, 240)

    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='saved-upload'")
    assert chosen_video() == ('fresh-upload-0', False, original[2])


def test_mix_batch_rechecks_catalog_after_a_committed_availability_change(fresh):
    from tests.home_fixture_support import service
    from yt_playlist.rec.home_cards import CardsRequest, HomeCards

    store, _ = fresh
    with store.read_view(seconds=None) as reader, reader.recommendations.reuse_complete_ranks():
        legacy = service(reader)
        batch = HomeCards(reader, legacy.jobs, legacy.logger, reuse_mix_candidates=True)
        first = batch.cards(CardsRequest(10, epoch=0, record=False)).protos
        picked = next(proto for proto in first if proto['lane'] == 'fresh')['tracks'][0]['video_id']
        with store.conn:
            store.conn.execute('UPDATE track_sources SET available=0 WHERE video_id=?', (picked,))
        actual = batch.cards(CardsRequest(10, epoch=1, record=False)).protos
        expected = legacy.cards(CardsRequest(10, epoch=1, record=False)).protos
        fresh_tracks = next(proto for proto in actual if proto['lane'] == 'fresh')['tracks']
        expected_tracks = next(proto for proto in expected if proto['lane'] == 'fresh')['tracks']
        assert len(fresh_tracks) == 12
        assert picked not in {track['video_id'] for track in fresh_tracks}
        assert [track['video_id'] for track in fresh_tracks] == [track['video_id'] for track in expected_tracks]


@pytest.mark.parametrize('share', [0, .5, 1])
@pytest.mark.parametrize('steering', [False, True], ids=['neutral', 'penalized'])
def test_real_typed_assembly_matches_complete_candidate_calculation_and_experiment_evidence(
        fresh, home_assembly_reads, share, steering, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE track_sources SET album='',mb_year='1980'")
    modes(store, [(1, 'rock-classic'), (2, 'rock-classic'), (3, 'rock-classic'), (4, 'rock-classic')])
    rec_params.set_param(store, 'ppr_ab_share', share)
    tracks = candidates(store)
    lanes = ('wheelhouse', 'explore', 'fresh', 'temporal')
    all_pools = {lane: deepcopy(tracks[index * 60:(index + 1) * 60]) for index, lane in enumerate(lanes)}
    value = {'all': all_pools, '_meta': {'full_pools': True, 'comfort_pool': 0,
             'year_cuts': [1990, 2010], 'ppr_policy': {'algorithm': 'controlled-ppr', 'built_at': 10}},
             '_ppr': {str(index + 1): [track['key'] for track in reversed(all_pools[lane])]
                      for index, lane in enumerate(lanes)}}
    for index, lane in enumerate(lanes):
        value[str(index + 1)] = {lane: deepcopy(all_pools[lane][:7])}
    generation = publish_bundle(store, value)
    if steering:
        vectors = [('one', np.array([1., 0.], dtype=np.float32).tobytes())]
        for index in range(260):
            angle = index / 260 * np.pi / 2
            vectors.append((f'fresh-{index}', np.array([np.cos(angle), np.sin(angle)], dtype=np.float32).tobytes()))
        store.rec_model.replace_rec_vectors(vectors)
        seed_dislike(store, 'one', 1000, 10)
    expected = complete_cards(store, value)
    selected = []
    original = RecommendationPublications.selected_candidates

    def hydrate(self, surface, selections, **kwargs):
        selected.append(selections)
        return original(self, surface, selections, **kwargs)

    monkeypatch.setattr(RecommendationPublications, 'selected_candidates', hydrate)
    actual = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1, 2, 3, 4])
    assert len(actual) == 4 and keys(actual) == keys(expected)
    assert len(selected) == sum(1 + bool(card.get('counterfactual')) for card in actual)
    assert all(sum(len(keys) for _, _, keys in batch) == 12 for batch in selected)
    assert any(len(batch) > 1 for batch in selected)  # Selected rows include real backfill.
    for old, new in zip(expected, actual):
        assert new['experiment'] == old['experiment']
        assert [track['key'] for track in new['counterfactual']['tracks']] == [
            track['key'] for track in old['counterfactual']['tracks']]
        assert {track['serving_context']['recommendation_generation_id'] for track in new['tracks']} == {generation}
        assert len(new['tracks']) == 12


def complete_cards(store, bundles):
    from yt_playlist.rec import experiments, home_themes, home_vibe, transient
    from yt_playlist.rec.eligibility import home_exclusions

    all_keys = {track['key'] for tracks in bundles['all'].values() for track in tracks}
    identities = store.artists.track_keys(keys=all_keys)
    bundles = {mode: {lane: [{**row, 'artist_ids': sorted(identities.get(row['key'], ()))} for row in tracks]
                             for lane, tracks in lanes.items()} if not mode.startswith('_') else lanes
               for mode, lanes in bundles.items()}
    now = 10
    inputs = {'blocked': home_exclusions(store, all_keys, now), 'muted': store.rec_model.muted_artists()}
    calculation = mode_surfaces.assembly_steps(inputs, bundles, store.modes.list_modes(active_only=True),
        [1, 2, 3, 4], 0, int(rec_params.get_param(store, 'modes_menu_size')), transient.facet_leans(store, now),
        home_vibe.ranker(store, now), cap_a=int(rec_params.get_param(store, 'modes_artist_cap')),
        cap_al=int(rec_params.get_param(store, 'modes_album_cap')), ab_share=rec_params.get_param(store, 'ppr_ab_share'),
        comfort_minimum=int(rec_params.get_param(store, 'comfort_min_pool')))
    acquire = {'decision': lambda _: experiments.last_decision(store),
               'pool': lambda lane: home_themes.resolved_genres(store, bundles['all'][lane]),
               'axes': store.genres.feature_axes}
    response = None
    while True:
        try:
            request, value = calculation.send(response)
        except StopIteration as completed:
            return completed.value
        response = acquire[request](value)


def test_thin_credible_comfort_rotates_to_the_current_temporal_band(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE track_sources SET album='',mb_year='1980'")
    modes(store, [(index, 'rock-classic') for index in range(1, 5)])
    tracks = candidates(store)
    value = {'1': {'wheelhouse': tracks[:30]}, '2': {'explore': tracks[30:60]},
             '3': {'fresh': tracks[60:90]}, '4': {'comfort': tracks[90:93], 'temporal': tracks[90:120]},
             '_meta': {'full_pools': True, 'comfort_pool': 1000, 'year_cuts': [1990, 2010]}}
    publish_bundle(store, value)
    forbid_legacy(monkeypatch)
    cards = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1, 2, 3, 4])
    assert [(card['lane'], card['mode_id']) for card in cards] == [
        ('wheelhouse', 1), ('explore', 2), ('fresh', 3), ('temporal', 4)]
    assert cards[-1]['label'] == 'Throwback' and len(cards[-1]['tracks']) == 12


def test_rare_tail_and_diversity_overflow_are_reachable_without_rank_pagination(fresh, monkeypatch):
    store, _ = fresh
    for index in range(254, 260):
        seed_discovered_enrichment(store, f'fresh-{index}', 'jazz', None)
    with store.conn:
        store.conn.execute("UPDATE track_sources SET album='' ")
        store.conn.execute("UPDATE track_sources SET artist='Repeated' WHERE recording_id<257")
    modes(store, [(1, 'jazz')])
    tracks = candidates(store)
    publish_bundle(store, {'1': {'wheelhouse': tracks}, 'all': {'wheelhouse': tracks},
                           '_meta': {'full_pools': True}})
    forbid_legacy(monkeypatch)
    monkeypatch.setattr(RecommendationPublications, 'candidate_page',
                        lambda *args, **kwargs: pytest.fail('Home rank pagination'))
    cards = mode_surfaces.assemble_cards(store, 10, 0, include_genres=('jazz',), mode_order=[1])
    assert keys(cards) == [('wheelhouse', 1, 'cosine', [f'fresh-{index}' for index in range(254, 260)])]


def test_first_unavailable_page_and_current_eligibility_do_not_hide_the_remaining_pool(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE track_sources SET album=''")
    modes(store, [(1, 'rock-classic')])
    tracks = candidates(store)
    publish_bundle(store, {'1': {'wheelhouse': tracks}, '_meta': {'full_pools': True}})
    with store.conn:
        store.conn.execute('UPDATE track_sources SET available=0 WHERE recording_id<131')
        store.conn.execute("UPDATE track_sources SET title='corrected ' || title,duration_s=241 "
                           'WHERE recording_id>=131')
        store.conn.execute('UPDATE recordings SET bpm=123 WHERE id>=131')
    seed_dislike(store, 'fresh-128', 100, 10)
    forbid_legacy(monkeypatch)
    cards = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1])
    selected = [int(track['key'].removeprefix('fresh-')) for track in cards[0]['tracks']]
    assert len(selected) == 12 and len(set(selected)) == 12
    assert all(129 <= index < 260 for index in selected)
    assert all(track['title'] == f"corrected {track['key']} title"
               and track['duration'] == 241 and track['bpm'] == 123 for track in cards[0]['tracks'])


def test_whole_considered_bucket_is_excluded_from_later_card_backfill(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE track_sources SET album=''")
    modes(store, [(1, 'rock-classic'), (2, 'rock-classic')])
    tracks = candidates(store)
    publish_bundle(store, {'1': {'wheelhouse': tracks[:128]}, '2': {'explore': tracks[:4]},
                           'all': {'explore': tracks}, '_meta': {'full_pools': True}})
    forbid_legacy(monkeypatch)
    cards = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1, 2])
    assert [(card['lane'], card['mode_id']) for card in cards] == [('wheelhouse', 1), ('explore', 2)]
    assert [track['key'] for track in cards[1]['tracks']] == [f'fresh-{index}' for index in range(128, 140)]


def test_failed_hydration_releases_reader_retention_and_private_scratch_files(fresh, monkeypatch):
    store, _ = fresh
    modes(store, [(1, 'rock-classic')])
    publish_bundle(store, {'1': {'wheelhouse': candidates(store)}, '_meta': {'full_pools': True}})
    def fail(self, *args, **kwargs):
        raise RuntimeError('controlled hydration failure')
    monkeypatch.setattr(RecommendationPublications, 'selected_candidates', fail)
    rec_params.set_param(store, 'modes_album_cap', 12)
    with pytest.raises(RuntimeError, match='controlled hydration'):
        mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1])
    assert not store.conn.execute("SELECT 1 FROM group_evidence_contexts WHERE kind='pending'").fetchone()


def test_unprepared_user_needs_no_legacy_bundle(recommendations, monkeypatch):
    forbid_legacy(monkeypatch)
    assert mode_surfaces.assemble_cards(recommendations, 10, 0) == []
