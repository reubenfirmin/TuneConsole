"""Complete typed Radio membership with bounded live hydration and reader roots."""
from dataclasses import replace
import json
import sqlite3
from contextlib import nullcontext

import numpy as np
import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_typed_recommendation_publication import recommendations, publish, plan, collect
from tests.rec.test_stored_fresh import fresh
from yt_playlist.core import recommendation_pools as pools
from yt_playlist.rec import musical_character, radio_intensity, radio_tuning
from yt_playlist.rec.radio_candidates import RadioCandidates, CLUSTER_POOL_LIMIT
from yt_playlist.rec.radio_genres import GenrePolicy
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.repos import recommendation_publication as publication
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo


@pytest.mark.parametrize('blocked_by', ['sql_writer', 'cancelled_work'])
def test_database_close_releases_radio_readers_when_retirement_cannot_commit(file_backed_store, blocked_by):
    from tests.recommendation_support import publish_bundles
    from yt_playlist.core import db_handles, group_documents
    from yt_playlist.core.activity import WorkClosing, cancellable_work
    from yt_playlist.core.store import Store
    from yt_playlist.rec import group_roots

    store = file_backed_store
    publish_bundles(store, {'all': {}}, now=1000)
    readers = [PreparedRadio(store, 1001) for _ in range(2)]
    directory = group_documents.root_for_database(store._db_path, store._evidence_key) / '.pending'
    assert all(reader._lifetime.alive for reader in readers)
    assert len(list(directory.glob('*.lock'))) == 2
    other = sqlite3.connect(store._db_path, timeout=0)
    cancelled = [False]
    try:
        if blocked_by == 'sql_writer':
            other.execute('BEGIN IMMEDIATE')
            scope = nullcontext()
            outcome = nullcontext()
        else:
            scope = cancellable_work(lambda: cancelled[0])
            outcome = pytest.raises(WorkClosing)
        with outcome:
            with scope:
                cancelled[0] = blocked_by == 'cancelled_work'
                store.conn.close()
                assert store.conn._handle_closed and store.conn._handle_lease is None
                assert not store.conn._before_close_callbacks
        assert len(list(directory.glob('*.lock'))) == 2
    finally:
        other.rollback()
        other.close()
        for reader in readers:
            reader.close()
        store.conn.close()
    with db_handles.lease(store._db_path, exclusive=True, timeout=0):
        pass
    reopened = Store.open_current(store._db_path)
    try:
        assert reopened.conn.execute(
            "SELECT count(*) FROM group_evidence_owners WHERE kind='pending'").fetchone()[0] == 2
        group_roots.reap_pending(reopened)
        assert not reopened.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
        assert not list(directory.glob('*.lock'))
    finally:
        reopened.conn.close()


@pytest.fixture
def radio(fresh):
    store, _ = fresh
    original = plan(store)
    store.modes.replace_modes([dict(mode_id=1, label='Test region', families=[['rock-classic', 260]],
        centroid=np.array([1., 0.], dtype=np.float32), size=260, rep_keys=['fresh-0'])], [], 1)
    ranked = pools.prepare('membership', original.policy_version, None, 'd' * 64, 'radio', [
        pools.Candidate(index + 3, .9, 40 if index >= 258 else 12, 'jazz' if index >= 258 else 'rock')
        for index in range(260)])
    home = pools.prepare('fresh', original.policy_version, None, 'e' * 64, 'fresh',
                         [pools.Candidate(index + 3, .9) for index in range(4)])
    ppr = pools.prepare('ppr', original.policy_version, None, 'f' * 64, 'ppr',
                        [pools.Candidate(index + 3) for index in reversed(range(260))])
    prepared = replace(original, external_key='radio-head', slots=(
        publication.Slot('home', '1', 'fresh', home), publication.Slot('radio', '1', 'membership', ranked),
        publication.Slot('radio', '1', 'ppr', ppr)), framing=publication.Framing(
            presence=sum(1 << bit for bit in (5, 6, 7, 8)), radio_generation=None, radio_space='',
            intensity_version=radio_intensity.VERSION, radio_profile_version=musical_character.VERSION))
    generation = publish(store, prepared).generation_id
    return store, generation, prepared


def forbid_bundle(monkeypatch):
    original = RecProposalsRepo._proposal_json
    def read(self, surface):
        if surface in ('mode_bundles', 'fresh_pool'):
            pytest.fail('Typed Radio decoded a complete legacy pool')
        return original(self, surface)
    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', read)


