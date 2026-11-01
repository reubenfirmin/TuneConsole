"""Plan bounded durable closure prefixes outside the serving connection lock."""
from yt_playlist.core.activity import check_cancellable_work

BATCH = 128
STATE = 'group_retention_walk'
GENERATIONS = 'group_retention_generations'
ALIASES = 'group_retention_aliases'
EDGES = 'group_retention_edges'


def _state(phase, source=0, cursor='', cursor2='', cursor3=''):
    return (f'UPDATE {STATE} SET phase=?,source=?,cursor=?,cursor2=?,cursor3=? WHERE id=1',
            (phase, source, str(cursor), cursor2, cursor3))


def _generation(identity, expand=False):
    return (f'INSERT INTO {GENERATIONS}(generation_id,expand,pending) VALUES (?,?,?) '
            'ON CONFLICT(generation_id) DO NOTHING', (identity, int(expand), int(expand)))


def _alias(conn, identity):
    commands = [(f'INSERT INTO {ALIASES}(external_version,pending) VALUES (?,1) '
                 'ON CONFLICT(external_version) DO NOTHING', (identity,))]
    owner = conn.execute('SELECT first_generation_id FROM group_version_aliases WHERE external_version=?',
                         (identity,)).fetchone()
    if owner:
        commands.append(_generation(owner[0]))
    return commands


def advance(conn):
    """Return prepared writes, or None once a current closure is complete.

    Every returned prefix fits in one transaction. Cancellation before commit
    repeats only that prefix. A changed root epoch resets scratch rows in bounded
    batches before reading any roots or permitting a deletion.
    """
    revision = conn.execute('SELECT revision FROM group_retention_epoch WHERE id=1').fetchone()[0]
    row = conn.execute(f'SELECT revision,phase,source,cursor,cursor2,cursor3 FROM {STATE} WHERE id=1').fetchone()
    if row is None or row[0] != revision or row[1] == 'reset':
        commands = [(f'INSERT INTO {STATE}(id,revision,phase) VALUES (1,?,\'reset\') '
                     "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,phase='reset'", (revision,))]
        for table, key in ((EDGES, '(source_alias,target_alias)'), (ALIASES, 'external_version'),
                           (GENERATIONS, 'generation_id')):
            if conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                columns = key.strip('()')
                commands.append((f'DELETE FROM {table} WHERE {key} IN '
                                 f'(SELECT {columns} FROM {table} LIMIT ?)', (BATCH,)))
                return commands
        return commands + [_state('roots')]
    _, phase, source, cursor, cursor2, cursor3 = row
    if phase == 'ready':
        return None
    if phase == 'roots':
        sources = [('group_head', 'generation_id', True),
                   ('group_evidence_generations INDEXED BY ix_group_evidence_generation', 'generation_id', True),
                   ('group_builds', 'generation_id', False),
                   ('group_builds INDEXED BY ix_compact_group_build_parent', 'parent_generation_id', True)]
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_generations'").fetchone():
            sources.append(('recommendation_generations', 'group_generation_id', True))
        if source == len(sources):
            return [_state('evidence')]
        table, column, expand = sources[source]
        rows = conn.execute(f'SELECT {column} FROM {table} WHERE {column}>? ORDER BY {column} LIMIT ?',
                            (int(cursor or 0), BATCH)).fetchall()
        commands = []
        for identity, in rows:
            check_cancellable_work()
            commands.append((f'INSERT INTO {GENERATIONS}(generation_id,expand,pending) VALUES (?,?,?) '
                'ON CONFLICT(generation_id) DO UPDATE SET expand=max(expand,excluded.expand),'
                'pending=max(pending,excluded.pending)', (identity, int(expand), int(expand))))
        return commands + [_state('roots', source, rows[-1][0]) if rows else _state('roots', source + 1)]
    if phase == 'evidence':
        rows = conn.execute('SELECT kind,external_key,external_version FROM group_evidence_versions '
            'WHERE (kind,external_key,external_version)>(?,?,?) '
            'ORDER BY kind,external_key,external_version LIMIT ?', (cursor, cursor2, cursor3, BATCH)).fetchall()
        commands = []
        for _, _, identity in rows:
            check_cancellable_work()
            commands.extend(_alias(conn, identity))
        return commands + [_state('evidence', 0, *rows[-1]) if rows else _state('expand')]
    if phase == 'expand':
        root = conn.execute(f'SELECT generation_id,cursor FROM {GENERATIONS} WHERE pending=1 '
                             'ORDER BY generation_id LIMIT 1').fetchone()
        if root is None:
            return [_state('walk')]
        generation, after = root
        rows = conn.execute('SELECT external_version FROM generation_groups WHERE generation_id=? '
            'AND external_version>? ORDER BY external_version LIMIT ?', (generation, after, BATCH)).fetchall()
        commands = []
        for identity, in rows:
            check_cancellable_work()
            commands.extend(_alias(conn, identity))
        commands.append((f'UPDATE {GENERATIONS} SET pending=?,cursor=? WHERE generation_id=?',
                         (int(len(rows) == BATCH), rows[-1][0] if rows else after, generation)))
        return commands
    if phase != 'walk':
        raise RuntimeError('Unknown group retirement phase: ' + phase)
    pending = conn.execute(f'SELECT external_version,cursor FROM {ALIASES} WHERE pending=1 '
                            'ORDER BY external_version LIMIT ?', (BATCH,)).fetchall()
    if not pending:
        return [_state('ready')]
    commands, remaining = [], BATCH
    for identity, after in pending:
        check_cancellable_work()
        edges = conn.execute('SELECT l.target_alias,(SELECT min(x.generation_id) '
            'FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
            'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) '
            'FROM group_lineage l WHERE l.source_alias=? AND l.target_alias>? AND EXISTS('
            'SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
            'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) '
            'ORDER BY l.target_alias LIMIT ?', (identity, after, remaining)).fetchall()
        for target, owner in edges:
            commands.append((f'INSERT INTO {EDGES} VALUES (?,?) ON CONFLICT DO NOTHING', (identity, target)))
            commands.append(_generation(owner))
            commands.extend(_alias(conn, target))
        commands.append((f'UPDATE {ALIASES} SET pending=?,cursor=? WHERE external_version=?',
                         (int(len(edges) == remaining), edges[-1][0] if edges else after, identity)))
        remaining -= len(edges)
        if not remaining:
            break
    return commands


