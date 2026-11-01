"""Compare retained state through the attached immutable predecessor snapshot."""
import json
import sqlite3

from yt_playlist.core import legacy_evidence


def equal_sets(conn, old, new):
    assert conn.execute(f'{old} EXCEPT {new} LIMIT 1').fetchone() is None
    assert conn.execute(f'{new} EXCEPT {old} LIMIT 1').fetchone() is None


def retained_originals(conn):
    counts = {domain: 0 for domain in legacy_evidence.DOMAINS}
    for kind, key, payload in conn.execute('SELECT kind,source_id,payload FROM original.legacy_track_evidence'):
        assert legacy_evidence.original(conn, 'catalog', kind, key) == payload
        counts['catalog'] += 1
    for kind, key, payload, reason in conn.execute('SELECT kind,source_id,payload,reason FROM original.identity_state_archive'):
        domain = legacy_evidence.archive_domain(kind)
        assert legacy_evidence.original(conn, domain, kind, key) == payload
        assert conn.execute(f'SELECT reason FROM {legacy_evidence.DOMAINS[domain]} WHERE kind=? AND source_id=?',
                            (kind, key)).fetchone()[0] == reason
        counts[domain] += 1
    for key, payload, reason in conn.execute('SELECT identity_key,payload,reason FROM original.legacy_enrichment_attempts'):
        kind = 'legacy_enrichment_attempt'
        assert legacy_evidence.original(conn, 'metadata', kind, key) == payload
        assert conn.execute('SELECT reason FROM metadata_legacy_evidence WHERE kind=? AND source_id=?',
                            (kind, key)).fetchone()[0] == reason
        counts['metadata'] += 1
    if conn.execute("SELECT 1 FROM original.sqlite_schema WHERE name='stale_dismissed'").fetchone():
        for key, until in conn.execute('SELECT ytm,until FROM original.stale_dismissed'):
            assert legacy_evidence.original(conn, 'playlist', 'stale_dismissed', key) == json.dumps({'ytm': key, 'until': until})
            assert conn.execute("SELECT reason FROM playlist_legacy_evidence WHERE kind='stale_dismissed' AND source_id=?",
                                (key,)).fetchone()[0] == 'retained dismissal; original key semantics preserved'
            scopes = conn.execute("SELECT ref FROM original.playlist_scopes WHERE provider_id=? AND kind='shared'", (key,)).fetchall()
            expected_scope = scopes[0][0] if len(scopes) == 1 else None
            expected_status = 'resolved' if expected_scope else 'ambiguous_account' if key in ('LM', 'SE') else 'missing_scope'
            actual = conn.execute('SELECT playlist_scope_id,resolution_status FROM legacy_playlist_dismissals WHERE source_ref=?',
                                  (key,)).fetchone()
            assert tuple(actual) == (expected_scope, expected_status)
            counts['playlist'] += 1
        equal_sets(conn, 'SELECT ytm,until FROM original.stale_dismissed',
                   'SELECT source_ref,until FROM legacy_playlist_dismissals')
        assert not conn.execute("SELECT 1 FROM legacy_playlist_dismissals d WHERE "
            "(d.resolution_status='resolved')<>(d.playlist_scope_id IS NOT NULL)").fetchone()
    for domain, table in legacy_evidence.DOMAINS.items():
        assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == counts[domain]
    equal_sets(conn, 'SELECT * FROM original.legacy_track_refs', 'SELECT * FROM main.legacy_track_refs')
    return {'exact_originals_by_domain': counts,
            'legacy_url_mappings': conn.execute('SELECT count(*) FROM legacy_track_refs').fetchone()[0],
            'typed_artist_ambiguities': conn.execute('SELECT count(*) FROM artist_ambiguities').fetchone()[0]}


