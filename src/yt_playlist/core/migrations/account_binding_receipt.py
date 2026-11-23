"""Keep the one-time configuration import receipt with the saved accounts."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'own_account_binding_receipt'
OLD_KEY = 'account_bindings_imported'


def predecessor_objects(conn):
    return [definitions(conn, ('accounts',))['accounts']]


def install(conn, _context):
    conn.execute('ALTER TABLE accounts ADD COLUMN bindings_saved INTEGER NOT NULL DEFAULT 0 '
                 'CHECK(bindings_saved IN (0,1))')
    conn.execute('UPDATE accounts SET bindings_saved=1 WHERE id=(SELECT min(id) FROM accounts) '
                 'AND EXISTS (SELECT 1 FROM settings WHERE key=?)', (OLD_KEY,))
    conn.execute('DELETE FROM settings WHERE key=?', (OLD_KEY,))


def validate(conn):
    if 'bindings_saved' not in tokens(definitions(conn, ('accounts',)).get('accounts')):
        raise MigrationError('Missing account binding receipt')
