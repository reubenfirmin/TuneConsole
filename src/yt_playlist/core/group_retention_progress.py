"""Durable, bounded scratch work for reference-safe group retirement.

Installing these empty owners never traverses historical evidence. Root changes
invalidate the walk; retirement itself leaves its conservative closure usable.
"""
from .migrations.runner import MigrationError

TABLE = 'group_retention_walk'


def available(conn):
    return conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (TABLE,)).fetchone() is not None


def publication_guard():
    # Both columns belong to the same alias FK. Starting from the generation's
    # aliases allows the existing compact-version index to answer each lookup.
    return ('CREATE TRIGGER retained_group_publication_delete BEFORE DELETE ON group_publications WHEN '
        'NOT EXISTS(SELECT 1 FROM group_retention_state WHERE id=1 AND roots_complete=1) OR '
        "NOT EXISTS(SELECT 1 FROM group_builds WHERE generation_id=OLD.generation_id AND owner='cleanup') OR "
        'EXISTS(SELECT 1 FROM group_head WHERE generation_id=OLD.generation_id) OR '
        'EXISTS(SELECT 1 FROM group_builds WHERE parent_generation_id=OLD.generation_id) OR '
        'EXISTS(SELECT 1 FROM group_evidence_generations WHERE generation_id=OLD.generation_id) OR '
        'EXISTS(SELECT 1 FROM '
        'group_version_aliases a CROSS JOIN group_evidence_versions e INDEXED BY ix_group_evidence_version '
        'ON e.group_version_id=a.group_version_id AND e.external_version=a.external_version '
        'WHERE a.first_generation_id=OLD.generation_id) '
        "BEGIN SELECT RAISE(ABORT,'Retained musical group publication is immutable'); END")


def definitions(conn):
    yield 'group_retention_epoch', ('CREATE TABLE group_retention_epoch ('
        'id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)')
    yield TABLE, (f'CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY CHECK(id=1),'
        'revision INTEGER NOT NULL,phase TEXT NOT NULL,source INTEGER NOT NULL DEFAULT 0,'
        "cursor TEXT NOT NULL DEFAULT '',cursor2 TEXT NOT NULL DEFAULT '',cursor3 TEXT NOT NULL DEFAULT '')")
    yield 'group_retention_generations', ('CREATE TABLE group_retention_generations ('
        'generation_id INTEGER PRIMARY KEY,expand INTEGER NOT NULL,pending INTEGER NOT NULL,'
        "cursor TEXT NOT NULL DEFAULT '')")
    yield 'ix_group_retention_generation_queue', ('CREATE INDEX ix_group_retention_generation_queue '
        'ON group_retention_generations(pending,generation_id)')
    yield 'group_retention_aliases', ('CREATE TABLE group_retention_aliases ('
        "external_version TEXT PRIMARY KEY,pending INTEGER NOT NULL,cursor TEXT NOT NULL DEFAULT '') WITHOUT ROWID")
    yield 'ix_group_retention_alias_queue', ('CREATE INDEX ix_group_retention_alias_queue '
        'ON group_retention_aliases(pending,external_version)')
    yield 'group_retention_edges', ('CREATE TABLE group_retention_edges ('
        'source_alias TEXT NOT NULL,target_alias TEXT NOT NULL,PRIMARY KEY(source_alias,target_alias)) WITHOUT ROWID')
    sources = {
        'group_head': None,
        'group_evidence_generations': None,
        'group_evidence_versions': None,
        'group_builds': "{row}.owner<>'cleanup'",
        'group_publications': None,
        'generation_groups': None,
        'group_generation_lineage': None,
    }
    if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recommendation_generations'").fetchone():
        sources['recommendation_generations'] = '{row}.group_generation_id IS NOT NULL'
    for table, condition in sources.items():
        # Deletions of derived publications/mappings cannot introduce a root.
        # Excluding them keeps the completed closure valid while it is consumed.
        events = ('INSERT',) if table in ('group_publications', 'generation_groups', 'group_generation_lineage') else ('INSERT', 'DELETE')
        for event in events:
            name = f'group_retention_changed_{table}_{event.lower()}'
            where = (' WHEN ' + condition.format(row='NEW' if event == 'INSERT' else 'OLD')) if condition else ''
            if table == 'recommendation_generations':
                row = 'NEW' if event == 'INSERT' else 'OLD'
                where += (f' AND NOT EXISTS(SELECT 1 FROM recommendation_generations r '
                          f'WHERE r.group_generation_id={row}.group_generation_id AND r.id<>{row}.id)')
            yield name, (f'CREATE TRIGGER {name} AFTER {event} ON {table}{where} BEGIN '
                'UPDATE group_retention_epoch SET revision=revision+1 WHERE id=1; END')
    for table, columns, condition in (
        ('group_head', 'generation_id', 'OLD.generation_id<>NEW.generation_id'),
        ('group_builds', 'generation_id,parent_generation_id,owner',
         "(OLD.owner<>'cleanup' OR NEW.owner<>'cleanup') AND (OLD.generation_id<>NEW.generation_id "
         'OR OLD.parent_generation_id IS NOT NEW.parent_generation_id OR OLD.owner<>NEW.owner)'),
    ):
        name = 'group_retention_changed_' + table + '_update'
        yield name, (f'CREATE TRIGGER {name} AFTER UPDATE OF {columns} ON {table} WHEN {condition} BEGIN '
            'UPDATE group_retention_epoch SET revision=revision+1 WHERE id=1; END')
    yield 'retained_group_publication_delete', publication_guard()


def validate(conn):
    from .migrations.current import _tokens
    for name, sql in definitions(conn):
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or _tokens(row[0]) != _tokens(sql):
            raise MigrationError('Incompatible group retirement progress: ' + name)


def install(conn):
    for name, sql in definitions(conn):
        if name == 'retained_group_publication_delete':
            conn.execute('DROP TRIGGER retained_group_publication_delete')
        conn.execute(sql)
    conn.execute('INSERT INTO group_retention_epoch VALUES (1,0)')
