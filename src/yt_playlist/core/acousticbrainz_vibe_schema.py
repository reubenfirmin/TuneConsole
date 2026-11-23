"""Storage for verified classifier inputs and their current fitted Vibe scores."""
from .audio_vibe_contract import ACOUSTICBRAINZ_ESTIMATE
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions

INPUTS = 'acousticbrainz_vibe_inputs'
ESTIMATES = 'acousticbrainz_vibe_estimates'
SERVED = 'served_vibe_fits'
CLASSIFIERS = 'acousticbrainz_classifiers'
CLASSIFIER_INDEX = 'ix_musical_links_acousticbrainz_classifiers'
MATCH_INDEX = 'ix_acousticbrainz_musicbrainz_match'
INDEXED_VIEW_CHANGE = 'seek_acousticbrainz_vibe_identifiers'
LINKED_CLASSIFIERS = f"""SELECT l.recording_id,l.source_key,lower(s.entity_id) AS mbid,l.{CLASSIFIERS} AS classifiers
    FROM rec_musical_links l JOIN rec_musical_sources s USING(source_key)
    WHERE l.{CLASSIFIERS} IS NOT NULL AND s.provider='acousticbrainz' AND s.scope='recording'"""
IDENTIFIER_MATCH = """EXISTS (
    SELECT 1 FROM recording_identifiers identifier{index}
    WHERE identifier.scheme='musicbrainz' AND identifier.entity_type='recording'
      AND identifier.recording_id=r.id AND identifier.source='musicbrainz'
      AND identifier.status<>'conflicting' AND {identifier_match})"""
INITIAL_VERIFIED_MATCH = 'lower(r.mb_recording_id)=i.mbid AND ' + IDENTIFIER_MATCH.format(
    index='', identifier_match='identifier.value IN (r.mb_recording_id,i.mbid)')
# Each catalog entry must agree while recording-level metadata is being prepared.
CATALOG_MATCH = """(SELECT MIN(lower(c.mb_recording_id) IS i.mbid)
    FROM catalog_tracks c WHERE c.recording_id=r.id)=1"""


def _verified_match(index=''):
    return CATALOG_MATCH + ' AND ' + IDENTIFIER_MATCH.format(
        index=index, identifier_match='identifier.value COLLATE NOCASE=i.mbid')

STORAGE = {
    INPUTS: f'''CREATE TABLE {INPUTS} (
        recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL, mbid TEXT NOT NULL, classifiers TEXT NOT NULL,
        FOREIGN KEY(source_key,recording_id) REFERENCES rec_musical_links(source_key,recording_id) ON DELETE CASCADE)''',
    ESTIMATES: f'''CREATE TABLE {ESTIMATES} (
        recording_id INTEGER NOT NULL REFERENCES {INPUTS}(recording_id) ON DELETE CASCADE,
        vibe TEXT NOT NULL CHECK(vibe IN ('cruise','release','relax','uplift','energize','concentrate')),
        score REAL NOT NULL CHECK(score BETWEEN 0 AND 1), inputs_version TEXT NOT NULL,
        PRIMARY KEY(recording_id,vibe)) WITHOUT ROWID''',
    'ix_acousticbrainz_vibe_rank': f'CREATE INDEX ix_acousticbrainz_vibe_rank ON {ESTIMATES}(vibe,score DESC,recording_id)',
}
MATCH_INDEX_SQL = f"""CREATE INDEX {MATCH_INDEX}
    ON recording_identifiers(recording_id,value COLLATE NOCASE)
    WHERE scheme='musicbrainz' AND entity_type='recording'
      AND source='musicbrainz' AND status<>'conflicting'"""


def _served_view(columns, match, inputs=INPUTS):
    return f'''CREATE VIEW {SERVED} AS
        SELECT {columns}
        FROM vibe_recording_fits WHERE source NOT IN ('provider_measurement','heuristic')
        UNION ALL
        SELECT e.recording_id,e.vibe,e.score,1.0,'{ACOUSTICBRAINZ_ESTIMATE}',NULL,NULL,e.inputs_version,NULL,NULL,NULL,NULL
        FROM {ESTIMATES} e JOIN {inputs} i USING(recording_id)
        JOIN recordings r ON r.id=e.recording_id WHERE {match}'''


INITIAL_VIEW = _served_view('*', 'lower(r.mb_recording_id)=i.mbid')
FIT_COLUMNS = (
    'recording_id,vibe,score,support,source,measurement_id,prediction_id,inputs_version,'
    'artist_key,subgenre_key,recordings,variance')
