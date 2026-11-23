"""Fold the selected choice projections into their publication rows."""
from .. import choice_learning_state, group_preview_work
from ..choice_learning_state import ATTRIBUTION, CONTEXT, GROUP, HEADER, MODE, SCALARS, _attribution_fields
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence
from .home_display_predecessor import release_group_context


NAME = 'compact_choice_learning_publications'
PUBLICATIONS = 'choice_learning_publications'
DESCRIPTORS = 'choice_interest_mix_descriptors'
DESCRIPTOR_INDEX = 'ix_choice_interest_descriptor'
IMMUTABLE = """CREATE TRIGGER choice_learning_publications_update
BEFORE UPDATE ON choice_learning_publications
BEGIN SELECT RAISE(ABORT,'Published choice learning is immutable'); END"""
RETIRED = (
    'prepared_choice_interest_mix_descriptors',
    'choice_learning_heads', 'choice_learning_group_roots',
    'choice_group_opportunities', 'choice_album_track_days', 'choice_album_interests',
    'choice_interest_features', 'choice_interest_tracks', 'choice_interest_contexts',
    'choice_group_estimates', 'choice_mode_estimates', 'choice_learning_exclusions',
    'choice_learning_parameters',
)
FOREIGN_KEY_OWNERS = (PUBLICATIONS, DESCRIPTORS, *RETIRED)
PUBLICATION_SQL = """CREATE TABLE choice_learning_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL CHECK(id>0),
    surface TEXT NOT NULL UNIQUE CHECK(surface IN ('choice_bandit','group_choice_bandit','choice_interests')),
    built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('integer','real') AND abs(built_at)<=1.7976931348623157e308)),
    document TEXT NOT NULL CHECK(json_valid(document) AND json_type(document)='object')
)"""
DESCRIPTOR_SQL = """CREATE TABLE choice_interest_mix_descriptors (
    publication_id INTEGER NOT NULL REFERENCES choice_learning_publications(id) ON DELETE CASCADE,
    context_position INTEGER NOT NULL,
    descriptor_id INTEGER NOT NULL REFERENCES mix_choice_descriptors(id),
    PRIMARY KEY(publication_id,context_position)
) WITHOUT ROWID"""


def installed(conn):
    return any(row[1] == 'document' for row in conn.execute(f'PRAGMA main.table_info({PUBLICATIONS})'))


def install(conn, _context):
    publications = conn.execute('SELECT p.id,p.surface,p.built_at FROM choice_learning_publications p '
                                'JOIN choice_learning_heads h ON h.publication_id=p.id ORDER BY p.id').fetchall()
    references = conn.execute('SELECT d.publication_id,d.context_position,d.descriptor_id '
                              'FROM choice_interest_mix_descriptors d '
                              'JOIN choice_learning_heads h ON h.publication_id=d.publication_id').fetchall()
    sequence = _sequence(conn, PUBLICATIONS)
    conn.execute(PUBLICATION_SQL.replace(PUBLICATIONS, 'choice_learning_replacement', 1))
    for identity, surface, built_at in publications:
        document = read_previous(conn, surface, identity)
        conn.execute('INSERT INTO choice_learning_replacement VALUES (?,?,?,?)',
                     (identity, surface, built_at, choice_learning_state.encode(document)))

    abandoned = conn.execute('SELECT p.id FROM choice_learning_publications p WHERE NOT EXISTS('
                             'SELECT 1 FROM choice_learning_heads h WHERE h.publication_id=p.id)').fetchall()
    conn.execute(f'DROP TABLE {DESCRIPTORS}')
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    # Only selected publications retain their existing evidence ownership.
    for identity, in abandoned:
        release_group_context(conn, 'prepared', f'choice-learning:{identity}')
    conn.execute(f'DROP TABLE {PUBLICATIONS}')
    conn.execute(f'ALTER TABLE choice_learning_replacement RENAME TO {PUBLICATIONS}')
    _preserve_sequence(conn, PUBLICATIONS, sequence)
    conn.execute(DESCRIPTOR_SQL)
    conn.executemany(f'INSERT INTO {DESCRIPTORS} VALUES (?,?,?)', references)
    conn.execute(f'CREATE INDEX {DESCRIPTOR_INDEX} ON {DESCRIPTORS}(descriptor_id)')
    conn.execute(IMMUTABLE)
    group_preview_work.install_guards(conn, sources={PUBLICATIONS})
    conn.execute("UPDATE group_preview_work SET invalid=1 WHERE invalid=0")


