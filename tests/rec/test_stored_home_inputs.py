"""Home inputs use small publication headers and distinct recording pages."""
from dataclasses import replace

import numpy as np
import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_typed_recommendation_publication import recommendations, publish, plan
from tests.rec.test_stored_fresh import fresh
from yt_playlist.core import recommendation_outputs, recommendation_pools as pools
from yt_playlist.rec import album_choices, home_prepared, home_themes, rec_params
from yt_playlist.repos import recommendation_publication as publication
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
from yt_playlist.repos.genres import GenreRepo


@pytest.fixture
def home(fresh):
    store, _ = fresh
    original = plan(store, key='home-inputs')
    ranked = pools.prepare('full-home', original.policy_version, None, '7' * 64, 'card',
                           [pools.Candidate(index + 3, 1 / (index + 1)) for index in range(260)])
    prepared = replace(original, slots=(publication.Slot('home', 'all', 'wheelhouse', ranked),
        publication.Slot('home', 'mode-one', 'wheelhouse', ranked),
        publication.Slot('home', 'mode-two', 'explore', ranked)))
    generation = publish(store, prepared).generation_id
    return store, generation, prepared


def forbid_bundle_and_files(monkeypatch):
    original = RecProposalsRepo._proposal_json
    def read(self, surface):
        if surface in ('mode_bundles', 'fresh_pool'):
            pytest.fail('Typed Home inputs decoded a legacy recommendation pool')
        return original(self, surface)
    def file(*args, **kwargs):
        pytest.fail('Metadata/header read opened a prepared display file')
    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', read)
    monkeypatch.setattr(recommendation_outputs, 'read', file)


def test_distinct_metadata_pages_advance_once_per_recording_across_shared_slots(home, monkeypatch):
    store, generation, _ = home
    forbid_bundle_and_files(monkeypatch)
    tracks, cursors, after = [], [], 0
    while True:
        page = store.recommendations.recording_metadata_page('home', generation=generation, after=after, limit=37)
        assert page['generation'] == generation and len(page['tracks']) <= 37
        tracks.extend(page['tracks'])
        cursors.append(page['after'])
        after = page['after']
        if page['exhausted']:
            break
    assert [track['key'] for track in tracks] == [f'fresh-{index}' for index in range(260)]
    assert cursors == sorted(set(cursors))
    assert len(tracks) == len({track['recording_id'] for track in tracks})
    assert store.recommendations.recording_metadata_page('home', mode_key='absent')['tracks'] == []


def test_unavailable_whole_page_still_advances_to_later_metadata(home):
    store, generation, _ = home
    with store.conn:
        store.conn.execute('UPDATE youtube_items SET available=0 WHERE recording_id BETWEEN 3 AND 130')
    first = store.recommendations.recording_metadata_page('home', generation=generation)
    assert first['tracks'] == [] and first['after'] == 130 and not first['exhausted']
    second = store.recommendations.recording_metadata_page('home', generation=generation, after=first['after'])
    assert second['tracks'][0]['key'] == 'fresh-128'


