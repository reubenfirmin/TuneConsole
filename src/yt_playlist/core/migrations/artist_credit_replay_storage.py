"""Keep artist delivery keys on their captured credits after active use ends."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'keep_replay_state_on_artist_credit_observations'
RETIRED = 'artist_credit_replays'
DELIVERY_GUARD = 'immutable_credit_metadata_delivery'
REPLAY_GUARD = 'retained_artist_credit_replay'
INDEX = 'ix_artist_credit_active_observation'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND (tbl_name=? OR name IN (?,?)) ORDER BY type,name',
        (RETIRED, 'artist_credit_observations', DELIVERY_GUARD))]


def install(conn, _context):
    if conn.execute('SELECT 1 FROM artist_credit_replays r JOIN artist_credit_observations o '
                    'ON o.id=r.observation_id WHERE (o.entry_id,o.source,o.delivery_key,o.credit_id) '
                    'IS NOT (r.entry_id,r.source,r.delivery_key,r.credit_id) LIMIT 1').fetchone():
        raise MigrationError('An artist replay and its observation have different captured credits')
    conn.execute('ALTER TABLE artist_credit_observations ADD COLUMN metadata_bound INTEGER NOT NULL DEFAULT 0 '
                 'CHECK(metadata_bound IN (0,1) AND (metadata_bound=0 OR delivery_key IS NOT NULL))')
    conn.execute('ALTER TABLE artist_credit_observations ADD COLUMN retired INTEGER NOT NULL DEFAULT 0 '
                 'CHECK(retired IN (0,1) AND (retired=0 OR (delivery_key IS NOT NULL '
                 'AND is_selected=0 AND is_capture_head=0 AND metadata_delivery_id IS NULL)))')
    conn.execute('UPDATE artist_credit_observations AS o SET metadata_bound=1 WHERE metadata_delivery_id IS NOT NULL '
                 'OR id IN (SELECT observation_id FROM artist_credit_replays WHERE metadata_bound=1)')
    conn.execute('INSERT INTO artist_credit_observations(id,entry_id,source,delivery_key,credit_id,processing_count,'
                 'capture_kind,metadata_bound,retired) SELECT r.observation_id,r.entry_id,r.source,r.delivery_key,'
                 "r.credit_id,1,'provider_delivery',r.metadata_bound,1 FROM artist_credit_replays r "
                 'WHERE NOT EXISTS(SELECT 1 FROM artist_credit_observations o WHERE o.id=r.observation_id)')
    conn.execute(f'DROP TRIGGER {DELIVERY_GUARD}')
    conn.execute(f'CREATE TRIGGER {DELIVERY_GUARD} BEFORE UPDATE OF metadata_delivery_id ON artist_credit_observations '
                 'WHEN NOT (NEW.retired=1 AND OLD.retired=0 AND NEW.metadata_delivery_id IS NULL) '
                 "BEGIN SELECT RAISE(ABORT,'Artist capture delivery is immutable'); END")
    conn.execute('CREATE TRIGGER immutable_artist_credit_replay_state '
                 'BEFORE UPDATE OF metadata_bound,retired ON artist_credit_observations '
                 'WHEN NEW.metadata_bound<>OLD.metadata_bound OR NEW.retired<OLD.retired '
                 "BEGIN SELECT RAISE(ABORT,'Artist delivery replay identity is immutable'); END")
    conn.execute(f'CREATE TRIGGER {REPLAY_GUARD} BEFORE DELETE ON artist_credit_observations '
                 'WHEN OLD.delivery_key IS NOT NULL AND OLD.is_selected=0 AND OLD.is_capture_head=0 '
                 "BEGIN SELECT RAISE(ABORT,'Artist delivery retains its replay identity'); END")
    conn.execute(f'CREATE INDEX {INDEX} ON artist_credit_observations(id) WHERE retired=0')
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    names = {DELIVERY_GUARD, REPLAY_GUARD, INDEX, 'immutable_artist_credit_replay_state'}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Artist deliveries were not folded into credit observations')
