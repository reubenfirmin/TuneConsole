"""Repair inherited private-list choices and persist saved-library concurrency state."""
import json

from .runner import MigrationError


SQL = (
    "CREATE TABLE album_library_revisions (identity_id INTEGER PRIMARY KEY REFERENCES identities(id),revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0))",
    "CREATE TABLE album_library_legacy_cohort (identity_id INTEGER PRIMARY KEY REFERENCES identities(id),observed_at REAL)",
)
# Recipes, pairs, merges and provenance describe relationships or ownership. An
# ambiguous legacy reference does not justify expanding those across accounts.
UNARY = ('playlist_group', 'hidden_playlists', 'overlap_ignored', 'cleanup_ignored')


def apply(conn, context):
    for sql in SQL:
        conn.execute(sql)
    # Include inactive identities: becoming unavailable does not resolve ownership.
    # An empty cohort deliberately cannot resolve unattributed legacy membership.
    has_legacy = conn.execute("SELECT 1 FROM identity_state_archive WHERE kind='album_legacy:saved_albums'").fetchone()
    if has_legacy or conn.execute('SELECT 1 FROM album_library_memberships WHERE identity_id IS NULL').fetchone():
        conn.execute('INSERT INTO album_library_legacy_cohort '
                     'SELECT i.id,s.observed_at FROM identities i '
                     'LEFT JOIN album_library_snapshots s ON s.identity_id=i.id')
        cohort = list(conn.execute('SELECT observed_at FROM album_library_legacy_cohort'))
        if not cohort or any(row[0] is None for row in cohort):
            # Older code could already have discarded this evidence after an
            # unobserved account was deactivated. Restore it from the v23 archive.
            conn.execute("INSERT OR IGNORE INTO album_library_memberships(album_id,source) "
                         "SELECT p.album_id,'legacy_combined' FROM identity_state_archive a "
                         "JOIN album_provider_ids p ON p.provider='youtube' AND p.provider_id=a.source_id "
                         "WHERE a.kind='album_legacy:saved_albums'")

    for table in UNARY:
        rows = conn.execute(f'SELECT p.* FROM {table} p JOIN playlist_scopes s '
                            "ON s.ref=p.ytm WHERE s.kind='legacy'").fetchall()
        for source in rows:
            row = dict(source)
            targets = conn.execute('SELECT target.ref FROM playlist_scopes target '
                'JOIN playlist_scopes original ON original.provider_id=target.provider_id '
                "WHERE original.ref=? AND target.kind='account' ORDER BY target.ref", (row['ytm'],)).fetchall()
            for target in targets:
                inherited = {**row, 'ytm': target[0]}
                columns = ','.join(inherited)
                inserted = conn.execute(f'INSERT OR IGNORE INTO {table}({columns}) '
                    f'VALUES ({",".join("?" for _ in inherited)})', tuple(inherited.values())).rowcount
                if inserted:
                    conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                        ('playlist_scope_inherited:'+table, json.dumps([row['ytm'],target[0],row.get('category')]),
                         json.dumps({'original':row,'inherited':inherited}),
                         'existing unary preference inherited once by an existing account reference'))
            if targets:
                # Original rows remain in the v24 archive; retire the ineffective
                # active token. Future accounts never inherit these choices.
                if table=='cleanup_ignored':
                    conn.execute('DELETE FROM cleanup_ignored WHERE ytm=? AND category=?',
                                 (row['ytm'],row['category']))
                else:
                    conn.execute(f'DELETE FROM {table} WHERE ytm=?', (row['ytm'],))


def validate(conn):
    for sql in SQL:
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or row[0]!=sql:
            raise MigrationError(f'Incompatible saved-library state: {name}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid saved-library account reference')
