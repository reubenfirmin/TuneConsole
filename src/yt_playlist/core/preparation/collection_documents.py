"""Stage import reports and saved playlist documents without replacing sources."""
import hashlib
import json
from pathlib import Path

from .. import collection_recovery, entity_names, policy_documents, recommendation_outputs as files, spotify_job_state
from ..evidence_ledger import canonical
from ..migrations.runner import MigrationError
from ..recommendation_work import finite
from . import document_plan, taste_axes
from .ordered_rows import OrderedJob
from .rows import Job, main as run


MAX_SOURCE_BYTES = 32 * 1024 * 1024
PART_CHARACTERS = 64 * 1024
SOURCES = {'import': ('settings', 'key', 'value'), 'recipes': ('rec_recipes', 'playlist_ytm', 'recipe'),
           'canvases': ('cluster_canvas', 'playlist_ytm', 'state')}
PREFIXES = {'import': 'prepared_spotify_import', 'recipes': 'prepared_playlist_documents', 'canvases': 'prepared_playlist_canvases'}


def table(name, suffix):
    return PREFIXES[name] + '_' + suffix


def number(field):
    return f"{field} REAL CHECK({field} IS NULL OR (typeof({field}) IN ('integer','real') AND abs({field})<=1.7976931348623157e308))"


