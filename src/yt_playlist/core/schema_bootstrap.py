"""Install the compiled destination in one caller-owned transaction."""
from . import destination_schema
from .schema_contract import MigrationError, TOKEN


def bootstrap(conn, registry, changes, now, *, validation):
    document = destination_schema.definition()
    if ([[m.version, m.name] for m in registry] != document['numbered_history']
            or [[c.version, c.name] for c in changes] != document['regular_history']):
        raise MigrationError('The shipped destination definition does not match the schema registry')
    if not conn.in_transaction:
        raise MigrationError('Destination bootstrap requires an owned transaction')
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        raise MigrationError('Destination bootstrap requires an empty database')
    # Alphabetical seed dumps may reference another owner's later seed row.
    # Defer foreign keys within this transaction and check them explicitly
    # before restoring the caller's setting. Guards are installed after seeds,
    # preserving the compiler's exact initial queues and revision counters.
    deferred = conn.execute('PRAGMA defer_foreign_keys').fetchone()[0]
    conn.execute('PRAGMA defer_foreign_keys=ON')
    try:
        guards = []
        for sql in document['destination_statements']:
            # Classification only needs two words, not every token in a large
            # trigger body. Full definitions are still checked by validation.
            words = (match.group().lower() for match in TOKEN.finditer(sql)
                     if not match.group().startswith(('--', '/*')))
            if next(words, None) == 'create' and next(words, None) == 'trigger':
                guards.append(sql)
            else:
                conn.execute(sql)
        for sql in document['destination_seeds']:
            conn.execute(sql)
        for table, key in (('schema_migrations', 'numbered_history'), ('schema_changes', 'regular_history')):
            conn.executemany('INSERT INTO '+table+' VALUES (?,?,?)',
                             [(version, name, now) for version, name in document[key]])
        for sql in guards:
            conn.execute(sql)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise MigrationError('Invalid destination seed foreign-key reference')
        validation(conn)
    finally:
        conn.execute('PRAGMA defer_foreign_keys='+str(int(deferred)))