def validate(conn):
    if not installed(conn) or definitions(conn, RETIRED):
        raise MigrationError('Incomplete choice learning publication consolidation')


def install_attribution(conn, context):
    for column, type_ in zip(ATTRIBUTION, ('TEXT', 'INTEGER', 'REAL', 'INTEGER'), strict=True):
        conn.execute(f'ALTER TABLE choice_interest_contexts ADD COLUMN {column} {type_}')


def validate_attribution(conn):
    columns = {row[1]: row[2] for row in conn.execute('PRAGMA table_info(choice_interest_contexts)')}
    if tuple(columns.get(column) for column in ATTRIBUTION) != ('TEXT', 'INTEGER', 'REAL', 'INTEGER'):
        raise MigrationError('Incompatible choice interest attribution fields')


def _view(row, fields):
    return {field: row[index + 1] for index, field in enumerate(fields) if row[0] & (1 << index)}


def _read_attribution(fields, values):
    return {field: bool(value) if field == 'sustained' else value
            for field, value in zip(fields, values, strict=True) if value is not None}


def read_previous(conn, surface, identity):
    columns = ','.join('p.schema_version' if field == 'schema' else 'p.' + field for field in SCALARS)
    row = conn.execute('SELECT p.id,p.field_mask,' + columns +
                       ' FROM choice_learning_publications p WHERE p.id=?', (identity,)).fetchone()
    identity, mask = row[:2]
    result = _view(row[1:], SCALARS)
    def present(field):
        return bool(mask & (1 << HEADER.index(field)))
    for field, owner in (('parameters', 'choice_learning_parameters'), ('excluded', 'choice_learning_exclusions')):
        if present(field):
            result[field] = dict(conn.execute(f'SELECT name,value FROM {owner} WHERE publication_id=? ORDER BY name', (identity,)))
    if present('rows'):
        fields, owner = (GROUP, 'choice_group_estimates') if surface == 'group_choice_bandit' else (MODE, 'choice_mode_estimates')
        scalar = fields[:-1] if surface == 'group_choice_bandit' else fields
        menus = {}
        if surface == 'group_choice_bandit':
            for position, opportunity in conn.execute(
                    'SELECT estimate_position,opportunity_id FROM choice_group_opportunities '
                    'WHERE publication_id=? ORDER BY estimate_position,position', (identity,)).fetchall():
                menus.setdefault(position, []).append(opportunity)
        result['rows'] = []
        for position, *record in conn.execute(f'SELECT position,field_mask,{",".join(scalar)} FROM {owner} '
                                               'WHERE publication_id=? ORDER BY position', (identity,)).fetchall():
            value = _view(record, scalar)
            if surface == 'group_choice_bandit' and record[0] & (1 << GROUP.index('menus')):
                value['menus'] = menus.get(position, [])
            result['rows'].append(value)
    if present('contexts'):
        result['contexts'] = []
        scalar = CONTEXT[:-2]
        attribution = _attribution_fields(result)
        descriptors = dict(conn.execute('SELECT context_position,descriptor_id FROM choice_interest_mix_descriptors WHERE publication_id=?', (identity,)))
        for position, *record in conn.execute(f'SELECT position,field_mask,{",".join((*scalar, *attribution))},feature_null FROM choice_interest_contexts '
                                               'WHERE publication_id=? ORDER BY position', (identity,)):
            value = _view(record, scalar)
            value.update(_read_attribution(attribution, record[1 + len(scalar):-1]))
            if record[0] & (1 << CONTEXT.index('feature')):
                value['feature'] = None if record[-1] else [entry[0] for entry in conn.execute('SELECT value FROM choice_interest_features '
                    'WHERE publication_id=? AND context_position=? ORDER BY dimension', (identity, position))]
            if record[0] & (1 << CONTEXT.index('keys')):
                value['keys'] = [entry[0] for entry in conn.execute('SELECT raw_key FROM choice_interest_tracks '
                    'WHERE publication_id=? AND context_position=? ORDER BY position', (identity, position))]
            if position in descriptors:
                value['descriptor_id'] = descriptors[position]
            result['contexts'].append(value)
    if present('album_track_days'):
        result['album_track_days'] = {browse: dict(conn.execute('SELECT raw_key,days FROM choice_album_track_days '
            'WHERE publication_id=? AND album_position=? ORDER BY position', (identity, position)))
            for position, browse in conn.execute('SELECT position,browse_id FROM choice_album_interests '
                'WHERE publication_id=? ORDER BY position', (identity,))}
    return result