def definitions(name):
    head = table(name, 'generations')
    if name == 'import':
        specifics = f'''account_id INTEGER, unresolved_account_id INTEGER, account_present INTEGER NOT NULL CHECK(account_present IN (0,1)),
            field_mask INTEGER NOT NULL CHECK(field_mask BETWEEN 0 AND 4095),
            status TEXT CHECK(status IN ('queued','running','preview','done','error')),
            phase TEXT CHECK(phase IN ('preview','import')), {number('started_at')}, {number('finished_at')},
            identity_label TEXT, report_schema INTEGER CHECK(report_schema IS NULL OR (typeof(report_schema)='integer' AND report_schema>=0)),
            dismissed INTEGER CHECK(dismissed IS NULL OR dismissed IN (0,1)), error TEXT,
            playlists_found INTEGER CHECK(playlists_found IS NULL OR (typeof(playlists_found)='integer' AND playlists_found>=0)),
            track_entries INTEGER CHECK(track_entries IS NULL OR (typeof(track_entries)='integer' AND track_entries>=0)),
            albums_found INTEGER CHECK(albums_found IS NULL OR (typeof(albums_found)='integer' AND albums_found>=0)),
            lookup_count INTEGER CHECK(lookup_count IS NULL OR (typeof(lookup_count)='integer' AND lookup_count>=0)), estimate_label TEXT,
            CHECK((account_id IS NULL OR unresolved_account_id IS NULL) AND
                (account_present=1 OR (account_id IS NULL AND unresolved_account_id IS NULL))),
            CHECK((state_present=0)=(status IS NULL))'''
    elif name == 'recipes':
        specifics = '''scope_ref TEXT, scope_resolution TEXT CHECK(scope_resolution='missing_scope'),
            media TEXT CHECK(media IN ('audio','video')), model TEXT, journey TEXT, opportunity_id TEXT, option_id TEXT,
            CHECK((scope_ref IS NULL)=(scope_resolution IS NOT NULL))'''
    else:
        specifics = '''scope_ref TEXT, scope_resolution TEXT CHECK(scope_resolution='missing_scope'),
            CHECK((scope_ref IS NULL)=(scope_resolution IS NOT NULL))'''
    yield head, f'''CREATE TABLE {head} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL CHECK(preparation_revision>=0),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')), state_present INTEGER NOT NULL CHECK(state_present IN (0,1)),
        {number('created_at')}, raw_digest TEXT NOT NULL CHECK(length(raw_digest)=64),
        raw_size INTEGER NOT NULL CHECK(raw_size BETWEEN 0 AND {MAX_SOURCE_BYTES}), part_count INTEGER NOT NULL CHECK(part_count BETWEEN 1 AND 512),
        member_count INTEGER NOT NULL CHECK(member_count>=0), unit_count INTEGER NOT NULL CHECK(unit_count>=1),
        plan_filename TEXT NOT NULL, plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64),
        plan_bytes INTEGER NOT NULL CHECK(plan_bytes BETWEEN 1 AND {files.MAX_BYTES}),
        recovery_filename TEXT, recovery_digest TEXT CHECK(length(recovery_digest)=64),
        recovery_bytes INTEGER CHECK(recovery_bytes BETWEEN 1 AND {collection_recovery.MAX_BYTES}),
        {specifics}, PRIMARY KEY(source,preparation_revision),
        CHECK((state_present=0)=(recovery_filename IS NULL)),
        CHECK((recovery_filename IS NULL)=(recovery_digest IS NULL)),
        CHECK((recovery_filename IS NULL)=(recovery_bytes IS NULL))) WITHOUT ROWID'''
    current = table(name, 'heads')
    yield current, f'''CREATE TABLE {current} (
        source TEXT PRIMARY KEY, preparation_revision INTEGER NOT NULL,
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    parts = table(name, 'parts')
    yield parts, f'''CREATE TABLE {parts} (
        source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position>=0),
        filename TEXT NOT NULL, content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
        byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND {files.MAX_BYTES}),
        PRIMARY KEY(source,preparation_revision,position),
        FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision)) WITHOUT ROWID'''
    if name == 'recipes':
        facets = table(name, 'facets')
        yield facets, f'''CREATE TABLE {facets} (
            source TEXT NOT NULL, preparation_revision INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('genre','era')),
            position INTEGER NOT NULL CHECK(position>=0), captured_value TEXT NOT NULL CHECK(length(captured_value)>0),
            axis_digest TEXT NOT NULL CHECK(length(axis_digest)=64), axis_ref INTEGER CHECK(axis_ref>0),
            axis_kind TEXT NOT NULL CHECK(axis_kind IN ('genre','family','other','unresolved_genre')),
            genre_key TEXT, family_key TEXT, value TEXT, resolution_reason TEXT,
            PRIMARY KEY(source,preparation_revision,kind,position),
            FOREIGN KEY(source,preparation_revision) REFERENCES {head}(source,preparation_revision),
            CHECK((axis_kind='genre' AND genre_key IS NOT NULL AND family_key IS NULL AND value IS NULL AND resolution_reason IS NULL)
                OR (axis_kind='family' AND genre_key IS NULL AND family_key IS NOT NULL AND value IS NULL AND resolution_reason IS NULL)
                OR (axis_kind='other' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL AND resolution_reason IS NULL)
                OR (axis_kind='unresolved_genre' AND genre_key IS NULL AND family_key IS NULL AND value IS NOT NULL
                    AND resolution_reason IN ('unknown_genre','ambiguous_genre'))),
            CHECK((kind='genre' AND axis_kind IN ('genre','family','unresolved_genre')) OR
                  (kind='era' AND axis_kind='other' AND value='era:'||captured_value))) WITHOUT ROWID'''


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise MigrationError('Collection document preparation requires a file-backed database')
    path = Path(database).resolve()
    return path.with_name(path.name + '.preparation') / 'collection-documents'


def recovery(root, kind, source, value):
    # Source-scoped custody does not invent the destination's numeric identity.
    encoded = canonical({'format': collection_recovery.FORMAT, 'kind': kind, 'owner': source, 'state': value})
    if len(encoded) > collection_recovery.MAX_BYTES:
        raise ValueError('Collection document recovery exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    return policy_documents._publish_encoded(root, encoded, digest), digest, len(encoded)


def import_fields(conn, value):
    fields = {'account_id': None, 'unresolved_account_id': None, 'account_present': 0, 'field_mask': 0}
    if value is None:
        return fields, None
    document = entity_names.account_document(value)
    if (not isinstance(document, dict) or 'status' not in document or document['status'] not in ('queued','running','preview','done','error')
            or set(document) - {'status', 'account_id', spotify_job_state.TOKEN, *spotify_job_state.FIELDS, *spotify_job_state.OPAQUE}
            or document.get(spotify_job_state.TOKEN) is not None):
        raise ValueError('Unsupported historical Spotify import job')
    account = document.get('account_id')
    if account is not None and (type(account) is not int or not -(2 ** 63) <= account < 2 ** 63):
        raise ValueError('Spotify import job account requires an integer identity')
    known = conn.execute('SELECT id FROM main.identities WHERE id=?', (account,)).fetchone() if account is not None else None
    fields.update(account_id=known[0] if known else None, unresolved_account_id=None if known else account,
                  account_present=int('account_id' in document),
                  field_mask=sum(1 << index for index, field in enumerate(spotify_job_state.FIELDS) if field in document),
                  status=document['status'])
    for field in spotify_job_state.FIELDS:
        item = document.get(field)
        if item is not None:
            if field in ('started_at', 'finished_at'):
                finite(item)
            elif field in ('report_schema', 'playlists_found', 'track_entries', 'albums_found', 'lookup_count'):
                if type(item) is not int or not 0 <= item < 2 ** 63:
                    raise ValueError('Import job counters require nonnegative integers')
            elif field == 'dismissed':
                if type(item) not in (bool, int) or item not in (0, 1):
                    raise ValueError('Import job dismissal requires a boolean')
                item = int(item)
            elif not isinstance(item, str) or field == 'phase' and item not in ('preview', 'import'):
                raise ValueError('Unsupported import job field')
        fields[field] = item
    return fields, {key: document[key] for key in spotify_job_state.OPAQUE if key in document}


def recipe_units(conn, value, fields):
    if not isinstance(value, dict):
        raise ValueError('A saved playlist recipe requires an object')
    for field in ('media', 'model', 'journey', 'opportunity_id', 'option_id'):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or field == 'media' and item not in ('audio', 'video')):
            raise ValueError('Unsupported playlist recipe execution field')
        fields[field] = item
    selections = {} if value.get('facets') is None else value['facets']
    if not isinstance(selections, dict):
        raise ValueError('Playlist recipe facets require an object')
    has_axes = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='taste_axes'").fetchone()
    for kind, field in (('genre', 'genres'), ('era', 'eras')):
        labels = selections.get(field)
        if labels is None:
            continue
        if not isinstance(labels, list):
            raise ValueError('Playlist recipe facets require ordered lists')
        for position, label in enumerate(labels):
            if not isinstance(label, str) or not label.strip():
                raise ValueError('Playlist recipe facets require nonempty labels')
            digest, meaning = taste_axes.axis(conn, ('genre:' if kind == 'genre' else 'era:') + label)
            axis = conn.execute('SELECT id FROM taste_axes WHERE kind=? AND genre_key IS ? AND family_key IS ? AND value IS ?',
                                meaning[:4]).fetchone() if has_axes else None
            fields['member_count'] += 1
            yield 'facet', (kind, position, label, digest, axis[0] if axis else None, *meaning)


def describe(name, text, created_at):
    raw = text.encode('utf-8') if text is not None else b''
    if len(raw) > MAX_SOURCE_BYTES:
        raise MigrationError('Collection document exceeds its preparation byte budget')
    value = text if name == 'canvases' else json.loads(text) if text else None
    fields = {'source_kind': 'null' if text is None else 'blank' if text == '' else 'recorded',
              'state_present': int(name != 'import' or value is not None),
              'created_at': created_at, 'raw_digest': hashlib.sha256(raw).hexdigest(), 'raw_size': len(raw),
              'part_count': max(1, (len(text or '') + PART_CHARACTERS - 1) // PART_CHARACTERS), 'member_count': 0,
              'recovery_filename': None, 'recovery_digest': None, 'recovery_bytes': None}
    if created_at is not None:
        finite(created_at)
    return fields, value


def prepare(name, conn, row, *, revision, offset, rebuild=False):
    source = row['source']
    previous = conn.execute(f'SELECT plan_filename,plan_digest,plan_bytes FROM {table(name,"generations")} '
                            'WHERE source=? AND preparation_revision=?', (source, revision)).fetchone() if offset else None
    if offset and not rebuild:
        if previous is None:
            raise MigrationError('Prepared collection document has no durable plan')
        return document_plan.load(source, previous)
    text = bytes(row['document']).decode('utf-8') if row['document'] is not None else None
    fields, value = describe(name, text, row['created_at'])
    root = directory(conn)
    if name == 'import':
        values, recovered = import_fields(conn, value)
        fields.update(values)
        kind = 'spotify-import'
    else:
        known = conn.execute('SELECT ref FROM main.playlist_scopes WHERE ref=?', (source,)).fetchone()
        fields.update(scope_ref=known[0] if known else None, scope_resolution=None if known else 'missing_scope')
        kind, recovered = ('playlist-recipe' if name == 'recipes' else 'playlist-canvas'), value
    def units():
        for position in range(fields['part_count']):
            part = None if text is None else text[position * PART_CHARACTERS:(position + 1) * PART_CHARACTERS]
            yield 'part', (position, *files.publish(root, files.encode(source, 'original:' + str(position), part)))
        if name == 'recipes':
            yield from recipe_units(conn, value, fields)
    if fields['state_present']:
        fields.update(zip(('recovery_filename', 'recovery_digest', 'recovery_bytes'), recovery(root, kind, source, recovered)))
    result = document_plan.build(root, source, fields, units())
    if offset and (previous is None or tuple(previous) != result.manifest):
        raise MigrationError('Rebuilt collection plan differs from its captured checkpoint')
    return result


def begin(name, conn, row, revision, plan):
    values = {'source': row['source'], 'preparation_revision': revision, **plan.fields,
              'plan_filename': plan.manifest[0], 'plan_digest': plan.manifest[1], 'plan_bytes': plan.manifest[2]}
    conn.execute(f'INSERT INTO {table(name,"generations")}({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))


def copy(name, conn, row, revision, position, unit):
    kind, values = unit
    suffix = 'parts' if kind == 'part' else 'facets'
    conn.execute(f'INSERT INTO {table(name,suffix)} VALUES ({",".join("?" for _ in range(2+len(values)))})',
                 (row['source'], revision, *values))


def complete(name, conn, row, revision, plan):
    conn.execute(f'INSERT INTO {table(name,"heads")} VALUES (?,?) ON CONFLICT(source) '
                 'DO UPDATE SET preparation_revision=excluded.preparation_revision', (row['source'], revision))


def delete(name, conn, owner, key):
    source, = json.loads(key)
    conn.execute(f'DELETE FROM {table(name,"heads")} WHERE source=?', (source,))


def resume_read(name, conn, owner, key):
    source, = json.loads(key)
    _, field, _ = SOURCES[name]
    return conn.execute(f'SELECT {field} AS source FROM main.{owner} WHERE {field}=? AND json_array({field})=?', (source, key)).fetchone()


def head(conn, name, source):
    return conn.execute(f'SELECT g.* FROM {table(name,"heads")} h JOIN {table(name,"generations")} g '
                        'USING(source,preparation_revision) WHERE source=?', (source,)).fetchone()


def read_original(conn, name, generation):
    source = generation['source']
    parts = list(conn.execute(f'SELECT * FROM {table(name,"parts")} WHERE source=? AND preparation_revision=? ORDER BY position LIMIT 513',
                             (source, generation['preparation_revision'])))
    if len(parts) != generation['part_count'] or [row['position'] for row in parts] != list(range(len(parts))):
        raise MigrationError('Prepared collection original has incomplete parts')
    values = [files.read(row['filename'], row['content_digest'], row['byte_size'], surface=source,
                         chunk_key='original:' + str(row['position'])) for row in parts]
    value = None if generation['source_kind'] == 'null' else ''.join(values)
    if value is None and values != [None]:
        raise MigrationError('Prepared collection original has an invalid null source')
    raw = value.encode() if value is not None else b''
    if len(raw) != generation['raw_size'] or hashlib.sha256(raw).hexdigest() != generation['raw_digest']:
        raise MigrationError('Prepared collection original differs from its captured bytes')
    return value


def definition(name):
    owner, key, field = SOURCES[name]
    dependencies = ('identities',) if name == 'import' else ('playlist_scopes',)
    optional = ()
    if name == 'recipes':
        dependencies += ('genre_catalog', 'genre_alias', 'genre_family')
        optional = ('genre_redirects', 'taste_axes')
    return Job('collection_' + name, 79, 96, {owner: (key,)}, tuple(definitions(name)), None, None,
        # Capture finite boundaries in the explicit worker. Dependency triggers
        # must not query a legacy source column while frozen v97 replaces it.
        dependencies=dependencies, optional_dependencies=optional, refresh_highwater=False,
        filters={owner: '{prefix}key=' + repr(spotify_job_state.KEY)} if name == 'import' else None,
        projections={owner: (key + ' AS source',
            f"CASE WHEN {field} IS NULL THEN NULL ELSE COALESCE(substr(CAST({field} AS BLOB),1,{MAX_SOURCE_BYTES + 1}),X'') END AS document",
            'NULL AS created_at' if name == 'import' else 'created_at')})


def repair_import(conn, progress):
    """Restore missing files for the single retained import document only."""
    try:
        conn.execute('BEGIN')
        captured = head(conn, 'import', spotify_job_state.KEY)
        if captured is None or captured['preparation_revision'] != progress.input_revision:
            return
        row = conn.execute("SELECT key AS source,CASE WHEN value IS NULL THEN NULL ELSE "
            "COALESCE(substr(CAST(value AS BLOB),1,?),X'') END AS document,NULL AS created_at "
            'FROM main.settings WHERE key=?', (MAX_SOURCE_BYTES+1, spotify_job_state.KEY)).fetchone()
        raw = bytes(row['document']) if row is not None and row['document'] is not None else b''
        if row is None or hashlib.sha256(raw).hexdigest() != captured['raw_digest']:
            return
    finally:
        conn.rollback()
    plan = prepare('import', conn, row, revision=progress.input_revision, offset=0)
    if plan.manifest != tuple(captured[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')):
        raise MigrationError('Rebuilt import plan differs from its retained generation')


def job(name):
    from functools import partial
    return OrderedJob(definition(name), *(partial(method, name) for method in (prepare, begin, copy, complete, delete)),
                      revision_aware=True, resume_read=partial(resume_read, name),
                      repair_files=repair_import if name == 'import' else None)


JOBS = {name: job(name) for name in SOURCES}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
