"""Install and validate the fixed SQL contracts for file receipts."""
from .schema_contract import MigrationError, tokens
from .schema_inspection import definitions


class ReceiptSchema:
    def __init__(self, **statements):
        self.statements = statements

    def install(self, conn, context=None):
        installed = definitions(conn, self.statements)
        for name, sql in self.statements.items():
            if name not in installed:
                conn.execute(sql)
            elif tokens(installed[name]) != tokens(sql):
                raise MigrationError('Incompatible file receipt schema: ' + name)

    def validate(self, conn):
        installed = definitions(conn, self.statements)
        for name, sql in self.statements.items():
            if tokens(installed.get(name)) != tokens(sql):
                raise MigrationError('Incompatible file receipt schema: ' + name)


collection = ReceiptSchema(collection_file_writes='''CREATE TABLE collection_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 16777216),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

recommendation = ReceiptSchema(recommendation_file_writes='''CREATE TABLE recommendation_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_owner TEXT NOT NULL,
    surface TEXT NOT NULL,
    chunk_key TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 1048576),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

action_backup = ReceiptSchema(action_backup_writes='''CREATE TABLE action_backup_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL CHECK(action_id>0),
    position INTEGER NOT NULL CHECK(position>=0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 67108864),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

annotation = ReceiptSchema(annotation_file_writes='''CREATE TABLE annotation_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL CHECK(length(run_id)>0),
    taxonomy_version TEXT NOT NULL CHECK(length(taxonomy_version)>0),
    revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 4194304),
    annotation_id INTEGER UNIQUE REFERENCES audio_annotations(id),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready')),
    CHECK((file_status='pending' AND annotation_id IS NULL) OR
          (file_status='ready' AND annotation_id IS NOT NULL))
)''')

playlist_backup = ReceiptSchema(playlist_backup_writes='''CREATE TABLE playlist_backup_writes (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    playlist_id INTEGER,
    account_id INTEGER NOT NULL,
    provider_playlist_id TEXT NOT NULL CHECK(length(provider_playlist_id)>0),
    captured_at REAL NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 67108864),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

