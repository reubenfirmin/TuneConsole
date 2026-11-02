"""Stage TV candidates, artwork, visual measurements and continuation receipts.

Each installed document has its own job. Frozen v95 relation checks are retained
in a generation namespace. Staging references are observations, so continuing
source writes are reconciled without adding retirement gates to their owners.
"""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .. import recommendation_outputs as files, tv_recommendation_state as state
from ..schema_contract import MigrationError
from . import artist_lookup
from .ordered_rows import OrderedJob
from .rows import Job, main as run


PART_CHARACTERS = 64 * 1024
MAX_PARTS = 32
SOURCES = {'pool': 'tv_pool', 'artwork': 'tv_artist_artwork', 'artwork-requests': 'tv_artist_artwork_requests',
           'visual-checks': 'tv_visual_checks', 'continuations': 'tv_continuations'}
RELATIONS = {'tv_pool': ('tv_pool_state', 'tv_pool_items', 'tv_pool_attempts'),
    'tv_artist_artwork': ('tv_artwork_state', 'tv_artist_artwork', 'tv_artist_images'),
    'tv_artist_artwork_requests': ('tv_artwork_request_state', 'tv_artist_requests'),
    'tv_visual_checks': ('tv_visual_state', 'tv_visual_results', 'tv_visual_samples'),
    'tv_continuations': ('tv_continuation_receipts', 'tv_continuation_items')}
DEPENDENCIES = {'tv_pool': ('recordings', 'youtube_items'),
    'tv_artist_artwork': ('artists', 'artist_aliases', 'artist_redirects', artist_lookup.JOB.state),
    'tv_artist_artwork_requests': ('artists', 'artist_aliases', 'artist_redirects', artist_lookup.JOB.state), 'tv_visual_checks': (),
    'tv_continuations': ('playlist_membership_refs', 'recordings', 'youtube_items')}


def _frozen():
    result, pending = {}, ''
    path = Path(__file__).parents[1] / 'migrations' / 'schema_tv_recommendations.sql'
    for line in path.read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            sql = pending.strip().removesuffix(';')
            if sql.startswith('CREATE TABLE '):
                result[sql.split()[2]] = sql
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete frozen TV relation definitions')
    return result


FROZEN = _frozen()


def tables(surface):
    prefix = 'prepared_' + surface
    return prefix + '_generations', prefix + '_heads', prefix + '_parts'


def relation(name):
    return 'prepared_tv_relation_' + name.removeprefix('tv_')


def definition_sql(surface, name):
    """Namespace only keys/references; preserve the frozen column constraints."""
    sql = FROZEN[name].removeprefix('CREATE TABLE ' + name + ' (')
    body = sql.removesuffix(' WITHOUT ROWID').removesuffix(')').strip()
    primary = re.search(r'id INTEGER PRIMARY KEY(?: AUTOINCREMENT)? NOT NULL CHECK\(id[=>][01]\)', body)
    if primary:
        body = body[:primary.start()] + primary[0].replace(' PRIMARY KEY', '').replace(' AUTOINCREMENT', '') + body[primary.end():]
    body = re.sub(r'(PRIMARY KEY|UNIQUE)\(([^)]+)\)', r'\1(source,preparation_revision,\2)', body)
    internal = []
    def unique(match):
        internal.append(f'UNIQUE(source,preparation_revision,{match[1]})')
        return match[1] + ' TEXT NOT NULL'
    body = re.sub(r'(\w+) TEXT NOT NULL UNIQUE', unique, body)
    def reference(match):
        field, kind, owner = match.groups()
        if owner in RELATIONS[surface]:
            internal.append(f'FOREIGN KEY(source,preparation_revision,{field}) REFERENCES {relation(owner)}(source,preparation_revision,id)')
        elif owner not in ('recordings', 'artists', 'playlist_membership_refs'):
            raise MigrationError('Unclassified frozen TV reference: ' + owner)
        return field + ' ' + kind
    body = re.sub(r'(\w+) (INTEGER(?: NOT NULL)?) REFERENCES (\w+)\(id\)(?: ON DELETE CASCADE)?', reference, body)
    if 'REFERENCES ' in body:
        raise MigrationError('Unclassified frozen TV relation definition')
    if primary:
        internal.append('PRIMARY KEY(source,preparation_revision,id)')
    internal.append(f'FOREIGN KEY(source,preparation_revision) REFERENCES {tables(surface)[0]}(source,preparation_revision)')
    return (f'CREATE TABLE {relation(name)} (source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, ' +
            body + ',' + ','.join(internal) + ') WITHOUT ROWID')


