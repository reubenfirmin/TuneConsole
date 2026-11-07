"""Restart checks specifically for the completed controlled #491 populated donor.

Snapshots retain private rows in memory only. Returned verification summaries
contain counts, digests and controlled clocks, never the underlying library rows.
"""
import hashlib
import json


TABLES = {
    'account_album_state': 'account_id',
    'playlist_scopes': 'ref',
    'account_albums': 'account_id,album_id',
    'unattributed_album_memberships': 'album_id',
    'playlist_membership_refs': 'id',
    'playlists': 'id',
    'identities': 'id',
}
COHORT_CLOCKS = (1793260320, 1793260321, 1793260322)
GROUP = 'Schema491 retained group'
PROMOTED_DESCRIPTION = 'Promoted into your library from TuneConsole'


def _typed(value):
    if value is None:
        return ('null', None)
    if type(value) is int:
        return ('integer', value)
    if type(value) is float:
        return ('real', value.hex())
    if type(value) is str:
        return ('text', value)
    if type(value) is bytes:
        return ('blob', value.hex())
    raise AssertionError(f'Unexpected SQLite value type: {type(value).__name__}')


def _sentinels(conn):
    """Fail closed for an arbitrary library or an unfinished #491 continuation."""
    cohort = conn.execute(
        'SELECT legacy_cohort_present,snapshot_observed_at,legacy_cohort_observed_at '
        'FROM account_album_state WHERE legacy_cohort_observed_at IN (?,?,?) '
        'ORDER BY account_id', COHORT_CLOCKS).fetchall()
    assert len(cohort) == 3, 'Use the completed #491 populated continuation donor'
    assert tuple(row[2] for row in cohort) == COHORT_CLOCKS
    assert all(row[0] == 1 and row[1] == row[2] for row in cohort)
    assert conn.execute('SELECT count(*) FROM unattributed_album_memberships').fetchone()[0] == 0
    new_account = conn.execute(
        'SELECT s.legacy_cohort_present,s.legacy_cohort_observed_at FROM identities i '
        'LEFT JOIN account_album_state s ON s.account_id=i.id WHERE i.label=?',
        ('schema491-new-account',)).fetchall()
    assert len(new_account) == 1, 'The controlled #491 account is required'
    assert new_account[0][0] in (None, 0) and new_account[0][1] is None
    curation = conn.execute(
        'SELECT ref,kind,hidden,overlap_ignored,overlap_ignored_at,origin,adopted,'
        'provenance_description FROM playlist_scopes WHERE group_name=?', (GROUP,)).fetchall()
    assert len(curation) == 1, 'The controlled #491 curation is required'
    row = curation[0]
    # The journey adopts the generated playlist; adoption replaces its initial
    # "Controlled populated provenance" description with the promoted marker.
    assert tuple(row[1:]) == ('shared', 1, 1, 1793260400, 'tuneconsole', 1,
                             PROMOTED_DESCRIPTION)
    return row[0], tuple(item[2] for item in cohort)


def snapshot(conn):
    """Capture every column and typed value, ordered by each owner's full key."""
    _sentinels(conn)
    result = {}
    for table, ordering in TABLES.items():
        cursor = conn.execute(f'SELECT * FROM {table} ORDER BY {ordering}')
        result[table] = {
            'columns': tuple(column[0] for column in cursor.description),
            'rows': tuple(tuple(_typed(value) for value in row) for row in cursor),
        }
    return result


def verify(store, expected):
    """Verify unchanged owners and their public reads after ordinary startup."""
    assert set(expected) == set(TABLES), 'Supply the complete pre-startup snapshot'
    actual = snapshot(store.conn)
    for table in TABLES:
        assert actual[table] == expected[table], f'{table} changed across normal startup/reopen'
    scope, clocks = _sentinels(store.conn)
    assert store.playlists.get_playlist_groups()[scope] == GROUP
    assert scope in store.playlists.get_hidden_playlists()
    assert scope in store.overlaps.get_overlap_ignored()
    assert store.playlists.playlist_provenance(scope) == {
        'origin': 'tuneconsole', 'adopted': 1, 'description': PROMOTED_DESCRIPTION,
    }
    assert not store.playlists.is_quarantined_playlist(scope)
    assert snapshot(store.conn) == expected, 'Public curation reads changed durable state'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    tables = {}
    for table, value in actual.items():
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')
        tables[table] = {'rows': len(value['rows']), 'sha256': hashlib.sha256(encoded).hexdigest()}
    return {
        'tables': tables,
        'controlled_cohort_accounts': len(clocks),
        'controlled_cohort_observed_at': list(clocks),
        'unattributed_memberships': 0,
        'new_account_excluded_from_frozen_cohort': True,
        'curation_scope_sha256': hashlib.sha256(scope.encode('utf-8')).hexdigest(),
        'curation_ignore_at': 1793260400,
        'public_adopted_curation_verified': True,
        'foreign_key_violations': 0,
    }