def test_small_initial_lookahead_uses_point_ppr_ranks_and_keeps_exact_publication(radio, monkeypatch):
    store, generation, _ = radio
    forbid_bundle(monkeypatch)
    pages, original = [], RecommendationPublicationsRepo.candidate_pages
    def page(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        pages.extend((kwargs['limit'], len(page['tracks'])) for page in value)
        return value
    monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_pages', page)
    candidates = RadioCandidates(store, 10, comparison=None)
    try:
        assert candidates.prepared.generation == generation
        assert candidates.cluster_members == {'1': 260}
        assert len(candidates.pools['1']) == 4
        assert candidates.prepared.positions('1', ['fresh-0', 'fresh-259', 'missing']) == {
            'fresh-0': 259, 'fresh-259': 0}
        assert candidates.choice_context['recommendation_generations'] == [generation]
        assert pages and all(limit == 128 and count <= 128 for limit, count in pages)
        assert all(track['serving_context']['recommendation_generation_id'] == generation
                   for track in candidates.seed_offer(4))
    finally:
        candidates.close()


def test_pinned_rare_genre_refills_beyond_two_complete_pages_without_full_vectors(radio, monkeypatch):
    store, generation, _ = radio
    forbid_bundle(monkeypatch)
    candidates = RadioCandidates(store, 10, {**radio_tuning.DEFAULTS, 'theme': 1}, comparison=None)
    def forbidden(*args, **kwargs):
        pytest.fail('Typed Radio refill requested the complete model')
    monkeypatch.setattr(store.rec_model, 'content_generation', forbidden)
    try:
        anchor = {'key': 'anchor', 'video_id': 'anchor-upload', 'genre': 'jazz'}
        genres = GenrePolicy(1, [], current=anchor)
        tracks = candidates._cluster_pool('1', [], set(), genres=genres)
        assert [track['key'] for track in tracks] == ['fresh-258', 'fresh-259']
        assert len(tracks) <= CLUSTER_POOL_LIMIT
        assert {track['serving_context']['recommendation_generation_id'] for track in tracks} == {generation}
        assert not candidates._cluster_pool('1', tracks, set(), genres=genres)
    finally:
        candidates.close()


def test_intensity_reads_selected_rare_region_and_rechecks_live_source_metadata(radio, monkeypatch):
    store, _, _ = radio
    forbid_bundle(monkeypatch)
    candidates = RadioCandidates(store, 10, comparison=None)
    try:
        assert candidates.prepared.has_intensity
        with store.conn:
            store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='fresh-upload-258'")
            store.conn.execute("UPDATE youtube_items SET duration_s=100000 WHERE video_id='fresh-upload-259'")
        priority, tracks = candidates.prepared.intensity_pool('1', .8, genre_allowed=lambda genre: genre == 'jazz',
            allowed=candidates._allowed, excluded=set(), used=set())
        assert priority is None and tracks == []
        with store.conn:
            store.conn.execute("UPDATE youtube_items SET duration_s=200,title='corrected' WHERE video_id='fresh-upload-259'")
        priority, tracks = candidates.prepared.intensity_pool('1', .8, genre_allowed=lambda genre: genre == 'jazz',
            allowed=candidates._allowed, excluded=set(), used=set())
        assert priority == 0 and [(track['key'], track['title'], track['duration']) for track in tracks] == [
            ('fresh-259', 'corrected', 200)]
    finally:
        candidates.close()


def test_planning_reuses_pages_but_rechecks_used_tracks_and_next_pass_catalog(radio, monkeypatch):
    store, _, _ = radio
    prepared = PreparedRadio(store, 10)
    reads, original = [], RecommendationPublicationsRepo.candidate_page
    facts, hydrate = [], type(store.rec_query).group_candidates

    def catalog(self, keys, *args, **kwargs):
        facts.extend(keys)
        return hydrate(self, keys, *args, **kwargs)

    def page(self, *args, **kwargs):
        reads.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_page', page)
    monkeypatch.setattr(type(store.rec_query), 'group_candidates', catalog)

    def pick(used=()):
        return prepared.intensity_pool('1', .8, genre_allowed=lambda label: label == 'jazz',
            allowed=lambda track: track.get('available') != 0, excluded=(), used=used, limit=1)[1]

    try:
        with prepared.planning_read():
            first = pick()[0]
            assert first['key'] == 'fresh-258'
            first['title'] = 'private branch mutation'
            first['serving_context']['recommendation_pool_rank'] = -1
            repeated = pick()[0]
            assert repeated['title'] != first['title']
            assert repeated['serving_context']['recommendation_pool_rank'] == 258
            assert pick({first['key']})[0]['key'] == 'fresh-259'
            assert len(reads) == 1, 'Successive song slots must reuse the hydrated rank page'
            # A different candidate query can overlap the same recordings. Its
            # pool attribution stays private while catalog hydration is shared.
            overlap = prepared.tracks('radio', '1', 'membership', band=40,
                allowed=lambda track: True, include_display=False)
            assert [track['key'] for track in overlap] == ['fresh-258', 'fresh-259']
            assert facts == ['fresh-258', 'fresh-259']
            assert len(reads) == 2
        assert prepared.current_reader is None
        with store.conn:
            store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='fresh-upload-258'")
            store.conn.execute("UPDATE youtube_items SET title='new catalog title' WHERE video_id='fresh-upload-259'")
        with pytest.raises(RuntimeError, match='cancel this plan'):
            with prepared.planning_read():
                current = pick()[0]
                assert current['key'] == 'fresh-259' and current['title'] == 'new catalog title'
                assert len(reads) == 3
                raise RuntimeError('cancel this plan')
        assert prepared.current_reader is None
    finally:
        prepared.close()


def test_active_radio_outlives_head_switch_then_releases_generation_on_close(radio):
    store, generation, prepared = radio
    candidates = RadioCandidates(store, 10, comparison=None)
    try:
        next_generation = publish(store, replace(prepared, external_key='new-radio-head')).generation_id
        collect(store)
        assert store.recommendations.head('radio')['id'] == next_generation
        assert candidates.prepared.generation == generation
        assert candidates.prepared.positions('1', ['fresh-259']) == {'fresh-259': 0}
        assert store.conn.execute('SELECT 1 FROM recommendation_generations WHERE id=?', (generation,)).fetchone()
    finally:
        candidates.close()
        candidates.close()
    collect(store)
    assert not store.conn.execute('SELECT 1 FROM recommendation_generations WHERE id=?', (generation,)).fetchone()


def test_band_headers_page_independently_and_filtered_queries_use_region_index(radio):
    store, generation, _ = radio
    repo = store.recommendations
    assert repo.band_page('radio', '1', 'membership', generation=generation, limit=1) == [
        {'intensity_band': 12, 'genre_key': 'rock'}]
    assert repo.band_page('radio', '1', 'membership', generation=generation, after=(12, 'rock'), limit=1) == [
        {'intensity_band': 40, 'genre_key': 'jazz'}]
    assert repo.band_page('radio', '1', 'membership', generation=generation, band=40) == [
        {'intensity_band': 40, 'genre_key': 'jazz'}]
    queries = []
    # Production snapshot reads execute on the private connection, so trace
    # that connection rather than an idle serving connection.
    with store.read_view() as reader:
        reader.conn.set_trace_callback(queries.append)
        try:
            rows = reader.recommendations.page('radio', '1', 'membership', generation=generation, band=40, genres=['jazz'])
        finally:
            reader.conn.set_trace_callback(None)
        assert [row['key'] for row in rows] == ['fresh-258', 'fresh-259']
        query = next(query for query in queries if 'CROSS JOIN recommendation_candidates' in query)
        details = [row[3] for row in reader.conn.execute('EXPLAIN QUERY PLAN ' + query)]
    assert any('ix_recommendation_radio_band' in detail and 'intensity_band=?' in detail for detail in details)


def test_unprepared_radio_uses_bounded_catalog_fallback_without_legacy_json(recommendations, monkeypatch):
    forbid_bundle(monkeypatch)
    candidates = RadioCandidates(recommendations, 10, comparison=None)
    try:
        assert candidates.prepared.generation is None and not candidates.choice_context
        assert candidates.seed_offer() == []
    finally:
        candidates.close()


def test_empty_radio_offer_can_become_a_single_real_measured_choice(radio):
    from yt_playlist.rec import choice_evidence
    from yt_playlist.web.radio_listening import RadioListening

    store, _, _ = radio
    listening = RadioListening(store, None, None, None, lambda: 10)
    with store.conn:
        store.conn.execute('UPDATE youtube_items SET available=0')
    empty = RadioCandidates(store, 10, comparison=None)
    try:
        assert listening.seed_offer(empty) == ([], None)
    finally:
        empty.close()
    assert store.settings.get_setting('radio_seed_offer') is None
    assert not store.rec_ledger.evidence().records('opportunity')
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=1 WHERE video_id='fresh-upload-0'")
    available = RadioCandidates(store, 10, comparison=None)
    try:
        tracks, menu_id = listening.seed_offer(available)
    finally:
        available.close()
    assert [track['video_id'] for track in tracks] == ['fresh-upload-0']
    assert menu_id
    offer = choice_evidence.opportunity(store, menu_id)
    assert [option['id'] for option in offer['options']] == ['fresh-upload-0']
    assert json.loads(store.settings.get_setting('radio_seed_offer'))['menu_id'] == menu_id