def test_genre_options_keep_rare_tail_without_duplicate_mode_votes(home, monkeypatch):
    store, generation, _ = home
    forbid_bundle_and_files(monkeypatch)
    reads, original = [], RecommendationPublicationsRepo.recording_metadata_page
    def page(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        reads.append((value['generation'], len(value['tracks'])))
        return value
    monkeypatch.setattr(RecommendationPublicationsRepo, 'recording_metadata_page', page)
    assert home_themes.genre_options(store, genre_plays={}) == [
        {'family': 'rock-classic', 'label': 'rock'}, {'family': 'jazz', 'label': 'jazz'}]
    assert len(reads) == 3 and all(gen == generation and size <= 128 for gen, size in reads)
    assert home_themes.genre_options(store, genre_plays={'jazz': 100})[0]['family'] == 'jazz'


def test_genre_options_resolve_current_metadata_after_preparation(home, monkeypatch):
    store, _, _ = home
    forbid_bundle_and_files(monkeypatch)
    with store.conn:
        store.conn.execute("UPDATE recordings SET genre='ambient' WHERE id>=261")
    options = home_themes.genre_options(store, genre_plays={})
    assert {option['family'] for option in options} == {'rock-classic', 'ambient'}


def test_genre_options_resolve_each_distinct_label_once_across_metadata_pages(home, monkeypatch):
    store, _, _ = home
    original = GenreRepo.axis_matches
    labels = []
    def axes(self, requested):
        labels.extend(requested)
        return original(self, requested)
    monkeypatch.setattr(GenreRepo, 'axis_matches', axes)
    assert home_themes.genre_options(store, genre_plays={})
    assert len(labels) == len(set(labels)) == 2


def test_generation_hash_changes_only_when_private_output_becomes_a_head(home, monkeypatch):
    store, _, prepared = home
    forbid_bundle_and_files(monkeypatch)
    before = home_prepared._proposal_generation(store, None, 10, preview={})
    next_plan = replace(prepared, external_key='private-home-inputs')
    build = store.recommendations.claim(next_plan)
    store.recommendations.stage(next_plan, build)
    assert home_prepared._proposal_generation(store, None, 10, preview={}) == before
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.recommendations.publish(build, 12)
    assert home_prepared._proposal_generation(store, None, 10, preview={}) != before


def test_unprepared_home_hash_and_genres_need_no_bundle(recommendations, monkeypatch):
    forbid_bundle_and_files(monkeypatch)
    assert isinstance(home_prepared._proposal_generation(recommendations, None, 10, preview={}), str)
    assert isinstance(home_themes.genre_options(recommendations, genre_plays={}), list)


def test_taste_examples_advance_past_an_unavailable_page_without_decoding_candidates(home, monkeypatch):
    from yt_playlist.rec import taste_viz
    store, generation, _ = home
    original = RecProposalsRepo._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool')
        return original(self, surface)
    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', read)
    with store.conn:
        store.conn.execute('UPDATE youtube_items SET available=0 WHERE recording_id BETWEEN 3 AND 130')
    examples = taste_viz._stored_examples(store, 10)
    assert examples['wheelhouse']['key'] == 'fresh-128'
    assert examples['wheelhouse']['serving_context']['recommendation_generation_id'] == generation
    assert all(examples[lane] is None for lane in ('explore', 'fresh', 'comfort'))


def test_tv_seed_interleave_keeps_rare_mode_candidates_without_a_global_head_cutoff(fresh, monkeypatch):
    from tests.rec.test_stored_home import candidates, publish_bundle
    from yt_playlist.rec import tv
    store, _ = fresh
    values = candidates(store)
    generation = publish_bundle(store, {'all': {'wheelhouse': values}, '1': {'wheelhouse': values[258:]}})
    original = RecProposalsRepo._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool')
        return original(self, surface)
    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', read)
    page, requests = RecommendationPublicationsRepo.candidate_page, []
    def bounded(self, *args, **kwargs):
        requests.append(kwargs['limit'])
        return page(self, *args, **kwargs)
    monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_page', bounded)
    seeds = tv._stored_seeds(store, [[values[0]]], 3)
    assert [track['key'] for track in seeds] == ['fresh-0', 'fresh-258', 'fresh-259']
    assert {track['serving_context']['recommendation_generation_id'] for track in seeds[1:]} == {generation}
    assert requests and max(requests) == 1


def test_album_companions_find_best_tail_matches_with_artist_caps_and_bounded_pages(home, monkeypatch):
    store, generation, _ = home
    forbid_bundle_and_files(monkeypatch)
    rec_params.set_param(store, 'modes_artist_cap', 1)
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET artist='Repeated artist' WHERE recording_id>=250")
        store.conn.execute('UPDATE youtube_items SET available=0 WHERE recording_id=260')
    angles = np.linspace(1.4, 0, 260)
    vectors = np.column_stack((np.cos(angles), np.sin(angles)))
    keys = {f'fresh-{index}': index for index in range(260)}
    albums = [{'keys': ['fresh-259']}, {'keys': ['fresh-0', 'fresh-1']}]
    features = np.eye(2)
    reads, original = [], RecommendationPublicationsRepo.recording_metadata_page
    def page(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        reads.append((kwargs['limit'], value['generation'], kwargs.get('mode_key')))
        return value
    monkeypatch.setattr(RecommendationPublicationsRepo, 'recording_metadata_page', page)
    actual = album_choices._companions(store, albums, features, vectors, keys)
    assert len(reads) == 3 and set(reads) == {(128, generation, 'all')}
    for position, tracks in enumerate(actual):
        # Independent complete sort, followed by the existing single-artist cap.
        scores = features[position] @ (vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)).T
        order = sorted(range(260), key=lambda index: (-scores[index], index))
        expected, artists = [], set()
        for index in order:
            key = f'fresh-{index}'
            artist = 'Repeated artist' if index + 3 >= 250 else f'artist-{index}'
            if index + 3 == 260 or key in albums[position]['keys'] or artist in artists:
                continue
            expected.append(key)
            artists.add(artist)
            if len(expected) == 12:
                break
        assert [track['key'] for track in tracks] == expected
        assert {track['serving_context']['recommendation_generation_id'] for track in tracks} == {generation}
    assert actual[0][0]['key'] == 'fresh-258'


def test_album_companion_ties_and_missing_vectors_have_stable_canonical_order(home, monkeypatch):
    store, _, _ = home
    forbid_bundle_and_files(monkeypatch)
    keys = {f'fresh-{index}': index for index in range(200, 260)}
    vectors = np.tile([1., 0.], (260, 1))
    tracks = album_choices._companions(store, [{}], np.array([[1., 0.]]), vectors, keys)[0]
    assert [track['key'] for track in tracks] == [f'fresh-{index}' for index in range(200, 212)]


def test_no_prepared_home_has_no_album_companions(recommendations, monkeypatch):
    forbid_bundle_and_files(monkeypatch)
    assert album_choices._companions(recommendations, [{}], np.eye(1), np.eye(1), {'absent': 0}) == [[]]
