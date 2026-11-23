"""Put the current capture slot on its metadata delivery."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_metadata_capture_slot'
TABLE = 'metadata_deliveries'
RETIRED = 'metadata_capture_heads'
INDEX = 'ix_metadata_delivery_capture_slot'
DELETE_GUARD = 'retained_metadata_capture_delivery'


def predecessor_objects(conn):
    return list(definitions(conn, (TABLE, RETIRED, 'ix_metadata_capture_delivery',
                                   'owned_metadata_capture_insert', 'owned_metadata_capture_update')).values())


def install(conn, _context):
    if conn.execute('SELECT 1 FROM metadata_capture_heads GROUP BY delivery_id HAVING count(*)>1 LIMIT 1').fetchone():
        raise MigrationError('A metadata delivery owns multiple current capture slots')
    if conn.execute('''SELECT 1 FROM metadata_capture_heads h LEFT JOIN metadata_deliveries d ON d.id=h.delivery_id
                       WHERE d.input_entry_id IS NOT h.entry_id OR d.provider IS NOT h.provider LIMIT 1''').fetchone():
        raise MigrationError('A metadata capture head does not match its delivery')

    conn.execute('ALTER TABLE metadata_deliveries ADD COLUMN capture_slot TEXT '
                 'CHECK(capture_slot IS NULL OR (input_entry_id IS NOT NULL AND length(capture_slot)>0))')
    conn.execute('''UPDATE metadata_deliveries SET capture_slot=h.capture_slot
                    FROM metadata_capture_heads h WHERE metadata_deliveries.id=h.delivery_id''')
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'CREATE UNIQUE INDEX {INDEX} ON metadata_deliveries(input_entry_id,provider,capture_slot) '
                 'WHERE capture_slot IS NOT NULL')
    conn.execute(f'''CREATE TRIGGER {DELETE_GUARD} BEFORE DELETE ON metadata_deliveries
        WHEN OLD.capture_slot IS NOT NULL BEGIN
        SELECT RAISE(ABORT,'Current metadata capture still owns this delivery'); END''')


def validate(conn):
    schema = definitions(conn, (RETIRED, INDEX, DELETE_GUARD))
    if set(schema) != {INDEX, DELETE_GUARD}:
        raise MigrationError('Incomplete metadata capture-slot storage')
