"""Fold the verified provider vector into the recording's existing source link."""
from .. import acousticbrainz_vibe_schema as schema
from ..schema_contract import MigrationError, table_clauses, tokens
from ..schema_inspection import definitions


COLUMN = schema.CLASSIFIERS + ' TEXT'
LINKS = 'rec_musical_links'
FK_OWNERS = (LINKS, schema.ESTIMATES)
UPDATE = 'acousticbrainz_classifiers_changed'
DELETE = 'acousticbrainz_classifiers_deleted'
RANK_INDEX = 'ix_acousticbrainz_vibe_rank'
SQL = {
    schema.ESTIMATES: schema.STORAGE[schema.ESTIMATES].replace(
        f'REFERENCES {schema.INPUTS}(recording_id)', 'REFERENCES recordings(id)'),
    RANK_INDEX: schema.STORAGE[RANK_INDEX],
    schema.CLASSIFIER_INDEX: f'''CREATE UNIQUE INDEX {schema.CLASSIFIER_INDEX}
        ON {LINKS}(recording_id) WHERE {schema.CLASSIFIERS} IS NOT NULL''',
    schema.SERVED: schema.LINKED_CLASSIFIER_VIEW,
    UPDATE: f'''CREATE TRIGGER {UPDATE} AFTER UPDATE OF {schema.CLASSIFIERS} ON {LINKS}
        WHEN OLD.{schema.CLASSIFIERS} IS NOT NULL AND NEW.{schema.CLASSIFIERS} IS NOT OLD.{schema.CLASSIFIERS}
        BEGIN DELETE FROM {schema.ESTIMATES} WHERE recording_id=OLD.recording_id; END''',
    DELETE: f'''CREATE TRIGGER {DELETE} AFTER DELETE ON {LINKS} WHEN OLD.{schema.CLASSIFIERS} IS NOT NULL
        BEGIN DELETE FROM {schema.ESTIMATES} WHERE recording_id=OLD.recording_id; END''',
}


def install(conn):
    conn.execute(f'ALTER TABLE {LINKS} ADD COLUMN {COLUMN}')
    conn.execute('''UPDATE rec_musical_links AS l SET acousticbrainz_classifiers=i.classifiers
        FROM acousticbrainz_vibe_inputs i JOIN rec_musical_sources s USING(source_key)
        WHERE l.recording_id=i.recording_id AND l.source_key=i.source_key AND lower(s.entity_id)=i.mbid
              AND s.provider='acousticbrainz' AND s.scope='recording' ''')
    conn.execute('''INSERT INTO rec_content_pending(identity_key)
        SELECT r.identity_key FROM acousticbrainz_vibe_inputs i JOIN recordings r ON r.id=i.recording_id
        WHERE NOT EXISTS(SELECT 1 FROM rec_musical_links l
                         WHERE l.recording_id=i.recording_id AND l.acousticbrainz_classifiers IS NOT NULL)
        ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1''')
    conn.execute(f'DROP VIEW {schema.SERVED}')
    conn.execute(f'DROP INDEX {RANK_INDEX}')
    conn.execute(f'ALTER TABLE {schema.ESTIMATES} RENAME TO previous_acousticbrainz_vibe_estimates')
    conn.execute(SQL[schema.ESTIMATES])
    conn.execute('''INSERT INTO acousticbrainz_vibe_estimates
        SELECT e.recording_id,e.vibe,e.score,e.inputs_version FROM previous_acousticbrainz_vibe_estimates e
        JOIN rec_musical_links l USING(recording_id) WHERE l.acousticbrainz_classifiers IS NOT NULL''')
    conn.execute('DROP TABLE previous_acousticbrainz_vibe_estimates')
    conn.execute(f'DROP TABLE {schema.INPUTS}')
    for name, sql in SQL.items():
        if name != schema.ESTIMATES:
            conn.execute(sql)


def validate(conn):
    actual = definitions(conn, (LINKS, schema.INPUTS, *SQL))
    if schema.INPUTS in actual:
        raise MigrationError('Retired AcousticBrainz classifier storage remains')
    if tokens(COLUMN) not in table_clauses(tokens(actual.get(LINKS))):
        raise MigrationError('AcousticBrainz link classifier column is missing')
    if (any(tokens(actual.get(name)) != tokens(sql) for name, sql in SQL.items() if name != schema.SERVED)
            or tokens(actual.get(schema.SERVED)) not in tuple(map(tokens, (
                schema.LINKED_CLASSIFIER_VIEW, schema.INDEXED_LINKED_CLASSIFIER_VIEW)))):
        raise MigrationError('Incompatible AcousticBrainz classifier links or estimates')
