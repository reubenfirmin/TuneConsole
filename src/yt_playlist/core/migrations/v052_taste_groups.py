"""Immutable musical group definitions; adaptive discovery starts in shadow mode."""


def apply(conn, context):
    statements = (
        'CREATE TABLE rec_group_clock (id INTEGER PRIMARY KEY CHECK(id=1),serial INTEGER NOT NULL)',
        'INSERT INTO rec_group_clock VALUES (1,0)',
        "CREATE INDEX ix_group_library_inputs ON rec_prepared_content(identity_key) "
        "WHERE json_type(payload,'$.library')='object'",
        'CREATE TABLE rec_group_generations (generation TEXT PRIMARY KEY,policy TEXT NOT NULL,'
        'fingerprint TEXT NOT NULL,created_at REAL NOT NULL,serial INTEGER NOT NULL,payload TEXT NOT NULL)',
        'CREATE TABLE rec_group_identities (group_id TEXT PRIMARY KEY,legacy_mode_id INTEGER UNIQUE,'
        'created_at REAL NOT NULL)',
        'CREATE TABLE rec_group_definitions (version TEXT PRIMARY KEY,generation TEXT NOT NULL '
        'REFERENCES rec_group_generations(generation),group_id TEXT NOT NULL REFERENCES rec_group_identities(group_id),'
        'parent_version TEXT REFERENCES rec_group_definitions(version),payload TEXT NOT NULL,'
        'UNIQUE(generation,group_id))',
        'CREATE TABLE rec_group_members (version TEXT NOT NULL REFERENCES rec_group_definitions(version),'
        'identity_key TEXT NOT NULL,similarity REAL NOT NULL CHECK(similarity BETWEEN 0 AND 1),'
        'support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),PRIMARY KEY(version,identity_key)) WITHOUT ROWID',
        'CREATE INDEX ix_group_member_key ON rec_group_members(identity_key,version)',
        'CREATE TABLE rec_group_unknown (generation TEXT NOT NULL REFERENCES rec_group_generations(generation),'
        'identity_key TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(generation,identity_key)) WITHOUT ROWID',
        'CREATE TABLE rec_group_lineage (source TEXT NOT NULL REFERENCES rec_group_definitions(version),'
        'target TEXT NOT NULL REFERENCES rec_group_definitions(version),relation TEXT NOT NULL '
        "CHECK(relation IN ('continuation','split','merge','legacy')),"
        'payload TEXT NOT NULL,PRIMARY KEY(source,target)) WITHOUT ROWID',
        'CREATE TABLE rec_group_head (id INTEGER PRIMARY KEY CHECK(id=1),generation TEXT NOT NULL '
        'REFERENCES rec_group_generations(generation))',
        # Only the current graph is cached. Definitions/membership/lineage remain durable.
        'CREATE TABLE rec_group_edges (left_key TEXT NOT NULL,right_key TEXT NOT NULL,weight REAL NOT NULL,'
        'support REAL NOT NULL,PRIMARY KEY(left_key,right_key)) WITHOUT ROWID',
        'CREATE TABLE rec_group_assignments (identity_key TEXT PRIMARY KEY,generation TEXT NOT NULL '
        'REFERENCES rec_group_generations(generation),version TEXT REFERENCES rec_group_definitions(version),'
        'similarity REAL NOT NULL,support REAL NOT NULL,profile_digest TEXT,prepared_at REAL NOT NULL)',
    )
    for sql in statements:
        conn.execute(sql)
    for table in ('rec_group_generations', 'rec_group_definitions', 'rec_group_members',
                  'rec_group_lineage', 'rec_group_unknown'):
        for event in ('UPDATE', 'DELETE'):
            conn.execute(f'CREATE TRIGGER immutable_{table}_{event.lower()} BEFORE {event} ON {table} '
                         "BEGIN SELECT RAISE(ABORT,'Musical group history is immutable'); END")
    for table in ('rec_prepared_content', 'rec_musical_profiles', 'taste_modes'):
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            conn.execute(f'CREATE TRIGGER group_clock_{table}_{event.lower()} AFTER {event} ON {table} '
                         'BEGIN UPDATE rec_group_clock SET serial=serial+1 WHERE id=1; END')
    # Existing centroids, labels and raw feedback remain untouched. Capture legacy definitions
    # in the first worker pass; exact old membership cannot be reconstructed by this upgrade.
