"""Stage exact playback attribution and pending album queues independently."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from .. import entity_names, playlist_scopes, recommendation_outputs as files
from ..migrations.runner import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 16384
MAX_PARTS = 256
PART_CHARACTERS = 64 * 1024
BINDINGS_KEY = 'listening_playback_bindings'
PENDING_KEY = 'listening_album_pending'
BINDINGS = 'prepared_playback_bindings'
INTENTS = 'prepared_album_playback_intents'
ITEMS = 'prepared_album_playback_items'
PREFIXES = {BINDINGS_KEY: 'prepared_playback_binding', PENDING_KEY: 'prepared_album_playback'}


def tables(source):
    prefix = PREFIXES[source]
    return prefix + '_generations', prefix + '_original_parts', prefix + '_heads'


def definitions(source):
    head, parts, current = tables(source)
    common = (
        (head, f'''CREATE TABLE {head} (
            source TEXT NOT NULL CHECK(source={source!r}), generation INTEGER NOT NULL CHECK(generation>=0),
            raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {MAX_BYTES}),
            part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND {MAX_PARTS}),
            item_count INTEGER NOT NULL CHECK(item_count BETWEEN 0 AND {MAX_ITEMS}),
            document_kind TEXT NOT NULL CHECK(document_kind IN ('bindings','pending','cleared')),
            PRIMARY KEY(source,generation)) WITHOUT ROWID'''),
        (parts, f'''CREATE TABLE {parts} (
            source TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_PARTS - 1}),
            filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
            byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
            PRIMARY KEY(source,generation,position), FOREIGN KEY(source,generation) REFERENCES {head}(source,generation)) WITHOUT ROWID'''),
        (current, f'''CREATE TABLE {current} (
            source TEXT PRIMARY KEY, generation INTEGER NOT NULL,
            FOREIGN KEY(source,generation) REFERENCES {head}(source,generation)) WITHOUT ROWID'''),
    )
    # Catalog/account/scope identities are observed, never created or pinned by
    # staging. Their edits invalidate the proof through dependency capture.
    account_columns = '''raw_account_id TEXT, account_kind TEXT NOT NULL CHECK(account_kind IN ('recorded','missing','null')),
            account_id INTEGER, unresolved_account_id INTEGER'''
    account_check = '''CHECK((account_kind='recorded' AND raw_account_id IS NOT NULL AND
                    ((account_id IS NOT NULL AND account_id>0 AND unresolved_account_id IS NULL AND CAST(account_id AS TEXT)=raw_account_id)
                     OR (account_id IS NULL AND unresolved_account_id IS NOT NULL AND unresolved_account_id>0
                         AND CAST(unresolved_account_id AS TEXT)=raw_account_id)))
               OR (account_kind IN ('missing','null') AND raw_account_id IS NULL AND account_id IS NULL AND unresolved_account_id IS NULL))'''
    scope_columns = '''raw_playlist_id TEXT NOT NULL CHECK(length(raw_playlist_id)>0), playlist_scope_id TEXT,
            unresolved_scope_reason TEXT'''
    scope_check = '''CHECK((playlist_scope_id IS NOT NULL AND unresolved_scope_reason IS NULL)
               OR (playlist_scope_id IS NULL AND unresolved_scope_reason IS NOT NULL AND
                   unresolved_scope_reason IN ('missing_account','missing_scope','account_mismatch','invalid_scope_reference')))'''
    entry_columns = '''video_id TEXT NOT NULL CHECK(length(video_id)>0), entry_id INTEGER, recording_id INTEGER,
            unresolved_video_reason TEXT'''
    entry_check = '''CHECK((entry_id IS NOT NULL AND recording_id IS NOT NULL AND unresolved_video_reason IS NULL)
               OR (entry_id IS NULL AND recording_id IS NULL AND unresolved_video_reason IS NOT NULL
                   AND unresolved_video_reason='unadmitted_video'))'''
    if source == BINDINGS_KEY:
        return (*common, (BINDINGS, f'''CREATE TABLE {BINDINGS} (
            source TEXT NOT NULL CHECK(source={source!r}), generation INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_ITEMS - 1}),
            {account_columns}, {scope_columns}, {entry_columns}, opportunity_id TEXT NOT NULL, option_id TEXT NOT NULL,
            bound_at REAL NOT NULL CHECK(abs(bound_at)<=1.7976931348623157e308),
            {account_check}, {scope_check}, {entry_check}, PRIMARY KEY(source,generation,position),
            FOREIGN KEY(source,generation) REFERENCES {head}(source,generation)) WITHOUT ROWID'''))
    return (*common,
        (INTENTS, f'''CREATE TABLE {INTENTS} (
            source TEXT NOT NULL CHECK(source={source!r}), generation INTEGER NOT NULL,
            {account_columns}, {scope_columns}, source_browse_id TEXT NOT NULL CHECK(length(source_browse_id)>0),
            album_id INTEGER, unresolved_album_reason TEXT,
            opportunity_id TEXT NOT NULL, action_id TEXT,
            started_at REAL NOT NULL CHECK(abs(started_at)<=1.7976931348623157e308),
            has_videos INTEGER NOT NULL CHECK(has_videos IN (0,1)), {account_check}, {scope_check},
            CHECK((album_id IS NOT NULL AND unresolved_album_reason IS NULL)
               OR (album_id IS NULL AND unresolved_album_reason IS NOT NULL AND unresolved_album_reason='missing_album')),
            PRIMARY KEY(source,generation),
            FOREIGN KEY(source,generation) REFERENCES {head}(source,generation)) WITHOUT ROWID'''),
        (ITEMS, f'''CREATE TABLE {ITEMS} (
            source TEXT NOT NULL CHECK(source={source!r}), generation INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_ITEMS - 1}), {entry_columns}, {entry_check},
            PRIMARY KEY(source,generation,position),
            FOREIGN KEY(source,generation) REFERENCES {INTENTS}(source,generation)) WITHOUT ROWID'''))


def _unused(*args):
    raise MigrationError('Playback context preparation requires its bounded file worker')


def account_input(value, required=False):
    if 'account_id' not in value or value['account_id'] is None:
        if required:
            raise ValueError('Playback attribution requires its recorded account ID')
        return None, 'null' if 'account_id' in value else 'missing'
    account = value['account_id']
    if ((type(account) is not int and not (isinstance(account, str) and account.isdecimal()
            and str(int(account)) == account)) or not 0 < int(account) < 2**63):
        raise ValueError('Playback attribution requires an exact positive account ID')
    return int(account), 'recorded'


def _scalar(value, nullable=False):
    return (value is None and nullable or isinstance(value, (str, int, float))
            and not (isinstance(value, float) and not math.isfinite(value))
            and not (isinstance(value, int) and not -2**63 <= value < 2**63))


def _validate(value, bindings):
    value = entity_names.account_document(value)
    required = ({'account_id','playlist','video','opportunity_id','option_id','at'} if bindings
                else {'opportunity_id','browse','playlist','at'})
    allowed = required if bindings else required | {'account_id','action_id','videos'}
    if (not isinstance(value, dict) or not required <= value.keys() or set(value)-allowed
            or not isinstance(value['playlist'], str) or not value['playlist']
            or not _scalar(value['opportunity_id'])
            or type(value['at']) not in (int, float) or not math.isfinite(value['at'])):
        raise ValueError('Unsupported legacy playback context; original retained')
    account_input(value, required=bindings)
    if bindings:
        if not isinstance(value['video'], str) or not value['video']:
            raise ValueError('Playback attribution requires its exact upload ID')
    elif (not isinstance(value['browse'], str) or not value['browse'] or not _scalar(value.get('action_id'), nullable=True)
            or ('videos' in value and (not isinstance(value['videos'], list) or len(value['videos']) > MAX_ITEMS
                or any(not isinstance(video, str) or not video for video in value['videos'])))):
        raise ValueError('Unsupported pending album playback context; original retained')
    return value


@dataclass(frozen=True)
class Plan:
    source: str
    digest: str
    size: int
    part_count: int
    item_count: int
    kind: str
    intent: dict | None
    units: tuple


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Playback context preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'playback-context'


def read_original(conn, row):
    _, parts, _ = tables(row['source'])
    original = conn.execute(f'SELECT position,filename,digest,byte_size FROM {parts} '
        'WHERE source=? AND generation=? ORDER BY position LIMIT ?',
        (row['source'], row['generation'], MAX_PARTS + 1)).fetchall()
    if [part[0] for part in original] != list(range(row['part_count'])):
        raise MigrationError('Prepared playback context original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['source'], chunk_key=str(part[0])) for part in original)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared playback context differs from its original digest')
    return raw


def describe(source, text):
    """Decode the exact original without database or file mutations."""
    raw = text.encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise MigrationError('Playback context exceeds its preparation byte budget')
    value = files.decode_json(text)
    if source == BINDINGS_KEY:
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            raise ValueError('Playback bindings require a bounded ordered list')
        items = [_validate(item, True) for item in value]
        intent, kind = None, 'bindings'
    else:
        intent = _validate(value, False) if value is not None else None
        items = intent.get('videos', []) if intent is not None else []
        kind = 'pending' if intent is not None else 'cleared'
    return kind, intent, items


def prepare(conn, row):
    raw = bytes(row['value'])
    text = raw.decode('utf-8')
    kind, intent, items = describe(row['key'], text)
    durable = [files.publish(directory(conn), files.encode(row['key'], str(position), text[start:start + PART_CHARACTERS]))
        for position, start in enumerate(range(0, len(text), PART_CHARACTERS))]
    if len(durable) > MAX_PARTS:
        raise MigrationError('Playback context exceeds its original part budget')
    units = tuple([('file', position, part) for position, part in enumerate(durable)]
                  + [('item', position, item) for position, item in enumerate(items)])
    return Plan(row['key'], hashlib.sha256(raw).hexdigest(), len(raw), len(durable), len(items), kind, intent, units)


def _account(conn, value):
    account, kind = account_input(value)
    known = (conn.execute(f'SELECT id FROM {entity_names.owner(conn, "accounts").table} WHERE id=?', (account,)).fetchone()
             if account is not None else None)
    return (str(account) if account is not None else None, kind, known[0] if known else None,
            account if account is not None and known is None else None)


def _scope(conn, value):
    playlist, account = value['playlist'], account_input(value)[0]
    if playlist in playlist_scopes.SYSTEM_IDS and account is None:
        return playlist, None, 'missing_account'
    try:
        key = playlist_scopes.resolve(conn, playlist, account)
    except ValueError:
        return playlist, None, 'invalid_scope_reference'
    owner = entity_names.owner(conn, 'playlist_scopes')
    found = conn.execute(f'SELECT kind,{owner.column("account_id")} FROM {owner.table} '
                         f'WHERE {owner.column("id")}=?', (key,)).fetchone()
    reason = ('missing_scope' if not found else None if found[0] == 'shared' or found[1] == account else
              'missing_account' if account is None else 'account_mismatch')
    return playlist, None if reason else key, reason


def _entry(conn, video):
    entry = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE video_id=?', (video,)).fetchone()
    return video, entry[0] if entry else None, entry[1] if entry else None, None if entry else 'unadmitted_video'


def begin(conn, row, generation, plan):
    head, _, _ = tables(row['key'])
    conn.execute(f'INSERT INTO {head} VALUES (?,?,?,?,?,?,?)',
                 (plan.source, generation, plan.digest, plan.size, plan.part_count, plan.item_count, plan.kind))
    value = plan.intent
    if value is not None:
        album = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (value['browse'],)).fetchone()
        conn.execute(f'INSERT INTO {INTENTS} VALUES ({",".join("?" for _ in range(16))})',
            (row['key'], generation, *_account(conn, value), *_scope(conn, value), value['browse'],
             album[0] if album else None, None if album else 'missing_album', value['opportunity_id'],
             value.get('action_id'), value['at'], int('videos' in value)))


def copy(conn, row, generation, position, unit):
    kind, rank, value = unit
    if kind == 'file':
        _, parts, _ = tables(row['key'])
        conn.execute(f'INSERT INTO {parts} VALUES (?,?,?,?,?,?)', (row['key'], generation, rank, *value))
    elif row['key'] == BINDINGS_KEY:
        conn.execute(f'INSERT INTO {BINDINGS} VALUES ({",".join("?" for _ in range(17))})',
            (row['key'], generation, rank, *_account(conn, value), *_scope(conn, value), *_entry(conn, value['video']),
             value['opportunity_id'], str(value['option_id']), value['at']))
    else:
        conn.execute(f'INSERT INTO {ITEMS} VALUES (?,?,?,?,?,?,?)', (row['key'], generation, rank, *_entry(conn, value)))


def complete(conn, row, generation, plan):
    _, _, current = tables(row['key'])
    conn.execute(f'INSERT INTO {current} VALUES (?,?) ON CONFLICT(source) DO UPDATE SET generation=excluded.generation',
                 (row['key'], generation))


def job(name, key, dependencies):
    definition = Job(name, 79, 93, {'settings': ('key',)}, definitions(key), _unused, _unused,
        dependencies=dependencies, filters={'settings': '{prefix}key=' + repr(key)},
        projections={'settings': ('key', f"COALESCE(substr(CAST(value AS BLOB),1,{MAX_BYTES + 1}),X'') AS value")})
    def remove(conn, owner, changed_key):
        if json.loads(changed_key)[0] == key:
            _, _, current = tables(key)
            conn.execute(f'DELETE FROM {current} WHERE source=?', (key,))
    def repair(conn, progress):
        from .document_publication import repair_parts
        repair_parts(conn, progress, source=key, tables=tables(key), prepare=prepare,
                     max_bytes=MAX_BYTES, max_parts=MAX_PARTS)
    return OrderedJob(definition, prepare, begin, copy, complete, remove, repair_files=repair)


BINDINGS_JOB = job('playback_bindings', BINDINGS_KEY, ('identities', 'playlist_scopes', 'catalog_entries'))
PENDING_JOB = job('album_playback_pending', PENDING_KEY, ('identities', 'playlist_scopes', 'catalog_entries', 'album_provider_ids'))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('bindings', 'pending-album'))
    args, remaining = parser.parse_known_args(argv)
    return run(BINDINGS_JOB if args.job == 'bindings' else PENDING_JOB, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
