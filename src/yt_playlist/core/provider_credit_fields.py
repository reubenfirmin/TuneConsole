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


def validate(conn):
    for name, sql in DEFINITIONS.items():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible provider credit owner: ' + name)


def install(conn):
    for sql in DEFINITIONS.values():
        conn.execute(sql)
    validate(conn)


STATE_SOURCE = ("(SELECT s.recording_id,s.provider,f.key field,f.value->>0 delivery_id "
                "FROM enrichment_provider_state s,json_each(s.field_links) f "
                "UNION ALL SELECT recording_id,provider,'artist_credits',credit_delivery_id "
                "FROM enrichment_provider_state WHERE credit_delivery_id IS NOT NULL)")
ATTEMPT_SOURCE = ("(SELECT a.id attempt_id,f.key field,a.delivery_id "
                  "FROM provider_attempts a,json_each(a.field_links) f "
                  "UNION ALL SELECT id,'artist_credits',delivery_id FROM provider_attempts WHERE artist_credits=1)")
