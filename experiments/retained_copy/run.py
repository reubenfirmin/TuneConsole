"""Explicit #658 experiment; never installs, migrates or opens the native DB for writes."""
import argparse
from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from yt_playlist.core import policy_documents, preparation_files
from yt_playlist.core.db_rebuild import build as rebuild
from experiments.retained_copy import playback_feedback_backup
from yt_playlist.repos import group_retention

Q = rebuild.quote


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def connect(path, readonly=True):
    conn = sqlite3.connect(path.resolve().as_uri() + ('?mode=ro' if readonly else ''), uri=True)
    rebuild._configure(conn)
    return conn


def tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM main.sqlite_schema WHERE type='table' "
                                    "AND name NOT GLOB 'sqlite_*' ORDER BY name")]


def key_definition(conn, table, keys):
    types = {row[1]: row[2] for row in rebuild._columns(conn, table)}
    # Matching affinity is necessary for indexed IS comparisons against rowids.
    # An untyped scratch key makes SQLite scan that set for each source row.
    return ','.join(Q(key) + ' ' + types.get(key, 'INTEGER') for key in keys)


def file_columns(conn):
    for table in tables(conn):
        for column in conn.execute('PRAGMA main.table_xinfo(' + Q(table) + ')'):
            name = column[1]
            if name == 'filename' or name.endswith('_filename'):
                yield table, name


def referenced_files(conn):
    paths = set()
    for table, column in file_columns(conn):
        for filename, in conn.execute('SELECT DISTINCT ' + Q(column) + ' FROM main.' + Q(table)
                                      + ' WHERE ' + Q(column) + ' IS NOT NULL'):
            paths.add(Path(filename).resolve(strict=True))
    visited = set()

    def nested(reference):
        paths.add(Path(reference[0]).resolve(strict=True))
        identity = tuple(reference)
        if identity not in visited:
            visited.add(identity)
            preparation_files._graph(preparation_files._read(reference), nested)
        return reference

    for owner in preparation_files.references(conn):
        if owner['columns'][0] == 'plan_filename':
            nested(owner['reference'])
    return sorted(paths)


def check(conn):
    integrity = [r[0] for r in conn.execute('PRAGMA integrity_check')]
    if integrity != ['ok']:
        raise ValueError(('integrity', integrity[:5]))
    violations = conn.execute('PRAGMA foreign_key_check').fetchmany(5)
    if violations:
        raise ValueError(('foreign keys', violations))


