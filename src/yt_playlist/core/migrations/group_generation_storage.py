"""Keep group publication and its build lease on the generation row."""
from .. import group_preview_work
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _preserve_sequence, _sequence
from .track_source_cutover import _replace_table


NAME = 'fold_group_generation_storage'
MARKER = 'uq_active_group_generation'
RETIRED = ('group_builds', 'group_head', 'group_publications', 'group_lineage_retirements')
FK_OWNERS = ('group_generations', 'recommendation_generations', *RETIRED)
_OWNERS = {'group_generations', 'group_preparation_state', 'recommendation_generations', *RETIRED}


def _objects(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name")
        if row[2] in _OWNERS or any(table in row[3] for table in RETIRED)]


def predecessor_objects(conn):
    return [sql for _, _, _, sql in _objects(conn)]


def _columns(conn):
    for column, definition in (
        ('published_at', "REAL CHECK(published_at IS NULL OR (typeof(published_at) IN ('real','integer') "
                         'AND abs(published_at)<=1.7976931348623157e308))'),
        ('is_published', 'INTEGER NOT NULL DEFAULT 0 CHECK(is_published IN (0,1) '
                         'AND (is_published=1 OR published_at IS NULL))'),
        ('is_active', 'INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1) AND is_active<=is_published)'),
        ('build_owner', 'TEXT CHECK(build_owner IS NULL OR (length(build_owner)>0 AND is_published=0))'),
        ('build_expires_at', "REAL CHECK((build_owner IS NULL)=(build_expires_at IS NULL) AND "
                             "(build_expires_at IS NULL OR (typeof(build_expires_at) IN ('real','integer') "
                             'AND abs(build_expires_at)<=1.7976931348623157e308)))'),
        ('build_ready', 'INTEGER NOT NULL DEFAULT 0 CHECK(build_ready IN (0,1) '
                        'AND (build_ready=0 OR build_owner IS NOT NULL))'),
        ('build_output_digest', "TEXT CHECK(build_output_digest IS NULL OR (build_owner IS NOT NULL "
                                "AND length(build_output_digest)=64 AND build_output_digest NOT GLOB '*[^0-9a-f]*'))"),
        ('build_parent_id', 'INTEGER REFERENCES group_generations(id) '
                            'CHECK(build_parent_id IS NULL OR build_owner IS NOT NULL)'),
    ):
        conn.execute(f'ALTER TABLE group_generations ADD COLUMN {column} {definition}')
    conn.execute('ALTER TABLE group_preparation_state ADD COLUMN retiring_source_alias TEXT')
    conn.execute('ALTER TABLE group_preparation_state ADD COLUMN retiring_target_alias TEXT '
                 'CHECK((retiring_source_alias IS NULL)=(retiring_target_alias IS NULL))')


