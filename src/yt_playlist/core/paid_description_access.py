"""DeepSeek cooldowns and credential blocks on its provider row."""
import json


# The backup manifest predates provider-row storage. Keep its wire keys stable.
BACKUP_KEY_PREFIX = 'album-description:access:'


def _key(scope, credential):
    return f'{scope}:{credential}'


def read(conn, scope, credential):
    row = conn.execute("""
        SELECT access.value FROM enrichment_provider_config AS config, json_each(config.description_access) AS access
        WHERE config.provider='deepseek' AND access.key=?
    """, (_key(scope, credential),)).fetchone()
    return json.loads(row[0]) if row else None


def write(conn, access):
    conn.execute('''
        INSERT INTO enrichment_provider_config(provider,description_access)
        VALUES ('deepseek',json_object(?,json_object('status',?,'retry_at',?,'updated_at',?)))
        ON CONFLICT(provider) DO UPDATE SET
            description_access=json_patch(coalesce(enrichment_provider_config.description_access,'{}'),
                                          excluded.description_access)
    ''', (_key(access['scope'], access['credential']), access['status'], access['retry_at'], access['updated_at']))


def receipts(conn):
    return conn.execute('''
        SELECT ? || access.key, json_extract(access.value,'$.evidence_filename'),
               json_extract(access.value,'$.evidence_digest')
        FROM enrichment_provider_config AS config, json_each(config.description_access) AS access
        WHERE config.provider='deepseek' AND json_extract(access.value,'$.evidence_filename') IS NOT NULL
        ORDER BY access.key
    ''', (BACKUP_KEY_PREFIX,))


def retains(conn, filename):
    return conn.execute('''
        SELECT 1 FROM enrichment_provider_config AS config, json_each(config.description_access) AS access
        WHERE config.provider='deepseek' AND json_extract(access.value,'$.evidence_filename')=? LIMIT 1
    ''', (str(filename),)).fetchone() is not None


def rebase_receipt(conn, key, filename):
    key = key.removeprefix(BACKUP_KEY_PREFIX)
    conn.execute("""
        UPDATE enrichment_provider_config SET description_access=
            json_patch(description_access,json_object(:key,json_object('evidence_filename',:filename)))
        WHERE provider='deepseek' AND EXISTS (
            SELECT 1 FROM json_each(description_access) AS access WHERE access.key=:key
        )
    """, {'key': key, 'filename': filename})