def scope_and_account_state(conn):
    equal_sets(conn,
        'SELECT s.ref,s.provider_id,s.identity_id,s.kind,g.name,h.ytm IS NOT NULL,o.ytm IS NOT NULL,'
        'o.created_at,p.origin,p.adopted,p.description FROM original.playlist_scopes s '
        'LEFT JOIN original.playlist_group g ON g.ytm=s.ref LEFT JOIN original.hidden_playlists h ON h.ytm=s.ref '
        'LEFT JOIN original.overlap_ignored o ON o.ytm=s.ref LEFT JOIN original.playlist_provenance p ON p.ytm=s.ref',
        'SELECT ref,provider_id,identity_id,kind,group_name,hidden,overlap_ignored,overlap_ignored_at,'
        'origin,adopted,provenance_description FROM main.playlist_scopes')
    equal_sets(conn,
        'SELECT i.id,COALESCE(r.revision,0),s.observed_at,c.identity_id IS NOT NULL,c.observed_at '
        'FROM original.accounts i LEFT JOIN original.album_library_revisions r ON r.identity_id=i.id '
        'LEFT JOIN original.album_library_snapshots s ON s.identity_id=i.id '
        'LEFT JOIN original.album_library_legacy_cohort c ON c.identity_id=i.id',
        'SELECT * FROM account_album_state')
    equal_sets(conn, 'SELECT identity_id,album_id,source,saved_at FROM original.album_library_memberships',
               'SELECT account_id,album_id,source,saved_at FROM saved_album_memberships')
    for table in ('playlist_membership_refs', 'playlists', 'identities', 'road_trip_recipes', 'road_trip_drafts'):
        equal_sets(conn, f'SELECT * FROM original.{table}', f'SELECT * FROM main.{table}')
    return {'scopes': conn.execute('SELECT count(*) FROM playlist_scopes').fetchone()[0],
            'retained_memberships': conn.execute('SELECT count(*) FROM playlist_membership_refs').fetchone()[0],
            'current_playlists': conn.execute('SELECT count(*) FROM playlists').fetchone()[0],
            'saved_memberships': conn.execute('SELECT count(*) FROM saved_album_memberships').fetchone()[0],
            'unknown_cohort_times': conn.execute('SELECT count(*) FROM account_album_state '
                'WHERE legacy_cohort_present=1 AND legacy_cohort_observed_at IS NULL').fetchone()[0]}


def account_boundaries(conn):
    before = list(map(tuple, conn.execute('SELECT * FROM identities ORDER BY id')))
    account = conn.execute('SELECT id FROM identities ORDER BY id LIMIT 1').fetchone()[0]
    rejected = 0
    for column, value in (('active', 7), ('active', -1), ('active', .5), ('is_master', 2), ('is_master', None)):
        try:
            with conn:
                conn.execute(f'UPDATE identities SET {column}=? WHERE id=?', (value, account))
        except sqlite3.IntegrityError:
            rejected += 1
        else:
            raise AssertionError(f'Invalid {column} accepted: {value!r}')
    master = conn.execute('SELECT id FROM identities WHERE active=1 AND is_master=1').fetchone()
    other = conn.execute('SELECT id FROM identities WHERE id<>? ORDER BY id LIMIT 1', (account,)).fetchone()
    if master:
        other = conn.execute('SELECT id FROM identities WHERE id<>? ORDER BY id LIMIT 1', (master[0],)).fetchone()
    if master and other:
        try:
            with conn:
                conn.execute('UPDATE identities SET active=1,is_master=1 WHERE id=?', (other[0],))
        except sqlite3.IntegrityError:
            rejected += 1
        else:
            raise AssertionError('Second active master accepted')
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('UPDATE identities SET is_master=0 WHERE id=?', (master[0],))
            conn.execute('UPDATE identities SET active=1,is_master=1 WHERE id=?', (other[0],))
            assert conn.execute('SELECT count(*) FROM identities WHERE active=1 AND is_master=1').fetchone()[0] == 1
        finally:
            conn.rollback()
    assert list(map(tuple, conn.execute('SELECT * FROM identities ORDER BY id'))) == before
    return {'invalid_account_writes_rejected': rejected, 'master_exchange_rolled_back': bool(master and other)}
