"""Deliberate returns follow verified releases without multiplying shared dates."""
from copy import deepcopy
from tests.enrichment_support import use_existing_album_tracks

import numpy as np
import pytest

from tests.repos.test_album_aliases import OLD, ALIAS, release
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader, without_query_timing
from yt_playlist.core.store import Store
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.rec import choice_evidence, scoring


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Scoring and evidence tests do not construct the web application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('rock', 'Rock'),)) as schema:
        yield schema


@pytest.fixture
def store(_pristine_current_schema, tmp_path, monkeypatch):
    database = Store(tmp_path / 'album-return.db')
    try:
        initialize_consumer(database, _pristine_current_schema)
        use_existing_album_tracks(database, monkeypatch)
        with reuse_reporting_reader(database, monkeypatch):
            yield database
    finally:
        database.conn.close()


def seed_return_catalog(store, first, before, saved):
    second = ALIAS if first == OLD else OLD
    payload = release()
    remember_album(store, first, payload)
    # Fetching a release caches catalog entries; it does not own its tracks.
    # Both vector spaces need actual library inputs for this scoring check.
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO library_memberships "
                           "SELECT id,'fixture',NULL FROM catalog_entries")
    if saved:
        store.collection.add_saved_album({'browse': first, 'title': payload['title'], 'artist': 'All Them Witches'})
    if not before:
        remember_album(store, second, payload)
    keys = [f'track {index}|all them witches' for index in range(9)]
    vectors = [(key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys]
    # The return-weight boundary consumes published spaces, independently of
    # the content encoder's training and provider metadata ingestion.
    store.rec_model.replace_rec_vectors(vectors)
    store.rec_model.replace_rec_content_vectors(vectors)
    return payload, keys


@pytest.fixture
def return_catalog(store, first, before, saved):
    # Preserve alias/save/model construction order for every distinct baseline.
    # Each combination is used once, so a retained template only copies it again.
    return seed_return_catalog(store, first, before, saved)


@pytest.mark.parametrize('first', [OLD, ALIAS])
@pytest.mark.parametrize('before', [True, False])
@pytest.mark.parametrize('saved', [True, False])
def test_confirmed_return_days_follow_verified_release_in_both_spaces(store, first, before, saved, return_catalog):
    second = ALIAS if first == OLD else OLD
    payload, keys = return_catalog
    tracks = [{'key': key, 'video_id': row['videoId']} for key, row in zip(keys, payload['tracks'])]
    start = 1_700_000_000
    for day in range(2):
        # The durable owner keeps both original aliases on each day. Pure
        # summary cases own preferred-only, alias-only and split-day arithmetic.
        for index, browse in enumerate((first, second)):
            at = start + day * 86400
            oid = choice_evidence.issue(store, 'albums', [{'id': browse, 'tracks': tracks}], at,
                context={}, opportunity_id=f'return-{day}-{index}')
            choice_evidence.respond(store, oid, 'choice', at + 1, option_id=browse)
            choice_evidence.respond(store, oid, 'start', at + 2, option_id=browse,
                                    details={'video_id': tracks[0]['video_id']})
    ledger = store.rec_ledger.evidence()
    original = {kind: deepcopy(ledger.records(kind)) for kind in ('opportunity', 'response')}
    now = start + 2 * 86400
    choice_evidence.refresh_report(store, now)
    if before:
        remember_album(store, second, payload)
        # Reconciliation must invalidate the cache inside its 15-minute deadline,
        # without requiring another listening event or an explicit full replay.
        choice_evidence.refresh_report(store, now + 1)
    assert store.collection.album_id(first) == store.collection.album_id(second)
    projection = store.rec_proposals.get_proposals('choice_interests')
    assert projection['album_track_days'] == {first: {keys[0]: 2}}
    for taste in (scoring.playlist_taste(store, now=now + 1), scoring.content_taste(store, now=now + 1)):
        source, = [row for row in taste.sources if row['kind'] == 'album']
        assert source['saved'] is saved
        assert source['return_days'] == 2
        assert source['return_coverage'] == pytest.approx(1 / 9)
        assert source['return_weight'] == pytest.approx(np.log(2) / 9)
    assert {kind: ledger.records(kind) for kind in original} == original


def test_distinct_editions_do_not_pool_days_and_withdrawals_recompute_returns(store):
    first, second = release(), release()
    second['year'] = '2016'
    remember_album(store, OLD, first)
    remember_album(store, ALIAS, second)
    assert store.collection.album_id(OLD) != store.collection.album_id(ALIAS)
    start = 1_700_000_000
    for day in range(2):
        browse = (OLD, ALIAS)[day]
        at = start + day * 86400
        oid = choice_evidence.issue(store, 'albums', [{'id': browse, 'tracks': [
            {'key': 'track 0|all them witches', 'video_id': first['tracks'][0]['videoId']}]}], at,
            context={}, opportunity_id=f'edition-{day}')
        choice_evidence.respond(store, oid, 'choice', at + 1, option_id=browse)
        choice_evidence.respond(store, oid, 'start', at + 2, option_id=browse,
                                details={'video_id': first['tracks'][0]['videoId']})
    now = start + 2 * 86400
    choice_evidence.refresh_report(store, now)
    assert store.rec_proposals.get_proposals('choice_interests')['album_track_days'] == {
        OLD: {'track 0|all them witches': 1}, ALIAS: {'track 0|all them witches': 1}}
    choice_evidence.respond(store, 'edition-1', 'withdraw', now + 1,
                            details={'action_id': f'edition-1:choice:{ALIAS}'})
    choice_evidence.refresh_report(store, now + 2)
    assert store.rec_proposals.get_proposals('choice_interests')['album_track_days'] == {
        OLD: {'track 0|all them witches': 1}}
