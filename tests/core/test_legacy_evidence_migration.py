"""Retained originals survive relocation, interrupted publication and recovery."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests import file_custody_support as custody
from yt_playlist.core import artists, legacy_evidence as evidence, policy_documents
from yt_playlist.core.migrations import v076_legacy_evidence_owners as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'archive.db')
    _pristine_installed_schema(75).backup(store.conn)
    store._bind_repositories()
    yield store
    store.conn.close()


def apply(store):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(42))
    migration.validate(store.conn)
    store._bind_repositories()


def test_original_text_and_exact_subjects_remain_distinct(predecessor):
    store = predecessor
    target = store.tracks.upsert_track('video', 'Song', 'Artist', None, 100)
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (target,)).fetchone()[0]
    with store.conn:
        store.conn.execute('INSERT INTO legacy_track_refs VALUES (17,?,?,?)', (target, recording, 'video'))
        store.conn.executemany('INSERT INTO legacy_track_evidence VALUES (?,?,?)',
                              [('library', '17', ' {"raw": 1} '), ('library', 'unknown', ' {"raw": 1} ')])
        store.conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                           ('actions', 'historical', 'non-JSON: exact signature', 'unknown remote outcome'))
    apply(store)
    assert evidence.original(store.conn, 'catalog', 'library', '17') == ' {"raw": 1} '
    assert evidence.original(store.conn, 'workflow', 'actions', 'historical') == 'non-JSON: exact signature'
    rows = store.conn.execute('SELECT source_id,entry_id,recording_id,resolution_status FROM legacy_catalog_evidence ORDER BY source_id').fetchall()
    assert list(map(tuple, rows)) == [('17', target, recording, 'resolved'), ('unknown', None, None, 'unresolved')]
    assert store.conn.execute('SELECT count(*) FROM legacy_track_refs').fetchone()[0] == 1
    assert not store.conn.execute("SELECT 1 FROM sqlite_schema WHERE name IN ('legacy_track_evidence','identity_state_archive','legacy_enrichment_attempts')").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        store.conn.execute("UPDATE legacy_catalog_evidence SET entry_id=NULL")
    with pytest.raises(sqlite3.IntegrityError, match='retention disposition'):
        store.conn.execute('DELETE FROM legacy_catalog_evidence')
    store.conn.rollback()


def test_ambiguity_guard_is_typed_and_survives_an_unreadable_original(predecessor):
    store = predecessor
    artist = store.artists.ensure_artist('Same')
    with store.conn:
        store.conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                           ('ambiguous_album_artist', str(artist), '{"artist_id":%d}' % artist, 'contextual split'))
    apply(store)
    filename = store.conn.execute('SELECT filename FROM artist_identity_evidence').fetchone()[0]
    Path(filename).write_text('corrupt')
    assert artists.ambiguous_context(store.conn, artist)
    with pytest.raises(ValueError, match='pack|changed'):
        evidence.original(store.conn, 'artist', 'ambiguous_album_artist', str(artist))


def test_dismissals_preserve_shared_scope_and_ambiguous_account_meaning(predecessor):
    store = predecessor
    account = store.accounts.upsert_account('one', 'cred', None, True)
    from yt_playlist.core import playlist_scopes
    with store.conn:
        shared = playlist_scopes.ensure(store.conn, 'ordinary', account)
        playlist_scopes.ensure(store.conn, 'SE', account)
        store.conn.execute('CREATE TABLE stale_dismissed(ytm TEXT PRIMARY KEY,until REAL)')
        store.conn.executemany('INSERT INTO stale_dismissed VALUES (?,?)', [('ordinary', 12), ('SE', None)])
    apply(store)
    assert list(map(tuple, store.conn.execute('SELECT * FROM legacy_playlist_dismissals ORDER BY source_ref'))) == [
        ('SE', None, None, 'ambiguous_account'), ('ordinary', shared, 12.0, 'resolved')]
    assert json.loads(evidence.original(store.conn, 'playlist', 'stale_dismissed', 'SE')) == {'ytm': 'SE', 'until': None}


def test_unknown_kind_aborts_before_writing_files(predecessor):
    with predecessor.conn:
        predecessor.conn.execute("INSERT INTO identity_state_archive VALUES ('unclassified','key','{}','retained')")
    with pytest.raises(MigrationError, match='Unclassified'):
        apply(predecessor)
    assert not evidence.directory(predecessor.conn).exists()
    assert predecessor.conn.execute('SELECT count(*) FROM identity_state_archive').fetchone()[0] == 1


@pytest.mark.parametrize(('kind', 'domain', 'row'), [
    ('constraint_legacy:playlist_items', 'playlist',
     {'playlist_id': 17, 'video_id': 'old-upload', 'position': -1}),
    ('constraint_legacy:unresolved_playlist_items', 'playlist',
     {'playlist_id': 17, 'entry_id': 123, 'position': -1}),
    ('constraint_legacy:playlists', 'playlist',
     {'id': 17, 'scope_ref': 'SE', 'ytm_playlist_id': 'SE', 'identity_id': 123}),
    ('constraint_legacy:road_trip_drafts', 'workflow',
     {'recipe_id': 17, 'state': 'retained draft', 'updated_at': 7}),
    ('road_trip_drafts', 'workflow',
     {'recipe_id': 18, 'state': 'original identity conversion', 'updated_at': 8}),
])
def test_relationship_repair_originals_survive_archive_cutover(predecessor, kind, domain, row):
    # v25 uses this frozen producer for repaired positions/scopes and orphan drafts.
    # These receipts remain supported even when their former subjects no longer exist.
    from yt_playlist.core.migrations.v008_recordings import _archive

    with predecessor.conn:
        _archive(predecessor.conn, kind, 'old-subject', row, 'retained relationship repair')
    original = predecessor.conn.execute(
        'SELECT payload FROM identity_state_archive WHERE kind=?', (kind,)).fetchone()[0]
    apply(predecessor)
    assert evidence.original(predecessor.conn, domain, kind, 'old-subject') == original
    retained = predecessor.conn.execute(
        f'SELECT reason,resolution_status FROM {evidence.DOMAINS[domain]} WHERE kind=?', (kind,)).fetchone()
    assert tuple(retained) == ('retained relationship repair', 'unresolved')
    assert not predecessor.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_failed_publication_rolls_back_then_retry_and_gc_are_fenced(predecessor, monkeypatch):
    with predecessor.conn:
        predecessor.conn.execute("INSERT INTO legacy_track_evidence VALUES ('library','old','exact')")
    publish = policy_documents._publish_encoded
    def fail(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError('interrupted after durable file')
    with monkeypatch.context() as changed:
        changed.setattr(policy_documents, '_publish_encoded', fail)
        with pytest.raises(OSError, match='interrupted'):
            apply(predecessor)
    assert predecessor.conn.execute('SELECT payload FROM legacy_track_evidence').fetchone()[0] == 'exact'
    apply(predecessor)
    assert evidence.original(predecessor.conn, 'catalog', 'library', 'old') == 'exact'
    root = evidence.directory(predecessor.conn)
    with predecessor.conn:
        predecessor.conn.execute('BEGIN IMMEDIATE')
        evidence.retain(predecessor.conn, 'actions', 'orphan', 'never committed', 'rollback')
        predecessor.conn.rollback()
    with policy_documents.retained_reader(root):
        assert evidence.collect_orphans(predecessor.conn) == 0
    assert evidence.collect_orphans(predecessor.conn) == 1
    assert evidence.original(predecessor.conn, 'catalog', 'library', 'old') == 'exact'


def test_backup_restores_every_owner_before_relocation(predecessor, tmp_path):
    store = predecessor
    with store.conn:
        store.conn.execute("INSERT INTO identity_state_archive VALUES ('actions','signature','not-json','retain')")
    apply(store)
    destination = sqlite3.connect(tmp_path / 'restored.db')
    destination.row_factory = sqlite3.Row
    try:
        with policy_documents.retained_reader(evidence.directory(store.conn)):
            store.conn.execute('BEGIN')
            store.conn.execute('SELECT 1 FROM workflow_legacy_evidence').fetchone()
            store.conn.backup(destination)
            manifests = custody.export_files(store.conn, tmp_path / 'bundle.db')
            store.conn.rollback()
        assert 'archives' in manifests
        custody.restore_files(destination, manifests)
        assert evidence.original(destination, 'workflow', 'actions', 'signature') == 'not-json'
        assert destination.execute('SELECT filename FROM workflow_legacy_evidence').fetchone()[0] != store.conn.execute('SELECT filename FROM workflow_legacy_evidence').fetchone()[0]
    finally:
        destination.close()


def test_continuing_unresolved_attempts_keep_independent_captures(predecessor):
    apply(predecessor)
    for when in (1, 2):
        predecessor.enrichment_state.record_enrichment_attempt('missing identity', 'provider', {}, now=when)
    rows = predecessor.conn.execute('SELECT kind,source_id FROM metadata_legacy_evidence').fetchall()
    assert len(rows) == 2
    captures = [json.loads(evidence.original(predecessor.conn, 'metadata', *row)) for row in rows]
    assert {row['at'] for row in captures} == {1, 2}
    assert all(row['identity_key'] == 'missing identity' for row in captures)