def backup(source, root):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'source': str(source), 'phases': {}, 'started_at': time.time()}
    started = time.monotonic()
    files = root / 'backup-files'
    files.mkdir(mode=0o700)
    destination = root / 'original.db'
    mapping = {}
    with closing(connect(source)) as reader, ExitStack() as fences:
        # The ledger lock is read-only on the source. No source directory/lock is created.
        ledger = source.with_name(source.name + '.evidence')
        if (ledger / '.budget').exists():
            stream = fences.enter_context((ledger / '.budget').open('rb'))
            unlock = policy_documents._try_file_lock(stream, exclusive=True)
            if unlock is None:
                raise RuntimeError('Recommendation evidence is busy; retry the backup')
            fences.callback(unlock)
        phase = time.monotonic()
        with playback_feedback_backup.pinned(reader, destination, backup_root=files) as feedback:
            report['playback_feedback'] = feedback
            with closing(connect(destination, False)) as copied:
                last = [-1]

                def progress(status, remaining, total):
                    bucket = (total - remaining) // 32768
                    if bucket != last[0]:
                        print(f'backup pages {total - remaining}/{total}', flush=True)
                        last[0] = bucket

                reader.backup(copied, pages=2048, progress=progress)
            report['phases']['sqlite_backup_seconds'] = time.monotonic() - phase
            phase = time.monotonic()
            referenced = referenced_files(reader)
            for index, path in enumerate(referenced):
                value = digest(path)
                target = files / 'objects' / value[:2] / value
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copyfile(path, target)
                    target.chmod(0o600)
                if digest(target) != value:
                    raise ValueError('Backup file changed: ' + str(path))
                mapping[str(path)] = {'path': str(target.resolve()), 'sha256': value,
                                      'bytes': target.stat().st_size}
                if index % 1000 == 0:
                    print(f'backup files {index}/{len(referenced)}', flush=True)
            report['phases']['referenced_files_seconds'] = time.monotonic() - phase
            phase = time.monotonic()
            if ledger.exists():
                shutil.copytree(ledger, files / 'evidence')
                report['evidence_files'] = {}
                for path in sorted((files / 'evidence').rglob('*')):
                    if path.is_file():
                        report['evidence_files'][str(path.relative_to(files / 'evidence'))] = digest(path)
            report['phases']['ledger_seconds'] = time.monotonic() - phase
    save(root / 'backup-file-map.json', mapping)
    phase = time.monotonic()
    with closing(connect(destination)) as conn:
        check(conn)
        report['schema_version'] = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        report['schema'] = conn.execute('SELECT type,name,sql FROM sqlite_schema '
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY name").fetchall()
        report['counts'] = {t: conn.execute('SELECT count(*) FROM ' + Q(t)).fetchone()[0] for t in tables(conn)}
        report['sequences'] = conn.execute('SELECT * FROM sqlite_sequence ORDER BY name').fetchall()
    report['phases']['integrity_and_inventory_seconds'] = time.monotonic() - phase
    report.update(sql_bytes=destination.stat().st_size, sql_sha256=digest(destination),
                  referenced_file_count=len(mapping),
                  referenced_file_bytes=sum(v['bytes'] for v in mapping.values()),
                  seconds=time.monotonic() - started)
    save(root / 'backup-report.json', report)
    destination.chmod(0o400)
    print(json.dumps({k: report[k] for k in ('sql_bytes','schema_version','referenced_file_count',
                                           'referenced_file_bytes','seconds','phases')}), flush=True)


class Plan:
    def __init__(self, conn, now):
        self.conn, self.now = conn, now
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
        self.names = tables(conn)
        self.rules = {}
        self.keys = {}

    def owner(self, name, rule):
        if name not in self.names:
            return
        _, keys = rebuild._fields(self.conn, name)
        self.keys[name] = keys
        key_list = ','.join(map(Q, keys))
        # CTAS can rename SELECT rowid to an INTEGER PRIMARY KEY's column name.
        # Declare keys explicitly; a quoted absent rowid in an index can otherwise
        # become SQLite's legacy double-quoted string literal and keep one row.
        self.conn.execute('CREATE TEMP TABLE ' + Q('keep_' + name) + '(' + key_definition(self.conn,name,keys)
                          + ',PRIMARY KEY(' + key_list + ')) WITHOUT ROWID')
        self.rules[name] = rule

    def predicate(self, table, alias='s'):
        if table not in self.rules:
            return '1'
        return 'EXISTS(SELECT 1 FROM temp.' + Q('keep_' + table) + ' k WHERE ' + ' AND '.join(
            'k.' + Q(k) + ' IS ' + alias + '.' + Q(k) for k in self.keys[table]) + ')'

    def add(self, name, predicate):
        keys = ','.join('s.' + Q(k) for k in self.keys[name])
        return self.conn.execute('INSERT OR IGNORE INTO temp.' + Q('keep_' + name)
            + ' SELECT ' + keys + ' FROM main.' + Q(name) + ' s WHERE ' + predicate).rowcount

    def key_in(self, owner, field, parent, parent_field):
        # A retained key may be the rowid while the relationship names its
        # INTEGER PRIMARY KEY alias. Read that field from the real parent.
        return ('s.' + Q(field) + ' IN (SELECT p.' + Q(parent_field) + ' FROM main.' + Q(parent)
                + ' p WHERE ' + self.predicate(parent, 'p') + ')')

    def prepare(self):
        conn = self.conn
        initial = rebuild.plan(conn, now=self.now)
        for name, predicate in initial.items():
            self.owner(name, 'existing recommendation retention rule: ' + predicate)
            self.add(name, predicate)
        for name in ('recommendation_generations', 'group_builds'):
            if name == 'recommendation_generations':
                selected = self.predicate(name)
            else:
                selected = 's.expires_at > ' + repr(self.now)
            conn.execute('CREATE TEMP VIEW ' + Q(name) + ' AS SELECT * FROM main.' + Q(name) + ' s WHERE ' + selected)
        budget = group_retention.MAX_REFERENCES
        try:
            group_retention.MAX_REFERENCES = 10**7  # Isolated diagnostic; never a runtime policy change.
            generations, edges = group_retention.closure(conn)
        finally:
            group_retention.MAX_REFERENCES = budget
        conn.execute('CREATE TEMP TABLE group_seed_generations(id INTEGER PRIMARY KEY)')
        conn.executemany('INSERT INTO group_seed_generations VALUES (?)', [(g,) for g in sorted(generations)])
        conn.execute('CREATE TEMP TABLE group_seed_edges(source_alias TEXT,target_alias TEXT,PRIMARY KEY(source_alias,target_alias)) WITHOUT ROWID')
        conn.executemany('INSERT INTO group_seed_edges VALUES (?,?)', sorted(edges))
        self.owner('group_generations', 'group-retention roots plus mandatory parent closure')
        self.add('group_generations', 's.id IN (SELECT id FROM group_seed_generations)')
        children = {
            'generation_groups': ('generation_id', 'group_generations', 'id'),
            'group_publications': ('generation_id', 'group_generations', 'id'),
            'group_assignments': ('generation_id', 'group_generations', 'id'),
            'unresolved_group_assignments': ('generation_id', 'group_generations', 'id'),
            'group_unknown': ('generation_id', 'group_generations', 'id'),
            'unresolved_group_unknown': ('generation_id', 'group_generations', 'id'),
            'group_graph': ('generation_id', 'group_generations', 'id'),
            'unresolved_group_graph': ('generation_id', 'group_generations', 'id'),
            'group_members': ('group_version_id', 'group_versions', 'id'),
            'unresolved_group_members': ('group_version_id', 'group_versions', 'id'),
            'group_representatives': ('group_version_id', 'group_versions', 'id'),
            'group_manifest_inputs': ('manifest_id', 'group_input_manifests', 'id'),
            'unresolved_group_manifest_inputs': ('manifest_id', 'group_input_manifests', 'id'),
            'group_build_versions': ('generation_id', 'group_builds', 'generation_id'),
        }
        for name in ('group_versions','group_version_aliases','group_input_manifests'):
            self.owner(name, 'mandatory parents of retained group selections and evidence')
        self.owner('group_builds', 'unexpired group build')
        self.add('group_builds', 's.expires_at > ' + repr(self.now))
        for name, (field, parent, key) in children.items():
            self.owner(name, f'complete children: {field} belongs to retained {parent}.{key}')
        self.owner('group_lineage', 'required historical lineage edge closure')
        self.add('group_lineage', 'EXISTS(SELECT 1 FROM group_seed_edges e WHERE e.source_alias=s.source_alias AND e.target_alias=s.target_alias)')
        self.owner('group_generation_lineage', 'retained generation and required lineage edge')
        for name in ('group_retention_aliases','group_retention_edges','group_retention_generations','group_retention_walk'):
            self.owner(name, 'empty resumable retirement scratch; preserve evidence roots/epoch')
        self.owner('metadata_observations', 'non-legacy observations, legacy choices and mandatory referenced ancestry')
        self.add('metadata_observations', "s.origin NOT LIKE 'legacy_%' OR s.origin='legacy_choice'")
        self.owner('metadata_deliveries', 'non-legacy deliveries plus mandatory referenced original deliveries')
        self.add('metadata_deliveries', "s.key_contract<>'legacy_record'")
        self.owner('metadata_legacy_records', 'legacy records of retained observations/deliveries')
        constraints = []
        for child in self.names:
            groups = {}
            for row in conn.execute('PRAGMA main.foreign_key_list(' + Q(child) + ')'):
                if row[2] in self.rules:
                    groups.setdefault(row[0], []).append(row)
            for rows in groups.values():
                constraints.append((child, rows[0][2], rows))
        rounds = []
        while True:
            changed = 0
            for name, (field, parent, key) in children.items():
                if name in self.rules:
                    changed += self.add(name, self.key_in(name, field, parent, key))
            changed += self.add('group_generation_lineage',
                self.key_in('group_generation_lineage','generation_id','group_generations','id')
                + ' AND EXISTS(SELECT 1 FROM group_seed_edges e WHERE e.source_alias=s.source_alias AND e.target_alias=s.target_alias)')
            changed += self.add('metadata_legacy_records', self.key_in('metadata_legacy_records','delivery_id','metadata_deliveries','id'))
            for child, parent, references in constraints:
                columns = ','.join('p.' + Q(k) for k in self.keys[parent])
                conditions = ' AND '.join('p.' + Q(r[4]) + '=s.' + Q(r[3]) for r in references)
                changed += conn.execute('INSERT OR IGNORE INTO temp.' + Q('keep_' + parent)
                    + ' SELECT ' + columns + ' FROM main.' + Q(child) + ' s JOIN main.' + Q(parent)
                    + ' p ON ' + conditions + ' WHERE ' + self.predicate(child)).rowcount
            rounds.append(changed)
            print(f'closure round {len(rounds)}: {changed} keys added', flush=True)
            if not changed:
                break
            if len(rounds) > 100:
                raise RuntimeError('Retention closure did not converge')
        return {'group_seed_generations': sorted(generations), 'group_seed_edges': len(edges), 'closure_rounds': rounds}

    def export(self, root):
        report = {'contract': 1, 'now': self.now, 'source': str(root / 'original.db'), 'tables': {}}
        report.update(self.prepare())
        key_path = root / 'retained-keys.sqlite3'
        if key_path.exists():
            raise ValueError('Retained key sets already exist; use a new plan destination')
        with closing(sqlite3.connect(key_path)) as target:
            for name in sorted(self.rules):
                keys = self.keys[name]
                target.execute('CREATE TABLE ' + Q(name) + '(' + key_definition(self.conn,name,keys) + ',PRIMARY KEY('
                               + ','.join(map(Q, keys)) + ')) WITHOUT ROWID')
                rows = self.conn.execute('SELECT * FROM temp.' + Q('keep_' + name))
                target.executemany('INSERT INTO ' + Q(name) + ' VALUES (' + ','.join('?' for _ in keys) + ')', rows)
            target.commit()
        for name in self.names:
            total = self.conn.execute('SELECT count(*) FROM main.' + Q(name)).fetchone()[0]
            kept = self.conn.execute('SELECT count(*) FROM temp.' + Q('keep_' + name)).fetchone()[0] if name in self.rules else total
            report['tables'][name] = {'rule': self.rules.get(name, 'ALL ROWS, unchanged'), 'source_rows': total,
                'retained_rows': kept, 'omitted_rows': total - kept, 'keys': self.keys.get(name),
                'predicate': self.predicate(name)}
        report['keys_sha256'] = digest(key_path)
        save(root / 'restore-plan.json', report)
        print('Saved complete restore plan before candidate creation', flush=True)
        return report


def plan(root):
    with closing(connect(root / 'original.db')) as conn:
        conn.execute('BEGIN')
        Plan(conn, time.time()).export(root)


def copy_files(conn, root, destination):
    mapping = json.loads((root / 'backup-file-map.json').read_text())
    needed = set()
    columns = list(file_columns(conn))
    for table, column in columns:
        needed.update(row[0] for row in conn.execute('SELECT DISTINCT ' + Q(column)
                      + ' FROM ' + Q(table) + ' WHERE ' + Q(column) + ' IS NOT NULL'))
    if preparation_files.available(conn):
        raise ValueError('Nested preparation plans require the domain restore codec; revise this experiment explicitly')
    replacements = []
    copied = set()
    for filename in sorted(needed):
        retained = mapping[str(Path(filename).resolve())]
        source = Path(retained['path'])
        value = retained['sha256']
        target = destination / 'files' / value[:2] / (value + '.json')
        if target not in copied:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o600)
            if target.stat().st_size != retained['bytes'] or digest(target) != value:
                raise ValueError('Candidate file differs from backup: ' + filename)
            copied.add(target)
        replacements.append((filename, str(target)))
    conn.execute('CREATE TEMP TABLE file_map(old TEXT PRIMARY KEY,new TEXT NOT NULL) WITHOUT ROWID')
    conn.executemany('INSERT INTO file_map VALUES (?,?)', replacements)
    for table, column in columns:
        conn.execute('UPDATE ' + Q(table) + ' SET ' + Q(column)
            + '=(SELECT new FROM file_map WHERE old=' + Q(table) + '.' + Q(column) + ') '
            'WHERE ' + Q(column) + ' IS NOT NULL')
    ledger = root / 'backup-files' / 'evidence'
    if ledger.exists():
        shutil.copytree(ledger, destination / 'state.db.evidence')
    feedback = json.loads((root / 'backup-report.json').read_text()).get('playback_feedback')
    if feedback:
        playback_feedback_backup.restore(conn, feedback)
    return {'distinct_files': len(copied), 'logical_bytes': sum(p.stat().st_size for p in copied),
            'allocated_bytes': sum(p.stat().st_blocks * 512 for p in copied)}


