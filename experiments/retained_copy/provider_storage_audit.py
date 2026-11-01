"""Measure metadata conversion cohorts in disposable SQLite databases.

Reads the pristine experiment candidate only. Each cohort receives identical
table/index definitions and values; no migrations, triggers or app startup run.
Temporary cohort databases are removed on success and failure. The report is
saved outside that disposable workspace.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time

from yt_playlist.core import retained_copy

Q = retained_copy.quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path)
    args = parser.parse_args()
    root = args.experiment.resolve()
    source = root / 'candidate' / 'state.db'
    started = time.monotonic()
    report = {'source': str(source), 'method': __doc__, 'cohorts': {}}
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        original.execute('PRAGMA query_only=ON')
        owners = original.execute('SELECT legacy_owner,count(*) FROM metadata_legacy_records '
                                  'GROUP BY legacy_owner ORDER BY count(*) DESC').fetchall()
        report['attempt_columns'] = original.execute('PRAGMA table_info(provider_attempts)').fetchall()
        report['attempt_statuses'] = original.execute('SELECT status,count(*) FROM provider_attempts GROUP BY status').fetchall()
        report['attempt_providers'] = original.execute('SELECT provider,count(*) FROM provider_attempts GROUP BY provider').fetchall()
        report['attempt_text_value_bytes'] = {}
        for column, kind in original.execute('SELECT name,type FROM pragma_table_info(\'provider_attempts\')'):
            if kind != 'TEXT':
                continue
            report['attempt_text_value_bytes'][column] = original.execute(
                'SELECT coalesce(sum(length(CAST(' + Q(column) + ' AS BLOB))),0) FROM provider_attempts').fetchone()[0]
        report['state_columns'] = original.execute('PRAGMA table_info(enrichment_provider_state)').fetchall()
        predicates = {
            'metadata_deliveries': 's.id IN (SELECT delivery_id FROM temp.cohort)',
            'metadata_legacy_records': 's.legacy_owner=:owner',
            'metadata_observations': 's.legacy_owner=:owner',
            'native_metadata_observations': 's.delivery_id IN (SELECT delivery_id FROM temp.cohort)',
            'provider_attempts': 's.delivery_id IN (SELECT delivery_id FROM temp.cohort)',
            'provider_attempt_fields': 's.attempt_id IN (SELECT a.id FROM main.provider_attempts a '
                                       'WHERE a.delivery_id IN (SELECT delivery_id FROM temp.cohort))',
            'provider_state_fields': 's.delivery_id IN (SELECT delivery_id FROM temp.cohort)',
            'enrichment_provider_state': 'EXISTS(SELECT 1 FROM main.provider_state_fields f '
                'WHERE f.recording_id=s.recording_id AND f.provider=s.provider '
                'AND f.delivery_id IN (SELECT delivery_id FROM temp.cohort))',
            'recording_identifiers': 's.delivery_id IN (SELECT delivery_id FROM temp.cohort)',
        }
        with TemporaryDirectory(prefix='tuneconsole-provider-storage-') as temporary:
            for owner, count in owners:
                target_path = Path(temporary) / (owner + '.db')
                with closing(sqlite3.connect(target_path, uri=True)) as target:
                    target.execute('PRAGMA page_size=4096')
                    target.execute('PRAGMA journal_mode=OFF')
                    target.execute('PRAGMA synchronous=OFF')
                    target.execute('ATTACH DATABASE ? AS original', (source.as_uri() + '?mode=ro',))
                    target.execute('CREATE TEMP TABLE cohort(delivery_id INTEGER PRIMARY KEY)')
                    target.execute('INSERT INTO temp.cohort SELECT delivery_id FROM '
                        'original.metadata_legacy_records WHERE legacy_owner=?', (owner,))
                    data = {'legacy_records': count, 'tables': {}}
                    for name, predicate in predicates.items():
                        sql = original.execute('SELECT sql FROM sqlite_schema WHERE type=\'table\' AND name=?', (name,)).fetchone()[0]
                        target.execute(sql)
                    target.commit()
                    target.execute('BEGIN')
                    for name, predicate in predicates.items():
                        # The source alias is changed without changing correlated main
                        # lookups above: these subqueries intentionally use the cohort's
                        # already copied attempt rows and state fields.
                        fields, _ = retained_copy._fields(original, name)
                        fields = ','.join(map(Q, fields))
                        target.execute('INSERT INTO main.' + Q(name) + '(' + fields + ') SELECT '
                            + fields + ' FROM original.' + Q(name) + ' s WHERE ' + predicate, {'owner': owner})
                        rows = target.execute('SELECT count(*) FROM main.' + Q(name)).fetchone()[0]
                        data['tables'][name] = {'rows': rows}
                    for name in predicates:
                        for index, sql in original.execute('SELECT name,sql FROM sqlite_schema '
                                'WHERE type=\'index\' AND tbl_name=? AND sql IS NOT NULL', (name,)):
                            target.execute(sql)
                    target.commit()
                    stat = {name: {'bytes': size, 'payload_bytes': payload, 'unused_bytes': unused}
                            for name, size, payload, unused in target.execute(
                                'SELECT name,sum(pgsize),sum(payload),sum(unused) FROM dbstat GROUP BY name')}
                    for name in predicates:
                        indexes = [r[0] for r in target.execute('SELECT name FROM sqlite_schema '
                            'WHERE type=\'index\' AND tbl_name=?', (name,))]
                        data['tables'][name].update(stat[name])
                        data['tables'][name]['indexes'] = {index: stat[index] for index in indexes}
                        data['tables'][name]['index_bytes'] = sum(stat[index]['bytes'] for index in indexes)
                        data['tables'][name]['combined_bytes'] = stat[name]['bytes'] + data['tables'][name]['index_bytes']
                    data['combined_nonempty_table_and_index_bytes'] = sum(
                        table['combined_bytes'] for table in data['tables'].values() if table['rows'])
                    data['average_bytes_per_legacy_record'] = data['combined_nonempty_table_and_index_bytes'] / count
                    data['indexes_bytes_nonempty'] = sum(table['index_bytes'] for table in data['tables'].values() if table['rows'])
                    if owner == 'provider_attempts':
                        delivery = target.execute('SELECT * FROM metadata_deliveries ORDER BY id LIMIT 1').fetchone()
                        columns = [r[1] for r in target.execute('PRAGMA table_info(metadata_deliveries)')]
                        data['delivery_text_lengths'] = {k: len(v.encode()) for k, v in zip(columns,delivery) if isinstance(v,str)}
                        data['delivery_text_averages'] = {column: target.execute('SELECT avg(length(CAST(' + Q(column)
                            + ' AS BLOB))) FROM metadata_deliveries').fetchone()[0] for column, value in zip(columns,delivery) if isinstance(value,str)}
                        observations = target.execute('SELECT count(*),coalesce(sum(length(CAST(text_value AS BLOB))),0),'
                            'count(number_value) FROM metadata_observations').fetchone()
                        data['observations_value_summary'] = {'rows':observations[0], 'text_value_bytes':observations[1], 'numeric_values':observations[2]}
                report['cohorts'][owner] = data
                print(owner, count, data['combined_nonempty_table_and_index_bytes'],
                      round(data['average_bytes_per_legacy_record'], 1), 'bytes/record', flush=True)
                target_path.unlink()
    report['seconds'] = time.monotonic() - started
    output = root / 'provider-storage-audit.json'
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    output.chmod(0o600)
    print('Saved ' + str(output), flush=True)


if __name__ == '__main__':
    main()