def _guards(conn, previous):
    conn.execute(previous['complete_group_publication']
        .replace('BEFORE INSERT ON group_publications WHEN ',
                 'BEFORE UPDATE OF is_published ON group_generations WHEN OLD.is_published=0 AND NEW.is_published=1 AND (')
        .replace('NEW.generation_id', 'NEW.id')
        .replace('EXISTS(SELECT 1 FROM group_builds WHERE generation_id=NEW.id AND ready=0)',
                 '(OLD.build_owner IS NOT NULL AND OLD.build_ready=0)')
        .replace(" BEGIN", ") BEGIN"))
    conn.execute('''CREATE TRIGGER new_group_generation_is_unpublished BEFORE INSERT ON group_generations
        WHEN NEW.is_published=1 BEGIN SELECT RAISE(ABORT,'Publish a staged musical group generation'); END''')
    conn.execute(previous['retained_group_publication_update'].replace(
        'BEFORE UPDATE ON group_publications', 'BEFORE UPDATE OF published_at ON group_generations '
        'WHEN OLD.is_published=1 AND NEW.is_published=1 AND OLD.published_at IS NOT NEW.published_at'))
    conn.execute(previous['retained_group_publication_delete']
        .replace('BEFORE DELETE ON group_publications WHEN ', 'BEFORE UPDATE OF is_published ON group_generations '
                 'WHEN OLD.is_published=1 AND NEW.is_published=0 AND (')
        .replace("NOT EXISTS(SELECT 1 FROM group_builds WHERE generation_id=OLD.generation_id AND owner='cleanup')",
                 "NEW.build_owner IS NOT 'cleanup'")
        .replace('EXISTS(SELECT 1 FROM group_head WHERE generation_id=OLD.generation_id)', 'OLD.is_active=1')
        .replace('FROM group_builds WHERE parent_generation_id=', 'FROM group_generations WHERE build_parent_id=')
        .replace('OLD.generation_id', 'OLD.id').replace(" BEGIN", ") BEGIN"))
    for name in ('retained_group_checkpoint_publication_delete', 'retained_recommendation_group'):
        conn.execute(previous[name].replace('BEFORE DELETE ON group_publications WHEN ',
            'BEFORE UPDATE OF is_published ON group_generations WHEN OLD.is_published=1 AND NEW.is_published=0 AND ')
            .replace('OLD.generation_id', 'OLD.id'))
    conn.execute('''CREATE TRIGGER retained_group_generation_delete BEFORE DELETE ON group_generations
        WHEN OLD.is_published=1 BEGIN SELECT RAISE(ABORT,'Retire musical group publication before deleting it'); END''')
    for action in ('insert', 'update'):
        for table, column in (('group_generations', 'build_parent_id'),
                              ('recommendation_generations', 'group_generation_id')):
            event = 'INSERT' if action == 'insert' else f'UPDATE OF {column}'
            conn.execute(f'''CREATE TRIGGER published_group_parent_{table}_{action} BEFORE {event} ON {table}
                WHEN NEW.{column} IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM group_generations WHERE id=NEW.{column} AND is_published=1)
                BEGIN SELECT RAISE(ABORT,'Musical group reference requires a published generation'); END''')
        name = 'staged_group_version_build_' + action
        conn.execute(previous[name].replace(
            'FROM group_builds WHERE generation_id=NEW.build_generation_id AND ready=0',
            'FROM group_generations WHERE id=NEW.build_generation_id AND build_ready=0 AND build_owner IS NOT NULL'))
    for action, event in (('delete', 'DELETE'), ('update', 'UPDATE OF build_owner')):
        conn.execute(f'''CREATE TRIGGER retained_group_build_{action} BEFORE {event} ON group_generations
            WHEN {'NEW.build_owner IS NULL AND ' if action == 'update' else ''}
                EXISTS(SELECT 1 FROM group_versions WHERE build_generation_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'Musical group build still owns staged versions'); END''')
    conn.execute('''CREATE TRIGGER group_retention_changed_generation AFTER UPDATE
        OF build_owner,build_parent_id,is_published,is_active ON group_generations
        WHEN OLD.is_active<>NEW.is_active OR (OLD.is_published=0 AND NEW.is_published=1)
            OR ((coalesce(OLD.build_owner,'cleanup')<>'cleanup' OR coalesce(NEW.build_owner,'cleanup')<>'cleanup')
                AND (OLD.build_owner IS NOT NEW.build_owner OR OLD.build_parent_id IS NOT NEW.build_parent_id))
        BEGIN UPDATE group_preparation_state SET retention_epoch=retention_epoch+1 WHERE singleton=1; END''')
    for event, row in (('INSERT', 'NEW'), ('DELETE', 'OLD')):
        conn.execute(f'''CREATE TRIGGER group_retention_changed_generation_{event.lower()}
            AFTER {event} ON group_generations WHEN {row}.build_owner<>'cleanup'
            BEGIN UPDATE group_preparation_state SET retention_epoch=retention_epoch+1 WHERE singleton=1; END''')
    conn.execute('''CREATE TRIGGER fenced_group_lineage_retirement
        BEFORE UPDATE OF retiring_source_alias,retiring_target_alias ON group_preparation_state
        WHEN NEW.retiring_source_alias IS NOT NULL AND (NEW.roots_complete<>1 OR NOT EXISTS(
            SELECT 1 FROM group_lineage WHERE source_alias=NEW.retiring_source_alias
                AND target_alias=NEW.retiring_target_alias))
        BEGIN SELECT RAISE(ABORT,'Musical group evidence backfill is incomplete or lineage edge is missing'); END''')


