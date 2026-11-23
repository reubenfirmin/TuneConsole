"""Fold the old provider-wide receipt into existing accounting and access owners."""
from .track_source_cutover import _replace_table
from ..schema_contract import MigrationError


NAME = 'fold_paid_description_usage'
OLD = 'legacy_album_description_usage'
OWNERS = ('provider_usage', 'album_description_access')
RETIRED_INDEX = 'ix_provider_usage_legacy_count'

FILE_CHECK = """CHECK((evidence_filename IS NULL AND evidence_digest IS NULL) OR
    (evidence_filename IS NOT NULL AND length(evidence_filename)>0 AND evidence_digest IS NOT NULL
    AND length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'))"""
TIME_CHECK = "CHECK(typeof({name}) IN ('real','integer') AND abs({name})<=1.7976931348623157e308)"

USAGE = f"""CREATE TABLE provider_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER REFERENCES album_description_requests(id),
    attempt_number INTEGER CHECK(attempt_number IS NULL OR (typeof(attempt_number)='integer' AND attempt_number>0)),
    reservation_key TEXT NOT NULL UNIQUE CHECK(length(reservation_key)>0),
    provider TEXT NOT NULL CHECK(provider='deepseek'),
    period INTEGER NOT NULL CHECK(typeof(period)='integer'),
    usage_kind TEXT NOT NULL CHECK(usage_kind IN ('requests','prompt_tokens','completion_tokens','total_tokens')),
    units INTEGER NOT NULL CHECK(typeof(units)='integer' AND units>=0),
    created_at REAL NOT NULL {TIME_CHECK.format(name='created_at')},
    evidence_filename TEXT,
    evidence_digest TEXT,
    {FILE_CHECK},
    CHECK((request_id IS NOT NULL AND evidence_filename IS NULL) OR
          (request_id IS NULL AND attempt_number IS NULL AND usage_kind='requests' AND evidence_filename IS NOT NULL)),
    UNIQUE(request_id,attempt_number,usage_kind)
)"""

ACCESS = f"""CREATE TABLE album_description_access (
    provider TEXT NOT NULL CHECK(provider='deepseek'),
    scope TEXT NOT NULL,
    credential_key TEXT NOT NULL,
    status TEXT NOT NULL,
    retry_at REAL NOT NULL {TIME_CHECK.format(name='retry_at')},
    updated_at REAL NOT NULL {TIME_CHECK.format(name='updated_at')},
    evidence_filename TEXT,
    evidence_digest TEXT,
    {FILE_CHECK},
    CHECK((scope='host' AND credential_key='' AND status='unavailable') OR
          (scope='credential' AND length(credential_key)>0 AND status IN ('auth','balance','rate_limit')) OR
          (scope='unattributed_request' AND credential_key='' AND status IN ('busy','uncertain'))),
    PRIMARY KEY(provider,scope,credential_key)
) WITHOUT ROWID"""

IMMUTABLE_USAGE = """CREATE TRIGGER provider_usage_update BEFORE UPDATE ON provider_usage
WHEN OLD.evidence_filename IS NULL OR NEW.evidence_filename IS NULL OR
     NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id OR NEW.attempt_number IS NOT OLD.attempt_number OR
     NEW.reservation_key IS NOT OLD.reservation_key OR NEW.provider IS NOT OLD.provider OR NEW.period IS NOT OLD.period OR
     NEW.usage_kind IS NOT OLD.usage_kind OR NEW.units IS NOT OLD.units OR NEW.created_at IS NOT OLD.created_at OR
     NEW.evidence_digest IS NOT OLD.evidence_digest
BEGIN SELECT RAISE(ABORT,'Metered provider usage is immutable'); END"""


def install(conn, _context):
    if conn.execute(f'''SELECT 1 FROM provider_usage u JOIN {OLD} l ON l.id=u.legacy_usage_id
        WHERE u.provider<>l.provider OR u.period<>l.period OR u.usage_kind<>'requests' OR u.units<>l.request_count''').fetchone():
        raise MigrationError('Paid provider totals disagree with their original receipt')
    guards = conn.execute("SELECT sql FROM main.sqlite_schema WHERE sql IS NOT NULL AND tbl_name='provider_usage' "
                          "AND type IN ('index','trigger') AND name NOT IN (?, 'provider_usage_update')",
                          (RETIRED_INDEX,)).fetchall()
    _replace_table(conn, 'provider_usage', USAGE, expressions={
        'evidence_filename': f'(SELECT filename FROM {OLD} WHERE id=original.legacy_usage_id)',
        'evidence_digest': f'(SELECT content_digest FROM {OLD} WHERE id=original.legacy_usage_id)',
    })
    conn.execute(f'''INSERT INTO provider_usage(reservation_key,provider,period,usage_kind,units,created_at,
        evidence_filename,evidence_digest)
        SELECT 'period:requests:'||l.period,l.provider,l.period,'requests',l.request_count,l.built_at,
               l.filename,l.content_digest FROM {OLD} l
        WHERE NOT EXISTS(SELECT 1 FROM provider_usage u WHERE u.request_id IS NULL AND u.period=l.period)''')
    for sql, in guards:
        conn.execute(sql)
    conn.execute('CREATE UNIQUE INDEX ix_provider_usage_period_total ON provider_usage(provider,period) '
                 'WHERE request_id IS NULL')
    conn.execute(IMMUTABLE_USAGE)

    _replace_table(conn, 'album_description_access', ACCESS)
    conn.execute(f'''INSERT INTO album_description_access(provider,scope,credential_key,status,retry_at,updated_at)
        SELECT provider,'unattributed_request','',CASE WHEN uncertain THEN 'uncertain' ELSE 'busy' END,
               lease_until,built_at FROM (
            SELECT *,coalesce(length(claim_token),0)>0 AND send_authorized=1
                AND status IN ('pending','claimed','sent','uncertain') AS uncertain FROM {OLD}
        ) WHERE uncertain OR lease_until>0''')
    conn.execute(f'DROP TABLE {OLD}')


def validate(conn):
    from ..schema_inspection import objects

    if OLD in objects(conn, (OLD,)):
        raise MigrationError('Old provider usage receipt still owns a table')
