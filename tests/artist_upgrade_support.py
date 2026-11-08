"""Small literal-credit inputs for independent artist upgrade owners."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.genre_catalog_support import bound_catalog
from yt_playlist.core import destination_schema, schema_admission, schema_definition
from yt_playlist.core.migrations import registry, regular


@pytest.fixture
def artist_legacy_input(tmp_path, monkeypatch, _pristine_installed_schema):
    bound_catalog(monkeypatch, entries=(('rock', 'Rock'),))
    path = tmp_path / 'artists.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=OFF')
        _pristine_installed_schema(20).backup(conn)
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'song|credit')")
            conn.execute("INSERT INTO youtube_items(video_id,recording_id,artist,orig_artist) VALUES ('v',1,'Group, with comma','Original credit')")
            conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'v')")
            conn.execute("INSERT INTO artist_scans VALUES ('Group, with comma',15)")
            conn.execute('INSERT INTO rec_artist_similar VALUES (?,?,?)',
                         ('group, with comma', '[["Neighbor",0.8]]', 20))
            conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES ('artist','Group, with comma','less',5)")
            conn.execute("INSERT INTO discovery_visible_impressions VALUES ('page','artist','Group, with comma',6)")
            conn.execute("INSERT INTO discovered_artists(artist,found_at,first_shown,offered_count) VALUES ('Group, with comma',10,50,3)")
            conn.execute("INSERT INTO discovered_artists(artist,found_at,first_shown,offered_count) VALUES ('Original credit',20,40,2)")
            conn.execute("INSERT INTO entry_field_choices VALUES (1,'artist','\"Group, with comma\"','user')")
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn, path


@pytest.fixture
def completed_artist_input(request, tmp_path, monkeypatch):
    """Completed lower rows/files; each named conversion still executes real SQL."""
    bound_catalog(monkeypatch, entries=(('rock', 'Rock'),))
    source, target = request.param
    document = json.loads((Path(__file__).parent /
                           'fixtures/migrations/literal_artist_completed.json').read_text())
    tables = {}
    for version, changes in document['versions'].items():
        if int(version) > source:
            break
        for name, data in changes.items():
            if data is None:
                tables.pop(name, None)
            else:
                tables[name] = data

    def expand(value):
        if isinstance(value, str):
            return value.replace('{root}', str(tmp_path))
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            if 'sqlite_real' in value:
                return float(value['sqlite_real'])
            if 'sqlite_blob_hex' in value:
                return bytes.fromhex(value['sqlite_blob_hex'])
        return value

    for relative in document['version_files'][str(source)]:
        filename = tmp_path / relative
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(expand(document['files'][relative]))
    path = tmp_path / 'artists.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=OFF')
        statements = (destination_schema.definition()['destination_statements'] if source == 107
                      else schema_admission.reference()['statements'] if source == 79
                      else schema_definition.statements(version=source))
        programs = []
        with conn:
            conn.execute('BEGIN')
            for sql in statements:
                head = sql.lstrip().upper()
                if head.startswith('INSERT '):
                    continue
                if head.startswith(('CREATE VIEW ', 'CREATE TRIGGER ')):
                    programs.append(sql)
                else:
                    conn.execute(sql)
            conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',
                             [(item.version, item.name) for item in registry.MIGRATIONS[:source]])
            if source == 107:
                conn.executemany('INSERT INTO schema_changes VALUES (?,?,1)',
                                 [(item.version, item.name) for item in regular.CHANGES])
            for name, data in tables.items():
                columns = ','.join('"' + column + '"' for column in data['columns'])
                placeholders = ','.join('?' for _ in data['columns'])
                conn.executemany(f'INSERT INTO "{name}"({columns}) VALUES ({placeholders})', expand(data['rows']))
            for sql in programs:
                conn.execute(sql)
        conn.execute('PRAGMA foreign_keys=ON')
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        conn.execute('PRAGMA journal_mode=WAL')
        yield conn, path, source, target