policy = ReceiptSchema(policy_file_writes='''CREATE TABLE policy_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body INTEGER NOT NULL CHECK(body IN (0,1)),
    version TEXT NOT NULL CHECK(length(version)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

policy_history = ReceiptSchema(
    policy_history_files='''CREATE TABLE policy_history_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL CHECK(length(version)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''',
    ix_policy_history_file_version='CREATE INDEX ix_policy_history_file_version ON policy_history_files(version,id)',
)

def install_policy_history(conn, context=None):
    if definitions(conn, ('policy_history_files',)):
        policy_history.validate(conn)
    else:
        policy_history.install(conn, context)


group = ReceiptSchema(group_file_writes='''CREATE TABLE group_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('definition','generation','lineage')),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

vibe_model = ReceiptSchema(
    vibe_model_file_writes='''CREATE TABLE vibe_model_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation BLOB NOT NULL CHECK(typeof(generation)='blob' AND length(generation)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 16777216),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''',
    ix_vibe_model_file_generation='CREATE INDEX ix_vibe_model_file_generation ON vibe_model_file_writes(generation,file_status,id)',
)

description = ReceiptSchema(description_file_writes='''CREATE TABLE description_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES album_description_requests(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK(attempt_number>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 262144),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

metadata = ReceiptSchema(
    metadata_file_writes='''CREATE TABLE metadata_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('alternatives','native','choices','conflicts')),
    owner_key TEXT NOT NULL CHECK(length(owner_key)>0),
    owner_id INTEGER,
    revision_key TEXT NOT NULL CHECK(length(revision_key)>0),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''',
    metadata_file_references='''CREATE TABLE metadata_file_references (
    kind TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    revision_key TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES metadata_file_writes(id),
    PRIMARY KEY(kind,owner_key,revision_key)
) WITHOUT ROWID''',
    ix_metadata_file_owner='CREATE INDEX ix_metadata_file_owner ON metadata_file_writes(kind,owner_id)',
    ix_metadata_file_reference='CREATE INDEX ix_metadata_file_reference ON metadata_file_references(file_id)',
)

delivery = ReceiptSchema(delivery_file_writes='''CREATE TABLE delivery_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_digest TEXT NOT NULL CHECK(length(contract_digest)=64 AND contract_digest NOT GLOB '*[^0-9a-f]*'),
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    delivery_id INTEGER REFERENCES metadata_deliveries(id),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')

evidence = ReceiptSchema(
    evidence_file_writes='''CREATE TABLE evidence_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('opportunity','response','forecast','artifact','report','checkpoint')),
    record_key TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''',
    evidence_file_references='''CREATE TABLE evidence_file_references (
    kind TEXT NOT NULL,
    record_key TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES evidence_file_writes(id),
    PRIMARY KEY(kind,record_key)
) WITHOUT ROWID''',
    evidence_file_maintenance='''CREATE TABLE evidence_file_maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id=1),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''',
)

impression = ReceiptSchema(impression_file_writes='''CREATE TABLE impression_file_writes (
    impression_id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    rank INTEGER NOT NULL,
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')


def install_sample_files(conn, context=None):
    """Add capture status without rewriting installed sample rows or files."""
    columns = {row[1] for row in conn.execute('PRAGMA main.table_info(sample_attributes)')}
    for name, declaration in (('content_digest', 'TEXT'), ('file_status', "TEXT NOT NULL DEFAULT 'ready'")):
        if name not in columns:
            conn.execute(f'ALTER TABLE sample_attributes ADD COLUMN {name} {declaration}')


def validate_sample_files(conn):
    columns = {row[1]: (row[2], row[3], row[4])
               for row in conn.execute('PRAGMA main.table_info(sample_attributes)')}
    if (columns.get('content_digest') != ('TEXT', 0, None)
            or columns.get('file_status') != ('TEXT', 1, "'ready'")):
        raise MigrationError('Incompatible sample file status')


LEGACY_ANNOTATION_CHECK = "CHECK(substr(model_output_filename,-69)=model_output_digest||'.json')"
NATIVE_ANNOTATION_CHECK = 'CHECK(length(model_output_filename)>0)'


def has_native_annotation_constraint(conn):
    actual = tokens(definitions(conn, ('audio_annotations',)).get('audio_annotations'))
    expected = tokens(NATIVE_ANNOTATION_CHECK)
    return any(actual[index:index + len(expected)] == expected for index in range(len(actual) - len(expected) + 1))


def native_annotation_definition(sql):
    if NATIVE_ANNOTATION_CHECK in sql:
        return sql
    if sql.count(LEGACY_ANNOTATION_CHECK) != 1:
        raise MigrationError('Unrecognized installed annotation filename constraint')
    return sql.replace(LEGACY_ANNOTATION_CHECK, NATIVE_ANNOTATION_CHECK)


embedding = ReceiptSchema(embedding_file_writes='''CREATE TABLE embedding_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('recording','artist')),
    dimensions INTEGER NOT NULL CHECK(typeof(dimensions)='integer' AND dimensions>0),
    content_digest BLOB NOT NULL CHECK(typeof(content_digest)='blob' AND length(content_digest)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 28 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')
EMBEDDING_FILE_COLUMN = 'file_id INTEGER REFERENCES embedding_file_writes(id)'


def embedding_head_definition(kind):
    spaces = {'recording': "'collaborative','content_library','content_discovery'",
              'artist': "'collaborative','content'"}[kind]
    table = kind + '_embedding_files'
    return (f'CREATE TABLE {table} ({kind}_id INTEGER NOT NULL REFERENCES {kind}s(id),'
        f'space TEXT NOT NULL CHECK(space IN ({spaces})),model_version TEXT NOT NULL CHECK(length(model_version)>0),'
        "dimensions INTEGER NOT NULL CHECK(typeof(dimensions)='integer' AND dimensions>0),publication BLOB NOT NULL CHECK(length(publication)=32),"
        'position INTEGER NOT NULL CHECK(position>=0 AND position<128),insertion_order INTEGER NOT NULL CHECK(insertion_order>0),'
        f"built_at REAL CHECK(built_at IS NULL OR (typeof(built_at) IN ('real','integer') AND abs(built_at)<=1.7976931348623157e308)),PRIMARY KEY({kind}_id,space,model_version))")


def native_embedding_definition(sql):
    return sql.replace(',PRIMARY KEY(', ',' + EMBEDDING_FILE_COLUMN + ',PRIMARY KEY(')


def has_native_embedding_heads(conn):
    sql = definitions(conn, ('recording_embedding_files',)).get('recording_embedding_files')
    return 'file_id' in tokens(sql)


def install_embedding(conn, context=None):
    tables = ('recording_embedding_files', 'artist_embedding_files')
    installed = definitions(conn, tables)
    embedding.install(conn, context)
    for table in tables:
        if 'file_id' not in tokens(installed[table]):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {EMBEDDING_FILE_COLUMN}')


def validate_embedding(conn):
    expected = dict(embedding.statements)
    expected.update((kind + '_embedding_files', native_embedding_definition(embedding_head_definition(kind)))
                    for kind in ('recording', 'artist'))
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if tokens(actual.get(name)) != tokens(sql):
            raise MigrationError('Incompatible embedding file identity owner: ' + name)


profile = ReceiptSchema(profile_file_writes='''CREATE TABLE profile_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('recording','source')),
    content_digest BLOB NOT NULL CHECK(typeof(content_digest)='blob' AND length(content_digest)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')
PROFILE_FILE_COLUMN = 'file_id INTEGER REFERENCES profile_file_writes(id)'


def profile_head_definitions():
    yield ('recording_profile_files', 'CREATE TABLE recording_profile_files ('
        'recording_id INTEGER PRIMARY KEY REFERENCES recordings(id) ON DELETE CASCADE,'
        'publication BLOB NOT NULL CHECK(length(publication)=32),byte_size INTEGER NOT NULL CHECK(byte_size>0))')
    yield ('source_profile_files', 'CREATE TABLE source_profile_files ('
        'source_key TEXT PRIMARY KEY NOT NULL REFERENCES rec_musical_source_profiles(source_key) ON DELETE CASCADE,'
        'publication BLOB NOT NULL CHECK(length(publication)=32),byte_size INTEGER NOT NULL CHECK(byte_size>0)) WITHOUT ROWID')


def native_profile_definition(sql):
    return sql.replace('CHECK(byte_size>0))', 'CHECK(byte_size>0),' + PROFILE_FILE_COLUMN + ')')


def install_profile(conn, context=None):
    tables = ('recording_profile_files', 'source_profile_files')
    installed = definitions(conn, tables)
    profile.install(conn, context)
    for table in tables:
        if 'file_id' not in tokens(installed[table]):
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {PROFILE_FILE_COLUMN}')


def validate_profile(conn):
    expected = dict(profile.statements)
    expected.update((name, native_profile_definition(sql)) for name, sql in profile_head_definitions())
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if tokens(actual.get(name)) != tokens(sql):
            raise MigrationError('Incompatible profile file identity owner: ' + name)


PROVENANCE_HEAD = '''CREATE TABLE prediction_provenance (
    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id) ON DELETE CASCADE,
    input_fingerprint BLOB NOT NULL CHECK(length(input_fingerprint)=32),
    byte_size INTEGER NOT NULL CHECK(byte_size>0),
    state TEXT NOT NULL CHECK(state IN ('ready','expired'))
)'''
PROVENANCE_FILE_COLUMN = 'file_id INTEGER REFERENCES provenance_file_writes(id)'
provenance = ReceiptSchema(provenance_file_writes='''CREATE TABLE provenance_file_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_digest BLOB NOT NULL CHECK(typeof(content_digest)='blob' AND length(content_digest)=32),
    byte_size INTEGER NOT NULL CHECK(typeof(byte_size)='integer' AND byte_size BETWEEN 1 AND 8388608),
    file_status TEXT NOT NULL CHECK(file_status IN ('pending','ready'))
)''')
provenance_head = ReceiptSchema(
    ix_prediction_provenance_file='CREATE INDEX ix_prediction_provenance_file ON prediction_provenance(file_id,state)',
    prediction_provenance_file_immutable=(
        'CREATE TRIGGER prediction_provenance_file_immutable BEFORE UPDATE OF file_id ON prediction_provenance '
        "BEGIN SELECT RAISE(ABORT,'Calculation provenance file is immutable'); END"),
)


def native_provenance_definition(sql):
    return sql.replace("state IN ('ready','expired'))", "state IN ('ready','expired')),\n    " + PROVENANCE_FILE_COLUMN)


def has_native_provenance_heads(conn):
    return any(row[1] == 'file_id' for row in conn.execute('PRAGMA main.table_xinfo(prediction_provenance)'))


def install_provenance(conn, context=None):
    provenance.install(conn, context)
    if 'file_id' not in tokens(definitions(conn, ('prediction_provenance',))['prediction_provenance']):
        conn.execute('ALTER TABLE prediction_provenance ADD COLUMN ' + PROVENANCE_FILE_COLUMN)
    provenance_head.install(conn, context)


def validate_provenance(conn):
    expected = {**provenance.statements, 'prediction_provenance': native_provenance_definition(PROVENANCE_HEAD),
                **provenance_head.statements}
    actual = definitions(conn, expected)
    for name, sql in expected.items():
        if tokens(actual.get(name)) != tokens(sql):
            raise MigrationError('Incompatible prediction provenance file owner: ' + name)
