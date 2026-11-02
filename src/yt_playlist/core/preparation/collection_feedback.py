"""Prepare retained album judgments and account authentication flags separately."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from .. import entity_names, recommendation_outputs as files
from ..schema_contract import MigrationError
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 16384
PART_CHARACTERS = 64 * 1024
MAX_PARTS = 256
RESPONSES = 'prepared_album_feedback'
AUTH = 'prepared_account_auth_expirations'
KEYS = ('album_feedback', 'auth_expired_identities')
PREFIXES = {'album_feedback': 'prepared_album_feedback', 'auth_expired_identities': 'prepared_account_auth'}


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
    responses = (RESPONSES, f'''CREATE TABLE {RESPONSES} (
            source TEXT NOT NULL CHECK(source='album_feedback'), generation INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_ITEMS - 1}),
            source_browse_id TEXT NOT NULL, album_id INTEGER, unresolved_reason TEXT,
            response TEXT NOT NULL CHECK(response IN ('more','known','reject')),
            created_at REAL NOT NULL CHECK(abs(created_at)<=1.7976931348623157e308),
            opportunity_id TEXT, action_id TEXT, account_id INTEGER CHECK(account_id IS NULL),
            legacy_author_unknown INTEGER NOT NULL CHECK(legacy_author_unknown=1),
            CHECK((album_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='missing_album')
               OR (album_id IS NOT NULL AND unresolved_reason IS NULL)),
            PRIMARY KEY(source,generation,position), UNIQUE(source,generation,source_browse_id),
            FOREIGN KEY(source,generation) REFERENCES {head}(source,generation)) WITHOUT ROWID''')
    auth = (AUTH, f'''CREATE TABLE {AUTH} (
            source TEXT NOT NULL CHECK(source='auth_expired_identities'), generation INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_ITEMS - 1}),
            raw_account_id TEXT NOT NULL, account_id INTEGER, unresolved_account_id INTEGER, label TEXT NOT NULL,
            CHECK((account_id IS NOT NULL AND account_id>0 AND unresolved_account_id IS NULL AND CAST(account_id AS TEXT)=raw_account_id)
               OR (account_id IS NULL AND unresolved_account_id IS NOT NULL AND unresolved_account_id>0
                   AND CAST(unresolved_account_id AS TEXT)=raw_account_id)),
            PRIMARY KEY(source,generation,position), UNIQUE(source,generation,raw_account_id),
            FOREIGN KEY(source,generation) REFERENCES {head}(source,generation)) WITHOUT ROWID''')
    return (*common, responses if source == 'album_feedback' else auth)


def _unused(*args):
    raise MigrationError('Collection feedback preparation requires its bounded file worker')


@dataclass(frozen=True)
class Plan:
    source: str
    digest: str
    size: int
    part_count: int
    item_count: int
    units: tuple


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Collection feedback preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'collection-feedback'


def read_original(conn, row):
    _, owner, _ = tables(row['source'])
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {owner} '
        'WHERE source=? AND generation=? ORDER BY position LIMIT ?',
        (row['source'], row['generation'], MAX_PARTS + 1)).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared collection feedback original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['source'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared collection feedback differs from its original digest')
    return raw


def describe(source, text):
    """Validate the retained wire document without SQL or file publication."""
    raw = text.encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise MigrationError('Collection feedback exceeds its preparation byte budget')
    value = files.decode_json(text)
    if not isinstance(value, dict) or len(value) > MAX_ITEMS:
        raise ValueError('Collection feedback requires a bounded original map')
    if source == 'album_feedback':
        for browse, item in value.items():
            if (not browse or not isinstance(item, dict) or set(item)-{'kind','at','opportunity_id','action_id'}
                    or item.get('kind') not in {'more','known','reject'}
                    or type(item.get('at')) not in (int, float) or not math.isfinite(item['at'])
                    or any(item.get(field) is not None and (not isinstance(item[field], (str, int, float))
                        or isinstance(item[field], float) and not math.isfinite(item[field])) for field in ('opportunity_id','action_id'))):
                raise ValueError('Unsupported legacy album feedback; original retained')
        items = sorted(value.items(), key=lambda item: (item[1]['at'], item[0]))
    else:
        for account, label in value.items():
            if (not account.isdecimal() or str(int(account)) != account or not 0 < int(account) < 2**63
                    or not isinstance(label, str)):
                raise ValueError('Legacy expired authentication requires an exact account ID and label')
        items = list(value.items())
    return items


def prepare(conn, row):
    raw = bytes(row['value'])
    text = raw.decode('utf-8')
    items = describe(row['key'], text)
    durable = [files.publish(directory(conn), files.encode(row['key'], str(position), text[start:start + PART_CHARACTERS]))
        for position, start in enumerate(range(0, len(text), PART_CHARACTERS))]
    if len(durable) > MAX_PARTS:
        raise MigrationError('Collection feedback exceeds its original part budget')
    units = tuple([('file', position, part) for position, part in enumerate(durable)]
                  + [('item', position, item) for position, item in enumerate(items)])
    return Plan(row['key'], hashlib.sha256(raw).hexdigest(), len(raw), len(durable), len(items), units)


def begin(conn, row, generation, plan):
    head, _, _ = tables(row['key'])
    conn.execute(f'INSERT INTO {head} VALUES (?,?,?,?,?,?)',
        (plan.source, generation, plan.digest, plan.size, plan.part_count, plan.item_count))


def copy(conn, row, generation, position, unit):
    kind, rank, item = unit
    source = row['key']
    if kind == 'file':
        _, parts, _ = tables(source)
        conn.execute(f'INSERT INTO {parts} VALUES (?,?,?,?,?,?)', (source, generation, rank, *item))
    elif source == 'album_feedback':
        browse, value = item
        album = conn.execute("SELECT album_id FROM album_provider_ids WHERE provider='youtube' AND provider_id=?", (browse,)).fetchone()
        conn.execute(f'INSERT INTO {RESPONSES} VALUES (?,?,?,?,?,?,?,?,?,?,NULL,1)',
            (source, generation, rank, browse, album[0] if album else None, None if album else 'missing_album',
             value['kind'], value['at'], value.get('opportunity_id'), value.get('action_id')))
    else:
        account, label = item
        known = conn.execute(f'SELECT id FROM {entity_names.accounts(conn)} WHERE id=?', (int(account),)).fetchone()
        conn.execute(f'INSERT INTO {AUTH} VALUES (?,?,?,?,?,?,?)',
            (source, generation, rank, account, known[0] if known else None, None if known else int(account), label))


def complete(conn, row, generation, plan):
    _, _, current = tables(row['key'])
    conn.execute(f'INSERT INTO {current} VALUES (?,?) ON CONFLICT(source) DO UPDATE SET generation=excluded.generation',
                 (row['key'], generation))


def delete(conn, owner, key):
    source = json.loads(key)[0]
    _, _, current = tables(source)
    conn.execute(f'DELETE FROM {current} WHERE source=?', (source,))


def job(name, key, dependencies):
    definition = Job(name, 79, 93, {'settings': ('key',)}, definitions(key), _unused, _unused,
        dependencies=dependencies, filters={'settings': '{prefix}key=' + repr(key)},
        projections={'settings': ('key', f"COALESCE(substr(CAST(value AS BLOB),1,{MAX_BYTES + 1}),X'') AS value")})
    def remove(conn, owner, changed_key):
        # A rename journals both keys. Only this job's installed source may
        # withdraw its head, even when the new key belongs to another job.
        if json.loads(changed_key)[0] == key:
            delete(conn, owner, changed_key)
    def repair(conn, progress):
        from .document_publication import repair_parts
        repair_parts(conn, progress, source=key, tables=tables(key), prepare=prepare,
                     max_bytes=MAX_BYTES, max_parts=MAX_PARTS)
    return OrderedJob(definition, prepare, begin, copy, complete, remove, repair_files=repair)


FEEDBACK_JOB = job('album_feedback', 'album_feedback', ('album_provider_ids',))
AUTH_JOB = job('account_auth_expirations', 'auth_expired_identities', ('identities',))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('album-feedback', 'account-auth'))
    args, remaining = parser.parse_known_args(argv)
    return run(FEEDBACK_JOB if args.job == 'album-feedback' else AUTH_JOB, remaining)


if __name__ == '__main__':
    raise SystemExit(main())
