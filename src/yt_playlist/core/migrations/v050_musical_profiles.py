"""Disposable source/profile projections and bounded, durable dependency fan-out."""


def _key(side):
    return (f"json_array({side}.provider,json_extract({side}.provenance_json,'$.scope'),"
            f"json_extract({side}.provenance_json,'$.entity_id'))")


def _provenance(column):
    # Full lookup evidence stays in history. Delivery/query details must not
    # restart shared artist fan-out when its actual tags have not changed.
    paths = ('delivery_id', 'match_evidence.input_title', 'match_evidence.input_artist',
             'match_evidence.returned_title', 'match_evidence.returned_artist',
             'match_evidence.recording_mbid', 'match_evidence.search_score')
    return f"json_remove({column}," + ','.join(f"'$.{path}'" for path in paths) + ')'


def statements():
    yield ("CREATE TABLE rec_musical_sources (source_key TEXT PRIMARY KEY NOT NULL,provider TEXT NOT NULL,"
           "scope TEXT NOT NULL,entity_id TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1)")
    yield ("CREATE TABLE rec_musical_links (source_key TEXT NOT NULL REFERENCES rec_musical_sources(source_key),"
           "recording_id INTEGER NOT NULL REFERENCES recordings(id),delivery_id TEXT NOT NULL,"
           "PRIMARY KEY(source_key,recording_id))")
    yield 'CREATE INDEX ix_musical_links_recording ON rec_musical_links(recording_id,source_key)'
    yield ("CREATE TABLE rec_musical_pending (position INTEGER PRIMARY KEY AUTOINCREMENT,source_key TEXT NOT NULL UNIQUE,"
           "revision INTEGER NOT NULL,cursor INTEGER NOT NULL DEFAULT 0)")
    for table, key in (('rec_musical_source_profiles', 'source_key'), ('rec_musical_profiles', 'identity_key')):
        yield (f'CREATE TABLE {table} ({key} TEXT PRIMARY KEY NOT NULL,payload TEXT NOT NULL,'
               'version TEXT NOT NULL,digest TEXT NOT NULL,prepared_at REAL NOT NULL)')
        yield f'CREATE INDEX ix_{table}_version ON {table}(version,{key})'
    for event in ('INSERT', 'UPDATE'):
        yield (f'CREATE TRIGGER musical_source_{event.lower()} AFTER {event} ON rec_musical_sources BEGIN '
               'INSERT INTO rec_musical_pending(source_key,revision) VALUES (NEW.source_key,NEW.revision) '
               'ON CONFLICT(source_key) DO UPDATE SET revision=excluded.revision,cursor=0; END')
    yield ("CREATE TRIGGER musical_link_insert AFTER INSERT ON rec_musical_links BEGIN "
           "INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings WHERE id=NEW.recording_id "
           "ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1; END")
    key = _key('NEW')
    path = "'$.' || json_quote(NEW.field)"
    value = f"json_object('value',json(NEW.value_json),'provenance',json({_provenance('NEW.provenance_json')}))"
    yield ("CREATE TRIGGER musical_observation_insert AFTER INSERT ON metadata_findings "
           "WHEN NEW.provenance_json IS NOT NULL AND json_extract(NEW.provenance_json,'$.schema')=1 "
           "AND json_extract(NEW.provenance_json,'$.scope') IN ('recording','artist','release') BEGIN "
           'INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id,payload) VALUES '
           f"({key},NEW.provider,json_extract(NEW.provenance_json,'$.scope'),"
           f"json_extract(NEW.provenance_json,'$.entity_id'),json_object(NEW.field,json({value}))) "
           f'ON CONFLICT(source_key) DO UPDATE SET payload=json_set(payload,{path},json({value})),revision=revision+1 '
           f'WHERE json_extract(payload,{path}) IS NOT {value}; '
           'DELETE FROM rec_musical_links WHERE recording_id=NEW.recording_id '
           f'AND source_key<>{key} AND delivery_id<>json_extract(NEW.provenance_json,\'$.delivery_id\') '
           'AND EXISTS (SELECT 1 FROM rec_musical_sources s WHERE s.source_key=rec_musical_links.source_key '
           "AND s.provider=NEW.provider AND s.scope=json_extract(NEW.provenance_json,'$.scope')); "
           f'INSERT INTO rec_musical_links VALUES ({key},NEW.recording_id,'
           "json_extract(NEW.provenance_json,'$.delivery_id')) ON CONFLICT(source_key,recording_id) "
           'DO UPDATE SET delivery_id=excluded.delivery_id WHERE delivery_id<>excluded.delivery_id; END')


def apply(conn, context):
    for sql in statements():
        conn.execute(sql)
    # Rebuildable projections of retained history, not a replacement evidence store.
    conn.execute(f"""WITH ranked AS (
        SELECT *,ROW_NUMBER() OVER (PARTITION BY provider,json_extract(provenance_json,'$.scope'),
            json_extract(provenance_json,'$.entity_id'),field ORDER BY id DESC) n
        FROM metadata_findings WHERE provenance_json IS NOT NULL
        AND json_extract(provenance_json,'$.schema')=1
        AND json_extract(provenance_json,'$.scope') IN ('recording','artist','release'))
        INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id,payload)
        SELECT json_array(provider,json_extract(provenance_json,'$.scope'),json_extract(provenance_json,'$.entity_id')),
            provider,json_extract(provenance_json,'$.scope'),json_extract(provenance_json,'$.entity_id'),
            json_group_object(field,json_object('value',json(value_json),
                'provenance',json({_provenance('provenance_json')})))
        FROM ranked WHERE n=1 GROUP BY provider,json_extract(provenance_json,'$.scope'),json_extract(provenance_json,'$.entity_id')""")
    conn.execute("""WITH scoped AS (
        SELECT *,json_extract(provenance_json,'$.scope') scope,
            json_extract(provenance_json,'$.delivery_id') delivery FROM metadata_findings
        WHERE provenance_json IS NOT NULL AND json_extract(provenance_json,'$.schema')=1
        AND json_extract(provenance_json,'$.scope') IN ('recording','artist','release')),
        latest AS (SELECT recording_id,provider,scope,MAX(id) id FROM scoped GROUP BY recording_id,provider,scope)
        INSERT OR IGNORE INTO rec_musical_links
        SELECT json_array(f.provider,f.scope,json_extract(f.provenance_json,'$.entity_id')),f.recording_id,f.delivery
        FROM scoped f JOIN latest l ON l.recording_id=f.recording_id AND l.provider=f.provider AND l.scope=f.scope
        JOIN scoped tail ON tail.id=l.id WHERE f.delivery=tail.delivery""")
    conn.execute('INSERT INTO rec_content_pending(identity_key) SELECT identity_key FROM recordings WHERE true '
                 'ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1')
