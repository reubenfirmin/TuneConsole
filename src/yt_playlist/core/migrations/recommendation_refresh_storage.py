"""Keep refresh notifications and revalidated policies on their owning rows."""
import re

from .. import group_preview_work
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'consolidate_recommendation_refresh_storage'
MARKER = 'ix_recommendation_surface_revalidated_policy'
RETIRED = ('rec_change_queue', 'recommendation_pool_revalidations')
SURFACE_POOLS = 'recommendation_surface_pools'
CHECKPOINT = 'content_full_checkpoint'
REQUEST_REFRESH = 'UPDATE content_full_checkpoint SET refresh_revision=refresh_revision+1 WHERE singleton=1'
REVALIDATIONS = 'recommendation_pool_revalidations'
PREVIEW_UPDATE = 'group_preview_changed_recommendation_surface_pools_update'
_OLD_NOTIFY = re.compile(r"INSERT INTO rec_change_queue\(kind,item\) VALUES \('refresh',''\)\s+"
                         r'ON CONFLICT\(kind,item\) DO UPDATE SET revision=revision\+1;')


def refresh_trigger(sql):
    return _OLD_NOTIFY.subn(REQUEST_REFRESH + ';', sql)


def predecessor_objects(conn):
    owners = (*RETIRED, SURFACE_POOLS, CHECKPOINT)
    slots = ','.join('?' for _ in owners)
    return [row[0] for row in conn.execute(
        f'SELECT sql FROM main.sqlite_schema WHERE sql IS NOT NULL AND (tbl_name IN ({slots}) '
        "OR name='retained_recommendation_pool' OR (type='trigger' AND instr(sql,'rec_change_queue')>0)) ORDER BY name",
        owners)]


def install(conn, _context):
    _move_refresh(conn)
    _move_revalidations(conn)


def _move_refresh(conn):
    if conn.execute("SELECT 1 FROM rec_change_queue WHERE kind<>'refresh' OR item<>'' LIMIT 1").fetchone():
        raise MigrationError('Unexpected recommendation refresh queue item')
    for column in ('refresh_revision', 'acknowledged_refresh_revision'):
        conn.execute(f'ALTER TABLE {CHECKPOINT} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
    conn.execute(f'UPDATE {CHECKPOINT} SET refresh_revision=q.revision FROM rec_change_queue q WHERE singleton=1')
    for name, sql in conn.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='trigger' "
                                  "AND instr(sql,'rec_change_queue')>0").fetchall():
        replacement, count = refresh_trigger(sql)
        if count != 1:
            raise MigrationError('Unexpected recommendation refresh trigger: ' + name)
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(replacement)
    conn.execute('DROP TABLE rec_change_queue')


def _move_revalidations(conn):
    slot = ('m.generation_id=v.generation_id AND m.surface=v.surface AND m.mode_key=v.mode_key '
            'AND m.purpose=v.purpose AND m.pool_id=v.pool_id')
    if conn.execute(f'SELECT 1 FROM {REVALIDATIONS} v JOIN recommendation_generations g ON g.id=v.generation_id '
                    f"WHERE g.status<>'retiring' AND NOT EXISTS(SELECT 1 FROM {SURFACE_POOLS} m WHERE {slot}) LIMIT 1").fetchone():
        raise MigrationError('Live pool revalidation has no matching recommendation slot')

    conn.execute(f'ALTER TABLE {SURFACE_POOLS} ADD COLUMN revalidated_policy_version TEXT '
                 'REFERENCES recommendation_policies(version)')
    immutable = ('immutable_recommendation_pool_selection', 'frozen_recommendation_surface_pools_update')
    guards = definitions(conn, immutable)
    for name in (*immutable, PREVIEW_UPDATE, 'complete_recommendation_pool_selection', 'retained_recommendation_pool'):
        conn.execute(f'DROP TRIGGER {name}')
    conn.execute(f'UPDATE {SURFACE_POOLS} AS m SET revalidated_policy_version=v.policy_version '
                 f'FROM {REVALIDATIONS} v WHERE {slot}')
    conn.execute(f'DROP TABLE {REVALIDATIONS}')
    for name in immutable:
        conn.execute(guards[name])
    conn.execute(f'CREATE INDEX {MARKER} ON {SURFACE_POOLS}(revalidated_policy_version) '
                 'WHERE revalidated_policy_version IS NOT NULL')
    conn.execute(f'''CREATE TRIGGER complete_recommendation_pool_selection BEFORE INSERT ON {SURFACE_POOLS}
        WHEN NOT EXISTS(SELECT 1 FROM recommendation_pools p
            JOIN recommendation_generations g ON g.id=NEW.generation_id
            JOIN recommendation_policies gp ON gp.version=g.policy_version
            JOIN recommendation_policies pp ON pp.version=COALESCE(NEW.revalidated_policy_version,p.policy_version)
            WHERE p.id=NEW.pool_id AND p.complete=1 AND p.retiring=0
            AND (NEW.revalidated_policy_version IS NULL OR g.status='building')
            AND COALESCE(gp.base_version,gp.version)=COALESCE(pp.base_version,pp.version))
        BEGIN SELECT RAISE(ABORT,'Recommendation pool has incompatible publication inputs'); END''')
    conn.execute(f'''CREATE TRIGGER retained_recommendation_pool BEFORE UPDATE OF retiring ON recommendation_pools
        WHEN NEW.retiring=1 AND OLD.retiring=0 AND (
            EXISTS(SELECT 1 FROM {SURFACE_POOLS} WHERE pool_id=NEW.id) OR
            EXISTS(SELECT 1 FROM recommendation_build_pools WHERE pool_id=NEW.id) OR
            EXISTS(SELECT 1 FROM prepared_outputs WHERE pool_id=NEW.id))
        BEGIN SELECT RAISE(ABORT,'Recommendation pool still has an owner'); END''')
    conn.execute(dict(group_preview_work.guards(conn, sources={SURFACE_POOLS}))[PREVIEW_UPDATE])
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE EXISTS(SELECT 1 FROM group_preview_dependencies '
                 'WHERE table_name=?)', (REVALIDATIONS,))


def validate(conn):
    if set(definitions(conn, (*RETIRED, MARKER))) != {MARKER}:
        raise MigrationError('Recommendation refresh storage was not consolidated')
