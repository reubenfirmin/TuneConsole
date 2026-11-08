"""Controller-led catalog admission keeps native and installed SQL owners writable."""
import pytest

from yt_playlist.core.migrations import regular
from yt_playlist.core.store import Store


def ingest(store):
    applied = regular.validate(store.conn)
    first = store.tracks.upsert_track('one', 'Song', 'Artist', 'Album', 180, created_at=10)
    store.tracks.set_track_title(first, 'My title')
    second = store.tracks.upsert_track('two', 'Song', 'Artist', 'Album', 181, created_at=20)
    assert first != second
    assert store.tracks.resolve_identity_key('song|artist') is None
    assert store.tracks.catalog_subject(first)['identity_key'].startswith('@recording:')
    assert store.tracks.catalog_subject(second)['identity_key'].startswith('@recording:')
    assert store.tracks.upsert_track('one', 'Refreshed', 'Artist', 'Album', 182, created_at=30) == first
    assert store.tracks.catalog_subject(first)['title'] == 'My title'
    assert store.tracks.catalog_subject(first)['duration_s'] == 182
    assert store.conn.execute('SELECT COUNT(*) FROM library_memberships').fetchone()[0] == 2
    assert not store.conn.in_transaction
    key = store.discovery.upsert_discovered_track('captured|artist', 'candidate', 'Candidate', 'Artist',
        'Album', None, 'Jazz', '2000', 'radio:one', 40, {'bpm': 120}, duration_s=183)
    assert key == 'captured|artist'
    assert store.conn.execute('SELECT COUNT(*) FROM library_memberships').fetchone()[0] == 2
    assert len(store.discovery.get_discovered_tracks()) == 1
    # Ordinary ingestion works while bounded DDL is pending. The explicit
    # association workflow starts after the real maintenance runner installs it.
    assert regular.validate(store.conn) == applied
    regular.migrate(store.conn)
    assert regular.validate(store.conn) == len(regular.CHANGES)
    recording = store.tracks.catalog_subject(first)['recording_id']
    store.tracks.associate_youtube_item('candidate', recording, source='review',
        evidence={'verified': True}, now=50, same_recording=True)
    assert store.tracks.catalog_subject(first)['title'] == 'My title'
    assert store.tracks.get_track_enrichment(first)[0] == 'Jazz'
    assert store.tracks.identity_key_for_video('candidate') == store.tracks.identity_key_for_video('one')
    assert len(store.discovery.get_discovered_tracks()) == 1
    assert not store.conn.in_transaction
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_native_catalog_ingestion(store):
    ingest(store)


def test_preferred_alias_disambiguation_retries_after_saved_document_failure(store, monkeypatch):
    first = store.tracks.upsert_track('one', 'Song', 'Artist')
    old = store.tracks.catalog_subject(first)['identity_key']
    publish = store.playlist_recipes.rekey_recordings

    def unavailable(*args):
        assert not store.conn.in_transaction
        raise OSError('unavailable saved document')

    monkeypatch.setattr(store.playlist_recipes, 'rekey_recordings', unavailable)
    with pytest.raises(OSError, match='unavailable saved document'):
        store.tracks.upsert_track('two', 'Different title', 'Other credit', preferred_key=old)
    assert store.tracks.catalog_subject(first)['identity_key'] == old
    monkeypatch.setattr(store.playlist_recipes, 'rekey_recordings', publish)
    second = store.tracks.upsert_track('two', 'Different title', 'Other credit', preferred_key=old)
    assert store.tracks.catalog_subject(first)['identity_key'] != old
    assert store.tracks.resolve_identity_key(old) is None
    assert store.tracks.catalog_subject(second)['title'] == 'Different title'
    assert not store.conn.in_transaction


@pytest.mark.parametrize('version', [79, 93, 102, 107])
def test_installed_catalog_ingestion(tmp_path, _admitted_installed_schema, version):
    template = _admitted_installed_schema(version)
    store = Store(tmp_path / 'installed.db')
    try:
        template.restore(store)
        store._bind_repositories()
        assert regular.validate(store.conn) < len(regular.CHANGES)
        assert not store.conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='recording_association_work'").fetchone()
        ingest(store)
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
        with store.read_view() as reopened:
            assert reopened.tracks.catalog_subject(1)['title'] == 'My title'
            assert len(reopened.discovery.get_discovered_tracks()) == 1
    finally:
        store.conn.close()
