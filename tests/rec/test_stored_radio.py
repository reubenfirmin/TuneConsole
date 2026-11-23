"""Complete typed Radio membership with bounded live hydration and reader roots."""
from dataclasses import replace
import json
import sqlite3
from contextlib import closing, nullcontext

import pytest

from tests.group_support import publish_keyed
from tests.legacy_recommendation_fixture_support import recommendations
from tests.repos.test_typed_recommendation_publication import publish, plan, collect
from tests.rec.test_stored_fresh import fresh
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, mock_snapshot_coordinator,
    native_sql_calls, without_query_timing,
)
from yt_playlist.core import recommendation_pools as pools
from yt_playlist.rec import musical_character, radio_intensity, radio_tuning
from yt_playlist.rec.radio_candidates import RadioCandidates, CLUSTER_POOL_LIMIT
from yt_playlist.rec.radio_genres import GenrePolicy
from yt_playlist.rec.stored_radio import HOME_LANES, PUBLICATION_SURFACES, PreparedRadio
from yt_playlist.repos import recommendation_publication as publication
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.recommendation_publications import RecommendationPublications


@pytest.fixture
def disable_periodic_library_sync():
    # These readers do not construct a web application or background sync loop.
    pass


@pytest.fixture
def reader_store(tmp_path, native_sql_calls, without_query_timing):
    # Lifetime roots use real writer transactions; only the seed publisher needs
    # snapshot coordination, supplied separately below the close operation.
    with bounded_consumer_schema((('rock', 'Rock'),), snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as store:
            yield store


@pytest.mark.parametrize('blocked_by', ['sql_writer', 'cancelled_work'])
def test_database_close_releases_radio_readers_when_retirement_cannot_commit(reader_store, monkeypatch, blocked_by):
    from tests.recommendation_support import publish_bundles
    from yt_playlist.core import group_admission, group_documents
    from yt_playlist.core.activity import WorkClosing, cancellable_work
    from yt_playlist.core.store import Store
    from yt_playlist.rec import group_roots

    store = reader_store
    store.conn.execute('PRAGMA synchronous=OFF')
    # Keep the actual competing SQLite writer. Each retirement can encounter
    # SQLITE_BUSY immediately rather than spending a second retrying this
    # deliberately held lock; writer retry timing has its own tests.
    atomic_write = group_admission.atomic_write
    monkeypatch.setattr(group_admission, 'atomic_write',
                        lambda target: atomic_write(target, wait_seconds=0))
    with mock_snapshot_coordinator(store, monkeypatch):
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
                with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
                    store.conn.execute('SELECT 1')
                assert not store.conn._before_close_callbacks
        assert len(list(directory.glob('*.lock'))) == 2
    finally:
        other.rollback()
        other.close()
        for reader in readers:
            reader.close()
        store.conn.close()
    reopened = Store.open_current(store._db_path)
    try:
        assert reopened.conn.execute(
            "SELECT count(*) FROM group_evidence_contexts WHERE kind='pending'").fetchone()[0] == 2
        group_roots.reap_pending(reopened)
        assert not reopened.conn.execute("SELECT 1 FROM group_evidence_contexts WHERE kind='pending'").fetchone()
        assert not list(directory.glob('*.lock'))
    finally:
        reopened.conn.close()


def seed_stored_radio(store):
    original = plan(store)
    keys = [f'fresh-{index}' for index in range(260)]
    members = publish_keyed(store, [('Rock', keys, .4)], now=3)
    assert set(members) == set(keys)
    group, = set(members.values())
    ranked = pools.prepare('membership', original.policy_version, 'd' * 64, 'radio', [
        pools.Candidate(index + 3, .9, 40 if index >= 258 else 12, 'jazz' if index >= 258 else 'rock')
        for index in range(260)])
    home = pools.prepare('fresh', original.policy_version, 'e' * 64, 'fresh',
                         [pools.Candidate(index + 3, .9) for index in range(4)])
    ppr = pools.prepare('ppr', original.policy_version, 'f' * 64, 'ppr',
                        [pools.Candidate(index + 3) for index in reversed(range(260))])
    prepared = replace(original, external_key='radio-head', slots=(
        publication.Slot('home', group, 'fresh', home), publication.Slot('radio', group, 'membership', ranked),
        publication.Slot('radio', group, 'ppr', ppr)), framing=publication.Framing(
            presence=sum(1 << bit for bit in (5, 6, 7, 8)), radio_generation=None, radio_space='',
            intensity_version=radio_intensity.VERSION, radio_profile_version=musical_character.VERSION))
    generation = publish(store, prepared).generation_id
    return generation, prepared, group


@pytest.fixture
def radio(tmp_path, stored_radio_template):
    with stored_radio_template.open(tmp_path / 'recommendations.db') as store:
        yield store, *stored_radio_template.result


def forbid_bundle(monkeypatch):
    original = RecommendationProposals._proposal_json
    def read(self, surface):
        if surface in ('mode_bundles', 'fresh_pool'):
            pytest.fail('Typed Radio decoded a complete legacy pool')
        return original(self, surface)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)


