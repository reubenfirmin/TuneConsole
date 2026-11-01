"""Retire only derived history with no actual evidence or required lineage root.

The backfill fence stays closed until every durable and pending producer uses
explicit roots. A guarded read snapshot plans bounded deletions; a new offer,
feedback capture, publication or external writer invalidates that plan.
"""
from yt_playlist.core.activity import WorkBudgetExceeded, check_cancellable_work
from . import group_publication as publication

MAX_REFERENCES = 8192


def _bounded(rows, label):
    values = list(rows)
    if len(values) > MAX_REFERENCES:
        raise WorkBudgetExceeded(f'Musical group retention exceeds its {label} budget')
    return values


def closure(conn):
    recommendations = conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' "
                                    "AND name='recommendation_generations'").fetchone() is not None
    prepared_roots = (' UNION SELECT group_generation_id FROM recommendation_generations '
                      'WHERE group_generation_id IS NOT NULL' if recommendations else '')
    generations = {row[0] for row in _bounded(conn.execute(
        'SELECT generation_id FROM group_head UNION SELECT generation_id FROM group_evidence_generations '
        'UNION SELECT generation_id FROM group_builds UNION SELECT parent_generation_id FROM group_builds '
        'WHERE parent_generation_id IS NOT NULL' + prepared_roots + ' LIMIT ?', (MAX_REFERENCES + 1,)), 'generation')}
    prepared_aliases = (' UNION SELECT x.external_version FROM generation_groups x '
                       'JOIN recommendation_generations r ON r.group_generation_id=x.generation_id'
                       if recommendations else '')
    aliases = {row[0] for row in _bounded(conn.execute(
        'SELECT external_version FROM group_evidence_versions UNION SELECT x.external_version '
        'FROM generation_groups x JOIN group_evidence_generations e ON e.generation_id=x.generation_id '
        'UNION SELECT x.external_version FROM generation_groups x JOIN group_builds b '
        'ON b.parent_generation_id=x.generation_id' + prepared_aliases + ' LIMIT ?', (MAX_REFERENCES + 1,)), 'evidence')}
    # Current aliases retain their first publication context, but do not cause
    # unrelated incoming historical lineage to become a new evidence root.
    current = {row[0] for row in _bounded(conn.execute('SELECT x.external_version FROM generation_groups x '
        'JOIN group_head h ON h.generation_id=x.generation_id LIMIT ?', (MAX_REFERENCES + 1,)), 'head')}
    aliases.update(current)
    pending, visited, edges = list(aliases), set(), set()
    while pending:
        check_cancellable_work()
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        for target, in conn.execute('SELECT l.target_alias FROM group_lineage l WHERE l.source_alias=? AND EXISTS('
                'SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
                'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) LIMIT ?',
                (source, MAX_REFERENCES + 1)):
            edges.add((source, target))
            if len(edges) > MAX_REFERENCES:
                raise WorkBudgetExceeded('Musical group retention exceeds its lineage budget')
            aliases.add(target)
            if target not in visited:
                pending.append(target)
        if len(aliases) > MAX_REFERENCES:
            raise WorkBudgetExceeded('Musical group retention exceeds its alias budget')
    for alias in aliases | current:
        row = conn.execute('SELECT first_generation_id FROM group_version_aliases WHERE external_version=?',
                            (alias,)).fetchone()
        if row is not None:
            generations.add(row[0])
    for source, target in edges:
        # One published owner establishes this historical edge. Repeated
        # publication of the same edge must not keep every generation alive.
        owner = conn.execute('SELECT x.generation_id FROM group_generation_lineage x '
            'JOIN group_publications p ON p.generation_id=x.generation_id '
            'WHERE x.source_alias=? AND x.target_alias=? ORDER BY x.generation_id LIMIT 1',
            (source, target)).fetchone()
        if owner is not None:
            generations.add(owner[0])
        if len(generations) > MAX_REFERENCES:
            raise WorkBudgetExceeded('Musical group retention exceeds its generation budget')
    return generations, edges