def build(root):
    started = time.monotonic()
    report = {'phases': {}}
    contract = json.loads((root / 'restore-plan.json').read_text())
    backup_report = json.loads((root / 'backup-report.json').read_text())
    if digest(root / 'original.db') != backup_report['sql_sha256']:
        raise ValueError('Original backup changed')
    if digest(root / 'retained-keys.sqlite3') != contract['keys_sha256']:
        raise ValueError('Retained key sets changed')
    destination = root / 'candidate'
    destination.mkdir(mode=0o700, exist_ok=False)
    output = destination / 'state.db'
    source = root / 'original.db'
    with closing(connect(source)) as conn:
        schema = list(conn.execute('SELECT type,name,sql FROM main.sqlite_schema '
                                  "WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*' ORDER BY name"))
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        phase = time.monotonic()
        with closing(connect(output, False)) as target:
            output.chmod(0o600)
            target.execute('PRAGMA page_size=' + str(page_size))
            target.execute('PRAGMA journal_mode=DELETE')
            target.execute('PRAGMA synchronous=FULL')
            for kind, name, sql in schema:
                if kind == 'table':
                    if 'VIRTUAL TABLE' in sql.upper():
                        raise ValueError('Unsupported virtual table: ' + name)
                    target.execute(sql)
            target.commit()
        report['phases']['schema_seconds'] = time.monotonic() - phase
        conn.execute('ATTACH DATABASE ? AS retained_keys', ((root / 'retained-keys.sqlite3').as_uri() + '?mode=ro',))
        for table, item in contract['tables'].items():
            if item['keys']:
                conn.execute('CREATE TEMP TABLE ' + Q('keep_' + table) + '('
                             + key_definition(conn,table,item['keys']) + ',PRIMARY KEY('
                             + ','.join(map(Q,item['keys'])) + ')) WITHOUT ROWID')
                conn.execute('INSERT INTO temp.' + Q('keep_' + table) + ' SELECT * FROM retained_keys.' + Q(table))
        conn.commit()
        conn.execute('DETACH DATABASE retained_keys')
        conn.execute('ATTACH DATABASE ? AS compacted', (str(output),))
        conn.execute('BEGIN')
        phase = time.monotonic()
        report['tables'] = {}
        for table, item in contract['tables'].items():
            fields, _ = rebuild._fields(conn, table)
            columns = ','.join(map(Q, fields))
            began = time.monotonic()
            rows = conn.execute('INSERT INTO compacted.' + Q(table) + '(' + columns + ') SELECT '
                                + columns + ' FROM main.' + Q(table) + ' s WHERE ' + item['predicate']).rowcount
            if rows != item['retained_rows']:
                raise ValueError('Planned row count changed: ' + table)
            report['tables'][table] = {'rows': rows, 'copy_seconds': time.monotonic() - began}
            if rows > 50000:
                print(f'copied {table}: {rows} rows', flush=True)
        conn.execute('DELETE FROM compacted.sqlite_sequence')
        conn.execute('INSERT INTO compacted.sqlite_sequence SELECT * FROM main.sqlite_sequence')
        for pragma in ('user_version','application_id'):
            conn.execute('PRAGMA compacted.' + pragma + '=' + str(conn.execute('PRAGMA main.' + pragma).fetchone()[0]))
        conn.commit()
        report['phases']['copy_seconds'] = time.monotonic() - phase
        print('Verifying all retained values/types/IDs before relocating candidate file paths', flush=True)
        phase = time.monotonic()
        for table, item in contract['tables'].items():
            rebuild._equal_rows(conn, table, item['predicate'])
        if conn.execute('SELECT * FROM main.sqlite_sequence ORDER BY name').fetchall() != conn.execute(
                'SELECT * FROM compacted.sqlite_sequence ORDER BY name').fetchall():
            raise ValueError('Sequence high-water marks changed')
        report['phases']['exact_row_verification_seconds'] = time.monotonic() - phase
        conn.execute('DETACH DATABASE compacted')
    with closing(connect(output, False)) as target:
        phase = time.monotonic()
        report['files'] = copy_files(target, root, destination)
        target.commit()
        report['phases']['candidate_files_and_paths_seconds'] = time.monotonic() - phase
        phase = time.monotonic()
        for kind in ('index','view','trigger'):
            for actual, name, sql in schema:
                if actual == kind:
                    target.execute(sql)
        target.commit()
        report['phases']['indexes_and_guards_seconds'] = time.monotonic() - phase
        phase = time.monotonic()
        print('Checking candidate integrity, FKs, schema and external-file closure', flush=True)
        check(target)
        actual = target.execute('SELECT type,name,sql FROM sqlite_schema '
                               "WHERE name NOT GLOB 'sqlite_*' ORDER BY name").fetchall()
        if actual != [tuple(row) for row in backup_report['schema']]:
            raise ValueError('Installed schema changed')
        for path in referenced_files(target):
            if not path.is_relative_to(destination):
                raise ValueError('Candidate retained a source file path: ' + str(path))
        report['tables_bytes'] = {}
        sizes = dict(target.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name'))
        for table in contract['tables']:
            indexes = [r[0] for r in target.execute('SELECT name FROM sqlite_schema WHERE type=\'index\' AND tbl_name=?', (table,))]
            report['tables_bytes'][table] = {'table_bytes': sizes.get(table,0),
                'index_bytes': sum(sizes.get(index,0) for index in indexes)}
        report['sql_bytes'] = output.stat().st_size
        report['index_bytes'] = sum(v['index_bytes'] for v in report['tables_bytes'].values())
        report['freelist_pages'] = target.execute('PRAGMA freelist_count').fetchone()[0]
        report['phases']['integrity_and_size_seconds'] = time.monotonic() - phase
    report['seconds'] = time.monotonic() - started
    report['sql_sha256'] = digest(output)
    save(root / 'build-report.json', report)
    print(json.dumps({k:report[k] for k in ('sql_bytes','index_bytes','files','seconds','phases')}), flush=True)


def baseline(root):
    import threading
    from yt_playlist.core.store import Store
    from yt_playlist.core.work_diagnostics import MaintenanceDiagnostics
    from yt_playlist.rec import (artist_model, daily_model_selection, embedding_training,
                                 embedding_vectors, musical_preparation, progressive, vibe_preparation)

    def fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':')).encode()).hexdigest()

    contract = json.loads((root / 'restore-plan.json').read_text())
    now = contract['now']
    reports = {}
    for label, path in (('original',root/'original.db'),('candidate',root/'candidate'/'state.db')):
        # Use the application's real read adapters without Store initialization/migration.
        host = Store.__new__(Store)
        host._db_path, host._evidence_key = path, '658-read-only-experiment'
        host.transient_radio_playlists = set()
        host._analytics_readers = threading.BoundedSemaphore(4)
        host.maintenance_diagnostics = MaintenanceDiagnostics()
        host._schema_compatibility = True
        reader = host._make_reader()
        try:
            reader.conn.execute('BEGIN') if not reader.conn.in_transaction else None
            reader.conn.execute('PRAGMA query_only=ON')
            baskets = embedding_training.canonical_baskets(reader.rec_evidence.rec_baskets())
            content = embedding_vectors.content_inputs(reader)
            historical, capture_id = daily_model_selection._capture(reader, now)
            preparation = progressive.Preparation(reader)
            group_head = reader.taste_groups.head(members=True)
            reports[label] = {
                'training_baskets': {'count':len(baskets),'sha256':fingerprint(baskets)},
                'artist_baskets': fingerprint(embedding_training.canonical_baskets(artist_model.artist_baskets(reader))),
                'content_inputs': fingerprint(content),
                'play_counts': fingerprint(reader.rec_query.play_counts()),
                'historical_comparison_inputs': capture_id,
                'history_bounds': list(reader.rec_evidence.evaluation_history_bounds()),
                'vibe_version': reader.settings.get_setting('rec_vibe_version'),
                'configured_vibe_version': vibe_preparation.version(),
                'vibe_work_due': vibe_preparation.due(reader),
                'daily_comparison_due': daily_model_selection.due(reader, now),
                'content_basis_reason': preparation._basis_reason(now),
                'full_retraining_due': preparation.full_due(now),
                'musical_source_work_due': musical_preparation.due(reader),
                'group_work_due': preparation.groups.due(now),
                'missing_preparation_inputs': reader.prepared_content.missing_unqueued(1),
                'published_group_contents': fingerprint(group_head),
                'published_group_count': len(group_head['groups']) if group_head else 0,
                'settings': [list(r) for r in reader.conn.execute('SELECT * FROM main.settings ORDER BY 1')],
                'pending': {t: reader.conn.execute('SELECT count(*) FROM main.' + Q(t)).fetchone()[0]
                            for t in tables(reader.conn) if t.endswith('_pending') or t in ('rec_change_queue','rec_preparation')},
                'heads': {t:[list(r) for r in reader.conn.execute('SELECT * FROM main.' + Q(t) + ' ORDER BY 1')]
                          for t in ('group_head','recommendation_heads')},
            }
        finally:
            reader.conn.close()
    save(root / 'baseline-report.json', reports)
    differences = [k for k in reports['original'] if reports['original'][k] != reports['candidate'][k]]
    if differences:
        raise ValueError('Application inputs/readiness changed: ' + repr(differences))
    print('Application training inputs, settings, queues, heads and readiness match', flush=True)
    print(json.dumps({k:v for k,v in reports['candidate'].items() if k not in ('settings','heads')}), flush=True)


