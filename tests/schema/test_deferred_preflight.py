"""Measure foreground admission on an already retained, disposable real library."""
import json
from pathlib import Path
import time

import pytest

from yt_playlist.core.store import Store


def test_actual_predecessor_opens_without_a_foreground_foreign_key_scan(request, tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply an isolated retained predecessor library')
    path = Path(option).resolve(strict=True)
    assert path.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache')
    previous = json.loads((path.parent / 'metadata-conversion.json').read_text())
    assert previous['interruption_rollback_verified']
    assert set(previous['copied_domains']) == {
        'annotations', 'policies', 'descriptions', 'groups', 'recommendations', 'metadata', 'archives'}
    store = Store(path)
    statements = []
    try:
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 79
        recordings = store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]
        assert recordings >= 10000
        store.conn.set_trace_callback(statements.append)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        elapsed = time.monotonic() - started
        assert store.activity_paused and store._schema_recovery_pending
        assert not any(sql.strip().upper().startswith('PRAGMA FOREIGN_KEY_CHECK') for sql in statements)
        assert elapsed < 5, 'Foreground admission still walks the populated library'
        (tmp_path / 'deferred-preflight.json').write_text(json.dumps({
            'database': str(path), 'recordings': recordings, 'schema': 79,
            'pre_http_schema_seconds': elapsed, 'foreign_key_scan_deferred': True,
            'status': 'passed', 'conversion_started': False}, indent=2) + '\n')
    finally:
        store.conn.close()
