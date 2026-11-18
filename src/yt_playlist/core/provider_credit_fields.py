"""Structured provider fields alongside the installed scalar-field owners.

Add empty reference tables rather than rebuilding historical attempt/evidence
tables during an ordinary startup. Values stay in their original deliveries.
"""
from yt_playlist.core.schema_contract import MigrationError, tokens

DEFINITIONS = {
    'provider_attempt_credits': '''CREATE TABLE provider_attempt_credits (
        attempt_id INTEGER PRIMARY KEY REFERENCES provider_attempts(id) ON DELETE CASCADE
    )''',
    'provider_state_credits': '''CREATE TABLE provider_state_credits (
        recording_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        delivery_id INTEGER NOT NULL REFERENCES metadata_deliveries(id),
        PRIMARY KEY(recording_id,provider),
        FOREIGN KEY(recording_id,provider) REFERENCES enrichment_provider_state(recording_id,provider) ON DELETE CASCADE
    ) WITHOUT ROWID''',
}
for operation in ('INSERT', 'UPDATE'):
    name = 'owned_provider_credit_' + operation.lower()
    DEFINITIONS[name] = f'''CREATE TRIGGER {name} BEFORE {operation} ON provider_state_credits
        WHEN NOT EXISTS(SELECT 1 FROM metadata_deliveries d WHERE d.id=NEW.delivery_id
            AND d.provider=NEW.provider AND d.input_recording_id=NEW.recording_id)
        BEGIN SELECT RAISE(ABORT,'Provider credits have a different subject or provider'); END'''


def available(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='provider_state_credits'").fetchone() is not None


def validate(conn):
    for name, sql in DEFINITIONS.items():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible provider credit owner: ' + name)


def install(conn):
    for sql in DEFINITIONS.values():
        conn.execute(sql)
    validate(conn)


def state_source(conn):
    source = 'SELECT recording_id,provider,field,delivery_id FROM provider_state_fields'
    if available(conn):
        source += " UNION ALL SELECT recording_id,provider,'artist_credits',delivery_id FROM provider_state_credits"
    return '(' + source + ')'


def attempt_source(conn):
    source = 'SELECT attempt_id,field FROM provider_attempt_fields'
    if available(conn):
        source += " UNION ALL SELECT attempt_id,'artist_credits' FROM provider_attempt_credits"
    return '(' + source + ')'


def reset(conn, recording, provider):
    if available(conn):
        conn.execute('DELETE FROM provider_state_credits WHERE recording_id=? AND provider=?', (recording, provider))


def record(conn, attempt, recording, provider, delivery):
    conn.execute('INSERT INTO provider_attempt_credits VALUES (?)', (attempt,))
    conn.execute('INSERT INTO provider_state_credits VALUES (?,?,?) ON CONFLICT(recording_id,provider) '
                 'DO UPDATE SET delivery_id=excluded.delivery_id', (recording, provider, delivery))
