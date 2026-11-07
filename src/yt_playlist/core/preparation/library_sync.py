"""Prepare retained sync reports and scalar stamps with independent progress."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from .. import entity_names, playlist_scopes, recommendation_outputs as files, sync_state
from ..schema_contract import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 32768
MAX_PARTS = 256
PART_CHARACTERS = 64 * 1024
HEAD = 'prepared_library_sync_generations'
PARTS = 'prepared_library_sync_original_parts'
CURSORS = 'prepared_library_sync_cursors'
CURRENT = 'prepared_library_sync_heads'
STAMPS = 'prepared_library_sync_stamps'
ORIGINAL_STAMPS = 'prepared_library_sync_stamp_originals'
STAMP_BYTES = 64 * 1024


def tables(source):
    if source != sync_state.KEY:
        raise ValueError('Unknown library sync document')
    return HEAD, PARTS, CURRENT


def _unused(*args):
    raise MigrationError('Library sync preparation requires its bounded file worker')


DEFINITION = Job('library_sync', 79, 93, {'settings': ('key',)}, (
    (HEAD, f'''CREATE TABLE {HEAD} (
        source TEXT NOT NULL CHECK(source='library_sync_state'), generation INTEGER NOT NULL CHECK(generation>=0),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {MAX_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 0 AND {MAX_PARTS}),
        item_count INTEGER NOT NULL CHECK(item_count BETWEEN 0 AND {MAX_ITEMS}),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','empty','sync')), phase TEXT,
        has_attempt_time INTEGER NOT NULL CHECK(has_attempt_time IN (0,1)), finished_at REAL,
        CHECK((source_kind='sync' AND phase IS NOT NULL AND phase IN ('complete','partial','failed','skipped'))
           OR (source_kind<>'sync' AND phase IS NULL AND has_attempt_time=0 AND finished_at IS NULL)),
        CHECK(finished_at IS NULL OR abs(finished_at)<=1.7976931348623157e308),
        PRIMARY KEY(source,generation)) WITHOUT ROWID'''),
    (PARTS, f'''CREATE TABLE {PARTS} (
        source TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_PARTS - 1}),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,generation,position), FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
    (CURSORS, f'''CREATE TABLE {CURSORS} (
        source TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_ITEMS - 1}),
        raw_account_id TEXT, account_id INTEGER, unresolved_account_id INTEGER,
        collection TEXT NOT NULL CHECK(collection IN ('account','playlist_listing','playlists','ratings','history',
            'saved_albums','saved_album_mirror','album_tracks','playlist')),
        raw_playlist_id TEXT, playlist_scope_id TEXT, unresolved_scope_reason TEXT,
        cursor TEXT, phase TEXT NOT NULL CHECK(phase IN ('complete','partial','failed','skipped')),
        updated_at REAL CHECK(updated_at IS NULL OR abs(updated_at)<=1.7976931348623157e308),
        last_success_at REAL CHECK(last_success_at IS NULL OR abs(last_success_at)<=1.7976931348623157e308),
        failures INTEGER CHECK(failures IS NULL OR failures BETWEEN 0 AND 16),
        retry_at REAL CHECK(retry_at IS NULL OR abs(retry_at)<=1.7976931348623157e308),
        CHECK((raw_account_id IS NOT NULL AND
            ((account_id IS NOT NULL AND account_id>0 AND unresolved_account_id IS NULL AND CAST(account_id AS TEXT)=raw_account_id)
             OR (account_id IS NULL AND unresolved_account_id IS NOT NULL AND unresolved_account_id>0
                 AND CAST(unresolved_account_id AS TEXT)=raw_account_id)))
           OR (raw_account_id IS NULL AND account_id IS NULL AND unresolved_account_id IS NULL
               AND collection IN ('saved_album_mirror','album_tracks'))),
        CHECK((collection='playlist' AND raw_account_id IS NOT NULL AND raw_playlist_id IS NOT NULL
               AND ((playlist_scope_id IS NOT NULL AND unresolved_scope_reason IS NULL)
                OR (playlist_scope_id IS NULL AND unresolved_scope_reason IS NOT NULL
                    AND unresolved_scope_reason IN ('missing_scope','account_mismatch','invalid_scope_reference'))))
           OR (collection<>'playlist' AND raw_playlist_id IS NULL AND playlist_scope_id IS NULL AND unresolved_scope_reason IS NULL)),
        PRIMARY KEY(source,generation,position),
        FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
    (CURRENT, f'''CREATE TABLE {CURRENT} (
        source TEXT PRIMARY KEY CHECK(source='library_sync_state'), generation INTEGER NOT NULL,
        FOREIGN KEY(source,generation) REFERENCES {HEAD}(source,generation)) WITHOUT ROWID'''),
), _unused, _unused, dependencies=('identities', 'playlist_scopes'),
    filters={'settings': "{prefix}key='library_sync_state'"}, projections={'settings':
        ('key', f"CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,{MAX_BYTES + 1}),X'') END AS value")})


def _finite(value):
    return value is None or type(value) in (int, float) and math.isfinite(value)


def _resources(result):
    accounts, global_resources = result.get('accounts', {}), result.get('resources', {})
    if not isinstance(accounts, dict) or not isinstance(global_resources, dict):
        raise ValueError('Sync progress requires recorded account and resource maps')
    for raw_account, account in accounts.items():
        if (not raw_account.isdecimal() or str(int(raw_account)) != raw_account or not 0 < int(raw_account) < 2**63
                or not isinstance(account, dict) or not isinstance(account.get('resources', {}), dict)):
            raise ValueError('Sync progress requires exact account IDs and resource maps')
        yield raw_account, 'account', None, account
        for name, item in account.get('resources', {}).items():
            if name not in sync_state.RESOURCES or not isinstance(item, dict) or not isinstance(item.get('items', {}), dict):
                raise ValueError('Unsupported sync resource; original retained')
            yield raw_account, name, None, item
            for provider, child in item.get('items', {}).items():
                if name != 'playlists' or not provider:
                    raise ValueError('Unsupported sync child progress; original retained')
                yield raw_account, 'playlist', provider, child
    for name, item in global_resources.items():
        if name not in {'saved_album_mirror', 'album_tracks'}:
            raise ValueError('Unsupported global sync progress; original retained')
        yield None, name, None, item


@dataclass(frozen=True)
class Plan:
    digest: str
    size: int
    part_count: int
    item_count: int
    kind: str
    phase: str | None
    has_attempt: bool
    finished_at: float | None
    units: tuple


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Library sync preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'library-sync'


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {PARTS} '
        'WHERE source=? AND generation=? ORDER BY position LIMIT ?',
        (row['source'], row['generation'], MAX_PARTS + 1)).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared library sync original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface='library_sync', chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared library sync differs from its original digest')
    return None if row['source_kind'] == 'null' else raw


def describe(source, original):
    raw = original.encode('utf-8') if original is not None else b''
    if len(raw) > MAX_BYTES:
        raise MigrationError('Library sync exceeds its preparation byte budget')
    text = raw.decode('utf-8')
    result = files.decode_json(text) if text else None
    if result is not None and not isinstance(result, dict):
        raise ValueError('Legacy library sync state must be an object or null')
    kind = 'null' if original is None else 'blank' if not text else 'empty' if not result else 'sync'
    if result and (result.get('status') not in sync_state.STATUSES or not _finite(result.get('last_attempt_at'))):
        raise ValueError('Unsupported library sync result; original retained')
    items = []
    for account, collection, provider, item in _resources(result or {}):
        if (len(items) >= MAX_ITEMS or not isinstance(item, dict) or item.get('status') not in sync_state.STATUSES
                or item.get('cursor') is not None and not isinstance(item['cursor'], str)
                or any(not _finite(item.get(field)) for field in ('last_attempt_at','last_success_at','retry_at'))
                or item.get('failures') is not None and (type(item['failures']) is not int or not 0 <= item['failures'] <= 16)):
            raise ValueError('Unsupported sync cursor; original retained')
        items.append(('cursor', len(items), (account, collection, provider, item)))
    return (kind, result['status'] if result else None, bool(result and 'last_attempt_at' in result),
            result.get('last_attempt_at') if result else None, items)


def prepare(conn, row):
    raw = bytes(row['value']) if row['value'] is not None else b''
    text = raw.decode('utf-8')
    kind, phase, has_attempt, finished_at, items = describe(row['key'], text if row['value'] is not None else None)
    durable = [('file', position, files.publish(directory(conn), files.encode('library_sync', str(position),
        text[start:start + PART_CHARACTERS]))) for position, start in enumerate(range(0, len(text), PART_CHARACTERS))]
    if len(durable) > MAX_PARTS:
        raise MigrationError('Library sync exceeds its original part budget')
    return Plan(hashlib.sha256(raw).hexdigest(), len(raw), len(durable), len(items), kind,
                phase, has_attempt, finished_at, tuple(durable + items))


def begin(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {HEAD} VALUES (?,?,?,?,?,?,?,?,?,?)',
        (row['key'], generation, plan.digest, plan.size, plan.part_count, plan.item_count,
         plan.kind, plan.phase, int(plan.has_attempt), plan.finished_at))


def reference(conn, value):
    """Observe one cursor's installed identity without creating owners."""
    raw_account, collection, provider, item = value
    account = int(raw_account) if raw_account is not None else None
    known = (conn.execute(f'SELECT id FROM {entity_names.owner(conn, "accounts").table} WHERE id=?', (account,)).fetchone()
             if account is not None else None)
    scope, reason = None, None
    if provider is not None:
        try:
            scope = playlist_scopes.resolve(conn, provider, account)
        except ValueError:
            reason = 'invalid_scope_reference'
        if reason is None:
            owner = entity_names.owner(conn, 'playlist_scopes')
            found = conn.execute(f'SELECT kind,{owner.column("account_id")} FROM {owner.table} '
                f'WHERE {owner.column("id")}=?', (scope,)).fetchone()
            reason = 'missing_scope' if not found else 'account_mismatch' if found[0] != 'shared' and found[1] != account else None
        if reason:
            scope = None
    return (raw_account, known[0] if known else None, account if not known else None,
            collection, provider, scope, reason, item.get('cursor'), item['status'], item.get('last_attempt_at'),
            item.get('last_success_at'), item.get('failures'), item.get('retry_at'))


