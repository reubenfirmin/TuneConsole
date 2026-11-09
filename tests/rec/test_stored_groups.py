"""Adaptive Home follows indexed memberships without copying global candidate bags."""
from copy import deepcopy
from tests.derived_rating_support import seed_dislike
from dataclasses import replace

import pytest

from tests.conftest import seed_catalog_track
from tests.group_support import publish as publish_groups, recordings
from tests.legacy_recommendation_fixture_support import recommendation_predecessor, recommendations
from tests.repos.test_typed_recommendation_publication import (
    plan,
)
from tests.rec.test_stored_fresh import fresh
from tests.rec.test_stored_home import publish_bundle, candidates
from tests.store_fixture_support import native_sql_calls, reuse_reporting_reader
from yt_playlist.rec import group_home, group_serving, home_vibe, stored_groups
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.recommendation_publications import RecommendationPublications


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Synchronous adaptive serving does not construct the web app."""


@pytest.fixture(autouse=True)
def prepared_vibe_access():
    """Ranking consumes an unlocked control; journey access has its own owners."""
    with home_vibe.access_policy(lambda _store, _now: True):
        yield


@pytest.fixture
def group_assembly_reads(recommendations, monkeypatch):
    """Keep real reporting SQL below synchronous preview/assembly comparisons."""
    with reuse_reporting_reader(recommendations, monkeypatch):
        yield


def forbid_bundle(monkeypatch):
    original = RecommendationProposals._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool'), 'Decoded the retired global candidate bundle'
        return original(self, surface)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)
    monkeypatch.setattr(RecommendationProposals, 'group_pools_json', lambda *args: pytest.fail('Decoded group pools'))


@pytest.mark.parametrize('preset', ['', 'energize'])
def test_real_adaptive_preview_and_choice_preserve_complete_legacy_membership_and_ranking(
        recommendations, group_assembly_reads, monkeypatch, preset):
    store = recommendations
    policy = plan(store).policy_version
    records = recordings([('House', 32, .1), ('Jazz', 32, .9)])
    items = []
    for index, record in enumerate(records):
        title, artist, album, video = f'Song {index}', f'Artist {index // 2}', f'Album {index // 2}', f'v{index}'
        seed_catalog_track(store, record.key, title, artist, video_id=video, genre=record.genre, album=album)
        items.append({'key': record.key, 'title': title, 'artist': artist, 'album': album,
                      'video_id': video, 'thumbnail': '', 'genre': record.genre, 'year': 2000,
                      'pool_policy': policy})
    head = publish_groups(store, records)
    value = {'all': {lane: deepcopy(items) for lane in ('wheelhouse', 'explore', 'comfort')},
             '_meta': {'comfort_pool': 64, 'full_pools': True}}
    generation = publish_bundle(store, value)
    if preset:
        store.settings.set_setting(home_vibe.SETTING, preset)
    assert home_vibe.current(store, 1200) == preset
    store._recommendation_files = False
    try:
        store.rec_proposals.put_proposals('mode_bundles', value, 1050)
        expected_preview = group_serving.calculate(store, head, 1100)
        store.rec_proposals.put_proposals(group_serving.SURFACE, expected_preview, 1100)
        expected = group_home.assemble(store, 1200, 0, group_home.inputs(store, 1200))
    finally:
        store._recommendation_files = True
    forbid_bundle(monkeypatch)
    actual_preview = group_serving.prepare(store, 1100)
    assert actual_preview['recommendation_generations'] == [generation]
    assert actual_preview['menus'] == expected_preview['menus']
    for old, new in zip(expected_preview['groups'], actual_preview['groups']):
        for lane, frame in new['frames'].items():
            assert 'keys' not in frame
            assert frame == {key: item for key, item in old['frames'][lane].items() if key != 'keys'}
    actual = group_home.assemble(store, 1200, 0, group_home.inputs(store, 1200))
    signature = lambda cards: [(card['lane'], card['group_version'], [track['key'] for track in card['tracks']])
                               for card in cards]
    assert actual and signature(actual) == signature(expected)
    assert {track['serving_context']['recommendation_generation_id'] for card in actual
            for track in card['tracks']} == {generation}
    # A new global head cannot change an already published adaptive preview's
    # candidate generation or framing before its own replacement is ready.
    replacement = deepcopy(value)
    replacement['all']['wheelhouse'].reverse()
    replacement['_meta']['comfort_pool'] = 0
    publish_bundle(store, replacement, key='replacement-home')
    assert signature(group_home.assemble(store, 1200, 0, group_home.inputs(store, 1200))) == signature(actual)
    assert not store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='pending'").fetchone()


def test_rare_group_seeks_its_complete_tail_without_hydrating_unrelated_candidates(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("INSERT INTO library_memberships SELECT entry_id,'import',5 FROM discovery_memberships")
        store.conn.execute("UPDATE youtube_items SET album='' ")
    records = [replace(record, key=f'fresh-{index + 248}')
               for index, record in enumerate(recordings([('Jazz', 12, .4)]))]
    head = publish_groups(store, records)
    values = candidates(store)
    generation = publish_bundle(store, {'all': {'explore': values}, '_meta': {'full_pools': True}})
    version = head['groups'][0]['version']
    original, reads = RecommendationPublications.candidate_page, []
    def page(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        reads.append((kwargs.get('group_version'), len(result['tracks']), kwargs['limit']))
        return result
    monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
    forbid_bundle(monkeypatch)
    with stored_groups.selection(store, generation, 1100) as selected:
        tracks = selected.tracks(version, 'explore', 0, (), (), set(), 2, 2, {})
    assert [track['key'] for track in tracks] == [f'fresh-{index}' for index in range(248, 260)]
    assert reads == [(version, 12, 128)]
    seed_dislike(store, 'fresh-248', None, 1101)
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE recording_id=252")
    with stored_groups.selection(store, generation, 1102) as selected:
        tracks = selected.tracks(version, 'explore', 0, (), (), set(), 2, 2, {})
    assert len(tracks) == 10
    assert not {'fresh-248', 'fresh-249'} & {track['key'] for track in tracks}
    assert not store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='pending'").fetchone()


def test_failed_group_selection_releases_its_publication_root(recommendations):
    store = recommendations
    generation = publish_bundle(store, {'all': {}, '_meta': {'unmodeled': True}})
    with pytest.raises(RuntimeError, match='controlled'):
        with stored_groups.selection(store, generation, 10):
            raise RuntimeError('controlled selection failure')
    assert not store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='pending'").fetchone()
