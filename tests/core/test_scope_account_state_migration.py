"""Scope presence and account observation fences survive state consolidation.

These are controlled predecessor/draft-migration checks. They do not replace
normal-startup or populated-library acceptance of the eventual registered tail.
"""
import sqlite3

import pytest

from yt_playlist.core import album_memberships as membership
from yt_playlist.core import entity_names
from yt_playlist.core import playlist_scope_state as scopes
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations import v077_scope_account_state as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'scope-state.db')
    _pristine_installed_schema(69).backup(store.conn)
    store._bind_repositories()
    with store.conn:
        store.conn.executemany('INSERT INTO identities(id,label,credential_ref,is_master,active) '
            "VALUES (?,?, 'bridge',?,?)", [(1, 'Main', 1, 1), (2, 'Retired', 0, 0), (3, 'Later', 0, 1)])
        store.conn.executemany('INSERT INTO albums(id,title,metadata_rank) VALUES (?,?,2)',
                               [(1, 'Unattributed'), (2, 'Owned'), (3, 'Equivalent')])
    yield store
    store.conn.close()


def apply(store):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(4))
    migration.validate(store.conn)


def state(store, account):
    return store.conn.execute('SELECT * FROM account_album_state WHERE account_id=?', (account,)).fetchone()


def historical_playlist(store, owner, remote, title):
    """Capture v69 membership through genuine predecessor columns and triggers."""
    scoped = remote in ('LM', 'SE')
    scope = f'@account:{owner}:{remote}' if scoped else remote
    with store.conn:
        store.conn.execute('INSERT OR IGNORE INTO playlist_scopes VALUES (?,?,?,?)',
                           (scope, remote, owner if scoped else None, 'account' if scoped else 'shared'))
        membership_id = store.conn.execute('INSERT INTO playlist_membership_refs(scope_ref,identity_id) '
                                           'VALUES (?,?)', (scope, owner)).lastrowid
        store.conn.execute('INSERT INTO playlists(id,identity_id,ytm_playlist_id,title,track_count,'
            'content_hash,first_seen,last_seen,last_changed,scope_ref) VALUES (?,?,?,?,0,\'\',1,1,1,?)',
            (membership_id, owner, remote, title, scope))
    return membership_id


def current_reads(store):
    """Use explicit read projections only after the frozen migration is checked.

    Physical v69/v77 owners and applied history remain unchanged. Private
    readers install the same projections when repository consumers open them.
    """
    store._schema_compatibility = True
    entity_names.predecessor_reads(store.conn)


def test_scope_defaults_explicit_presence_and_unknown_ignore_time(predecessor):
    store = predecessor
    with store.conn:
        store.conn.executemany('INSERT INTO playlist_scopes VALUES (?,?,NULL,\'shared\')',
                               [(key, key) for key in ('absent', 'ordinary', 'generated', 'empty-group')])
        store.conn.execute("INSERT INTO playlist_provenance VALUES ('ordinary','ordinary',1,NULL)")
        store.conn.execute("INSERT INTO playlist_provenance VALUES ('generated','tuneconsole',0,'original')")
        store.conn.execute("INSERT INTO overlap_ignored VALUES ('generated',NULL)")
        store.conn.execute("INSERT INTO hidden_playlists VALUES ('ordinary')")
        store.conn.execute("INSERT INTO playlist_group VALUES ('empty-group','')")
    counts = tuple(store.conn.execute('SELECT evidence_changes FROM rec_preparation').fetchone())
    changes = list(map(tuple, store.conn.execute('SELECT * FROM rec_change_queue ORDER BY kind,item')))
    apply(store)
    rows = {row['ref']: dict(row) for row in store.conn.execute('SELECT * FROM playlist_scopes')}
    assert rows['absent']['origin'] is None and rows['absent']['adopted'] is None
    assert rows['ordinary']['origin'] == 'ordinary' and rows['ordinary']['adopted'] == 1
    assert rows['ordinary']['hidden'] == 1 and rows['absent']['hidden'] == 0
    assert rows['generated']['overlap_ignored'] == 1 and rows['generated']['overlap_ignored_at'] is None
    assert rows['generated']['provenance_description'] == 'original'
    assert rows['empty-group']['group_name'] == '' and rows['absent']['group_name'] is None
    assert [row[0] for row in store.conn.execute('SELECT ytm FROM quarantined_playlists')] == ['generated']
    assert tuple(store.conn.execute('SELECT evidence_changes FROM rec_preparation').fetchone()) == counts
    assert list(map(tuple, store.conn.execute('SELECT * FROM rec_change_queue ORDER BY kind,item'))) == changes
    for name in ('playlist_group', 'hidden_playlists', 'overlap_ignored', 'playlist_provenance'):
        assert not store.conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (name,)).fetchone()


