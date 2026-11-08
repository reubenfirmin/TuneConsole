"""Completed synthetic publication inputs below registered archive upgrades."""
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import destination_schema, legacy_journals, schema_admission, schema_definition
from yt_playlist.core.migrations import (registry, regular, v082_recommendation_work,
    v083_home_offers, v084_album_note_requests, v086_genre_classifications, v103_entity_names)
from yt_playlist.core.preparation import (conflict_publication, discovery_artist_publication,
    discovery_artists, discovery_publication, discovery_subjects, metadata_conflicts,
    metadata_evidence, metadata_publication, model_artifacts, model_publication)
from yt_playlist.core.store import Store


def archive_definitions(conn):
    """Canonical lower-owner DDL; the fixture records which objects survived."""
    for job in (metadata_evidence.JOB, metadata_conflicts.DEFINITION,
                model_artifacts.DEFINITION, model_artifacts.FINGERPRINT_JOB,
                discovery_subjects.JOB, discovery_artists.JOBS['artists_legacy'].definition):
        yield from job.definitions(conn)
    for owner in (metadata_publication, conflict_publication.OWNER, model_publication,
                  discovery_artist_publication.OWNERS['artists_legacy'],
                  discovery_publication.OWNERS['legacy']):
        yield from owner.definitions()


@pytest.fixture
def completed_archive_input(request, tmp_path, monkeypatch):
    bound_catalog(monkeypatch, entries=(('jazz', 'Jazz'), ('blues', 'Blues'), ('rock', 'Rock')))
    source, target = request.param
    document = json.loads((Path(__file__).parent /
                           'fixtures/migrations/early_publication_archives.json').read_text())

    def expand(value):
        if isinstance(value, str):
            for token, substitution in document['substitutions'].items():
                value = value.replace(token, substitution['text'] * substitution['count'])
            return value.replace('{root}', str(tmp_path))
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            if 'sqlite_real' in value:
                return float(value['sqlite_real'])
            if 'sqlite_blob_hex' in value:
                return bytes.fromhex(value['sqlite_blob_hex'])
            return {key: expand(item) for key, item in value.items()}
        return value

    tables, objects = {}, set()
    for version, changes in document['versions'].items():
        if int(version) > source:
            break
        for name, data in changes.items():
            if data is None:
                tables.pop(name, None)
            else:
                tables[name] = data
        objects.update(document['objects'][version]['add'])
        objects.difference_update(document['objects'][version]['remove'])
    references = json.dumps(tables)
    for relative, text in document['files'].items():
        if relative.endswith('.lock'):
            continue
        if relative not in references:
            continue  # Later conversion output must not preexist its owner.
        filename = tmp_path / relative
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(expand(text))
    path = tmp_path / 'archive.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        conn.row_factory = sqlite3.Row
        # Conversion cases need their numbered input and retained preparation
        # owners. Full regular admission has its own native startup owner.
        full_regular = source == 107
        statements = list(destination_schema.definition()['destination_statements'] if full_regular
                          else schema_admission.reference()['statements'] if source == 79
                          else schema_definition.statements(version=source))
        if source == 80:
            statements.extend(sql for sql in schema_admission.reference()['statements']
                              if sql.replace('"', '').startswith('CREATE TABLE genre_whitelist '))
        programs = []
        earlier_work = (v082_recommendation_work.OBJECTS | v083_home_offers.OBJECTS |
                        v084_album_note_requests.OBJECTS | {'genre_preferences', 'ix_genre_preference_enabled'})
        conn.execute('PRAGMA foreign_keys=OFF')
        with conn:
            conn.execute('BEGIN')
            for sql in statements:
                head = sql.lstrip().upper()
                if source == 80 and any(' ' + name + ' ' in sql.replace('\n', ' ') or
                                        ' ' + name + '(' in sql for name in earlier_work):
                    continue
                if head.startswith('INSERT '):
                    continue  # The completed rows contain the initial seeds.
                if head.startswith(('CREATE VIEW ', 'CREATE TRIGGER ')):
                    programs.append(sql)
                else:
                    conn.execute(sql)
            conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                             [(item.version, item.name) for item in registry.MIGRATIONS[:source]])
            if full_regular:
                conn.executemany('INSERT INTO schema_changes VALUES (?,?,1)',
                                 [(item.version, item.name) for item in regular.CHANGES])
            elif source >= 103:
                # These completed heads retain the artist/discovery originals.
                # Their bounded regular owner is part of this conversion input.
                legacy_journals.install(conn)
            existing = {name for name, in conn.execute('SELECT name FROM sqlite_schema')}
            definitions = dict(archive_definitions(conn))
            for name in sorted(objects - existing):
                # Deferred numbered views/triggers already have canonical SQL.
                if name not in definitions:
                    continue
                sql = definitions[name]
                if source >= 86:
                    sql = v086_genre_classifications.transform(sql)
                if source >= 103:
                    sql = v103_entity_names.transform(sql)
                if sql.startswith('CREATE TABLE '):
                    conn.execute(sql)
                else:
                    programs.append(sql)
            names = {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            omitted_regular = {'artist_credit_retention', 'collection_recovery_epoch',
                'group_retention_epoch', 'group_retention_state', 'metadata_runtime_state',
                'prediction_id_state', 'served_fact_state', 'taste_mode_retention'}
            assert not (tables.keys() - names - omitted_regular), tables.keys() - names - omitted_regular
            for name, data in tables.items():
                if name not in names:
                    # Unrelated completed regular owners are not inputs to
                    # these conversions. The native startup owner includes all.
                    continue
                columns = ','.join('"' + column + '"' for column in data['columns'])
                placeholders = ','.join('?' for _ in data['columns'])
                conn.executemany(f'INSERT INTO "{name}"({columns}) VALUES ({placeholders})', expand(data['rows']))
            for sql in dict.fromkeys(programs):
                conn.execute(sql)
            # Unrelated empty mix/retention owners and recording-input revision
            # guards are supplied only to the full native admission fixture.
            omitted_objects = set() if full_regular else {
                'prepared_choice_interest_mix_descriptors', 'published_taste_mode_reactivation',
                *(f'prepared_recording_inputs_joint_metadata_{operation}'
                  for operation in ('insert', 'update', 'delete'))}
            missing = objects - omitted_objects - {name for name, in conn.execute('SELECT name FROM sqlite_schema')}
            assert not missing, missing
        conn.execute('PRAGMA foreign_keys=ON')
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn, path, source, target, expand(document['expected'])


@contextmanager
def archive_reader(path, version):
    store = Store(path)
    try:
        store._schema_compatibility = version < 103
        store._rebuild_source_version = None
        store._schema_recovery_pending = False
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()
