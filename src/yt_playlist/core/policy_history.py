"""Historical schema definitions for policy attribution changes 22 and 24."""


def definitions():
    yield ('recommendation_policy_history', 'CREATE TABLE recommendation_policy_history ('
        'version TEXT PRIMARY KEY NOT NULL CHECK(length(version)>0),created_at REAL NOT NULL,'
        "state TEXT NOT NULL CHECK(state IN ('archived','expired'))) WITHOUT ROWID")
    yield ('historical_impression_policies', 'CREATE TABLE historical_impression_policies ('
        'impression_id INTEGER NOT NULL REFERENCES rec_served_impressions(id) ON DELETE CASCADE,'
        "kind TEXT NOT NULL CHECK(kind IN ('primary','pool')),policy_version TEXT NOT NULL "
        'REFERENCES recommendation_policy_history(version),PRIMARY KEY(impression_id,kind)) WITHOUT ROWID')
    yield ('ix_historical_impression_policy','CREATE INDEX ix_historical_impression_policy '
        'ON historical_impression_policies(policy_version,impression_id)')
    yield ('policy_history_archive','CREATE TABLE policy_history_archive ('
        'id INTEGER PRIMARY KEY CHECK(id=1),publication BLOB NOT NULL CHECK(length(publication)=32))')
    yield ('historical_impression_matches', 'CREATE TRIGGER historical_impression_matches BEFORE INSERT '
        'ON historical_impression_policies WHEN NOT EXISTS(SELECT 1 FROM rec_served_impressions i '
        "WHERE i.id=NEW.impression_id AND ((NEW.kind='primary' AND NEW.policy_version=i.model_version) OR "
        "(NEW.kind='pool' AND NEW.policy_version=CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT)))) "
        "BEGIN SELECT RAISE(ABORT,'Historical policy does not match its impression'); END")
    yield ('historical_impression_immutable','CREATE TRIGGER historical_impression_immutable BEFORE UPDATE '
        "ON historical_impression_policies BEGIN SELECT RAISE(ABORT,'Historical policy attribution is immutable'); END")
    yield ('historical_impression_retained','CREATE TRIGGER historical_impression_retained BEFORE DELETE '
        'ON historical_impression_policies WHEN EXISTS(SELECT 1 FROM rec_served_impressions WHERE id=OLD.impression_id) '
        "BEGIN SELECT RAISE(ABORT,'Retained impression requires its historical identity'); END")


def capture_guards():
    yield ('recommendation_impression_link_retained',
        'CREATE TRIGGER recommendation_impression_link_retained BEFORE DELETE ON recommendation_impression_policies '
        'WHEN EXISTS(SELECT 1 FROM rec_served_impressions WHERE id=OLD.impression_id) AND NOT EXISTS('
        'SELECT 1 FROM historical_impression_policies h WHERE h.impression_id=OLD.impression_id '
        'AND h.kind=OLD.kind AND h.policy_version=OLD.raw_version) '
        "BEGIN SELECT RAISE(ABORT,'Retained impression requires its policy evidence'); END")
    yield ('historical_impression_capture',
        'CREATE TRIGGER historical_impression_capture AFTER INSERT ON recommendation_impression_policies '
        "WHEN NEW.resolution_reason='missing_policy' AND EXISTS(SELECT 1 FROM recommendation_policy_history "
        'WHERE version=NEW.raw_version) BEGIN INSERT INTO historical_impression_policies '
        'VALUES (NEW.impression_id,NEW.kind,NEW.raw_version); DELETE FROM recommendation_impression_policies '
        'WHERE impression_id=NEW.impression_id AND kind=NEW.kind; END')


def install_capture(conn, context=None):
    for name,sql in capture_guards():
        conn.execute('DROP TRIGGER IF EXISTS '+name)
        conn.execute(sql)


def validate_capture(conn):
    from .schema_contract import tokens as _tokens
    for name,sql in capture_guards():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0])!=_tokens(sql):
            raise ValueError('Incompatible historical policy capture: '+name)


def install(conn, context=None):
    for _,sql in definitions():
        conn.execute(sql)


def validate(conn):
    from .schema_contract import tokens as _tokens
    for name,sql in definitions():
        row = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?',(name,)).fetchone()
        if row is None or _tokens(row[0])!=_tokens(sql):
            raise ValueError('Incompatible historical policy owner: '+name)
