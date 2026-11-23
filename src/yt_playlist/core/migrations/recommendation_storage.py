"""Store recommendation leases, selections and retirement on their owners."""
from .. import recommendation_revalidations
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'fold_recommendation_generation_state'
PREVIEW_REVISION = 'track_recommendation_selection_revisions'
MARKER = 'uq_recommendation_selected_surface'
RETIRED = ('recommendation_builds', 'recommendation_bundle_framing', 'recommendation_heads',
           'recommendation_pool_retirements', 'recommendation_report_heads')
OWNERS = ('recommendation_generations', 'recommendation_generation_surfaces', 'recommendation_pools',
          'recommendation_build_pools', 'recommendation_report_publications')
FRAMING = {
    'presence': 'INTEGER CHECK(presence BETWEEN 0 AND 2047)',
    'comfort_pool': "INTEGER CHECK(typeof(comfort_pool)='integer' AND comfort_pool>=0 OR comfort_pool IS NULL)",
    'year_low': "INTEGER CHECK(typeof(year_low)='integer' OR year_low IS NULL)",
    'year_high': "INTEGER CHECK(year_high IS NULL AND year_low IS NULL OR typeof(year_high)='integer' AND year_low IS NOT NULL AND year_low<=year_high)",
    'full_pools': 'INTEGER CHECK(full_pools IN (0,1))',
    'unmodeled': 'INTEGER CHECK(unmodeled IN (0,1))',
    'musical_generation': 'TEXT', 'radio_generation': 'TEXT', 'radio_space': 'TEXT',
    'intensity_version': "INTEGER CHECK(typeof(intensity_version)='integer' AND intensity_version>=0 OR intensity_version IS NULL)",
    'radio_profile_version': 'TEXT', 'vibe_version': 'TEXT', 'vibe_profile_version': 'TEXT',
}
FLAGS = ('recommendation_generation_surfaces', 'recommendation_report_publications')
REPLACEMENTS = {
    'FROM recommendation_heads WHERE generation_id=':
        'FROM recommendation_generation_surfaces WHERE selected=1 AND generation_id=',
    'NOT EXISTS(SELECT 1 FROM recommendation_bundle_framing WHERE generation_id=OLD.id)':
        'NEW.presence IS NULL',
    'NOT EXISTS(SELECT 1 FROM recommendation_report_heads WHERE publication_id=OLD.id)':
        'NEW.selected=0',
    'BEFORE UPDATE ON recommendation_generation_surfaces':
        'BEFORE UPDATE OF generation_id,surface,pool_count,output_count ON recommendation_generation_surfaces',
    'EXISTS(SELECT 1 FROM recommendation_pool_retirements WHERE pool_id=NEW.pool_id)':
        'EXISTS(SELECT 1 FROM recommendation_pools WHERE id=NEW.pool_id AND retiring=1)',
}
PREVIEW_UPDATE = 'group_preview_changed_recommendation_generation_surfaces_update'
INDEXES = (
    f'CREATE UNIQUE INDEX {MARKER} ON recommendation_generation_surfaces(surface) WHERE selected=1',
    'CREATE UNIQUE INDEX uq_recommendation_selected_report ON recommendation_report_publications(surface) WHERE selected=1',
    'CREATE INDEX ix_recommendation_build_expiry ON recommendation_generations(build_expires_at,id) '
    'WHERE build_expires_at IS NOT NULL',
)


def _affected(conn):
    return {(kind, name, owner): sql for kind, name, owner, sql in conn.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema WHERE sql IS NOT NULL ORDER BY name")
        if owner in RETIRED or owner == 'recommendation_build_pools' or name in (*OWNERS, PREVIEW_UPDATE)
        or kind == 'trigger' and any(fragment in sql for fragment in REPLACEMENTS)}


def predecessor_objects(conn):
    return list(_affected(conn).values())


