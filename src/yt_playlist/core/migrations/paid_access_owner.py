"""Move paid-description access to the existing DeepSeek provider row."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from .paid_access_settings import PREFIX


NAME = 'own_paid_description_access'
TABLE = 'enrichment_provider_config'
COLUMN = """description_access TEXT CHECK(description_access IS NULL OR
    (provider='deepseek' AND json_type(description_access) IS 'object'))"""


def predecessor_objects(conn):
    return [definitions(conn, (TABLE,))[TABLE]]


def install(conn, _context):
    conn.execute(f'ALTER TABLE {TABLE} ADD COLUMN {COLUMN}')
    conn.execute("""
        INSERT INTO enrichment_provider_config(provider,description_access)
        SELECT 'deepseek',json_group_object(substr(key,?),json(value))
        FROM settings WHERE key GLOB ? HAVING count(*)>0
        ON CONFLICT(provider) DO UPDATE SET description_access=excluded.description_access
    """, (len(PREFIX) + 1, PREFIX + '*'))
    conn.execute('DELETE FROM settings WHERE key GLOB ?', (PREFIX + '*',))


def validate(conn):
    statement = definitions(conn, (TABLE,)).get(TABLE)
    if 'description_access' not in tokens(statement):
        raise MigrationError('Paid description access column is missing')
