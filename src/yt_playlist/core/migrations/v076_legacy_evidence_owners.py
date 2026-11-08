"""Relocate exact historical originals to their domain's retained files.

No age-based deletion and no live VACUUM. Legacy-to-current catalog mappings
remain queryable. An artist ambiguity guard is current state, never a JSON scan.
"""
import json

from yt_playlist.core import legacy_evidence as evidence
from yt_playlist.core.activity import check_cancellable_work
from .runner import MigrationError


def statements():
    for domain, table in evidence.DOMAINS.items():
        subjects = ''.join(f'{column} {"TEXT" if column == "playlist_scope_id" else "INTEGER"} REFERENCES {owner},'
                           for column, owner in evidence.SUBJECTS[domain].items())
        yield (f'CREATE TABLE {table} (kind TEXT NOT NULL CHECK(length(kind)>0),source_id TEXT NOT NULL,'
               'reason TEXT NOT NULL,filename TEXT NOT NULL CHECK(length(filename)>0),'
               "content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),"
               "byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),"
               "file_offset INTEGER NOT NULL CHECK(typeof(file_offset)='integer' AND file_offset>=0),"
               "format_version TEXT NOT NULL CHECK(format_version='legacy-evidence-1'),"
               "archived_at REAL NOT NULL DEFAULT (unixepoch('subsec')),"
               "resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','unresolved'))," + subjects +
               'PRIMARY KEY(kind,source_id)) WITHOUT ROWID')
        yield f'CREATE INDEX ix_{table}_file ON {table}(filename)'
        for column in evidence.SUBJECTS[domain]:
            yield f'CREATE INDEX ix_{table}_{column} ON {table}({column}) WHERE {column} IS NOT NULL'
        immutable = ','.join((*evidence.SUBJECTS[domain], 'resolution_status', 'archived_at'))
        yield (f'CREATE TRIGGER immutable_{table} BEFORE UPDATE OF kind,source_id,reason,content_digest,byte_size,'
               f'format_version,{immutable} ON {table} BEGIN SELECT RAISE(ABORT,\'Retained original is immutable\'); END')
        yield (f'CREATE TRIGGER retained_{table} BEFORE DELETE ON {table} '
               "BEGIN SELECT RAISE(ABORT,'Legacy evidence needs an explicit retention disposition'); END")
    yield ('CREATE TABLE artist_ambiguities (artist_id INTEGER PRIMARY KEY NOT NULL REFERENCES artists(id),'
           "reason TEXT NOT NULL CHECK(reason='contextual_release_credit')) WITHOUT ROWID")
    yield ('CREATE TABLE legacy_playlist_dismissals (source_ref TEXT PRIMARY KEY NOT NULL,'
           'playlist_scope_id TEXT REFERENCES playlist_scopes(ref),until REAL,'
           "resolution_status TEXT NOT NULL CHECK(resolution_status IN ('resolved','ambiguous_account','missing_scope')),"
           "CHECK((resolution_status='resolved')=(playlist_scope_id IS NOT NULL))) WITHOUT ROWID")
    yield ('CREATE INDEX ix_legacy_playlist_dismissal_scope ON legacy_playlist_dismissals(playlist_scope_id) '
           'WHERE playlist_scope_id IS NOT NULL')


def _batches(conn, table, fields):
    after = None
    while batch := conn.execute(f'SELECT rowid,{fields} FROM {table}'
            + (' WHERE rowid>?' if after is not None else '') + ' ORDER BY rowid LIMIT 128',
            (after,) if after is not None else ()).fetchall():
        check_cancellable_work()
        yield batch
        after = batch[-1][0]


def apply(conn, context):
    # Classify and bound every original before making any files. A new unknown
    # kind requires its actual owner's disposition rather than a catch-all.
    for kind, in conn.execute('SELECT DISTINCT kind FROM identity_state_archive'):
        try:
            evidence.archive_domain(kind)
        except ValueError as exc:
            raise MigrationError(str(exc)) from exc
    for table in ('legacy_track_evidence', 'identity_state_archive', 'legacy_enrichment_attempts'):
        for payload, in conn.execute(f'SELECT payload FROM {table}'):
            evidence._encode('catalog', payload)
    for sql in statements():
        conn.execute(sql)
    for batch in _batches(conn, 'legacy_track_evidence', 'kind,source_id,payload'):
        evidence.retain_many(conn, 'catalog', [(kind, key, payload, 'retained original catalog capture')
                                             for _, kind, key, payload in batch])
    for batch in _batches(conn, 'identity_state_archive', 'kind,source_id,payload,reason'):
        for domain in evidence.DOMAINS:
            rows = [(kind, key, payload, reason) for _, kind, key, payload, reason in batch
                    if evidence.archive_domain(kind) == domain]
            if rows:
                evidence.retain_many(conn, domain, rows)
        for _, kind, key, _, _ in batch:
            if kind == 'ambiguous_album_artist':
                row = conn.execute('SELECT id FROM artists WHERE CAST(id AS TEXT)=?', (key,)).fetchone()
                if row:
                    evidence.mark_artist_ambiguity(conn, row[0])
    for batch in _batches(conn, 'legacy_enrichment_attempts', 'identity_key,payload,reason'):
        evidence.retain_many(conn, 'metadata', [('legacy_enrichment_attempt', key, payload, reason)
                                              for _, key, payload, reason in batch])
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='stale_dismissed'").fetchone():
        for source, until in conn.execute('SELECT ytm,until FROM stale_dismissed'):
            scopes = conn.execute("SELECT ref FROM playlist_scopes WHERE provider_id=? AND kind='shared'",
                                  (source,)).fetchall()
            scope = scopes[0][0] if len(scopes) == 1 else None
            status = 'resolved' if scope else 'ambiguous_account' if source in ('LM', 'SE') else 'missing_scope'
            conn.execute('INSERT INTO legacy_playlist_dismissals VALUES (?,?,?,?)', (source, scope, until, status))
            evidence.retain_many(conn, 'playlist', [('stale_dismissed', source,
                json.dumps({'ytm': source, 'until': until}), 'retained dismissal; original key semantics preserved')])
        conn.execute('DROP TABLE stale_dismissed')
    for table in ('legacy_track_evidence', 'identity_state_archive', 'legacy_enrichment_attempts'):
        conn.execute('DROP TABLE ' + table)


def validate(conn, *, foreign_keys=True):
    from .current import _tokens
    from ..schema_inspection import objects
    definitions = {name: sql for name, (_kind, sql) in objects(conn).items()}
    for sql in statements():
        name = sql.split()[2]
        actual = definitions.get(name)
        if actual is None or _tokens(actual) != _tokens(sql):
            raise MigrationError(f'Incompatible retained evidence owner: {name}')
    if foreign_keys and conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid retained evidence reference')