VERIFIED_VIEW = _served_view(FIT_COLUMNS, INITIAL_VERIFIED_MATCH)
VERIFIED_MATCH = _verified_match()
SELECTED_RECORDING_VIEW = _served_view(FIT_COLUMNS, VERIFIED_MATCH)
LINKED_CLASSIFIER_VIEW = _served_view(FIT_COLUMNS, VERIFIED_MATCH, '(' + LINKED_CLASSIFIERS + ')')
# Use the recording and case-insensitive identifier together. The general
# identifier index can scan every MusicBrainz identifier for each estimate.
INDEXED_LINKED_CLASSIFIER_VIEW = _served_view(
    FIT_COLUMNS, _verified_match(f' INDEXED BY {MATCH_INDEX}'), '(' + LINKED_CLASSIFIERS + ')')


def uses_link_classifiers(conn):
    return CLASSIFIER_INDEX in definitions(conn, (CLASSIFIER_INDEX,))


def _enqueue(side, condition):
    return f'''
        INSERT INTO rec_content_pending(identity_key)
        SELECT identity_key FROM recordings WHERE id={side}.recording_id
            AND {side}.source='musicbrainz' AND {side}.scheme='musicbrainz'
            AND {side}.entity_type='recording' AND {side}.status<>'conflicting'
            {condition.format(side=side)}
        ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1;
    '''


def _identifier_triggers(condition):
    for event, sides in (('insert', ('NEW',)), ('update', ('OLD', 'NEW')), ('delete', ('OLD',))):
        name = 'acousticbrainz_vibe_identifier_' + event
        update = ' OF recording_id,source,scheme,entity_type,status,value' if event == 'update' else ''
        writes = ''.join(_enqueue(side, condition) for side in sides)
        yield name, f'CREATE TRIGGER {name} AFTER {event.upper()}{update} ON recording_identifiers BEGIN {writes} END'


INITIAL_IDENTIFIER_TRIGGERS = dict(_identifier_triggers('AND {side}.value IN (mb_recording_id,lower(mb_recording_id))'))
IDENTIFIER_TRIGGERS = dict(_identifier_triggers(''))


def install(conn, _context=None):
    for sql in STORAGE.values():
        conn.execute(sql)
    conn.execute(INITIAL_VIEW)


def _validate_relations(conn, expected):
    from . import content_work

    moved = bool(definitions(conn, (content_work.INDEX,)))
    owned = bool(definitions(conn, (content_work.RECORDING_INDEX,)))
    observed = definitions(conn, expected)
    for name, allowed in expected.items():
        if moved:
            allowed = tuple(content_work.upgrade_enqueue_sql(sql) for sql in allowed)
        if owned:
            allowed = tuple(content_work.upgrade_recording_enqueue_sql(sql) for sql in allowed)
        sql = observed.get(name)
        if sql is None or tokens(sql) not in tuple(map(tokens, allowed)):
            raise MigrationError(f'Incompatible AcousticBrainz Vibe relation: {name}')


def validate(conn):
    # Some databases installed the verified view before it had its own migration.
    expected = {name: (sql,) for name, sql in STORAGE.items()}
    expected[SERVED] = INITIAL_VIEW, VERIFIED_VIEW, SELECTED_RECORDING_VIEW
    _validate_relations(conn, expected)


def _install_verification(conn, view, triggers):
    conn.execute(f'DROP VIEW {SERVED}')
    conn.execute(view)
    for name, sql in triggers.items():
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
        conn.execute(sql)


def install_verification(conn, _context=None):
    _install_verification(conn, VERIFIED_VIEW, INITIAL_IDENTIFIER_TRIGGERS)


def validate_verification(conn):
    expected = {name: (sql, IDENTIFIER_TRIGGERS[name]) for name, sql in INITIAL_IDENTIFIER_TRIGGERS.items()}
    expected[SERVED] = VERIFIED_VIEW, SELECTED_RECORDING_VIEW
    _validate_relations(conn, expected)


def install_selected_recording(conn, _context):
    _install_verification(conn, SELECTED_RECORDING_VIEW, IDENTIFIER_TRIGGERS)


def validate_selected_recording(conn):
    expected = {name: (sql,) for name, sql in IDENTIFIER_TRIGGERS.items()}
    expected[SERVED] = ((LINKED_CLASSIFIER_VIEW, INDEXED_LINKED_CLASSIFIER_VIEW) if uses_link_classifiers(conn)
                        else (SELECTED_RECORDING_VIEW,))
    _validate_relations(conn, expected)


def install_match_index(conn, _context):
    conn.execute(MATCH_INDEX_SQL)


def validate_match_index(conn):
    _validate_relations(conn, {MATCH_INDEX: (MATCH_INDEX_SQL,)})


def install_indexed_linked_classifier_view(conn, _context):
    conn.execute(f'DROP VIEW {SERVED}')
    conn.execute(INDEXED_LINKED_CLASSIFIER_VIEW)


def validate_indexed_linked_classifier_view(conn):
    validate_match_index(conn)
    _validate_relations(conn, {SERVED: (INDEXED_LINKED_CLASSIFIER_VIEW,)})