def plan(conn):
    state = conn.execute('SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()
    if state is None or not state[0]:
        return None
    from yt_playlist.core import group_retention_progress
    if group_retention_progress.available(conn):
        from . import group_retention_walk
        revision = conn.execute('SELECT revision FROM group_retention_epoch WHERE id=1').fetchone()[0]
        commands = group_retention_walk.advance(conn)
        if commands is not None:
            return {'walk': commands, 'revision': revision}
        selected = group_retention_walk.expired(conn, publication.BATCH_ROWS)
        return dict(selected, revision=revision) if selected else None
    generations, required = closure(conn)
    rows = conn.execute('SELECT l.source_alias,l.target_alias FROM group_lineage l WHERE EXISTS('
        'SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
        'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) AND NOT EXISTS('
        'SELECT 1 FROM group_generation_lineage x JOIN group_builds b ON b.generation_id=x.generation_id '
        'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) '
        'ORDER BY l.source_alias,l.target_alias LIMIT ?', (len(required) + publication.BATCH_ROWS,))
    expired_edges = [tuple(row) for row in rows if tuple(row) not in required][:publication.BATCH_ROWS]
    if expired_edges:
        selections = []
        for source, target in expired_edges:
            selections.extend((row[0], source, target) for row in conn.execute(
                'SELECT generation_id FROM group_generation_lineage WHERE source_alias=? AND target_alias=? '
                'ORDER BY generation_id LIMIT ?', (source, target, publication.BATCH_ROWS - len(selections))))
            if len(selections) == publication.BATCH_ROWS:
                break
        return {'edges': selections}
    for generation_id, in conn.execute('SELECT generation_id FROM group_publications ORDER BY generation_id DESC LIMIT ?',
                                      (len(generations) + publication.BATCH_ROWS,)):
        if generation_id in generations:
            continue
        # Another selection can still use an alias whose first publication is
        # this generation. Retire that unreferenced selection first, never break
        # its exact alias context or release an actual evidence owner by age.
        if conn.execute('SELECT 1 FROM group_version_aliases a WHERE a.first_generation_id=? AND EXISTS('
                'SELECT 1 FROM generation_groups x WHERE x.external_version=a.external_version AND x.generation_id<>?) '
                'LIMIT 1', (generation_id, generation_id)).fetchone():
            continue
        return {'generation': generation_id}
    return None


def apply(conn, selected):
    if not conn.in_transaction:
        raise RuntimeError('Musical group retirement requires guarded publication')
    if not conn.execute('SELECT 1 FROM group_retention_state WHERE id=1 AND roots_complete=1').fetchone():
        raise RuntimeError('Musical group evidence backfill is incomplete')
    if 'revision' in selected and conn.execute(
            'SELECT revision FROM group_retention_epoch WHERE id=1').fetchone()[0] != selected['revision']:
        raise RuntimeError('Musical group retirement roots changed')
    if 'walk' in selected:
        for statement, parameters in selected['walk']:
            check_cancellable_work()
            conn.execute(statement, parameters)
    elif 'edges' in selected:
        for generation_id, source, target in selected['edges']:
            conn.execute('INSERT INTO group_lineage_retirements VALUES (?,?)', (source, target))
            conn.execute('DELETE FROM group_generation_lineage WHERE generation_id=? AND source_alias=? AND target_alias=?',
                         (generation_id, source, target))
            conn.execute('DELETE FROM group_lineage_retirements WHERE source_alias=? AND target_alias=?', (source, target))
            conn.execute('DELETE FROM group_lineage WHERE source_alias=? AND target_alias=? AND NOT EXISTS('
                'SELECT 1 FROM group_generation_lineage WHERE source_alias=? AND target_alias=?)',
                (source, target, source, target))
    else:
        generation_id = selected['generation']
        conn.execute("INSERT INTO group_builds VALUES (?,'cleanup',0,0,NULL,NULL)", (generation_id,))
        conn.execute('DELETE FROM group_publications WHERE generation_id=?', (generation_id,))


def step(repo):
    with repo._store.read_snapshot() as snapshot:
        selected = plan(snapshot.store.conn)
        if selected is None:
            return False
        snapshot.release_reader()
        return snapshot.publish(lambda: apply(repo.conn, selected))
