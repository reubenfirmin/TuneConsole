"""Discovery membership and shared preparation reads preserve the selected corpus."""
import sqlite3
from tests.enrichment_support import seed_discovered_audio, seed_discovered_enrichment

import pytest

from tests.core.test_predecessor_writes import installed
from tests.publication_fixture_support import installed as prepared_discovery
from tests.store_fixture_support import _publication_preparation_input, native_sql_calls
from yt_playlist.core.preparation.discovery_publication import read_owners


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Repository assertions do not construct an HTTP application."""


@pytest.fixture(autouse=True)
def repository_sql(native_sql_calls):
    """Use real SQLite below these synchronous repository reads.

    Instrumented handle admission and thread serialization have their own
    test_db_thread_serialization owners.
    """


def seed(store):
    key = store.discovery.upsert_discovered_track(
        'captured spelling', 'upload', 'Song', 'Artist', 'Album', 'art',
        'House', '2001', 'radio:seed', 10)
    seed_discovered_audio(store, key, bpm=123, energy=.7)
    store.discovery.record_discovery_impressions('track', [key], 'visit', 20)
    return key


@pytest.mark.parametrize('installed', [79, 99, 107], indirect=True)
def test_membership_reads_canonical_keys_without_hydrating_track_fields(installed, monkeypatch):
    store, _ = installed
    key = seed(store)
    assert store.discovery.upsert_discovered_track(
        'another captured spelling', 'upload', 'Song', 'Artist', 'Album', 'art',
        'House', '2001', 'radio:other-seed', 30) == key
    store.tracks.upsert_track('owned', 'Owned', 'Other', None, None)
    with store.read_view() as reader:
        denied = []

        def authorize(action, table, column, database, origin):
            if action == sqlite3.SQLITE_READ and table in {
                    'discovery_sources', 'discovery_origin_tokens', 'metadata_observations',
                    'metadata_selections', 'recording_audio'}:
                denied.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def unexpected_hydration(row):
            pytest.fail('Membership reads must not decode track metadata or audio')

        monkeypatch.setattr(type(reader.discovery.repository), '_dtrack_row', staticmethod(unexpected_hydration))
        reader.conn.set_authorizer(authorize)
        try:
            assert reader.discovery.discovered_track_keys() == {key}
        finally:
            reader.conn.set_authorizer(None)
        assert not denied


@pytest.mark.parametrize('tracked', [False, True])
def test_full_reads_reuse_detached_rows_and_keep_pinned_state(store, monkeypatch, tracked):
    key = seed(store)
    decoded = []
    repo = type(store.discovery.repository)
    original = repo._dtrack_row

    def decode(row):
        decoded.append(row['identity_key'])
        return original(row)

    monkeypatch.setattr(repo, '_dtrack_row', staticmethod(decode))
    with store.read_view(tracked=tracked) as reader:
        relation = read_owners(reader.conn).relation('discovered_tracks')
        expected = [original(row) for row in reader.conn.execute('SELECT * FROM ' + relation)]
        first = reader.discovery.get_discovered_tracks()
        assert first == expected
        first[0]['audio']['bpm'] = -1
        first[0]['title'] = 'Caller mutation'
        seed_discovered_audio(store, key, bpm=140)
        seed_discovered_enrichment(store, key, 'Techno', '2020')
        store.discovery.upsert_discovered_track('second', 'new', 'New', 'Other', None, None,
                                               None, None, 'radio:seed', 30)
        assert reader.discovery.get_discovered_tracks() == expected
        assert decoded == [key]
    with store.read_view(tracked=tracked) as reader:
        rows = {row['identity_key']: row for row in reader.discovery.get_discovered_tracks()}
        assert len(rows) == 2
        assert rows[key]['audio']['bpm'] == 140
        assert (rows[key]['genre'], rows[key]['year']) == ('Techno', '2020')
        assert reader.discovery.discovered_track_keys() == rows.keys()


def test_shared_read_keeps_publication_dependencies_on_cache_hits(store):
    key = seed(store)
    with store.read_snapshot() as snapshot:
        assert snapshot.store.discovery.get_discovered_tracks()[0]['identity_key'] == key
    with store.read_snapshot() as snapshot:
        assert snapshot.store.discovery.get_discovered_tracks()[0]['audio']['bpm'] == 123
        seed_discovered_audio(store, key, bpm=140)
        assert not snapshot.publish(lambda: pytest.fail('Changed discovery input was published'))
    with store.read_snapshot() as snapshot:
        assert snapshot.store.discovery.get_discovered_tracks()[0]['audio']['bpm'] == 140


@pytest.mark.parametrize('change', ['metadata', 'audio', 'offer', 'source', 'membership'])
def test_tracked_full_read_invalidates_each_changed_input(store, change):
    key = seed(store)
    with store.read_view(tracked=True) as reader:
        before = reader.discovery.get_discovered_tracks()
    if change == 'metadata':
        seed_discovered_enrichment(store, key, 'Techno', '2020')
    elif change == 'audio':
        seed_discovered_audio(store, key, bpm=140)
    elif change == 'offer':
        store.discovery.record_discovery_impressions('track', [key], 'new-visit', 30)
    elif change == 'source':
        store.discovery.upsert_discovered_track(key, 'upload', 'Song', 'Artist', 'Album', 'art',
                                               'House', '2001', 'radio:earlier', 1)
    else:
        store.discovery.prune_discovered_tracks({key})
    with store.read_view(tracked=True) as reader:
        relation = read_owners(reader.conn).relation('discovered_tracks')
        expected = [reader.discovery.repository._dtrack_row(row) for row in
                    reader.conn.execute('SELECT * FROM ' + relation)]
        assert expected != before
        assert reader.discovery.get_discovered_tracks() == expected
        assert reader.discovery.discovered_track_keys() == {row['identity_key'] for row in expected}


@pytest.fixture
def prepared_read_sql(prepared_discovery, native_sql_calls, monkeypatch):
    # Admission and historical replay belong to the installed membership cases
    # above and test_installed_fixture. Keep real preparation/publication SQL,
    # files and fresh reader connections beneath these repository assertions.
    store, _ = prepared_discovery
    _publication_preparation_input(store, monkeypatch)


@pytest.mark.parametrize('prepared_discovery', [79, 99], indirect=True)
def test_reads_follow_prepared_discovery_selection_and_continuing_writes(
        prepared_discovery, prepared_read_sql):
    from tests.core.test_discovery_owner_preparation import seed as seed_owners
    from tests.core.test_discovery_publication import ready

    store, _ = prepared_discovery
    seed_owners(store)
    owner, stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    with store.read_view(tracked=True) as reader:
        assert read_owners(reader.conn).revision is not None
        before = reader.discovery.get_discovered_tracks()
        assert reader.discovery.discovered_track_keys() == {row['identity_key'] for row in before}
    store.discovery.upsert_discovered_track('new', 'new', 'New', 'Other', None, None,
                                           'Techno', '2020', 'radio:seed', 40)
    with store.read_view(tracked=True) as reader:
        relation = read_owners(reader.conn).relation('discovered_tracks')
        expected = [reader.discovery.repository._dtrack_row(row) for row in
                    reader.conn.execute('SELECT * FROM ' + relation)]
        assert len(expected) == len(before) + 1
        assert reader.discovery.get_discovered_tracks() == expected
        assert reader.discovery.discovered_track_keys() == {row['identity_key'] for row in expected}