def test_membership_lifetimes_scope_kinds_and_old_ids_are_not_collapsed(predecessor):
    store = predecessor
    old = historical_playlist(store, 1, 'shared', 'First')
    with store.conn:
        store.conn.execute('DELETE FROM playlists WHERE id=?', (old,))
    current = historical_playlist(store, 1, 'shared', 'Returned')
    historical_playlist(store, 3, 'shared', 'Other account')
    historical_playlist(store, 1, 'LM', 'Liked one')
    historical_playlist(store, 3, 'LM', 'Liked three')
    with store.conn:
        store.conn.execute('INSERT INTO playlist_membership_refs(scope_ref,identity_id) VALUES (NULL,NULL)')
        store.conn.execute('INSERT INTO playlist_membership_refs(scope_ref,identity_id) VALUES (NULL,NULL)')
        store.conn.execute("INSERT INTO playlist_scopes VALUES ('@legacy:LM','LM',NULL,'legacy')")
    before = list(map(tuple, store.conn.execute('SELECT * FROM playlist_membership_refs ORDER BY id')))
    scopes = list(map(tuple, store.conn.execute('SELECT * FROM playlist_scopes ORDER BY ref')))
    apply(store)
    assert current != old
    assert list(map(tuple, store.conn.execute('SELECT * FROM playlist_membership_refs ORDER BY id'))) == before
    assert list(map(tuple, store.conn.execute('SELECT ref,provider_id,identity_id,kind FROM playlist_scopes ORDER BY ref'))) == scopes
    assert not store.conn.execute('SELECT 1 FROM playlists WHERE id=?', (old,)).fetchone()
    assert store.conn.execute('SELECT 1 FROM playlists WHERE id=?', (current,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute('UPDATE playlist_membership_refs SET identity_id=3 WHERE id=?', (current,))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute('DELETE FROM playlist_membership_refs WHERE id=?', (current,))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO playlist_scopes(ref,provider_id,kind) VALUES ('duplicate','shared','shared')")


def test_checkpoint_union_keeps_inactive_unknown_observations_and_all_memberships(predecessor):
    store = predecessor
    with store.conn:
        store.conn.execute('INSERT INTO album_library_revisions VALUES (1,154)')
        store.conn.execute('INSERT INTO album_library_snapshots VALUES (1,10)')
        store.conn.executemany('INSERT INTO album_library_legacy_cohort VALUES (?,?)', [(1, 10), (2, None)])
        store.conn.executemany('INSERT INTO album_library_memberships(album_id,identity_id,source,saved_at) VALUES (?,?,?,?)',
            [(1, None, 'legacy_combined', None), (2, 1, 'youtube', 8), (2, 3, 'youtube', None)])
    apply(store)
    assert tuple(state(store, 1)) == (1, 154, 10, 1, 10)
    assert tuple(state(store, 2)) == (2, 0, None, 1, None)
    assert tuple(state(store, 3)) == (3, 0, None, 0, None)
    assert list(map(tuple, store.conn.execute('SELECT * FROM saved_album_memberships '
                                             'ORDER BY account_id,album_id'))) == [
        (None, 1, 'legacy_combined', None), (1, 2, 'youtube', 8), (3, 2, 'youtube', None)]
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        membership.observe_snapshot(store.conn, 1, 20)
        membership.retire_observed_legacy(store.conn)
    assert store.conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0] == 1
    # The deactivated member remains an obligation; a newly added account's
    # successful snapshot cannot satisfy it.
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        membership.observe_snapshot(store.conn, 3, 21)
        membership.retire_observed_legacy(store.conn)
    assert state(store, 3)['legacy_cohort_present'] == 0
    assert store.conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0] == 1
    with store.conn:
        store.conn.execute('UPDATE identities SET active=1 WHERE id=2')
        membership.observe_snapshot(store.conn, 2, 22)
        membership.retire_observed_legacy(store.conn)
    assert store.conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0] == 0
    assert store.conn.execute('SELECT count(*) FROM account_albums').fetchone()[0] == 2
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_snapshot_is_one_checkpoint_write_and_rolls_back_with_membership(predecessor):
    store = predecessor
    apply(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        membership.save(store.conn, 2, 1)
        membership.observe_snapshot(store.conn, 1, 10)
    captured = []
    store.conn.set_trace_callback(captured.append)
    try:
        with pytest.raises(RuntimeError, match='interrupted'):
            with store.conn:
                store.conn.execute('DELETE FROM account_albums WHERE account_id=1')
                membership.observe_snapshot(store.conn, 1, 20)
                raise RuntimeError('interrupted publication')
    finally:
        store.conn.set_trace_callback(None)
    assert tuple(state(store, 1)) == (1, 1, 10, 0, None)
    assert store.conn.execute('SELECT count(*) FROM account_albums').fetchone()[0] == 1
    writes = [sql for sql in captured if sql.startswith('INSERT INTO account_album_state')]
    assert len(writes) == 1


def test_legacy_batch_resets_original_cohort_without_adding_later_accounts(predecessor):
    store = predecessor
    apply(store)
    current_reads(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        membership.start_legacy_cohort(store.conn)
        membership.save(store.conn, 1)
        membership.observe_snapshot(store.conn, 1, 10)
        store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master,active) VALUES (4,'New','bridge',0,1)")
        membership.start_legacy_cohort(store.conn)
        membership.save(store.conn, 2)
    assert [row[0] for row in store.conn.execute('SELECT account_id FROM account_album_state '
                                                'WHERE legacy_cohort_present=1 ORDER BY account_id')] == [1, 2, 3]
    assert state(store, 1)['legacy_cohort_observed_at'] is None
    assert state(store, 4) is None
    assert not store.conn.execute('SELECT count(*) FROM account_albums').fetchone()[0]


def test_empty_original_cohort_does_not_resolve_unknown_membership_vacuously(predecessor):
    store = predecessor
    apply(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        membership.save(store.conn, 1)
        membership.observe_snapshot(store.conn, 1, 10)
        membership.retire_observed_legacy(store.conn)
    assert store.conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0] == 1


def test_release_merge_keeps_known_and_unattributed_sets_and_earliest_known_time(predecessor):
    store = predecessor
    apply(store)
    current_reads(store)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        membership.save(store.conn, 2, 1, saved_at=20)
        membership.save(store.conn, 3, 1, saved_at=10)
        membership.save(store.conn, 3, 3)
        membership.save(store.conn, 2, saved_at=8)
        membership.save(store.conn, 3)
        before = membership.revision(store.conn, 1)
        membership.merge(store.conn, 2, 3)
    assert list(map(tuple, store.conn.execute('SELECT * FROM saved_album_memberships '
                                             'ORDER BY account_id,album_id'))) == [
        (None, 2, 'youtube', 8), (1, 2, 'youtube', 10), (3, 2, 'youtube', None)]
    assert membership.revision(store.conn, 1) == before + 1


@pytest.mark.parametrize('table,assignment', [
    ('playlist_scopes', 'hidden=2'), ('playlist_scopes', 'overlap_ignored=0,overlap_ignored_at=20'),
    ('playlist_scopes', "origin='ordinary',adopted=NULL"), ('playlist_scopes', 'origin=NULL,adopted=1'),
    ('playlist_scopes', "origin=NULL,adopted=NULL,provenance_description='orphan'"),
    ('account_album_state', 'revision=1.5'), ('account_album_state', 'legacy_cohort_present=2'),
    ('account_album_state', 'legacy_cohort_present=0,legacy_cohort_observed_at=20'),
    ('account_album_state', "snapshot_observed_at='unknown'"),
])
def test_direct_sql_enforces_presence_type_and_owner_contracts(predecessor, table, assignment):
    store = predecessor
    with store.conn:
        store.conn.execute("INSERT INTO playlist_scopes VALUES ('scope','scope',NULL,'shared')")
    apply(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(f'UPDATE {table} SET {assignment}')


def test_nonfinite_legacy_clock_aborts_before_structural_changes(predecessor):
    store = predecessor
    with store.conn:
        store.conn.execute("INSERT INTO playlist_scopes VALUES ('scope','scope',NULL,'shared')")
        store.conn.execute("INSERT INTO overlap_ignored VALUES ('scope',?)", (float('inf'),))
    with pytest.raises(MigrationError, match='overlap_ignored created_at'):
        apply(store)
    assert 'group_name' not in {row[1] for row in store.conn.execute('PRAGMA table_info(playlist_scopes)')}
    assert store.conn.execute('SELECT count(*) FROM overlap_ignored').fetchone()[0] == 1


def test_external_reference_to_discarded_surrogate_is_diagnosed(predecessor):
    store = predecessor
    with store.conn:
        store.conn.execute('CREATE TABLE custom_membership_links(id INTEGER PRIMARY KEY, '
                           'membership_id INTEGER REFERENCES album_library_memberships(id))')
    with pytest.raises(MigrationError, match='custom_membership_links'):
        apply(store)
    assert 'group_name' not in {row[1] for row in store.conn.execute('PRAGMA table_info(playlist_scopes)')}


def test_curation_replay_is_stable_and_scope_resolution_remains_account_specific(predecessor):
    store = predecessor
    for account in (1, 3):
        historical_playlist(store, account, 'LM', 'Liked')
    apply(store)
    first, second = '@account:1:LM', '@account:3:LM'
    assert scopes.available(store.conn)
    assert scopes.provenance(store.conn, first) == {'origin': 'ordinary', 'adopted': 1, 'description': None}
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        scopes.set_provenance(store.conn, first, origin='tuneconsole', adopted=False, description='generated')
        scopes.set_group(store.conn, first, 'A')
        scopes.set_hidden(store.conn, first, True)
        scopes.ignore_overlap(store.conn, first, None)
    before = store.conn.total_changes
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        scopes.set_provenance(store.conn, first, origin='tuneconsole', adopted=False, description='generated')
        scopes.set_group(store.conn, first, 'A')
        scopes.set_hidden(store.conn, first, 1)
        scopes.ignore_overlap(store.conn, first, 20)
    assert store.conn.total_changes == before
    assert scopes.groups(store.conn) == {first: 'A'}
    assert scopes.hidden(store.conn) == {first}
    assert scopes.overlap_ignored(store.conn) == {first}
    assert store.conn.execute('SELECT overlap_ignored_at FROM playlist_scopes WHERE ref=?', (first,)).fetchone()[0] is None
    assert scopes.provenance(store.conn, second)['origin'] == 'ordinary'
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        scopes.adopt(store.conn, first, 'promoted')
        scopes.unignore_overlap(store.conn, first)
        scopes.set_hidden(store.conn, first, False)
        scopes.set_group(store.conn, first, None)
    assert scopes.provenance(store.conn, first)['adopted'] == 1
    assert not scopes.groups(store.conn) and not scopes.hidden(store.conn) and not scopes.overlap_ignored(store.conn)
    assert not store.conn.execute('SELECT * FROM quarantined_playlists').fetchall()


@pytest.mark.parametrize('bad', ['true', 2, -1, 0.5, None])
def test_curation_flag_validation_precedes_any_write(predecessor, bad):
    store = predecessor
    with store.conn:
        store.conn.execute("INSERT INTO playlist_scopes VALUES ('scope','scope',NULL,'shared')")
    apply(store)
    before = store.conn.total_changes
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError):
            scopes.set_provenance(store.conn, 'scope', origin='ordinary', adopted=bad)
        with pytest.raises(ValueError):
            scopes.set_hidden(store.conn, 'scope', bad)
    assert store.conn.total_changes == before


def album(browse):
    return {'browse': browse, 'title': browse, 'artist': 'Collected Artist'}


def test_collection_workflow_keeps_frozen_cohort_and_rejects_stale_snapshots(predecessor):
    store = predecessor
    apply(store)
    current_reads(store)
    store.collection.add_saved_album(album('unattributed'))
    before = store.collection.saved_album_revision(1)
    store.collection.add_saved_album(album('owned'), account_id=1)
    assert store.collection.replace_saved_album_snapshots({1: []}, expected_revisions={1: before}, now=10) == set()
    assert store.collection.saved_album_ids() == {'unattributed', 'owned'}
    assert store.collection.replace_saved_album_snapshots({1: [album('owned')], 2: [], 3: []}, now=11) == {1, 3}
    assert store.collection.saved_album_ids() == {'unattributed', 'owned'}
    owners = store.collection.saved_album_owners()
    assert owners['unattributed'] == [{'account_id': None, 'label': None, 'active': False}]
    assert owners['owned'] == [{'account_id': 1, 'label': 'Main', 'active': True}]
    assert [row['browse'] for row in store.collection.get_saved_albums(account_id=1)] == ['owned']
    assert store.collection.get_saved_albums(account_id=3) == []
    with store.conn:
        store.conn.execute('UPDATE identities SET active=1 WHERE id=2')
    assert store.collection.replace_saved_album_snapshots({2: []}, now=12) == {2}
    assert store.collection.saved_album_ids() == {'owned'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_failed_collection_publication_cannot_advance_checkpoint_or_erase_membership(predecessor):
    store = predecessor
    apply(store)
    current_reads(store)
    store.collection.add_saved_album(album('owned'), account_id=1)
    revision = store.collection.saved_album_revision(1)
    with pytest.raises(KeyError):
        store.collection.replace_saved_album_snapshots({1: [{'title': 'No provider ID'}]},
                                                       expected_revisions={1: revision}, now=20)
    assert store.collection.saved_album_revision(1) == revision
    assert state(store, 1)['snapshot_observed_at'] is None
    assert store.collection.saved_album_ids() == {'owned'}
    assert store.collection.replace_saved_album_snapshots({1: []}, expected_revisions={1: revision}, now=21) == {1}
    assert state(store, 1)['snapshot_observed_at'] == 21
    assert store.collection.replace_saved_album_snapshots({1: [album('stale')]},
                                                        expected_revisions={1: revision}, now=22) == set()
    assert store.collection.saved_album_ids() == set()


def test_saved_membership_projections_share_the_consolidated_owner(predecessor):
    store = predecessor
    apply(store)
    current_reads(store)
    store.collection.add_saved_album(album('one'), account_id=1)
    store.collection.add_saved_album(album('two'))
    ids = store.collection.canonical_album_ids(['one', 'two'])
    assert store.rec_query.owned_albums() == set(ids.values())
    assert store.collection.saved_album_ids(browse_ids=['one', 'two', 'absent']) == {'one', 'two'}
    assert {row['album'] for row in store.collection.artist_albums('Collected Artist')} == {'one', 'two'}
    assert len(store.collection.collection_artists()) == 1
    assert store.discovery.interested_artists()[0]['score'] == 6
    result = store.search.omni_search('Collected Artist')
    assert any(section['kind'] == 'albums_by' and len(section['rows']) == 2 for section in result['sections'])
    store.collection.remove_saved_album('one', account_id=1)
    assert store.collection.saved_album_ids() == {'two'}
    # Replacing the unattributed set never erases a known owner's membership.
    store.collection.add_saved_album(album('one'), account_id=1)
    store.collection.replace_saved_albums([album('replacement')])
    assert store.collection.saved_album_ids() == {'one', 'replacement'}


def test_public_scope_mutations_resolve_once_and_survive_membership_recreation(predecessor):
    store = predecessor
    for account in (1, 3):
        historical_playlist(store, account, 'LM', 'Liked')
    apply(store)
    current_reads(store)
    store.playlists.set_playlist_provenance('LM', account_id=1, origin='tuneconsole', adopted=False,
                                           description='Recorded provenance')
    store.playlists.set_playlist_group('LM', ' Group ', account_id=1)
    store.playlists.hide_playlist('LM', account_id=1)
    store.overlaps.ignore_overlap_playlist('@account:1:LM', 10)
    store.overlaps.ignore_overlap_playlist('@account:1:LM', 20)
    assert store.playlists.playlist_provenance('LM', account_id=3)['origin'] == 'ordinary'
    assert store.playlists.get_playlist_groups() == {'@account:1:LM': 'Group'}
    assert store.playlists.get_hidden_playlists() == {'@account:1:LM'}
    assert store.overlaps.get_overlap_ignored() == {'@account:1:LM'}
    assert store.conn.execute("SELECT overlap_ignored_at FROM main.playlist_scopes WHERE ref='@account:1:LM'").fetchone()[0] == 10
    prior = store.playlists.upsert_playlist(1, 'LM', 'Liked', 0, '', 2)
    store.playlists.remove_playlist(prior)
    current = store.playlists.upsert_playlist(1, 'LM', 'Liked again', 0, '', 3)
    assert current != prior
    assert store.playlists.playlist_provenance('LM', account_id=1)['description'] == 'Recorded provenance'
    assert store.playlists.get_hidden_playlists() == {'@account:1:LM'}
    store.playlists.unhide_playlist('LM', account_id=1)
    store.playlists.set_playlist_group('LM', '', account_id=1)
    store.overlaps.unignore_overlap_playlist('@account:1:LM')
    assert not store.playlists.get_hidden_playlists()
    assert not store.playlists.get_playlist_groups()
    assert not store.overlaps.get_overlap_ignored()


def test_listening_format_classification_survives_scope_provenance_consolidation(predecessor):
    from yt_playlist.rec import listening_analytics
    store = predecessor
    store.tracks.upsert_track('format-video', 'Format song', 'Artist', None, None)
    key = store.tracks.identity_key_for_video('format-video')
    historical_playlist(store, 1, 'generated-list', 'Mix')
    current_reads(store)
    store.playlists.set_playlist_provenance('generated-list', origin='tuneconsole', adopted=True)
    store.history.record_play_event(1, key, 'format-video', 10, playlist_ytm_id='generated-list')
    with store.read_view() as reader:
        before = listening_analytics.calculate(reader, 20)
    assert before['subjects'][key]['formats'] == ['mixes']
    entity_names.clear_predecessor_reads(store.conn)
    apply(store)
    current_reads(store)
    with store.read_view() as reader:
        after = listening_analytics.calculate(reader, 20)
    assert after == before
    # This predecessor/draft fixture deliberately rebinds without normal init;
    # it verifies the reader after reopening, not registered-tail acceptance.
    reopened = Store(store._db_path)
    try:
        reopened._schema_compatibility = True
        reopened._bind_repositories()
        with reopened.read_view() as reader:
            assert listening_analytics.calculate(reader, 20) == before
    finally:
        reopened.conn.close()
