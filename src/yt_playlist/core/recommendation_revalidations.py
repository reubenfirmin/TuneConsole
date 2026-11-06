"""Explicit ownership for exactly revalidated immutable candidate pools."""


def statements():
    yield ('CREATE TABLE recommendation_pool_revalidations ('
           'generation_id INTEGER NOT NULL REFERENCES recommendation_generations(id),'
           'surface TEXT NOT NULL,mode_key TEXT NOT NULL,purpose TEXT NOT NULL,'
           'pool_id INTEGER NOT NULL REFERENCES recommendation_pools(id),'
           'policy_version TEXT NOT NULL REFERENCES recommendation_policies(version),'
           'PRIMARY KEY(generation_id,surface,mode_key,purpose)) WITHOUT ROWID')
    yield ('CREATE INDEX ix_recommendation_pool_revalidation_owner '
           'ON recommendation_pool_revalidations(pool_id,generation_id)')
    yield ('CREATE INDEX ix_recommendation_pool_revalidation_policy '
           'ON recommendation_pool_revalidations(policy_version,generation_id)')
    yield ('CREATE TRIGGER live_recommendation_pool_revalidation BEFORE INSERT ON recommendation_pool_revalidations '
           'WHEN NOT EXISTS(SELECT 1 FROM recommendation_generations g JOIN recommendation_pools p '
           'JOIN recommendation_policies gp ON gp.version=g.policy_version '
           'JOIN recommendation_policies vp ON vp.version=NEW.policy_version '
           "WHERE g.id=NEW.generation_id AND g.status='building' AND p.id=NEW.pool_id AND p.complete=1 "
           'AND COALESCE(gp.base_version,gp.version)=COALESCE(vp.base_version,vp.version)) '
           'OR EXISTS(SELECT 1 FROM recommendation_pool_retirements WHERE pool_id=NEW.pool_id) '
           "BEGIN SELECT RAISE(ABORT,'Pool revalidation requires a private build and complete pool'); END")
    yield ('CREATE TRIGGER immutable_recommendation_pool_revalidation BEFORE UPDATE ON recommendation_pool_revalidations '
           "BEGIN SELECT RAISE(ABORT,'Pool revalidation is immutable'); END")
    yield ('CREATE TRIGGER retained_recommendation_pool_revalidation BEFORE DELETE ON recommendation_pool_revalidations '
           'WHEN EXISTS(SELECT 1 FROM recommendation_surface_pools m '
           'WHERE m.generation_id=OLD.generation_id AND m.surface=OLD.surface AND m.mode_key=OLD.mode_key '
           'AND m.purpose=OLD.purpose AND m.pool_id=OLD.pool_id) '
           "BEGIN SELECT RAISE(ABORT,'Pool revalidation still has a selected owner'); END")
    yield 'DROP TRIGGER complete_recommendation_pool_selection'
    yield ('CREATE TRIGGER complete_recommendation_pool_selection BEFORE INSERT ON recommendation_surface_pools '
           'WHEN EXISTS(SELECT 1 FROM recommendation_pool_retirements WHERE pool_id=NEW.pool_id) OR '
           'NOT EXISTS(SELECT 1 FROM recommendation_pools p JOIN recommendation_generations g '
           'ON g.id=NEW.generation_id JOIN recommendation_policies pp ON pp.version=p.policy_version '
           'JOIN recommendation_policies gp ON gp.version=g.policy_version WHERE p.id=NEW.pool_id AND p.complete=1 '
           'AND (COALESCE(pp.base_version,pp.version)=COALESCE(gp.base_version,gp.version) OR '
           'EXISTS(SELECT 1 FROM recommendation_pool_revalidations v '
           'WHERE v.generation_id=g.id AND v.pool_id=p.id AND v.surface=NEW.surface '
           'AND v.mode_key=NEW.mode_key AND v.purpose=NEW.purpose)) '
           'AND (p.group_version_id IS NULL OR EXISTS(SELECT 1 FROM generation_groups x '
           'WHERE x.generation_id=g.group_generation_id AND x.group_version_id=p.group_version_id))) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool has incompatible publication inputs'); END")
    yield 'DROP TRIGGER retained_recommendation_pool'
    yield ('CREATE TRIGGER retained_recommendation_pool BEFORE INSERT ON recommendation_pool_retirements '
           'WHEN EXISTS(SELECT 1 FROM recommendation_surface_pools WHERE pool_id=NEW.pool_id) OR '
           'EXISTS(SELECT 1 FROM recommendation_build_pools WHERE pool_id=NEW.pool_id) OR '
           'EXISTS(SELECT 1 FROM recommendation_pool_revalidations WHERE pool_id=NEW.pool_id) OR '
           'EXISTS(SELECT 1 FROM prepared_outputs WHERE pool_id=NEW.pool_id) '
           "BEGIN SELECT RAISE(ABORT,'Recommendation pool still has an owner'); END")


def definitions():
    for sql in statements():
        if sql.startswith('CREATE '):
            yield sql.split()[2], sql


def guards():
    return [(name, sql) for name, sql in definitions()
            if name in {'complete_recommendation_pool_selection', 'retained_recommendation_pool'}]


def available(conn):
    return bool(conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='recommendation_pool_revalidations'").fetchone())


def install(conn, context=None):
    if available(conn):
        validate(conn)
        return
    for sql in statements():
        conn.execute(sql)


def install_guards(conn):
    """Keep admitted regular guards after a numbered upgrade replaces owners."""
    for name, sql in guards():
        conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute(sql)


def validate(conn):
    from .schema_contract import MigrationError, tokens
    for name, sql in definitions():
        row = conn.execute('SELECT sql FROM main.sqlite_schema WHERE name=?', (name,)).fetchone()
        if row is None or tokens(row[0]) != tokens(sql):
            raise MigrationError('Incompatible recommendation pool revalidation owner: ' + name)