def stage(root, config):
    """Prepare a separately owned app home; never launch it or alter the baseline."""
    source = root / 'candidate'
    destination = root / 'startup'
    destination.mkdir(mode=0o700, exist_ok=False)
    for path in source.iterdir():
        if path.name.startswith('state.db.playback-feedback'):
            continue  # Restore with its codec so the inbox's filenames relocate too.
        target = destination / path.name
        if path.is_dir():
            shutil.copytree(path, target)
        elif path.name == 'state.db':
            shutil.copyfile(path, target)
            target.chmod(0o600)
    replacements = []
    with closing(connect(destination / 'state.db', False)) as conn:
        schema = conn.execute('SELECT name,sql FROM sqlite_schema WHERE type=\'trigger\' ORDER BY name').fetchall()
        for name, sql in schema:
            conn.execute('DROP TRIGGER ' + Q(name))
        for path in referenced_files(conn):
            relative = path.relative_to(source)
            target = destination / relative
            if digest(path) != digest(target):
                raise ValueError('Startup file changed: ' + str(relative))
            replacements.append((str(path), str(target)))
        conn.execute('CREATE TEMP TABLE startup_files(old TEXT PRIMARY KEY,new TEXT NOT NULL) WITHOUT ROWID')
        conn.executemany('INSERT INTO startup_files VALUES (?,?)', replacements)
        for table, column in file_columns(conn):
            conn.execute('UPDATE ' + Q(table) + ' SET ' + Q(column)
                + '=(SELECT new FROM startup_files WHERE old=' + Q(table) + '.' + Q(column) + ') '
                'WHERE ' + Q(column) + ' IS NOT NULL')
        for name, sql in schema:
            conn.execute(sql)
        conn.commit()
        feedback = json.loads((root / 'backup-report.json').read_text()).get('playback_feedback')
        if feedback:
            playback_feedback_backup.restore(conn, feedback)
        check(conn)
        for path in referenced_files(conn):
            if not path.is_relative_to(destination):
                raise ValueError('Startup retained a baseline file path: ' + str(path))
        actual = conn.execute('SELECT type,name,sql FROM sqlite_schema '
                              "WHERE name NOT GLOB 'sqlite_*' ORDER BY name").fetchall()
        expected = json.loads((root / 'backup-report.json').read_text())['schema']
        if actual != [tuple(row) for row in expected]:
            raise ValueError('Startup installed schema changed')
    shutil.copyfile(config, destination / 'config.toml')
    (destination / 'config.toml').chmod(0o600)
    evidence = json.loads((root / 'backup-report.json').read_text()).get('evidence_files', {})
    for relative, value in evidence.items():
        if digest(destination / 'state.db.evidence' / relative) != value:
            raise ValueError('Startup evidence ledger changed: ' + relative)
    save(root / 'startup-report.json', {'home': str(destination), 'referenced_files': len(replacements),
         'evidence_files_verified': len(evidence), 'sql_sha256': digest(destination / 'state.db'),
         'config_sha256': digest(destination / 'config.toml'), 'launched': False})
    print('Independent startup home prepared; application has not been launched', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup','plan','build','baseline','stage'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    if args.action == 'backup':
        if args.source is None:
            parser.error('--source is required for backup')
        backup(args.source.resolve(strict=True), root)
    elif args.action == 'plan':
        plan(root)
    elif args.action == 'build':
        build(root)
    elif args.action == 'stage':
        if args.config is None:
            parser.error('--config is required for stage')
        stage(root, args.config.resolve(strict=True))
    else:
        baseline(root)


if __name__ == '__main__':
    main()
