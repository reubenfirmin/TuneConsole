"""Keep model heads on Vibe definitions and coordinate rebuilds on the work queue."""
import re

from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from ..vibe_definitions import JOINT_HEAD, JOINT_OUTCOME


NAME = 'own_vibe_worker_state'
OWNERS = ('vibe_definitions', 'rec_vibe_pending')
OLD_HEAD = 'rec_vibe_joint_head_'
OLD_OUTCOME = 'rec_vibe_joint_state_'
OLD_REVISION = 'rec_vibe_joint_revision'
OLD_POLICY = 'rec_vibe_rebuild_policy'
OLD_ACOUSTICBRAINZ = 'rec_acousticbrainz_vibe_model'
BUMP = 'UPDATE rec_vibe_pending SET evidence_revision=evidence_revision+1 WHERE position=0;'
_PREVIOUS_BUMP = re.compile(
    r"INSERT INTO settings\(key,value\) VALUES \('rec_vibe_joint_revision','1'\)\s*"
    r"ON CONFLICT\(key\) DO UPDATE SET value=CAST\(CAST\(value AS INTEGER\)\+1 AS TEXT\);")


def _previous_triggers(conn):
    return dict(conn.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='trigger' AND instr(sql,?) "
                             "ORDER BY name", (OLD_REVISION,)))


def predecessor_objects(conn):
    schema = definitions(conn, OWNERS)
    return [*(schema[owner] for owner in OWNERS), *_previous_triggers(conn).values()]


def install(conn, _context):
    conn.execute(f'ALTER TABLE vibe_definitions ADD COLUMN {JOINT_HEAD}')
    conn.execute(f'ALTER TABLE vibe_definitions ADD COLUMN {JOINT_OUTCOME}')
    conn.execute("""UPDATE vibe_definitions SET
        joint_head=(SELECT value FROM settings WHERE key=? || vibe),
        joint_outcome=(SELECT nullif(value,'') FROM settings WHERE key=? || vibe)
    """, (OLD_HEAD, OLD_OUTCOME))

    conn.execute("""ALTER TABLE rec_vibe_pending ADD COLUMN evidence_revision INTEGER
        CHECK((position>0 AND group_key<>'*maintenance' AND evidence_revision IS NULL)
            OR (position=0 AND group_key='*maintenance' AND typeof(evidence_revision)='integer'
                AND evidence_revision>=0))""")
    conn.execute("""ALTER TABLE rec_vibe_pending ADD COLUMN rebuild_policy TEXT
        CHECK(rebuild_policy IS NULL OR position=0)""")
    conn.execute("""ALTER TABLE rec_vibe_pending ADD COLUMN acousticbrainz_fit TEXT
        CHECK(acousticbrainz_fit IS NULL OR (position=0 AND json_type(acousticbrainz_fit) IS 'object'))""")
    conn.execute("""INSERT INTO rec_vibe_pending(position,group_key,evidence_revision,rebuild_policy,acousticbrainz_fit)
        SELECT 0,'*maintenance',coalesce((SELECT CAST(value AS INTEGER) FROM settings WHERE key=?),0),
            (SELECT nullif(value,'') FROM settings WHERE key=?),
            (SELECT nullif(value,'') FROM settings WHERE key=?)
    """, (OLD_REVISION, OLD_POLICY, OLD_ACOUSTICBRAINZ))
    for name, sql in _previous_triggers(conn).items():
        replacement, count = _PREVIOUS_BUMP.subn(BUMP, sql)
        if count != 1:
            raise MigrationError(f'Unknown Vibe evidence revision trigger: {name}')
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(replacement)
    conn.execute('DELETE FROM settings WHERE key IN (?,?,?) OR key GLOB ? OR key GLOB ?',
                 (OLD_REVISION, OLD_POLICY, OLD_ACOUSTICBRAINZ, OLD_HEAD + '*', OLD_OUTCOME + '*'))


def validate(conn):
    schema = definitions(conn, OWNERS)
    if ('joint_outcome' not in tokens(schema.get('vibe_definitions'))
            or 'evidence_revision' not in tokens(schema.get('rec_vibe_pending'))
            or not conn.execute("SELECT 1 FROM rec_vibe_pending WHERE position=0 AND group_key='*maintenance' "
                                "AND evidence_revision IS NOT NULL").fetchone()):
        raise MigrationError('Incomplete Vibe worker state storage')
