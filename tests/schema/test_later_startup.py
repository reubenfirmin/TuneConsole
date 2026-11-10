"""Read-only admission/HTTP evidence on retained, already isolated later libraries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.migrations import registry, runner
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app


class Cards(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.cards, self.current, self.depth = [], None, 0
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        classes = values.get('class', '').split()
        if tag == 'div':
            if self.current is not None:
                self.depth += 1
            elif 'gen-card-body' in classes:
                self.current = {'name': values.get('data-name'), 'rows': []}
                self.cards.append(self.current)
                self.depth = 1
        if tag == 'li' and self.current is not None and 'gen-row' in classes:
            self.current['rows'].append(values.get('data-key'))

    def handle_endtag(self, tag):
        if tag == 'div' and self.current is not None:
            self.depth -= 1
            if self.depth == 0:
                self.current = None


class Playlists(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.rows = []
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        value = dict(attributes).get('x-data', '')
        if value.startswith('playlistsTab(') and value.endswith(')'):
            self.rows = json.loads(value[len('playlistsTab('):-1])


def sources(root):
    names = ('core/store.py', 'core/storage_maintenance.py', 'core/migrations/runner.py',
             'core/migrations/current.py', 'core/migrations/v066_explicit_keys.py',
             'core/migrations/v078_account_contracts.py', 'rec/home_prepared.py',
             'rec/home_cards.py', 'rec/account_growth.py', 'web/routes/home.py',
             'web/app.py', 'providers/genres.py')
    return {name: hashlib.sha256((root / 'src/yt_playlist' / name).read_bytes()).hexdigest() for name in names}


@pytest.mark.parametrize('option,advance82', [
    ('--schema-source-db', False), ('--schema-source-db', True), ('--schema-recovery-db', False)])
def test_actual_later_library_admits_and_serves_before_validation(request, tmp_path, monkeypatch, option, advance82):
    supplied = request.config.getoption(option)
    if not supplied:
        pytest.skip('Supply a retained isolated later-version library')
    root = Path(request.config.rootpath).resolve()
    path = Path(supplied).resolve(strict=True)
    assert path.is_relative_to(root / '.pytest_cache'), 'Never run against the native library'
    source = path
    if advance82:
        path = tmp_path / 'actual82.db'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original, \
                closing(sqlite3.connect(path)) as target:
            assert original.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 81
            original.backup(target)
            target.execute('PRAGMA foreign_keys=ON')
            # Use the genuine next migration, without rewriting version history
            # or repeating the large metadata conversion that produced schema81.
            runner.migrate(target, registry=registry.MIGRATIONS[:82])
            target.execute('PRAGMA journal_mode=WAL')
    report = {'status': 'incomplete', 'database': str(path), 'python': sys.version,
              'sqlite': sqlite3.sqlite_version, 'requests': {}, 'denied_writes': [],
              'source_before': sources(root), 'controlled': [
                  'background validation held before its full data checks, then cancelled',
                  'logical HTTP clock 1793289600, controlled remote client, background_work=False'],
              'not_established': ['conversion completion, native deployment, browser rendering']}
    if advance82:
        report['controlled'].append('coherent backup of actual schema81, advanced with registered v82 only')
        report['source_database'] = str(source)
    monitor = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    store = maintenance = client = pool = None
    entered = threading.Event()
    original = runner.validate_version
    stats = {}
    version = data_version = None
    try:
        dataset = {table: monitor.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                   for table in ('recordings', 'playable_library_tracks', 'playlists', 'identities')}
        version = monitor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert version >= 81 and dataset['recordings'] >= 10000
        assert monitor.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
        report.update(schema=version, dataset=dataset)
        data_version = monitor.execute('PRAGMA data_version').fetchone()[0]
        stats = {suffix: (candidate.stat().st_size, candidate.stat().st_mtime_ns)
                 for suffix in ('', '-wal') if (candidate := Path(str(path) + suffix)).exists()}
        expected_playlists = {row[0]: (row[1], row[2]) for row in monitor.execute(
            'SELECT id,title,ytm_playlist_id FROM playlists WHERE title IS NOT NULL')}
        expected_menus = set()
        for raw, in monitor.execute("SELECT payload FROM rec_proposals WHERE surface IN "
                                    "('home_prepared','home_tuned','home_choice')"):
            value = json.loads(raw)
            menus = value.get('menus', []) + ([value['menu']] if isinstance(value.get('menu'), dict) else [])
            for menu in menus:
                for proto in menu.get('protos', []):
                    expected_menus.add((proto['name'], tuple(row['key'] for row in proto['tracks'])))
        assert expected_menus
        track = monitor.execute('SELECT video_id,title,duration_s FROM catalog_tracks '
                                'WHERE video_id IS NOT NULL AND duration_s>60 ORDER BY id LIMIT 1').fetchone()
        assert track is not None
        store = Store(path)
        statements = []
        store.conn.set_trace_callback(statements.append)

        def guard(action, first, second, database, trigger):
            # TEMP triggers authorize an internal sqlite_master write as "main".
            # Guard real table writes and main-schema DDL separately.
            main_ddl = (sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
                        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
                        sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX,
                        sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
                        sqlite3.SQLITE_ALTER_TABLE)
            main_write = action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)
            if database == 'main' and (action in main_ddl or
                    main_write and first not in ('sqlite_master', 'sqlite_schema')):
                report['denied_writes'].append([action, first, second])
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and (first or '').lower() == 'foreign_key_check':
                pytest.fail('Whole-library foreign-key validation reached foreground admission')
            return sqlite3.SQLITE_OK

        store.conn.set_authorizer(guard)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        report['pre_http_schema_seconds'] = time.monotonic() - started
        assert report['pre_http_schema_seconds'] < 5
        assert store._schema_recovery_pending and store.activity_paused
        assert not any(sql.upper().startswith('PRAGMA FOREIGN_KEY_CHECK') for sql in statements)
        identity = store.accounts.get_master_account()
        started = time.monotonic()
        app = create_app(store, lambda: {identity.id: FakeClient()},
                         now_fn=lambda: 1793289600, background_work=False)
        report['app_construction_seconds'] = time.monotonic() - started
        report['pre_http_seconds'] = report['pre_http_schema_seconds'] + report['app_construction_seconds']
        assert report['pre_http_seconds'] < 5
        maintenance = app.state.ctx.storage_maintenance

        def held_validation(conn, installed, **options):
            if options.get('data') is False:
                return original(conn, installed, **options)
            entered.set()
            assert maintenance._stop.wait(60), 'Validation hold exceeded its cleanup deadline'
            raise WorkClosing('Actual-library validation deliberately held and cancelled')

        monkeypatch.setattr(runner, 'validate_version', held_validation)
        maintenance.start()
        assert entered.wait(5)
        assert maintenance.status()['active'] and not maintenance.ready.is_set()
        client = TestClient(app, base_url='http://127.0.0.1')
        pool = ThreadPoolExecutor(max_workers=1)
        responses = {}
        for label, route in (('home', '/'), ('playlists', '/playlists'),
                             ('track', '/track/' + track[0]), ('onboarding', '/home/onboard/status')):
            started = time.monotonic()
            response = pool.submit(client.get, route).result(timeout=30)
            elapsed = time.monotonic() - started
            report['requests'][label] = {'status_code': response.status_code, 'seconds': elapsed}
            assert response.status_code == 200 and elapsed < 10
            responses[label] = response
            assert not maintenance.ready.is_set() and store.activity_paused
        cards = Cards(responses['home'].text).cards
        report['rendered_cards'] = cards
        (tmp_path / 'actual-home.html').write_text(responses['home'].text)
        (tmp_path / 'actual-playlists.html').write_text(responses['playlists'].text)
        assert len(cards) >= 4
        for card in cards:
            assert len(card['rows']) >= 4
            # Feedback recorded after preparation may remove songs. The served
            # card must retain a real stored card's identity and song order.
            matching = [keys for name, keys in expected_menus if name == card['name']
                        and tuple(card['rows']) == tuple(key for key in keys if key in set(card['rows']))]
            assert matching, card
            card['source_song_count'] = min(map(len, matching))
        rows = Playlists(responses['playlists'].text).rows
        matching = [row for row in rows if expected_playlists.get(row['id']) == (row['title'], row['ytm'])]
        assert matching
        report['exact_playlist_rows'] = len(matching)
        assert escape(track[1]) in responses['track'].text
        assert responses['onboarding'].json()['songs'] == dataset['playable_library_tracks']
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        if maintenance is not None:
            maintenance.stop()
            maintenance.join(15)
        if pool is not None:
            pool.shutdown()
        if client is not None:
            client.close()
        if store is not None:
            try:
                store.conn.set_authorizer(None)
            except sqlite3.ProgrammingError:
                pass
            store.conn.close()
        report['source_after'] = sources(root)
        report['schema_after'] = monitor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        report['data_version_unchanged'] = monitor.execute('PRAGMA data_version').fetchone()[0] == data_version
        report['database_bytes_unchanged'] = all(
            (candidate.stat().st_size, candidate.stat().st_mtime_ns) == before
            for suffix, before in stats.items() if (candidate := Path(str(path) + suffix)).exists())
        monitor.close()
        (tmp_path / 'later-startup.json').write_text(json.dumps(report, indent=2) + '\n')
        assert report['data_version_unchanged'] and report['database_bytes_unchanged']
        assert report['schema_after'] == version and not report['denied_writes']
        assert report['source_before'] == report['source_after']
