"""Durable playlist scopes preserve curation independently of account memberships."""
import json

from .runner import MigrationError


REPLACED = {'playlist_group','hidden_playlists','suppressed_overlaps','overlap_ignored',
            'overlap_kept','cleanup_ignored','ignored_merges','rec_recipes','cluster_canvas','playlist_provenance'}
TABLES = (
    "CREATE TABLE playlist_scopes (ref TEXT PRIMARY KEY NOT NULL,provider_id TEXT NOT NULL,identity_id INTEGER REFERENCES identities(id),kind TEXT NOT NULL CHECK(kind IN ('shared','account','legacy')),CHECK((kind='account' AND identity_id IS NOT NULL) OR (kind<>'account' AND identity_id IS NULL)),UNIQUE(provider_id,identity_id))",
    "CREATE UNIQUE INDEX ix_playlist_shared ON playlist_scopes(provider_id,kind) WHERE identity_id IS NULL",
    "CREATE TABLE playlist_group (ytm TEXT PRIMARY KEY NOT NULL REFERENCES playlist_scopes(ref),name TEXT NOT NULL)",
    "CREATE TABLE hidden_playlists (ytm TEXT PRIMARY KEY NOT NULL REFERENCES playlist_scopes(ref))",
    "CREATE TABLE suppressed_overlaps (a TEXT NOT NULL REFERENCES playlist_scopes(ref),b TEXT NOT NULL REFERENCES playlist_scopes(ref),created_at REAL,PRIMARY KEY(a,b))",
    "CREATE TABLE overlap_ignored (ytm TEXT PRIMARY KEY NOT NULL REFERENCES playlist_scopes(ref),created_at REAL)",
    "CREATE TABLE overlap_kept (a TEXT NOT NULL REFERENCES playlist_scopes(ref),b TEXT NOT NULL REFERENCES playlist_scopes(ref),created_at REAL,PRIMARY KEY(a,b))",
    "CREATE TABLE cleanup_ignored (ytm TEXT NOT NULL REFERENCES playlist_scopes(ref),category TEXT NOT NULL,created_at REAL,PRIMARY KEY(ytm,category))",
    "CREATE TABLE ignored_merges (signature TEXT PRIMARY KEY NOT NULL,members TEXT NOT NULL,created_at REAL)",
    "CREATE TABLE ignored_merge_members (signature TEXT NOT NULL REFERENCES ignored_merges(signature) ON DELETE CASCADE,playlist_ref TEXT NOT NULL REFERENCES playlist_scopes(ref),PRIMARY KEY(signature,playlist_ref))",
    "CREATE TABLE rec_recipes (playlist_ytm TEXT PRIMARY KEY NOT NULL REFERENCES playlist_scopes(ref),recipe TEXT NOT NULL,created_at REAL)",
    "CREATE TABLE cluster_canvas (playlist_ytm TEXT PRIMARY KEY NOT NULL REFERENCES playlist_scopes(ref),state TEXT NOT NULL,created_at REAL)",
    "CREATE TABLE playlist_provenance (ytm TEXT PRIMARY KEY NOT NULL REFERENCES playlist_scopes(ref),origin TEXT NOT NULL CHECK(origin IN ('tuneconsole','ordinary')),adopted INTEGER NOT NULL CHECK(adopted IN (0,1)),description TEXT)",
    "CREATE TABLE album_library_snapshots (identity_id INTEGER PRIMARY KEY REFERENCES identities(id),observed_at REAL NOT NULL)",
)


def apply(conn,context):
    original = {t:[dict(r) for r in conn.execute(f'SELECT rowid migration_rowid,* FROM {t}')] for t in REPLACED}
    for table,rows in original.items():
        for row in rows:
            conn.execute('INSERT INTO identity_state_archive VALUES (?,?,?,?)',
                         ('playlist_scope_legacy:'+table,str(row['migration_rowid']),json.dumps(row),'original unscoped curation'))
    conn.execute(TABLES[0])
    conn.execute(TABLES[1])
    owners = {}
    for row in conn.execute('SELECT identity_id,ytm_playlist_id FROM playlists'):
        owners.setdefault(row['ytm_playlist_id'],set()).add(row['identity_id'])

    def scope(remote,identity=None):
        if remote in {'LM','SE'}:
            if identity is None and len(owners.get(remote,set()))==1:
                identity = next(iter(owners[remote]))
            ref = f'@account:{identity}:{remote}' if identity is not None else f'@legacy:{remote}'
            kind = 'account' if identity is not None else 'legacy'
        else:
            ref,kind,identity = remote,'shared',None
        conn.execute('INSERT OR IGNORE INTO playlist_scopes VALUES (?,?,?,?)', (ref,remote,identity,kind))
        return ref

    conn.execute('ALTER TABLE playlists ADD COLUMN scope_ref TEXT REFERENCES playlist_scopes(ref)')
    for row in conn.execute('SELECT id,identity_id,ytm_playlist_id FROM playlists').fetchall():
        conn.execute('UPDATE playlists SET scope_ref=? WHERE id=?', (scope(row['ytm_playlist_id'],row['identity_id']),row['id']))
    conn.execute('CREATE INDEX ix_playlist_scope ON playlists(scope_ref,identity_id)')
    for table in REPLACED:
        conn.execute(f'DROP TABLE {table}')
    for sql in TABLES[2:]:
        conn.execute(sql)
    for table,rows in original.items():
        for original_row in rows:
            row = {k:v for k,v in original_row.items() if k!='migration_rowid'}
            if table=='ignored_merges':
                try:
                    raw = json.loads(row['members'])
                    if not isinstance(raw,list) or not all(isinstance(v,str) and v for v in raw):
                        continue
                    members = sorted({scope(v) for v in raw})
                except (ValueError,TypeError):
                    continue
                row.update(signature='|'.join(members),members=json.dumps(members))
            else:
                fields = ('a','b') if table in {'suppressed_overlaps','overlap_kept'} else ('playlist_ytm',) if table in {'rec_recipes','cluster_canvas'} else ('ytm',)
                if any(not row[field] for field in fields):
                    continue  # Invalid legacy rows remain in the archive, not invented references.
                for field in fields:
                    row[field] = scope(row[field])
                if len(fields)==2:
                    row['a'],row['b'] = sorted((row['a'],row['b']))
            columns = ','.join(row)
            conn.execute(f'INSERT OR IGNORE INTO {table}({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
            if table=='ignored_merges':
                conn.executemany('INSERT OR IGNORE INTO ignored_merge_members VALUES (?,?)', [(row['signature'],m) for m in members])


def validate(conn):
    from .v019_playlist_provenance import VIEW_SQL
    if conn.execute("SELECT sql FROM sqlite_master WHERE name='quarantined_playlists'").fetchone()[0]!=VIEW_SQL:
        raise MigrationError('Incompatible quarantine projection')
    for sql in TABLES:
        parts = sql.split()
        name = parts[3] if parts[1]=='UNIQUE' else parts[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if not row or row[0]!=sql:
            raise MigrationError(f'Incompatible account scope: {name}')
    columns = {r[1]:r[2] for r in conn.execute('PRAGMA table_info(playlists)')}
    if columns.get('scope_ref')!='TEXT' or not any(r[2]=='playlist_scopes' and r[3]=='scope_ref' for r in conn.execute('PRAGMA foreign_key_list(playlists)')):
        raise MigrationError('Missing playlist account reference')
    if conn.execute('SELECT 1 FROM playlists WHERE scope_ref IS NULL LIMIT 1').fetchone():
        raise MigrationError('Unscoped playlist membership')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid account scope relationship')
