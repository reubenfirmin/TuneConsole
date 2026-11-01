"""Read-only SQL/index accounting for the #494 metadata storage investigation."""
import argparse
import json
from pathlib import Path
import sqlite3
import time


def audit(path):
    started = time.monotonic()
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        definitions = {name: (kind, owner, sql) for kind, name, owner, sql in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE type IN ('table','index')")}
        owners = {name for name, (kind, _, _) in definitions.items() if kind == 'table' and (
            name.startswith(('metadata_', 'entry_metadata_', 'recording_metadata_', 'provider_state_'))
            or name in {'provider_attempts', 'provider_attempt_fields', 'enrichment_provider_state'})}
        sizes = {name: {'bytes': size, 'payload_bytes': payload, 'unused_bytes': unused}
                 for name, size, payload, unused in conn.execute(
                     'SELECT name,sum(pgsize),sum(payload),sum(unused) FROM dbstat GROUP BY name')}
        tables = {}
        for name in sorted(owners):
            quoted = '"' + name.replace('"', '""') + '"'
            indexes = {index: dict(sizes.get(index, {}), sql=sql)
                       for index, (kind, owner, sql) in definitions.items() if kind == 'index' and owner == name}
            tables[name] = dict(sizes.get(name, {}), rows=conn.execute('SELECT count(*) FROM ' + quoted).fetchone()[0],
                indexes=indexes, sql=definitions[name][2])
        deliveries = {}
        if 'metadata_deliveries' in owners:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(metadata_deliveries)')}
            for field in ('raw_filename', 'raw_digest', 'contract_digest', 'delivery_key', 'provider_entity_id',
                          'input_video_id', 'provider', 'key_contract', 'format_version'):
                if field in columns:
                    row = conn.execute('SELECT count("' + field + '"),count(DISTINCT "' + field + '"),'
                        'coalesce(sum(length(CAST("' + field + '" AS BLOB))),0) FROM metadata_deliveries').fetchone()
                    deliveries[field] = dict(zip(('non_null', 'distinct', 'value_bytes'), row))
        legacy = []
        if 'metadata_legacy_records' in owners:
            legacy = [dict(zip(('owner', 'disposition', 'rows'), row)) for row in conn.execute(
                'SELECT legacy_owner,disposition,count(*) FROM metadata_legacy_records GROUP BY 1,2 ORDER BY 1,2')]
        return {'source': str(path.resolve()), 'access': 'mode=ro; query_only=ON; pinned SQL transaction',
            'sqlite': sqlite3.sqlite_version, 'source_sql_bytes': path.stat().st_size,
            'sqlite_user_version': conn.execute('PRAGMA user_version').fetchone()[0],
            'schema_version': conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],
            'tables': tables, 'delivery_fields': deliveries, 'legacy_owners': legacy,
            'sql_metadata_bytes': sum(table.get('bytes', 0) for table in tables.values()),
            'index_metadata_bytes': sum(index.get('bytes', 0) for table in tables.values() for index in table['indexes'].values()),
            'seconds': time.monotonic() - started}
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.database)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('source', 'schema_version', 'sql_metadata_bytes',
                                                  'index_metadata_bytes', 'seconds')}))
