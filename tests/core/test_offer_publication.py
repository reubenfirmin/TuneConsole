"""Offer publication preserves deduplication across readers and continuing writes."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.compaction_fixture_support import completed_installed as admitted_installed
from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.store_fixture_support import native_sql_calls, without_query_timing
from yt_playlist.core import home_offers
from yt_playlist.core.migrations import MigrationError, migrate, registry, regular
from yt_playlist.core.preparation import offer_publication
from yt_playlist.core.preparation.recommendation_state import BYTE_LIMITS, JOBS
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture
def disable_periodic_library_sync():
    # These cases open stores directly and never construct an HTTP application.
    pass


@pytest.fixture
def admitted_offer(admitted_installed, compaction_inputs):
    from yt_playlist.core.db_revisions import Revisions

    store, version = admitted_installed
    source = compaction_inputs(version).source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(map(tuple, source.conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:version]]
    assert list(map(tuple, source.conn.execute('SELECT version,name FROM schema_changes ORDER BY version'))) == [
        (entry.version, entry.name) for entry in regular.CHANGES]
    assert isinstance(source._snapshot_revisions, Revisions)
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()
    assert store._snapshot_revisions.conn is store.conn
    yield store, version


def prepare(store):
    store.rec_proposals.put_proposals('home_shown', {'epoch': 12, 'signatures': ['second', 'first']}, 100)
    JOBS['home-offers'].install(store.conn)
    return JOBS['home-offers'].step(store.conn).prepared_revision


@pytest.mark.parametrize('admitted_installed', [79, 82], indirect=True)
def test_publication_and_continuing_writes_share_one_owner(admitted_offer):
    store, _ = admitted_offer
    revision = prepare(store)
    original = tuple(store.conn.execute("SELECT * FROM rec_proposals WHERE surface='home_shown'").fetchone())
    offer_publication.publish(store.conn, revision=revision)
    offer_publication.publish(store.conn, revision=revision)
    assert home_offers.available(store.conn)
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 12, 'signatures': ['first', 'second']}
    # A temporary typed change proves reads use the published owner.
    with store._lock:
        store.conn.execute('BEGIN')
        store.conn.execute('UPDATE prepared_home_offer_dedup SET epoch=13')
        assert store.rec_proposals.get_proposals('home_shown')['epoch'] == 13
        store.conn.rollback()
    continued = {'epoch': 12, 'signatures': ['first', 'second', 'third']}
    store.rec_proposals.put_proposals('home_shown', continued, 101)
    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        assert reopened.rec_proposals.get_proposals('home_shown') == continued
        reopened.rec_proposals.put_proposals('home_shown', {'epoch': 13, 'signatures': ['first']}, 103)
    finally:
        reopened.conn.close()
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 13, 'signatures': ['first']}
    with closing(sqlite3.connect(store._db_path)) as old_writer, old_writer:
        old_writer.execute("UPDATE rec_proposals SET payload=?,built_at=200 WHERE surface='home_shown'",
                           (json.dumps({'epoch': 13, 'signatures': ['old-writer']}),))
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 13, 'signatures': ['old-writer']}
    # A rename may replace the unrelated document body with non-JSON text.
    with store.conn:
        store.conn.execute("UPDATE rec_proposals SET surface='unrelated',payload='opaque {text' WHERE surface='home_shown'")
    assert not store.rec_proposals.has_proposals('home_shown')
    store.rec_proposals.put_proposals('home_shown', {}, 202)
    assert store.rec_proposals.get_proposals('home_shown') is None
    store.rec_proposals.put_proposals('home_shown', {'epoch': 14, 'signatures': ['fresh']}, 203)
    assert store.rec_proposals.get_proposals('home_shown') == {'epoch': 14, 'signatures': ['fresh']}
    assert tuple(store.conn.execute('SELECT * FROM published_offers_originals').fetchone()) == original
    with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
        with store.conn:
            store.conn.execute('DELETE FROM published_offers_originals')
    regular.validate(store.conn)


@pytest.mark.parametrize('installed', [82], indirect=True)
def test_stale_corrupt_and_interrupted_publications_cannot_admit_a_head(installed):
    store, _ = installed
    revision = prepare(store)
    store.rec_proposals.put_proposals('home_shown', {'epoch': 14, 'signatures': ['new']}, 200)
    with pytest.raises(MigrationError, match='reconciled preparation revision'):
        offer_publication.publish(store.conn, revision=revision)
    revision = JOBS['home-offers'].step(store.conn).prepared_revision
    with store.conn:
        store.conn.execute('DELETE FROM prepared_home_offer_signatures')
    with pytest.raises(MigrationError, match='differs from its verified input'):
        offer_publication.publish(store.conn, revision=revision)
    with store.conn:
        store.conn.execute("INSERT INTO prepared_home_offer_signatures VALUES ('new')")
    class InterruptCommit:
        def __getattr__(self, name):
            return getattr(store.conn, name)
        def commit(self):
            raise KeyboardInterrupt('controlled publication interruption')
    with pytest.raises(KeyboardInterrupt):
        offer_publication.publish(InterruptCommit(), revision=revision)
    assert not home_offers.available(store.conn)
    assert not store.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name='published_offers'").fetchone()
    offer_publication.publish(store.conn, revision=revision)


@pytest.mark.parametrize('admitted_installed', [82], indirect=True)
def test_retained_source_supports_the_explicit_frozen_upgrade(admitted_offer):
    store, _ = admitted_offer
    offer_publication.publish(store.conn, revision=prepare(store))
    store.rec_proposals.put_proposals('home_shown', {'epoch': 14, 'signatures': ['new']}, 200)
    store.rec_proposals.put_proposals('home_shown', {'epoch': 14, 'signatures': ['last', 'new']}, 300)
    # The compatible current source still feeds the actual frozen upgrade.
    with closing(sqlite3.connect(store._db_path)) as converter, converter:
        converter.row_factory = sqlite3.Row
        converter.execute('PRAGMA foreign_keys=ON')
        migrate(converter, registry=registry.MIGRATIONS[:83])
    assert not home_offers.prepared_owner(store.conn)
    assert home_offers.read(store.conn) == {'epoch': 14, 'signatures': ['last', 'new']}
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 83


@pytest.mark.parametrize('payload', [
    'null', '[]', '{"epoch":1}', '{"epoch":-1,"signatures":[]}', '{"epoch":true,"signatures":[]}',
    '{"epoch":1.0,"signatures":[]}', '{"epoch":9223372036854775808,"signatures":[]}',
    '{"epoch":1,"signatures":["a","a"]}', '{"epoch":1,"signatures":[0]}',
    '{"epoch":1,"signatures":[""]}', '{"epoch":1,"epoch":2,"signatures":[]}',
    json.dumps({'epoch': 1, 'signatures': ['x'] * 101}),
    json.dumps({'epoch': 1, 'signatures': ['x' * 1025]}),
    '{}' + ' ' * BYTE_LIMITS['offers'],
])
@pytest.mark.parametrize('installed', [82], indirect=True)
def test_invalid_continuing_writes_are_rejected_atomically(installed, payload):
    store, _ = installed
    offer_publication.publish(store.conn, revision=prepare(store))
    with pytest.raises(sqlite3.DatabaseError):
        with store.conn:
            store.conn.execute("UPDATE rec_proposals SET payload=? WHERE surface='home_shown'", (payload,))
    assert home_offers.read(store.conn) == {'epoch': 12, 'signatures': ['first', 'second']}


@pytest.mark.parametrize('installed', [82], indirect=True)
def test_startup_rejects_missing_publication_capture(installed):
    store, _ = installed
    offer_publication.publish(store.conn, revision=prepare(store))
    store.conn.execute('BEGIN')
    try:
        store.conn.execute('DROP TRIGGER published_offers_sync_insert')
        with pytest.raises(MigrationError, match='publication object'):
            regular.validate(store.conn)
    finally:
        store.conn.rollback()


@pytest.mark.parametrize('installed', [82], indirect=True)
def test_published_window_rejects_orphaned_and_excess_signatures(installed):
    store, _ = installed
    offer_publication.publish(store.conn, revision=prepare(store))
    with pytest.raises(sqlite3.IntegrityError, match='still reference the window'):
        with store.conn:
            store.conn.execute('DELETE FROM prepared_home_offer_dedup')
    signatures = [f'offer-{i}' for i in range(100)]
    store.rec_proposals.put_proposals('home_shown', {'epoch': 12, 'signatures': signatures}, 200)
    with pytest.raises(sqlite3.IntegrityError, match='at most 100'):
        with store.conn:
            store.conn.execute("INSERT INTO prepared_home_offer_signatures VALUES ('excess')")
    # Renaming one signature at capacity does not introduce an extra entry.
    with store.conn:
        store.conn.execute("UPDATE prepared_home_offer_signatures SET signature='renamed' WHERE signature='offer-0'")
    store.rec_proposals.put_proposals('home_shown', {}, 201)
    with pytest.raises(sqlite3.IntegrityError, match='require a window'):
        with store.conn:
            store.conn.execute("INSERT INTO prepared_home_offer_signatures VALUES ('orphan')")
