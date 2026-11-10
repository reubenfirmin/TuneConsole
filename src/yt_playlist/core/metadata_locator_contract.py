"""Retired metadata layout read contract; retirement work remains explicit."""
from .metadata_contract import runtime_active
from .schema_contract import MigrationError, tokens


REMOVED = frozenset(('ix_metadata_delivery_raw_content','ix_metadata_delivery_raw_file'))
MARKER = 'retired_metadata_delivery_insert'
TABLE = '''CREATE TABLE metadata_deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 provider TEXT NOT NULL CHECK(length(provider)>0),
 key_contract TEXT NOT NULL CHECK(key_contract IN ('provider_revision','capture','legacy_record')),
 delivery_key TEXT NOT NULL CHECK(length(delivery_key)>0),
 provider_entity_type TEXT NOT NULL CHECK(provider_entity_type IN ('recording','release','artist','genre','item','lookup','legacy_unknown')),
 provider_entity_id TEXT CHECK(provider_entity_id IS NULL OR length(provider_entity_id)>0),
 input_entry_id INTEGER REFERENCES catalog_entries(id),
 input_recording_id INTEGER REFERENCES recordings(id),
 input_video_id TEXT REFERENCES youtube_items(video_id),
 observed_at REAL CHECK(observed_at IS NULL OR (typeof(observed_at) IN ('integer','real') AND observed_at BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308)),
 format_version TEXT NOT NULL CHECK(format_version='metadata-delivery-1'),
 CHECK(provider_entity_id IS NOT NULL OR provider_entity_type IN ('lookup','legacy_unknown')),
 CHECK(observed_at IS NOT NULL OR key_contract='legacy_record'),
 CHECK((input_entry_id IS NULL AND input_recording_id IS NULL AND input_video_id IS NULL) OR (input_entry_id IS NOT NULL AND input_recording_id IS NOT NULL))
)'''
GUARDS = {
    'immutable_metadata_delivery': "CREATE TRIGGER immutable_metadata_delivery BEFORE UPDATE ON metadata_deliveries BEGIN SELECT RAISE(ABORT,'Retired metadata history is read-only'); END",
    'owned_metadata_legacy_record': "CREATE TRIGGER owned_metadata_legacy_record BEFORE INSERT ON metadata_legacy_records BEGIN SELECT RAISE(ABORT,'Retired metadata history is read-only'); END",
    MARKER: "CREATE TRIGGER retired_metadata_delivery_insert BEFORE INSERT ON metadata_deliveries BEGIN SELECT RAISE(ABORT,'Retired metadata history is read-only'); END",
}


def retired(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='trigger' AND name=?",(MARKER,)).fetchone() is not None


def validate(conn):
    if not runtime_active(conn):
        raise MigrationError('Retired metadata locators require active runtime metadata')
    row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?',(MARKER,)).fetchone()
    if row is None or tokens(row[0])!=tokens(GUARDS[MARKER]):
        raise MigrationError('Invalid metadata locator retirement guard')
    if conn.execute('SELECT 1 FROM metadata_deliveries LIMIT 1').fetchone():
        raise MigrationError('Retired metadata delivery owner must remain empty')