def install(conn, _context):
    affected = _affected(conn)
    for kind, name, owner in affected:
        if kind == 'trigger' and owner not in RETIRED:
            conn.execute(f'DROP TRIGGER {name}')

    conn.execute("ALTER TABLE recommendation_generations ADD COLUMN build_owner TEXT CHECK(length(build_owner)>0)")
    conn.execute('ALTER TABLE recommendation_generations ADD COLUMN build_expires_at REAL '
                 "CHECK((build_owner IS NULL)=(build_expires_at IS NULL) AND "
                 "(build_expires_at IS NULL OR typeof(build_expires_at) IN ('integer','real') "
                 'AND abs(build_expires_at)<=1.7976931348623157e308))')
    for column, declaration in FRAMING.items():
        conn.execute(f'ALTER TABLE recommendation_generations ADD COLUMN {column} {declaration}')
    if conn.execute('SELECT 1 FROM recommendation_builds b JOIN recommendation_generations g ON g.id=b.generation_id '
                    'WHERE b.plan_digest<>g.output_digest LIMIT 1').fetchone():
        raise MigrationError('Recommendation build digest differs from its generation')
    conn.execute('UPDATE recommendation_generations AS g SET build_owner=b.owner,build_expires_at=b.expires_at '
                 'FROM recommendation_builds b WHERE g.id=b.generation_id')
    fields = ','.join(f'{field}=b.{field}' for field in FRAMING)
    conn.execute(f'UPDATE recommendation_generations AS g SET {fields} '
                 'FROM recommendation_bundle_framing b WHERE g.id=b.generation_id')

    for owner in FLAGS:
        conn.execute(f'ALTER TABLE {owner} ADD COLUMN selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0,1))')
    conn.execute('UPDATE recommendation_generation_surfaces AS s SET selected=1 '
                 'FROM recommendation_heads h WHERE s.generation_id=h.generation_id AND s.surface=h.surface')
    conn.execute('UPDATE recommendation_report_publications SET selected=1 '
                 'WHERE id IN (SELECT publication_id FROM recommendation_report_heads)')
    conn.execute('ALTER TABLE recommendation_pools ADD COLUMN retiring INTEGER NOT NULL DEFAULT 0 CHECK(retiring IN (0,1))')
    conn.execute('UPDATE recommendation_pools SET retiring=1 WHERE id IN (SELECT pool_id FROM recommendation_pool_retirements)')

    table = 'recommendation_build_pools'
    sql = affected['table', table, table].replace('REFERENCES recommendation_builds(generation_id)',
                                                   'REFERENCES recommendation_generations(id)')
    _replace_table(conn, table, sql)
    for name in RETIRED:
        conn.execute(f'DROP TABLE {name}')
    for sql in INDEXES:
        conn.execute(sql)

    regenerated = {name for name, _ in recommendation_revalidations.guards()}
    regenerated.add('live_recommendation_pool_revalidation')
    for (kind, name, owner), sql in affected.items():
        if owner == table and kind == 'index':
            conn.execute(sql)
        if kind != 'trigger' or owner in RETIRED or name in regenerated or name == PREVIEW_UPDATE:
            continue
        for previous, current in REPLACEMENTS.items():
            sql = sql.replace(previous, current)
        conn.execute(sql)
    group_scoped = 'group_version_id' in definitions(conn, ('recommendation_pools',))['recommendation_pools']
    for name, sql in recommendation_revalidations.definitions(group_scoped=group_scoped, retirement_inline=True):
        if name in regenerated:
            conn.execute(sql)
    _guards(conn)
    conn.execute("UPDATE group_preview_work SET invalid=1 WHERE EXISTS(SELECT 1 FROM group_preview_dependencies "
                 "WHERE table_name='recommendation_heads')")
    conn.execute(preview_guard(conn, selection=False))


def preview_guard(conn, *, selection):
    from .. import group_preview_work
    return dict(group_preview_work.guards(conn, sources={'recommendation_generation_surfaces'},
                                          recommendation_selection=selection))[PREVIEW_UPDATE]


def install_preview_revision(conn, _context):
    conn.execute(f'DROP TRIGGER {PREVIEW_UPDATE}')
    conn.execute(preview_guard(conn, selection=True))


def validate_preview_revision(conn):
    actual = definitions(conn, (PREVIEW_UPDATE,)).get(PREVIEW_UPDATE, '')
    if "record_key='group:selection'" not in actual:
        raise MigrationError('Recommendation preview does not track selection revisions')


def _guards(conn):
    framing = ','.join(FRAMING)
    conn.execute(f'''CREATE TRIGGER frozen_recommendation_framing BEFORE UPDATE OF {framing} ON recommendation_generations
        WHEN OLD.status<>'building' BEGIN SELECT RAISE(ABORT,'Recommendation publication content is immutable'); END''')
    conn.execute('''CREATE TRIGGER recommendation_published_lease BEFORE UPDATE OF build_owner ON recommendation_generations
        WHEN NEW.build_owner IS NULL AND EXISTS(SELECT 1 FROM recommendation_build_pools WHERE generation_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Recommendation publication has unfinished pools'); END''')
    conn.execute('''CREATE TRIGGER active_recommendation_build_pool BEFORE INSERT ON recommendation_build_pools
        WHEN NOT EXISTS(SELECT 1 FROM recommendation_generations WHERE id=NEW.generation_id
            AND status IN ('building','ready') AND build_owner IS NOT NULL AND build_owner<>'cleanup')
        BEGIN SELECT RAISE(ABORT,'Recommendation pool requires an active build'); END''')
    conn.execute('''CREATE TRIGGER published_recommendation_surface_insert BEFORE INSERT ON recommendation_generation_surfaces
        WHEN NEW.selected=1 BEGIN SELECT RAISE(ABORT,'Recommendation head requires published output'); END''')
    conn.execute('''CREATE TRIGGER published_recommendation_surface_update BEFORE UPDATE OF selected ON recommendation_generation_surfaces
        WHEN NEW.selected=1 AND NOT EXISTS(SELECT 1 FROM recommendation_generations WHERE id=NEW.generation_id AND status='published')
        BEGIN SELECT RAISE(ABORT,'Recommendation head requires published output'); END''')
    conn.execute('''CREATE TRIGGER published_recommendation_report_insert BEFORE INSERT ON recommendation_report_publications
        WHEN NEW.selected=1 BEGIN SELECT RAISE(ABORT,'Report head requires a published report'); END''')
    conn.execute('''CREATE TRIGGER published_recommendation_report_update BEFORE UPDATE OF selected ON recommendation_report_publications
        WHEN NEW.selected=1 AND NEW.status<>'published' BEGIN SELECT RAISE(ABORT,'Report head requires a published report'); END''')


def validate(conn):
    actual = definitions(conn, (*RETIRED, MARKER))
    if MARKER not in actual or actual.keys() & set(RETIRED):
        raise MigrationError('Recommendation generation state was not folded')