def copy(conn, row, generation, position, unit):
    kind, rank, value = unit
    if kind == 'file':
        conn.execute(f'INSERT INTO {PARTS} VALUES (?,?,?,?,?,?)', (row['key'], generation, rank, *value))
    else:
        conn.execute(f'INSERT INTO {CURSORS} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (row['key'], generation, rank, *reference(conn, value)))


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {CURRENT} VALUES (?,?) ON CONFLICT(source) DO UPDATE SET generation=excluded.generation',
                 (row['key'], generation))


def delete(conn, owner, key):
    if json.loads(key)[0] == sync_state.KEY:
        conn.execute(f'DELETE FROM {CURRENT} WHERE source=?', (sync_state.KEY,))


def repair(conn, progress):
    from .document_publication import repair_parts
    repair_parts(conn, progress, source=sync_state.KEY, tables=tables(sync_state.KEY), prepare=prepare,
                 max_bytes=MAX_BYTES, max_parts=MAX_PARTS)


RESULT_JOB = OrderedJob(DEFINITION, prepare, begin, copy, complete, delete, repair_files=repair)


def stamp(conn, owner, key, row):
    if row['value'] is not None and len(row['value']) > STAMP_BYTES:
        raise ValueError('Sync stamp exceeds its preparation byte budget')
    raw = bytes(row['value']).decode() if row['value'] is not None else None
    kind = 'null' if raw is None else 'blank' if raw == '' else 'recorded'
    value = None if kind != 'recorded' else raw if row['key'] == 'last_sync_outcome' else float(raw)
    if value is not None and (value not in sync_state.STATUSES if row['key'] == 'last_sync_outcome' else not math.isfinite(value)):
        raise ValueError('Unsupported library sync stamp; original retained')
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
    # A previously published original is immutable. Avoid attempting another
    # insert even when reconciliation observes the exact same source again.
    conn.execute(f'INSERT INTO {ORIGINAL_STAMPS} SELECT ?,?,? WHERE NOT EXISTS('
                 f'SELECT 1 FROM {ORIGINAL_STAMPS} WHERE source=? AND digest=?)',
                 (row['key'], digest, raw, row['key'], digest))
    conn.execute(f'INSERT INTO {STAMPS} VALUES (?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET '
        'source_kind=excluded.source_kind,timestamp=excluded.timestamp,outcome=excluded.outcome,source_digest=excluded.source_digest',
        (row['key'], kind, None if row['key'] == 'last_sync_outcome' else value,
         value if row['key'] == 'last_sync_outcome' else None, digest))