def test_apply_and_fill_ranks_choose_playable_uploads_within_length_limit(radio):
    store, _, _, _ = radio
    with store.conn:
        store.conn.execute("UPDATE track_sources SET duration_s=600,library_origin='observed' "
                           "WHERE video_id='fresh-upload-10'")
        store.conn.execute('INSERT INTO track_sources '
            '(recording_id,video_id,available,duration_s,library_origin,title,artist) '
            "SELECT recording_id,'eligible-upload',1,NULL,library_origin,title,artist "
            "FROM track_sources WHERE video_id='fresh-upload-10'")

    with closing(RadioCandidates(store, 10)) as model:
        def ranks(*, fill, maximum):
            return [(row['purpose'], row['video_id']) for row in model.prepared.tuning_ranks(
                'membership', energy=False, include_fresh=fill, pinned_genre='rock' if fill else None,
                maximum_duration=maximum) if row['key'] == 'fresh-10']

        assert ranks(fill=False, maximum=300) == [('membership', 'eligible-upload')]
        assert sorted(ranks(fill=True, maximum=300)) == [('catalog', 'eligible-upload'),
                                                         ('membership', 'eligible-upload')]
        assert ranks(fill=False, maximum=600) == [('membership', 'fresh-upload-10')]
        with store.conn:
            store.conn.execute("UPDATE track_sources SET duration_s=301 WHERE video_id='eligible-upload'")
        assert ranks(fill=False, maximum=300) == []
        assert ranks(fill=True, maximum=300) == []


def test_fresh_and_combined_fill_keep_recording_rank_when_switching_upload(fresh):
    store, generation = fresh
    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=1,duration_s=180 WHERE video_id='fresh-upload-0'")
        store.conn.execute('INSERT INTO track_sources(recording_id,video_id,available,duration_s,title,artist) '
                           "SELECT recording_id,'fresh-preferred',1,180,title,artist "
                           "FROM track_sources WHERE video_id='fresh-upload-0'")
        store.conn.execute('UPDATE recordings SET discovery_entry_id='
                           "(SELECT id FROM track_sources WHERE video_id='fresh-preferred') "
                           "WHERE identity_key='fresh-0'")

    def ranks(surface, purpose, **options):
        rows = store.radio_catalog.tuning_ranks(generation, surface, purpose, maximum_duration=300, **options)
        return [row for row in rows if row['key'] == 'fresh-0']

    preferred, = ranks('fresh', 'fresh')
    assert preferred['video_id'] == 'fresh-preferred'
    assert preferred['purpose'] == 'fresh' and preferred['_radio_fresh'] and not preferred['heard']
    assert ranks('radio', 'membership', fresh_surface='fresh') == [preferred]

    for available, duration in [(0, 180), (1, 600)]:
        with store.conn:
            store.conn.execute("UPDATE track_sources SET available=?,duration_s=? WHERE video_id='fresh-preferred'",
                               (available, duration))
        alternate, = ranks('fresh', 'fresh')
        assert alternate['video_id'] == 'fresh-upload-0' and alternate['duration'] == 180
        for field in ('recording_id', 'pool_id', 'rank', 'score', 'purpose', '_radio_fresh', 'heard'):
            assert alternate[field] == preferred[field]
        assert ranks('radio', 'membership', fresh_surface='fresh') == [alternate]

    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='fresh-upload-0'")
    assert ranks('fresh', 'fresh') == []
    assert ranks('radio', 'membership', fresh_surface='fresh') == []


