import json

from fastapi.testclient import TestClient

from yt_playlist.core import updatecheck
from yt_playlist.web.app import create_app


def test_schema_map_covers_all_installed_tables_views_and_foreign_keys(file_backed_store, monkeypatch):
    monkeypatch.setattr(updatecheck, 'is_dev_install', lambda: True)
    conn = file_backed_store.conn
    conn.executescript('''
        CREATE TABLE "audit parent" (a TEXT, b TEXT, PRIMARY KEY (a, b));
        CREATE TABLE "audit child" (a TEXT, b TEXT, UNIQUE (a, b),
            FOREIGN KEY (a, b) REFERENCES "audit parent");
        CREATE VIEW "audit view" AS SELECT a, b FROM "audit child";
    ''')
    expected = {row['name']: row['type'] for row in conn.execute('PRAGMA main.table_list')
                if row['type'] in ('table', 'view', 'virtual') and not row['name'].startswith('sqlite_')}
    foreign_keys = {tuple(row) for row in conn.execute('''
        SELECT s.name, f.id FROM main.sqlite_schema s JOIN pragma_foreign_key_list(s.name) f
        WHERE s.type = 'table' AND substr(s.name, 1, 7) != 'sqlite_'
    ''')}
    app = create_app(file_backed_store, lambda: {}, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/schema-admin')
    assert response.status_code == 200
    assert 'href="/schema-admin"' in response.text
    content = response.text.split('<script type="application/json" id="sd-data">', 1)[1].split('</script>', 1)[0]
    diagram = json.loads(content)
    entities = {entity['id']: entity for entity in diagram['entities']}
    assert {name: entity['kind'] for name, entity in entities.items()} == expected
    assert len(entities) == len(diagram['entities'])
    assert {edge['id'] for edge in diagram['edges']} == {f'{name}:{key}' for name, key in foreign_keys}
    assert [column['name'] for column in entities['audit view']['columns']] == ['a', 'b']
    assert next(edge for edge in diagram['edges'] if edge['source'] == 'audit child') == {
        'id': 'audit child:0', 'source': 'audit child', 'target': 'audit parent',
        'columns': ['a', 'b'], 'referenced_columns': ['a', 'b'], 'many': False,
        'on_delete': 'NO ACTION', 'on_update': 'NO ACTION',
    }


def test_schema_map_is_a_development_tool(store, monkeypatch):
    monkeypatch.setattr(updatecheck, 'is_dev_install', lambda: False)
    with TestClient(create_app(store, lambda: {}, background_work=False), base_url='http://127.0.0.1') as client:
        assert client.get('/schema-admin').status_code == 404
        assert 'href="/schema-admin"' not in client.get('/').text