def expired(conn, batch):
    source, cursor, cursor2 = conn.execute(f'SELECT source,cursor,cursor2 FROM {STATE} WHERE id=1').fetchone()
    if source == 0:
        rows = conn.execute(f'SELECT l.source_alias,l.target_alias,NOT EXISTS('
            f'SELECT 1 FROM {EDGES} k WHERE k.source_alias=l.source_alias AND k.target_alias=l.target_alias) AND EXISTS('
            'SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
            'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) AND NOT EXISTS('
            'SELECT 1 FROM group_generation_lineage x JOIN group_builds b ON b.generation_id=x.generation_id '
            'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) '
            'FROM group_lineage l WHERE (l.source_alias,l.target_alias)>(?,?) '
            'ORDER BY l.source_alias,l.target_alias LIMIT ?', (cursor, cursor2, BATCH)).fetchall()
        selections = []
        for source_alias, target, eligible in rows:
            check_cancellable_work()
            if not eligible:
                continue
            selections.extend((row[0], source_alias, target) for row in conn.execute(
                'SELECT generation_id FROM group_generation_lineage WHERE source_alias=? AND target_alias=? '
                'ORDER BY generation_id LIMIT ?', (source_alias, target, batch - len(selections))))
            if len(selections) == batch:
                break
        if selections:
            return {'edges': selections}
        return {'walk': [_state('ready', 0, rows[-1][0], rows[-1][1]) if rows else _state('ready', 1)]}
    generation = conn.execute('SELECT p.generation_id FROM group_publications p WHERE NOT EXISTS('
        f'SELECT 1 FROM {GENERATIONS} k WHERE k.generation_id=p.generation_id) AND NOT EXISTS('
        'SELECT 1 FROM group_version_aliases a WHERE a.first_generation_id=p.generation_id AND EXISTS('
        'SELECT 1 FROM generation_groups x WHERE x.external_version=a.external_version '
        'AND x.generation_id<>p.generation_id)) ORDER BY p.generation_id DESC LIMIT 1').fetchone()
    return {'generation': generation[0]} if generation else None