def test_upgrade_readiness_uses_the_same_publication_as_the_radio_reader(radio):
    store, home_generation, prepared, _ = radio
    _, membership, ppr = prepared.slots
    publish(store, replace(prepared, external_key='membership-only', slots=(membership,), outputs=()))
    assert store.recommendations.radio_group_pools_ready(PUBLICATION_SURFACES, HOME_LANES) is True

    newest = publish(store, replace(prepared, external_key='ppr-only', slots=(ppr,), outputs=())).generation_id
    assert store.recommendations.head('home')['id'] == home_generation
    with store.read_view() as reader:
        assert PreparedRadio(reader, 10).generation == newest
        assert reader.recommendations.radio_group_pools_ready(PUBLICATION_SURFACES, HOME_LANES) is False


def test_small_initial_lookahead_uses_point_ppr_ranks_and_keeps_exact_publication(radio, monkeypatch):
    store, generation, _, group = radio
    forbid_bundle(monkeypatch)
    pages, original = [], RecommendationPublications.candidate_pages
    def page(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        pages.extend((kwargs['limit'], len(page['tracks'])) for page in value)
        return value
    monkeypatch.setattr(RecommendationPublications, 'candidate_pages', page)
    candidates = RadioCandidates(store, 10)
    try:
        assert candidates.prepared.generation == generation
        assert candidates.cluster_members == {group: 260}
        assert not pages
        assert len(candidates._initial_track_group_pool(group)) == 4
        assert candidates.prepared.positions(group, ['fresh-0', 'fresh-259', 'missing']) == {
            'fresh-0': 259, 'fresh-259': 0}
        assert candidates.choice_context['recommendation_generations'] == [generation]
        assert pages and all(limit == 128 and count <= 128 for limit, count in pages)
        assert all(track['serving_context']['recommendation_generation_id'] == generation
                   for track in candidates.seed_offer(4))
    finally:
        candidates.close()


def test_pinned_rare_genre_refills_beyond_two_complete_pages_without_full_vectors(radio, monkeypatch):
    store, generation, _, group = radio
    forbid_bundle(monkeypatch)
    candidates = RadioCandidates(store, 10, {**radio_tuning.DEFAULTS, 'theme': 1})
    def forbidden(*args, **kwargs):
        pytest.fail('Typed Radio refill requested the complete model')
    monkeypatch.setattr(store.rec_model, 'content_generation', forbidden)
    try:
        anchor = {'key': 'anchor', 'video_id': 'anchor-upload', 'genre': 'jazz'}
        genres = GenrePolicy(1, [], current=anchor)
        tracks = candidates._cluster_pool(group, [], set(), genres=genres)
        assert {track['key'] for track in tracks} == {'fresh-258', 'fresh-259'}
        assert len(tracks) == 2
        assert len(tracks) <= CLUSTER_POOL_LIMIT
        assert {track['serving_context']['recommendation_generation_id'] for track in tracks} == {generation}
        assert not candidates._cluster_pool(group, tracks, set(), genres=genres)
    finally:
        candidates.close()


def test_intensity_reads_selected_rare_region_and_rechecks_live_source_metadata(radio, monkeypatch):
    store, _, _, group = radio
    forbid_bundle(monkeypatch)
    candidates = RadioCandidates(store, 10)
    try:
        assert candidates.prepared.has_intensity
        with store.conn:
            store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='fresh-upload-258'")
            store.conn.execute("UPDATE track_sources SET duration_s=100000 WHERE video_id='fresh-upload-259'")
        priority, tracks = candidates.prepared.intensity_pool(group, radio_intensity.FULL_ON, genre_allowed=lambda genre: genre == 'jazz',
            allowed=candidates._allowed, excluded=set(), used=set())
        assert priority is None and tracks == []
        with store.conn:
            store.conn.execute("UPDATE track_sources SET duration_s=200,title='corrected' WHERE video_id='fresh-upload-259'")
        priority, tracks = candidates.prepared.intensity_pool(group, radio_intensity.FULL_ON, genre_allowed=lambda genre: genre == 'jazz',
            allowed=candidates._allowed, excluded=set(), used=set())
        assert priority == radio_intensity.band_priority(40, radio_intensity.FULL_ON)
        assert [(track['key'], track['title'], track['duration']) for track in tracks] == [
            ('fresh-259', 'corrected', 200)]
    finally:
        candidates.close()


def test_old_intensity_bands_do_not_override_current_group_membership(radio):
    store, _, prepared, group = radio
    legacy = replace(prepared.framing, intensity_version=2)
    publish(store, replace(prepared, framing=legacy, external_key='legacy-radio-bands'))
    candidates = RadioCandidates(store, 10)
    try:
        assert group in candidates.cluster_members
        assert not candidates.prepared.has_intensity
        assert candidates.prepared.intensity_headings() == {}
    finally:
        candidates.close()


def test_planning_reuses_pages_but_rechecks_used_tracks_and_next_pass_catalog(radio, monkeypatch):
    store, _, _, group = radio
    candidates = RadioCandidates(store, 10)
    prepared = candidates.prepared
    reads, original = [], RecommendationPublications.candidate_page
    facts, hydrate = [], type(store.rec_query).group_candidates

    def catalog(self, keys, *args, **kwargs):
        facts.extend(keys)
        return hydrate(self, keys, *args, **kwargs)

    def page(self, *args, **kwargs):
        reads.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
    monkeypatch.setattr(type(store.rec_query), 'group_candidates', catalog)

    def pick(used=()):
        return prepared.intensity_pool(group, radio_intensity.FULL_ON, genre_allowed=lambda label: label == 'jazz',
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
            overlap = prepared.tracks('radio', group, 'membership', band=40,
                allowed=lambda track: True, include_display=False)
            assert [track['key'] for track in overlap] == ['fresh-258', 'fresh-259']
            assert facts == ['fresh-258', 'fresh-259']
            # A rotated rank window may wrap once to the beginning. Each page
            # still hydrates only once in this planning snapshot.
            count = len(reads)
            assert count == len({repr(read) for read in reads}) <= 3
        assert prepared.current_reader is None
        with store.conn:
            store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='fresh-upload-258'")
            store.conn.execute("UPDATE track_sources SET title='new catalog title' WHERE video_id='fresh-upload-259'")
        with pytest.raises(RuntimeError, match='cancel this plan'):
            with prepared.planning_read():
                current = pick()[0]
                assert current['key'] == 'fresh-259' and current['title'] == 'new catalog title'
                assert len(reads) == count + 1
                raise RuntimeError('cancel this plan')
        assert prepared.current_reader is None
    finally:
        candidates.close()


def test_active_radio_outlives_head_switch_then_releases_generation_on_close(radio):
    store, generation, prepared, group = radio
    candidates = RadioCandidates(store, 10)
    try:
        next_generation = publish(store, replace(prepared, external_key='new-radio-head')).generation_id
        collect(store)
        assert store.recommendations.head('radio')['id'] == next_generation
        assert candidates.prepared.generation == generation
        assert candidates.prepared.positions(group, ['fresh-259']) == {'fresh-259': 0}
        assert store.conn.execute('SELECT 1 FROM recommendation_generations WHERE id=?', (generation,)).fetchone()
    finally:
        candidates.close()
        candidates.close()
    collect(store)
    assert not store.conn.execute('SELECT 1 FROM recommendation_generations WHERE id=?', (generation,)).fetchone()


def test_band_headers_page_independently_and_filtered_queries_use_region_index(radio):
    store, generation, _, group = radio
    repo = store.recommendations
    assert repo.band_page('radio', group, 'membership', generation=generation, limit=1) == [
        {'intensity_band': 12, 'genre_key': 'rock'}]
    assert repo.band_page('radio', group, 'membership', generation=generation, after=(12, 'rock'), limit=1) == [
        {'intensity_band': 40, 'genre_key': 'jazz'}]
    assert repo.band_page('radio', group, 'membership', generation=generation, band=40) == [
        {'intensity_band': 40, 'genre_key': 'jazz'}]
    queries = []
    # Production snapshot reads execute on the private connection, so trace
    # that connection rather than an idle serving connection.
    with store.read_view() as reader:
        reader.conn.set_trace_callback(queries.append)
        try:
            rows = reader.recommendations.page('radio', group, 'membership', generation=generation, band=40, genres=['jazz'])
            genre_rows = reader.recommendations.page('radio', group, 'membership', generation=generation,
                                                     after=rows[0]['rank'], genres=['jazz'])
        finally:
            reader.conn.set_trace_callback(None)
        assert [row['key'] for row in rows] == ['fresh-258', 'fresh-259']
        assert [row['key'] for row in genre_rows] == ['fresh-259']
        for order, field in (('band', 'intensity_band'), ('genre', 'genre_key')):
            index = f'ix_recommendation_radio_{order}'
            query = next(query for query in queries if f'INDEXED BY {index}' in query)
            details = [row[3] for row in reader.conn.execute('EXPLAIN QUERY PLAN ' + query)]
            assert any(index in detail and f'{field}=?' in detail for detail in details)


def test_unprepared_radio_uses_bounded_catalog_fallback_without_legacy_json(recommendations, monkeypatch):
    forbid_bundle(monkeypatch)
    candidates = RadioCandidates(recommendations, 10)
    try:
        assert candidates.prepared.generation is None and not candidates.choice_context
        assert candidates.seed_offer() == []
    finally:
        candidates.close()


def test_empty_radio_offer_can_become_a_single_real_measured_choice(radio):
    from yt_playlist.rec import choice_evidence
    from yt_playlist.web.radio_listening import RadioListening

    store, _, _, _ = radio
    listening = RadioListening(store, None, None, None, lambda: 10)
    with store.conn:
        store.conn.execute('UPDATE track_sources SET available=0')
    empty = RadioCandidates(store, 10)
    try:
        assert listening.seed_offer(empty) == ([], None)
    finally:
        empty.close()
    assert store.settings.get_setting('radio_seed_offer') is None
    assert not store.rec_ledger.evidence().records('opportunity')
    with store.conn:
        store.conn.execute("UPDATE track_sources SET available=1 WHERE video_id='fresh-upload-0'")
    available = RadioCandidates(store, 10)
    try:
        tracks, menu_id = listening.seed_offer(available)
    finally:
        available.close()
    assert [track['video_id'] for track in tracks] == ['fresh-upload-0']
    assert menu_id
    offer = choice_evidence.opportunity(store, menu_id)
    assert [option['id'] for option in offer['options']] == ['fresh-upload-0']
    assert json.loads(store.settings.get_setting('radio_seed_offer'))['menu_id'] == menu_id
