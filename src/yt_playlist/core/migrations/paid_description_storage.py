"""Keep paid requests and their metering receipts in one indexed owner."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence
from .paid_usage import FILE_CHECK, TIME_CHECK
from .track_source_cutover import _replace_table


NAME = 'own_paid_description_metering'
TABLE = 'album_description_requests'
RETIRED = 'provider_usage'
MARKER = 'ix_paid_description_daily_usage'
USAGE_FIELDS = ('request_id', 'attempt_number', 'reservation_key', 'provider', 'period', 'usage_kind',
                'units', 'evidence_filename', 'evidence_digest')

SQL = f'''CREATE TABLE {TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER REFERENCES albums(id),
    request_key TEXT UNIQUE CHECK(length(request_key)>0),
    policy_version TEXT REFERENCES recommendation_policies(version),
    resolution_status TEXT CHECK(resolution_status IN ('resolved','legacy_unresolved')),
    legacy_identity TEXT UNIQUE,
    status TEXT CHECK(status IN ('pending','claimed','sent','uncertain','ready','unknown','invalid_response',
                                 'auth','balance','rate_limit','unavailable','paused')),
    claim_token TEXT UNIQUE,
    lease_until REAL NOT NULL DEFAULT 0 {TIME_CHECK.format(name='lease_until')},
    attempt_count INTEGER CHECK(attempt_count IS NULL OR (typeof(attempt_count)='integer' AND attempt_count>=0)),
    credential_key TEXT,
    authorized_at REAL CHECK(authorized_at IS NULL OR (typeof(authorized_at) IN ('real','integer')
                             AND abs(authorized_at)<=1.7976931348623157e308)),
    retry_at REAL NOT NULL DEFAULT 0 {TIME_CHECK.format(name='retry_at')},
    created_at REAL NOT NULL {TIME_CHECK.format(name='created_at')},
    updated_at REAL CHECK(updated_at IS NULL OR (typeof(updated_at) IN ('real','integer')
                          AND abs(updated_at)<=1.7976931348623157e308)),
    outcome_json TEXT CHECK(outcome_json IS NULL OR (length(CAST(outcome_json AS BLOB))<=262144
                            AND json_valid(outcome_json) AND json_type(outcome_json)='object')),
    result_created_at REAL,
    result_order INTEGER,
    usage_id INTEGER UNIQUE CHECK(usage_id IS NULL OR typeof(usage_id)='integer'),
    request_id INTEGER REFERENCES {TABLE}(id),
    attempt_number INTEGER CHECK(attempt_number IS NULL OR (typeof(attempt_number)='integer' AND attempt_number>0)),
    reservation_key TEXT UNIQUE CHECK(length(reservation_key)>0),
    provider TEXT CHECK(provider='deepseek'),
    period INTEGER CHECK(period IS NULL OR typeof(period)='integer'),
    usage_kind TEXT CHECK(usage_kind IN ('requests','prompt_tokens','completion_tokens','total_tokens')),
    units INTEGER CHECK(units IS NULL OR (typeof(units)='integer' AND units>=0)),
    evidence_filename TEXT,
    evidence_digest TEXT,
    {FILE_CHECK},
    CHECK((usage_id IS NULL AND request_key IS NOT NULL AND resolution_status IS NOT NULL
           AND status IS NOT NULL AND updated_at IS NOT NULL
           AND {' AND '.join(field + ' IS NULL' for field in USAGE_FIELDS)})
       OR (usage_id IS NOT NULL AND reservation_key IS NOT NULL AND provider IS NOT NULL AND period IS NOT NULL
           AND usage_kind IS NOT NULL AND units IS NOT NULL
           AND album_id IS NULL AND request_key IS NULL AND policy_version IS NULL AND resolution_status IS NULL
           AND legacy_identity IS NULL AND status IS NULL AND claim_token IS NULL AND lease_until=0
           AND attempt_count IS NULL AND credential_key IS NULL AND authorized_at IS NULL AND retry_at=0
           AND updated_at IS NULL AND outcome_json IS NULL AND result_created_at IS NULL AND result_order IS NULL)),
    CHECK(status<>'ready' OR (outcome_json IS NOT NULL AND result_created_at IS NOT NULL AND result_order IS NOT NULL)),
    CHECK(usage_id IS NOT NULL OR
          (resolution_status='resolved' AND album_id IS NOT NULL AND policy_version IS NOT NULL
           AND legacy_identity IS NULL AND attempt_count IS NOT NULL) OR
          (resolution_status='legacy_unresolved' AND album_id IS NULL AND policy_version IS NULL
           AND legacy_identity IS NOT NULL AND length(legacy_identity)>0 AND attempt_count IS NULL)),
    CHECK((status IN ('claimed','sent','uncertain') AND claim_token IS NOT NULL AND length(claim_token)>0
           AND credential_key IS NOT NULL AND lease_until>=0) OR
          (status NOT IN ('claimed','sent','uncertain') AND claim_token IS NULL AND lease_until=0)),
    CHECK(status NOT IN ('sent','uncertain') OR authorized_at IS NOT NULL),
    CHECK(usage_id IS NULL OR (request_id IS NOT NULL AND evidence_filename IS NULL) OR
          (request_id IS NULL AND attempt_number IS NULL AND usage_kind='requests' AND evidence_filename IS NOT NULL)),
    UNIQUE(album_id,policy_version),
    UNIQUE(request_id,attempt_number,usage_kind)
)'''


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?) AND sql IS NOT NULL ORDER BY type,name',
        (TABLE, RETIRED))]


def install(conn, _context):
    request_objects = dict(conn.execute('SELECT name,sql FROM sqlite_schema WHERE tbl_name=? '
                                       "AND type IN ('trigger','index') AND sql IS NOT NULL", (TABLE,)))
    sequence = _sequence(conn, TABLE)
    _replace_table(conn, TABLE, SQL, expressions={field: 'NULL' for field in ('usage_id', *USAGE_FIELDS)})
    _preserve_sequence(conn, TABLE, sequence)
    fields = ','.join((*USAGE_FIELDS, 'created_at'))
    conn.execute(f'INSERT INTO {TABLE}(usage_id,{fields}) SELECT id,{fields} FROM {RETIRED} ORDER BY id')
    conn.execute(f'DROP TABLE {RETIRED}')

    ownership = request_objects['album_description_owner_immutable']
    request_objects['album_description_owner_immutable'] = ownership.replace(
        'WHEN ', 'WHEN NEW.usage_id IS NOT OLD.usage_id OR (OLD.usage_id IS NULL AND (', 1
    ).replace(' BEGIN', ')) BEGIN', 1)
    for statement in request_objects.values():
        conn.execute(statement)

    conn.execute(f'CREATE INDEX {MARKER} ON {TABLE}(provider,period,usage_kind,units) WHERE usage_id IS NOT NULL')
    conn.execute(f'CREATE UNIQUE INDEX ix_paid_description_period_total ON {TABLE}(provider,period) '
                 'WHERE usage_id IS NOT NULL AND request_id IS NULL')
    conn.execute(f'CREATE UNIQUE INDEX ix_paid_description_unattributed_result ON {TABLE}(request_id,usage_kind) '
                 'WHERE request_id IS NOT NULL AND attempt_number IS NULL')
    conn.execute(f'''CREATE TRIGGER paid_description_usage_attempt BEFORE INSERT ON {TABLE}
        WHEN NEW.request_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {TABLE} r WHERE r.id=NEW.request_id AND
            ((r.resolution_status='resolved' AND NEW.attempt_number BETWEEN 1 AND r.attempt_count) OR
             (r.resolution_status='legacy_unresolved' AND NEW.attempt_number IS NULL)))
        BEGIN SELECT RAISE(ABORT,'Usage requires its actual request attempt'); END''')
    immutable = ('id', 'usage_id', 'created_at', *(field for field in USAGE_FIELDS if field != 'evidence_filename'))
    conn.execute(f'''CREATE TRIGGER paid_description_usage_immutable BEFORE UPDATE ON {TABLE}
        WHEN OLD.usage_id IS NOT NULL AND (OLD.evidence_filename IS NULL OR NEW.evidence_filename IS NULL
            OR {' OR '.join(f'NEW.{field} IS NOT OLD.{field}' for field in immutable)})
        BEGIN SELECT RAISE(ABORT,'Metered provider usage is immutable'); END''')


def validate(conn):
    if set(definitions(conn, (MARKER, RETIRED))) != {MARKER}:
        raise MigrationError('Incompatible paid description metering storage')
