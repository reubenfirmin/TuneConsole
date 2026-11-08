"""Add shared policy storage without scanning or rewriting legacy policy JSON."""


def statements():
    yield ('CREATE TABLE rec_policy_bases (base_version TEXT PRIMARY KEY,configuration TEXT NOT NULL,'
           'byte_size INTEGER NOT NULL CHECK(byte_size>=0),created_at REAL NOT NULL,'
           'served INTEGER NOT NULL DEFAULT 0 CHECK(served IN (0,1)))')
    yield ('CREATE TABLE rec_policy_variants (model_version TEXT PRIMARY KEY '
           'REFERENCES rec_serving_policies(model_version) ON DELETE CASCADE,'
           'base_version TEXT NOT NULL REFERENCES rec_policy_bases(base_version),'
           'served INTEGER NOT NULL DEFAULT 0 CHECK(served IN (0,1)))')
    yield 'CREATE INDEX ix_rec_policy_base ON rec_policy_variants(base_version,served,model_version)'
    yield 'CREATE INDEX ix_rec_policy_unused_variants ON rec_policy_variants(model_version) WHERE served=0'
    yield 'CREATE INDEX ix_rec_policy_unused_bases ON rec_policy_bases(base_version) WHERE served=0'


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)


def validate(conn, *, replaced=frozenset()):
    from .current import _tokens
    from .runner import MigrationError
    from ..schema_inspection import objects
    definitions = {name: sql for name, (_kind, sql) in objects(conn).items()}
    for sql in statements():
        name = sql.split()[2]
        if name in replaced:
            continue
        actual = definitions.get(name)
        if actual is None or _tokens(actual) != _tokens(sql):
            raise MigrationError(f'Incompatible shared policy structure: {name}')