def definitions(surface):
    head, current, parts = tables(surface)
    yield head, f'''CREATE TABLE {head} (
        source TEXT NOT NULL CHECK(source={surface!r}), preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        built_at REAL CHECK(built_at IS NULL OR abs(built_at)<=1.7976931348623157e308),
        raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64), raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 1 AND {state.MAX_BYTES}),
        part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND {MAX_PARTS}), row_count INTEGER NOT NULL CHECK(row_count>=0),
        PRIMARY KEY(source,preparation_revision)) WITHOUT ROWID'''
    yield current, f'''CREATE TABLE {current} (
        source TEXT PRIMARY KEY CHECK(source={surface!r}), preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    yield parts, f'''CREATE TABLE {parts} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND {MAX_PARTS - 1}),
        filename TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64), byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision,position), FOREIGN KEY(source,preparation_revision)
            REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    for name in RELATIONS[surface]:
        yield relation(name), definition_sql(surface, name)


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('TV preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'tv-state'


def item(table, parent, identity, position, value):
    fields = {('source_group' if field == 'group' else field): value.get(field) for field in state.ITEM}
    fields.update({parent: identity, 'position': position, 'field_mask': state._mask(value, state.ITEM)})
    return table, fields, 'item'


def rows(surface, value, at):
    if surface == 'tv_pool':
        yield 'tv_pool_state', dict(id=1, built_at=at, field_mask=state._mask(value, state.POOL),
            schema_version=value.get('schema'), cache_key=value.get('key'), at=value.get('at'),
            offset=value.get('offset'), state=value.get('state'), retry_at=value.get('retry_at')), None
        for position, entry in enumerate(value.get('items', [])):
            yield item('tv_pool_items', 'pool_id', 1, position, entry)
        for position, key in enumerate(value.get('attempted', [])):
            yield 'tv_pool_attempts', dict(pool_id=1, position=position, raw_key=key), 'attempt'
    elif surface == 'tv_artist_artwork':
        yield 'tv_artwork_state', dict(id=1, built_at=at), None
        for identity, (key, entry) in enumerate(value.items(), 1):
            yield 'tv_artist_artwork', dict(id=identity, artist_key=key, field_mask=state._mask(entry, state.ARTWORK),
                retry_at=entry.get('retry_at'), at=entry.get('at')), 'artist-key'
            for position, url in enumerate(entry.get('images', [])):
                yield 'tv_artist_images', dict(artwork_id=identity, position=position, url=url), None
    elif surface == 'tv_artist_artwork_requests':
        yield 'tv_artwork_request_state', dict(id=1, built_at=at), None
        for identity, (key, name) in enumerate(value.items(), 1):
            # The installed request did not capture an admission time.
            yield 'tv_artist_requests', dict(id=identity, artist_key=key, display_name=name, requested_at=None), 'artist-name'
    elif surface == 'tv_visual_checks':
        yield 'tv_visual_state', dict(id=1, built_at=at), None
        for identity, (video, entry) in enumerate(value.items(), 1):
            fields = {field: entry.get(field) for field in state.VISUAL[:-1]}
            yield 'tv_visual_results', dict(id=identity, video_id=video, field_mask=state._mask(entry, state.VISUAL), **fields), None
            for position, moment in enumerate(entry.get('sample_times', [])):
                yield 'tv_visual_samples', dict(result_id=identity, position=position, at=moment), None
    else:
        for identity, (key, entry) in enumerate(value.items(), 1):
            source, stream, request = state._address(key)
            yield 'tv_continuation_receipts', dict(id=identity, request_key=key, source_playlist_id=source,
                input_stream=stream, request_id=request, status='complete', target_playlist_id=entry['playlist_id'],
                target_stream=entry['stream'], built_at=at), 'receipt'
            for position, track in enumerate(entry.get('items', [])):
                yield item('tv_continuation_items', 'receipt_id', identity, position, track)


@dataclass(frozen=True)
class Plan:
    digest: str
    size: int
    part_count: int
    row_count: int
    units: tuple


def prepare(conn, row):
    if row['surface'] in ('tv_artist_artwork', 'tv_artist_artwork_requests'):
        artist_lookup.require(conn)
    raw = bytes(row['payload'])
    if len(raw) > state.MAX_BYTES:
        raise MigrationError('TV state exceeds its preparation byte budget')
    at = row['built_at']
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        raise ValueError('TV state requires a finite captured observation time')
    text = raw.decode('utf-8')
    value = state.decode(row['surface'], text)
    members = tuple(rows(row['surface'], value, at))
    parts = tuple(files.publish(directory(conn), files.encode(row['surface'], str(position), text[start:start + PART_CHARACTERS]))
                  for position, start in enumerate(range(0, len(text), PART_CHARACTERS)))
    if not 1 <= len(parts) <= MAX_PARTS:
        raise MigrationError('TV state exceeds its original part budget')
    return Plan(hashlib.sha256(raw).hexdigest(), len(raw), len(parts), len(members),
                tuple(('file', position, part) for position, part in enumerate(parts)) + members)


def begin(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {tables(row["surface"])[0]} VALUES (?,?,?,?,?,?,?)',
        (row['surface'], generation, row['built_at'], plan.digest, plan.size, plan.part_count, plan.row_count))


def resolve(conn, fields, kind):
    def capture(name, raw, lookup, reason):
        reference = lookup(conn, raw)
        fields[name] = reference
        fields[reason[0]] = None if reference is not None else reason[1]
    if kind == 'item':
        capture('source_recording_id', fields['source_key'], state._recording, ('source_resolution', 'missing_recording'))
        video = conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?', (fields['video_id'],)).fetchone()
        fields.update(recording_id=video[0] if video else None, video_resolution=None if video else 'external_video')
    elif kind == 'attempt':
        capture('recording_id', fields['raw_key'], state._recording, ('resolution_reason', 'missing_recording'))
    elif kind in ('artist-key', 'artist-name'):
        capture('artist_id', fields['artist_key' if kind == 'artist-key' else 'display_name'], artist_lookup.lookup,
                ('resolution_reason', 'missing_artist'))
    elif kind == 'receipt':
        capture('source_ref', fields['source_playlist_id'], state._playlist, ('source_resolution', 'missing_playlist'))
        capture('target_ref', fields['target_playlist_id'], state._playlist, ('target_resolution', 'missing_playlist'))


def copy(conn, row, generation, position, unit):
    table, values, kind = unit
    if table == 'file':
        conn.execute(f'INSERT INTO {tables(row["surface"])[2]} VALUES (?,?,?,?,?,?)', (row['surface'], generation, values, *kind))
        return
    fields = dict(values)
    resolve(conn, fields, kind)
    columns = ','.join('"' + field + '"' for field in fields)
    conn.execute(f'INSERT INTO {relation(table)}(source,preparation_revision,{columns}) VALUES (?,?,{",".join("?" for _ in fields)})',
                 (row['surface'], generation, *fields.values()))


def complete(conn, row, generation, plan):
    conn.execute(f'INSERT INTO {tables(row["surface"])[1]} VALUES (?,?) '
                 'ON CONFLICT(source) DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['surface'], generation))


def delete(conn, owner, key):
    surface, = json.loads(key)
    if surface in RELATIONS:
        conn.execute(f'DELETE FROM {tables(surface)[1]} WHERE source=?', (surface,))


def read_original(conn, row):
    parts = conn.execute(f'SELECT position,filename,digest,byte_size FROM {tables(row["source"])[2]} '
        'WHERE preparation_revision=? ORDER BY position LIMIT ?', (row['preparation_revision'], MAX_PARTS + 1)).fetchall()
    if [part[0] for part in parts] != list(range(row['part_count'])):
        raise MigrationError('Prepared TV original parts are incomplete')
    raw = ''.join(files.read(*part[1:], surface=row['source'], chunk_key=str(part[0])) for part in parts)
    if len(raw.encode()) != row['raw_size'] or hashlib.sha256(raw.encode()).hexdigest() != row['raw_digest']:
        raise MigrationError('Prepared TV state differs from its original digest')
    return raw


class TVJob(Job):
    def install(self, conn, **kwargs):
        if artist_lookup.JOB.state in self.dependencies:
            artist_lookup.JOB.install(conn, **kwargs)
        return super().install(conn, **kwargs)


def definition(surface):
    return TVJob(surface, 79, 94, {'rec_proposals': ('surface',)}, tuple(definitions(surface)), None, None,
        dependencies=DEPENDENCIES[surface], filters={'rec_proposals': '{prefix}surface=' + repr(surface)},
        projections={'rec_proposals': ('surface', f"COALESCE(substr(CAST(payload AS BLOB),1,{state.MAX_BYTES + 1}),X'') AS payload", 'built_at')})


def repair_files(conn, progress, *, surface):
    try:
        conn.execute('BEGIN')
        generations, current, parts = tables(surface)
        captured = conn.execute('SELECT g.* FROM '+current+' h JOIN '+generations+
            ' g USING(source,preparation_revision) WHERE source=?', (surface,)).fetchone()
        if captured is None or captured['preparation_revision'] != progress.input_revision:
            return
        row = conn.execute('SELECT surface,COALESCE(substr(CAST(payload AS BLOB),1,?),X\'\') AS payload,built_at '
            'FROM main.rec_proposals WHERE surface=?', (state.MAX_BYTES+1, surface)).fetchone()
        if row is None or hashlib.sha256(bytes(row['payload'])).hexdigest() != captured['raw_digest']:
            return
        retained = list(map(tuple, conn.execute('SELECT position,filename,digest,byte_size FROM '+parts+
            ' WHERE source=? AND preparation_revision=? ORDER BY position LIMIT ?',
            (surface, captured['preparation_revision'], MAX_PARTS+1))))
    finally:
        conn.rollback()
    plan = prepare(conn, row)
    if retained != [(position, *reference) for kind, position, reference in plan.units if kind == 'file']:
        raise MigrationError('Rebuilt TV original differs from its retained manifest')


def worker(surface):
    from functools import partial
    return OrderedJob(definition(surface), prepare, begin, copy, complete, delete,
                      repair_files=partial(repair_files, surface=surface))


JOBS = {name: worker(surface) for name, surface in SOURCES.items()}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=('aliases', *JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(artist_lookup.JOB if args.job == 'aliases' else JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
