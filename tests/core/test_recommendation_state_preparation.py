"""Small domain-limited documents prepare without changing their live owners."""
import json

import pytest

from tests.core.test_predecessor_writes import installed
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation.recommendation_state import BYTE_LIMITS, JOBS


CASES = [(79, 'work'), (81, 'work'), (79, 'home-offers'), (82, 'home-offers'),
         (79, 'album-requests'), (83, 'album-requests')]
VALUES = {
    'work': ('daily_model_schedule', {'next_at': 1234.5}),
    'home-offers': ('home_shown', {'epoch': 123, 'signatures': ['second', 'first']}),
    'album-requests': ('album_note_requests', {'release': {'browse_id': 'release', 'artist': 'Artist',
                                                        'title': 'Album', 'year': '2000'}}),
}


def finish(job, conn):
    for _ in range(20):
        result = job.step(conn, batch_size=1)
        if result.status == 'prepared':
            return result
    pytest.fail('Bounded document job did not finish')


@pytest.mark.parametrize('installed,job_name', CASES, indirect=['installed'])
def test_documents_stay_authoritative_and_capture_continuing_replacements(installed, job_name):
    store, _ = installed
    job = JOBS[job_name]
    surface, value = VALUES[job_name]
    store.rec_proposals.put_proposals(surface, value, 1000)
    store.rec_proposals.put_proposals('unrelated-output', {'retained': True}, 999)
    if job_name == 'work':
        store.rec_proposals.put_proposals('tv_preparation', {'failed': False, 'at': 100}, 1001)
    raw = store.conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
    job.install(store.conn)
    job.step(store.conn, batch_size=1)
    regular.migrate(store.conn)
    finish(job, store.conn)
    assert tuple(store.conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()) == tuple(raw)
    assert store.rec_proposals.get_proposals(surface) == value
    assert not store.activity_paused
    if job_name == 'work':
        assert tuple(store.conn.execute('SELECT failed,observed_at FROM prepared_tv_preparation_state').fetchone()) == (0, 100)
        assert store.conn.execute('SELECT next_at FROM prepared_model_selection_schedule').fetchone()[0] == 1234.5
    elif job_name == 'home-offers':
        assert [r[0] for r in store.conn.execute('SELECT signature FROM prepared_home_offer_signatures ORDER BY signature')] == ['first', 'second']
    else:
        assert tuple(store.conn.execute('SELECT year_text,year_number,album_provider_id,unresolved_reason FROM prepared_album_note_requests').fetchone()) == ('2000', None, None, 'provider_album_not_admitted')
    complete = job.progress(store.conn)
    store.rec_proposals.put_proposals('unrelated-output', {'retained': 'edited'}, 1010)
    assert job.progress(store.conn) == complete
    store.rec_proposals.put_proposals(surface, {}, 1020)
    assert job.progress(store.conn).prepared_revision is None
    finish(job, store.conn)
    assert store.rec_proposals.get_proposals(surface) == {}
    # A key rename must withdraw the prepared original even when the new key
    # belongs to a different, unselected domain.
    with store.conn:
        store.conn.execute('UPDATE rec_proposals SET surface=? WHERE surface=?', ('renamed-original', surface))
    finish(job, store.conn)
    assert not store.conn.execute(f'SELECT 1 FROM prepared_{job.name}_originals WHERE surface=?', (surface,)).fetchone()
    assert store.rec_proposals.get_proposals('renamed-original') == {}


@pytest.mark.parametrize('installed,job_name', CASES[::2], indirect=['installed'])
def test_invalid_document_failure_keeps_source_and_requires_explicit_retry(installed, job_name):
    store, _ = installed
    job = JOBS[job_name]
    surface, value = VALUES[job_name]
    store.rec_proposals.put_proposals(surface, value, 1000)
    job.install(store.conn)
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET payload='{' WHERE surface=?", (surface,))
    with pytest.raises(ValueError):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == '{'
    regular.migrate(store.conn)
    store.rec_proposals.put_proposals(surface, value, 1001)
    with pytest.raises(MigrationError, match='retry explicitly'):
        job.step(store.conn)
    job.step(store.conn, retry=True)
    assert finish(job, store.conn).status == 'prepared'


@pytest.mark.parametrize('installed,job_name', CASES[::2], indirect=['installed'])
def test_oversized_original_is_not_silently_truncated_to_a_valid_document(installed, job_name):
    store, _ = installed
    job = JOBS[job_name]
    surface, _ = VALUES[job_name]
    raw = '{}' + ' ' * BYTE_LIMITS[job.name]
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', (surface, raw, 100))
    job.install(store.conn)
    with pytest.raises(ValueError, match='byte budget'):
        finish(job, store.conn)
    assert job.progress(store.conn).status == 'failed'
    assert store.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()[0] == raw
