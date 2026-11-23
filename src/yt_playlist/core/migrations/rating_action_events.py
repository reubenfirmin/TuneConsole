"""Retain historical rating actions on their existing event owner."""
from .. import rating_actions, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_confirmed_rating_actions_on_player_events'
RETIRED = 'listening_rating_actions'
BY_ID = 'uq_confirmed_rating_action_id'
BY_TIME = 'ix_confirmed_rating_action_time'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL "
        "AND (name='player_events' OR tbl_name=?) ORDER BY type,name", (RETIRED,))]


def install(conn, _context):
    conn.execute(f"""ALTER TABLE player_events ADD COLUMN rating_action_id INTEGER CHECK(
        CASE WHEN rating_action_id IS NULL THEN kind<>'{rating_actions.KIND}' ELSE
            COALESCE(kind='{rating_actions.KIND}' AND typeof(rating_action_id)='integer'
            AND video_id IS NOT NULL AND json_type(payload,'$.status')='text'
            AND json_type(payload,'$.source')='text',0) END)""")
    conn.execute(f"""INSERT INTO player_events(account_id,kind,video_id,payload,at,rating_action_id)
        SELECT account_id,?,video_id,json_object('previous_status',previous_status,'status',status,'source',source),at,id
        FROM {RETIRED} ORDER BY id""", (rating_actions.KIND,))
    serial = conn.execute(f"""SELECT max(COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0),
                                       COALESCE((SELECT max(id) FROM {RETIRED}),0))""", (RETIRED,)).fetchone()[0]
    if serial:
        worker_settings.save(conn, rating_actions.SERIAL, str(serial))
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'CREATE UNIQUE INDEX {BY_ID} ON player_events(rating_action_id) WHERE rating_action_id IS NOT NULL')
    conn.execute(f'CREATE INDEX {BY_TIME} ON player_events(at,rating_action_id) WHERE rating_action_id IS NOT NULL')
    conn.execute(f"""CREATE VIEW {rating_actions.VIEW} AS SELECT rating_action_id AS id,account_id,video_id,
        payload->>'previous_status' AS previous_status,payload->>'status' AS status,payload->>'source' AS source,at
        FROM player_events WHERE rating_action_id IS NOT NULL""")


def validate(conn):
    names = {rating_actions.VIEW, BY_ID, BY_TIME}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Rating actions were not moved to player events')
