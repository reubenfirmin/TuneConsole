"""Ordinary account/scope writes continue on a migrated real library copy."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.schema.test_populated_groups import fingerprint, populated
from tests.schema.populated_state_owners import retained_originals, scope_and_account_state
from yt_playlist.core import album_memberships, legacy_evidence
from yt_playlist.core.db_diagnostics import DiagnosticCursor
from yt_playlist.core.store import Store


def _storage(conn, schema, tables):
    placeholders = ','.join('?' for _ in tables)
    names = [r[0] for r in conn.execute(
        f'SELECT name FROM {schema}.sqlite_schema WHERE tbl_name IN ({placeholders})', tables)]
    placeholders = ','.join('?' for _ in names)
    return conn.execute(f'SELECT coalesce(sum(pgsize),0) FROM dbstat(?) WHERE name IN ({placeholders})',
                        (schema, *names)).fetchone()[0]


def test_populated_scope_snapshots_interruptions_cohort_and_restart(populated, request, monkeypatch):
    source, target, output = populated
    source_path = Path(source.execute('PRAGMA database_list').fetchone()[2])
    donor = Path(request.config.getoption('--schema-upgraded-db')).resolve(strict=True)
    digest = fingerprint(donor)
    db_path = Path(target.execute('PRAGMA database_list').fetchone()[2])
    store = Store(db_path)
    reader = None
    report = {'donor': str(donor), 'donor_sha256': digest, 'controlled_transitions_on_real_library': True}
    try:
        store.init_schema()
        conn = store.conn
        reader = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
        reader.row_factory = sqlite3.Row
        reader.execute('ATTACH DATABASE ? AS original', (source_path.as_uri() + '?mode=ro&immutable=1',))
        report['retained_originals'] = retained_originals(reader)
        report['original_scope_state'] = scope_and_account_state(reader)
        scope_before = ('playlist_scopes', 'playlist_group', 'hidden_playlists', 'overlap_ignored',
                        'playlist_provenance', 'album_library_revisions', 'album_library_snapshots',
                        'album_library_legacy_cohort', 'album_library_memberships')
        scope_after = ('playlist_scopes', 'account_album_state', 'account_albums', 'unattributed_album_memberships')
        report['scope_sql_bytes_before'] = _storage(reader, 'original', scope_before)
        report['scope_sql_bytes_after'] = _storage(reader, 'main', scope_after)
        report['archive_sql_bytes_before'] = _storage(reader, 'original',
            ('legacy_track_evidence', 'identity_state_archive', 'legacy_enrichment_attempts', 'stale_dismissed'))
        report['archive_sql_bytes_after'] = _storage(reader, 'main',
            (*legacy_evidence.DOMAINS.values(), 'artist_ambiguities', 'legacy_playlist_dismissals'))
        filenames = {r[0] for table in legacy_evidence.DOMAINS.values()
                     for r in reader.execute(f'SELECT DISTINCT filename FROM {table}')}
        report['archive_file_bytes_after'] = sum(Path(filename).stat().st_size for filename in filenames)
        retained = list(map(tuple, conn.execute('SELECT * FROM playlist_membership_refs ORDER BY id')))
        playlists = list(map(tuple, conn.execute('SELECT * FROM playlists ORDER BY id')))
        account = conn.execute('SELECT id FROM identities WHERE active=1 ORDER BY id LIMIT 1').fetchone()[0]
        albums = store.collection.get_saved_albums(identity_id=account)
        revision = store.collection.saved_album_revision(account)
        unknown = conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0]
        assert albums and unknown > 0
        statements = []
        execute = DiagnosticCursor.execute
        def capture_execute(cursor, sql, parameters=()):
            if cursor.connection is conn:
                statements.append(sql)
            return execute(cursor, sql, parameters)
        with monkeypatch.context() as captured:
            captured.setattr(DiagnosticCursor, 'execute', capture_execute)
            accepted = store.collection.replace_saved_album_snapshots({account: albums},
                expected_revisions={account: revision}, now=1793260300)
        assert accepted == {account}
        assert store.collection.saved_album_revision(account) == revision + 1
        # Count submitted statements, excluding SQLite's repeated trace events
        # while the account ownership guards execute.
        checkpoint_writes = [sql for sql in statements if sql.startswith('INSERT INTO account_album_state')]
        assert len(checkpoint_writes) == 1
        report['accepted_snapshot_checkpoint_writes'] = len(checkpoint_writes)
        before = list(map(tuple, conn.execute('SELECT * FROM account_album_state ORDER BY account_id')))
        assert store.collection.replace_saved_album_snapshots({account: []},
            expected_revisions={account: revision}, now=1793260301) == set()
        assert list(map(tuple, conn.execute('SELECT * FROM account_album_state ORDER BY account_id'))) == before
        membership_before = list(map(tuple, conn.execute('SELECT * FROM account_albums ORDER BY account_id,album_id')))
        observe = album_memberships.observe_snapshot
        def interrupt(*args):
            observe(*args)
            raise RuntimeError('interrupted populated checkpoint publication')
        with monkeypatch.context() as patched:
            patched.setattr(album_memberships, 'observe_snapshot', interrupt)
            with pytest.raises(RuntimeError, match='interrupted populated'):
                store.collection.replace_saved_album_snapshots({account: []}, now=1793260302)
        assert list(map(tuple, conn.execute('SELECT * FROM account_album_state ORDER BY account_id'))) == before
        assert list(map(tuple, conn.execute('SELECT * FROM account_albums ORDER BY account_id,album_id'))) == membership_before
        cohort = conn.execute('SELECT i.* FROM identities i JOIN account_album_state s ON s.account_id=i.id '
            'WHERE s.legacy_cohort_present=1 AND s.legacy_cohort_observed_at IS NULL ORDER BY i.id').fetchall()
        assert cohort and any(not r['active'] for r in cohort)
        new_account = store.accounts.upsert_account('schema491-new-account', 'controlled', None, False)
        assert not conn.execute('SELECT 1 FROM account_album_state WHERE account_id=? AND legacy_cohort_present=1',
                                (new_account,)).fetchone()
        for index, original in enumerate(cohort):
            if not original['active']:
                assert store.collection.replace_saved_album_snapshots({original['id']: []}, now=1793260310) == set()
                assert conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0] == unknown
            assert store.accounts.upsert_account(original['label'], original['credential_ref'],
                original['brand_account_id'], bool(original['is_master'])) == original['id']
            assert store.collection.replace_saved_album_snapshots({original['id']: []}, now=1793260320+index) == {original['id']}
            remaining = conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0]
            assert remaining == (unknown if index + 1 < len(cohort) else 0)
        scope = conn.execute("SELECT ref FROM playlist_scopes WHERE kind='shared' ORDER BY ref LIMIT 1").fetchone()[0]
        store.playlists.set_playlist_group(scope, 'Schema491 retained group')
        store.playlists.hide_playlist(scope)
        store.overlaps.ignore_overlap_playlist(scope, 1793260400)
        store.playlists.mark_generated_playlist(scope, 'Controlled populated provenance')
        assert store.playlists.is_quarantined_playlist(scope)
        store.playlists.adopt_generated_playlist(scope)
        assert not store.playlists.is_quarantined_playlist(scope)
        assert list(map(tuple, conn.execute('SELECT * FROM playlist_membership_refs ORDER BY id'))) == retained
        assert list(map(tuple, conn.execute('SELECT * FROM playlists ORDER BY id'))) == playlists
        store.conn.close()
        store = Store(db_path)
        store.init_schema()
        assert store.playlists.get_playlist_groups()[scope] == 'Schema491 retained group'
        assert scope in store.playlists.get_hidden_playlists()
        assert scope in store.overlaps.get_overlap_ignored()
        assert store.playlists.playlist_provenance(scope)['adopted'] == 1
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        report.update(stale_revision_rejected=True, interrupted_snapshot_rolled_back=True,
                      original_cohort_accounts_observed=len(cohort), new_account_joined_frozen_cohort=False,
                      retained_memberships_unchanged=len(retained), restart_preserved_scope_preferences=True,
                      foreign_key_violations=0)
    finally:
        if reader is not None:
            reader.close()
        store.conn.close()
        assert fingerprint(donor) == digest
    (output / 'scope-owner-continuations.json').write_text(json.dumps(report, indent=2) + '\n')
