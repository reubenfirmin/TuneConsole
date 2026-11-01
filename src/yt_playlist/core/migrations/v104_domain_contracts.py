"""Close audited flag, count, credit and ranking contracts on their own rows."""
import re

from .runner import MigrationError


def integer(column, minimum=0):
    return f"typeof({column})='integer' AND {column}>={minimum}"


def flag(column, *, nullable=False):
    expression = f"typeof({column})='integer' AND {column} IN (0,1)"
    return f'{column} IS NULL OR ({expression})' if nullable else expression


def finite(column, *, nullable=True):
    expression = f"typeof({column}) IN ('integer','real') AND abs({column})<=1.7976931348623157e308"
    return f'{column} IS NULL OR ({expression})' if nullable else expression


CONTRACTS = {
    'recordings': {'provisional':flag('provisional')},
    'taste_modes': {'active':flag('active'),'size':integer('size'),
                   'first_seen':finite('first_seen',nullable=False),'last_seen':finite('last_seen',nullable=False)},
    'youtube_items': {'available':flag('available',nullable=True)},
    'unresolved_items': {'available':flag('available',nullable=True)},
    'playlists': {'track_count':'track_count IS NULL OR ('+integer('track_count')+')'},
    'entry_artists': {'position':integer('position')},
    'album_artists': {'position':integer('position')},
    'discovery_memberships': {'offered_count':integer('offered_count'),
        **{column:finite(column) for column in ('found_at','first_shown','last_shown')}},
    'album_discovery_memberships': {
        **{column:integer(column) for column in ('offered_count','legacy_offered_count')},
        **{column:finite(column) for column in ('found_at','first_shown','last_shown','legacy_first_shown','legacy_last_shown')}},
    'discovered_artists': {
        **{column:integer(column) for column in ('offered_count','legacy_offered_count')},
        **{column:finite(column) for column in ('score','found_at','first_shown','last_shown','legacy_first_shown','legacy_last_shown')}},
    'discovery_legacy_offers': {'offered_count':integer('offered_count'),
        **{column:finite(column) for column in ('first_shown','last_shown')}},
    'discovery_impressions': {'shown_at':finite('shown_at',nullable=False)},
    'unresolved_discovery_impressions': {'shown_at':finite('shown_at',nullable=False)},
    'rec_response_events': {'is_generated':flag('is_generated')},
    'rec_served_impressions': {'rank':integer('rank'),'score':finite('score'),
                              'served_at':finite('served_at',nullable=False)},
    'rec_content_pending': {'position':integer('position'),'revision':integer('revision',1)},
    'rec_musical_pending': {'position':integer('position'),'revision':integer('revision',1),'cursor':integer('cursor')},
    'rec_vibe_pending': {'position':integer('position'),'revision':integer('revision',1)},
}
TABLE = re.compile(r'CREATE TABLE\s+["`\[]?(\w+)["`\]]?\s*\(',re.I)


def transform(sql):
    match = TABLE.match(sql.lstrip())
    if match is None or match[1] not in CONTRACTS:
        return sql
    checks = ''.join(f',CONSTRAINT ck_{match[1]}_{column} CHECK({expression})'
                     for column,expression in CONTRACTS[match[1]].items())
    closing = sql.rfind(')')
    return sql[:closing]+checks+sql[closing:]


def preflight(conn):
    """Reject unsupported history intact, before any table or SQL is changed."""
    for table,fields in CONTRACTS.items():
        keys = [row[1] for row in conn.execute('PRAGMA table_info('+table+')') if row[5]]
        for column,expression in fields.items():
            row = conn.execute(f'SELECT {",".join(keys)},{column},typeof({column}) FROM {table} '
                               f'WHERE NOT ({expression}) LIMIT 1').fetchone()
            if row is not None:
                raise MigrationError(f'Invalid {table}.{column}: key={tuple(row[:-2])!r}, '
                                     f'value={row[-2]!r}, stored_type={row[-1]}; original rows retained')


def _replace(conn,table,definition):
    columns = ','.join('"'+row[1].replace('"','""')+'"' for row in conn.execute('PRAGMA table_info('+table+')'))
    indexes = [row[0] for row in conn.execute("SELECT sql FROM sqlite_schema WHERE type='index' "
                                           'AND tbl_name=? AND sql IS NOT NULL',(table,))]
    sequence = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(table,)).fetchone()
    replacement = table+'_domain_replacement'
    conn.execute(TABLE.sub(lambda match:'CREATE TABLE '+replacement+' (',definition,count=1))
    conn.execute(f'INSERT INTO {replacement}({columns}) SELECT {columns} FROM {table}')
    if (conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] !=
            conn.execute(f'SELECT count(*) FROM {replacement}').fetchone()[0]
            or conn.execute(f'SELECT {columns} FROM {table} EXCEPT SELECT {columns} FROM {replacement} LIMIT 1').fetchone()):
        raise MigrationError(f'Domain cutover changed {table}; original rows retained')
    conn.execute('DROP TABLE '+table)
    conn.execute(f'ALTER TABLE {replacement} RENAME TO {table}')
    if sequence is not None:
        conn.execute('DELETE FROM sqlite_sequence WHERE name=?',(table,))
        conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)',(table,sequence[0]))
    for sql in indexes:
        conn.execute(sql)


def apply(conn,context):
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise MigrationError('Domain replacement requires the migration runner')
    preflight(conn)
    dependent = list(conn.execute("SELECT type,name,sql FROM sqlite_schema WHERE type IN ('view','trigger') ORDER BY rowid"))
    for kind,name,_ in reversed(dependent):
        conn.execute('DROP '+kind+' "'+name.replace('"','""')+'"')
    from .current import statements
    definitions = {}
    for sql in statements(version=104):
        match = TABLE.match(sql.lstrip())
        if match and match[1] in CONTRACTS:
            definitions[match[1]] = sql
    if definitions.keys()!=CONTRACTS.keys():
        raise MigrationError('Incomplete domain replacement definitions')
    for table,definition in definitions.items():
        _replace(conn,table,definition.lstrip())
    for _,_,sql in dependent:
        conn.execute(sql)