def install(conn, _context):
    prior_schema = conn.execute('PRAGMA main.schema_version').fetchone()[0]
    objects = _objects(conn)
    previous = {name: sql for _, name, _, sql in objects}
    for kind, name, owner, sql in objects:
        if kind == 'trigger':
            conn.execute('DROP TRIGGER ' + name)
    _columns(conn)
    conn.execute('''UPDATE group_generations AS g SET
        (build_owner,build_expires_at,build_ready,build_output_digest,build_parent_id)=
            (SELECT owner,expires_at,ready,output_digest,parent_generation_id FROM group_builds WHERE generation_id=g.id)
        WHERE EXISTS(SELECT 1 FROM group_builds WHERE generation_id=g.id)''')
    conn.execute('''UPDATE group_generations AS g SET is_published=1,
        published_at=(SELECT published_at FROM group_publications WHERE generation_id=g.id),
        is_active=EXISTS(SELECT 1 FROM group_head WHERE generation_id=g.id)
        WHERE EXISTS(SELECT 1 FROM group_publications WHERE generation_id=g.id)''')
    conn.execute('''UPDATE group_preparation_state SET (retiring_source_alias,retiring_target_alias)=
        (SELECT source_alias,target_alias FROM group_lineage_retirements LIMIT 1)
        WHERE singleton=1''')
    if conn.execute('SELECT count(*) FROM group_lineage_retirements').fetchone()[0] > 1:
        raise MigrationError('Multiple unfinished musical group lineage retirements')
    highwater = _sequence(conn, 'recommendation_generations')
    _replace_table(conn, 'recommendation_generations', previous['recommendation_generations'].replace(
        'REFERENCES group_publications(generation_id)', 'REFERENCES group_generations(id)'))
    _preserve_sequence(conn, 'recommendation_generations', highwater)
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON group_generations(is_active) WHERE is_active=1')
    conn.execute('CREATE INDEX ix_compact_group_build_expiry ON group_generations(build_expires_at,id)')
    conn.execute('CREATE INDEX ix_compact_group_build_parent ON group_generations(build_parent_id,id)')
    for kind, name, owner, sql in objects:
        if kind == 'index' and owner == 'recommendation_generations':
            conn.execute(sql)
        if kind != 'trigger' or owner in RETIRED or name in ('fenced_group_lineage_retirement',
                'staged_group_version_build_insert', 'staged_group_version_build_update'
                ) or name.startswith('group_preview_changed_group_generations_'):
            continue
        for side in ('OLD', 'NEW'):
            sql = sql.replace(f'FROM group_publications WHERE generation_id={side}.generation_id',
                              f'FROM group_generations WHERE id={side}.generation_id AND is_published=1')
        conn.execute(sql.replace(
            'FROM group_lineage_retirements WHERE source_alias=OLD.source_alias AND target_alias=OLD.target_alias',
            'FROM group_preparation_state WHERE singleton=1 AND retiring_source_alias=OLD.source_alias '
            'AND retiring_target_alias=OLD.target_alias'))
    conn.execute("""INSERT OR IGNORE INTO group_preview_dependencies
        SELECT 'group_generations',CASE table_name WHEN 'group_head' THEN 'group:selection' ELSE record_key END
        FROM group_preview_dependencies WHERE table_name IN ('group_head','group_publications')""")
    conn.execute("DELETE FROM group_preview_dependencies WHERE table_name IN ('group_head','group_publications')")
    group_preview_work.install_guards(conn, sources={'group_generations'})
    _guards(conn, previous)
    schema = conn.execute('PRAGMA main.schema_version').fetchone()[0]
    conn.execute('UPDATE group_preview_work SET schema_version=? WHERE schema_version=? AND invalid=0',
                 (schema, prior_schema))


def validate(conn):
    objects = definitions(conn, (MARKER, *RETIRED))
    if MARKER not in objects or any(name in objects for name in RETIRED):
        raise MigrationError('Musical group generation storage was not folded')