def delete_stamp(conn, owner, key):
    source = json.loads(key)[0]
    if source in sync_state.STAMPS:
        conn.execute(f'DELETE FROM {STAMPS} WHERE source=?', (source,))


STAMP_JOB = Job('library_sync_stamps', 79, 93, {'settings': ('key',)}, (
    (ORIGINAL_STAMPS, f'''CREATE TABLE {ORIGINAL_STAMPS} (
        source TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64), raw_value TEXT,
        PRIMARY KEY(source,digest)) WITHOUT ROWID'''),
    (STAMPS, f'''CREATE TABLE {STAMPS} (
        source TEXT PRIMARY KEY CHECK(source IN ('last_sync_at','last_sync_attempt_at','last_sync_outcome')),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')), timestamp REAL, outcome TEXT, source_digest TEXT NOT NULL,
        CHECK(timestamp IS NULL OR abs(timestamp)<=1.7976931348623157e308),
        CHECK((source_kind<>'recorded' AND timestamp IS NULL AND outcome IS NULL)
           OR (source_kind='recorded' AND ((source='last_sync_outcome' AND timestamp IS NULL AND outcome IS NOT NULL
                  AND outcome IN ('complete','partial','failed','skipped'))
               OR (source<>'last_sync_outcome' AND timestamp IS NOT NULL AND outcome IS NULL)))),
        FOREIGN KEY(source,source_digest) REFERENCES {ORIGINAL_STAMPS}(source,digest)) WITHOUT ROWID'''),
), stamp, delete_stamp, filters={'settings': "{prefix}key IN ('last_sync_at','last_sync_attempt_at','last_sync_outcome')"},
    projections={'settings': ('key', f"CASE WHEN value IS NULL THEN NULL ELSE COALESCE(substr(CAST(value AS BLOB),1,{STAMP_BYTES + 1}),X'') END AS value")}, batch_limit=3)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('result', 'stamps'))
    args, remaining = parser.parse_known_args(argv)
    return run(RESULT_JOB if args.job == 'result' else STAMP_JOB, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
