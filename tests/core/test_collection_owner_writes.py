"""Normal retained-format mutations preserve other owners' committed choices."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
import threading

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_collection_feedback_publication import prepare
from tests.repos.test_album_aliases import release
from tests.prepared_fixture_support import prepared_template
from yt_playlist.core import album_feedback_storage
from yt_playlist.core.preparation import collection_feedback_publication
from yt_playlist.core.store import Store
from yt_playlist.rec import album_feedback, choice_evidence
from yt_playlist.repos.collection import CollectionRepo
from yt_playlist.repos.album_feedback import AlbumFeedbackRepo
from yt_playlist.repos.settings import SettingsRepo
from yt_playlist.web.context import Ctx


def context(store):
    return Ctx(store=store, client_provider=lambda: {}, now_fn=lambda: 1, templates=None, jobs=None)


def select(store, key):
    owner = collection_feedback_publication.SOURCES[key]
    owner.publish(store.conn, revision=prepare(store.conn, owner))


FORMATS = [(version, state) for version in (79, 93) for state in ('source-only', 'partial', 'selected')]
FORMATS.append((107, 'native'))


def prepare_owner(store, key, state):
    if state == 'selected':
        select(store, key)
    elif state == 'partial':
        owner = collection_feedback_publication.SOURCES[key]
        owner.worker.install(store.conn)
        owner.worker.step(store.conn, batch_size=1)
        assert owner.selected(store.conn) is None
        assert owner.worker.progress(store.conn).prepared_revision is None


def seed_feedback_writers(store):
    store.accounts.upsert_account('Main', 'main.json', None, True)
    for browse in ('baseline-release', 'first-release', 'second-release'):
        document = deepcopy(release())
        document['title'] = browse
        document['tracks'][0]['videoId'] = browse
        store.collection.observe_album_representation(browse, document)
        choice_evidence.issue(store, 'albums', [{'id': browse}], 0, context={}, opportunity_id=browse)
    album_feedback.set_feedback(store, 'baseline-release', 'reject', 1, 'baseline-release')


@pytest.fixture(scope='module')
def feedback_writer_templates(_pristine_installed_schema, tmp_path_factory):
    # Source-only, partial and selected cases share just the ordinary catalog
    # and evidence prefix. Every case still prepares its own owner and opens
    # its second Store before exercising both real overlapping writes.
    with ExitStack() as resources:
        templates = {}

        def get(version):
            if version not in templates:
                database = tmp_path_factory.mktemp('feedback-writer-prefix') / 'source.db'
                templates[version] = resources.enter_context(prepared_template(
                    _pristine_installed_schema(version), seed_feedback_writers,
                    database=database, defer_metadata=True))
            return templates[version]

        yield get


@pytest.fixture
def feedback_writer_prefix(installed, feedback_writer_templates):
    store, version = installed
    template = feedback_writer_templates(version)
    template.restore(store)
    return template


@pytest.mark.parametrize('installed,state', FORMATS, indirect=['installed'])
@pytest.mark.parametrize('operation', ['flag', 'clear'])
def test_existing_context_updates_keep_other_accounts(installed, operation, state):
    store, version = installed
    first = store.accounts.upsert_account('First', 'first.json', None, True)
    second = store.accounts.upsert_account('Second', 'second.json', None, False)
    if operation == 'clear':
        context(store).flag_auth_expired(first, 'First')
    baseline = {}
    if operation == 'flag' and state == 'partial':
        baseline = {999: 'Historical account'}
        store.settings.set_setting('auth_expired_identities', json.dumps({'999': baseline[999]}))
    prepare_owner(store, 'auth_expired_identities', state)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        earlier, later = context(store), context(other)
        if operation == 'flag':
            earlier.flag_auth_expired(first, 'First')
            assert context(other).auth_expired == {**baseline, first: 'First'}
            later.flag_auth_expired(second, 'Second')
            expected = {**baseline, first: 'First', second: 'Second'}
        else:
            later.flag_auth_expired(second, 'Second')
            assert context(store).auth_expired == {first: 'First', second: 'Second'}
            earlier.clear_auth_expired(first)
            expected = {second: 'Second'}
        assert context(store).auth_expired == expected
        assert context(other).auth_expired == expected
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed,state', FORMATS, indirect=['installed'])
@pytest.mark.usefixtures('feedback_writer_prefix')
def test_overlapping_normal_feedback_writers_keep_each_release(
        installed, state, monkeypatch):
    store, version = installed
    prepare_owner(store, 'album_feedback', state)
    other = Store(store._db_path)
    other.init_schema(defer_metadata=True)
    assert not other.activity_paused
    assert other.rec_ledger.evidence().get('opportunity', 'second-release') is not None
    entered, resume, second_started = threading.Event(), threading.Event(), threading.Event()
    original = CollectionRepo.preferred_album_ids
    original_facts = AlbumFeedbackRepo.replacement_facts
    held = False

    def hold_first(repo, values):
        nonlocal held
        if repo._store is store and not held:
            held = True
            entered.set()
            assert resume.wait(5), 'Coordinator did not release the first normal writer'
        return original(repo, values)

    def second():
        second_started.set()
        album_feedback.set_feedback(other, 'second-release', 'known', 2, 'second-release')

    def hold_native(repo, browse, account_id):
        if repo._store is store:
            hold_first(store.collection, [browse])
        return original_facts(repo, browse, account_id)

    monkeypatch.setattr(CollectionRepo, 'preferred_album_ids', hold_first)
    if version == 107:
        monkeypatch.setattr(AlbumFeedbackRepo, 'replacement_facts', hold_native)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(album_feedback.set_feedback, store, 'first-release', 'more', 3, 'first-release')
            try:
                assert entered.wait(5)
                later = workers.submit(second)
                assert second_started.wait(5)
                # Before repair the second commits here, while the first still
                # holds a stale map. With admission it waits outside ServingLock.
                try:
                    later.result(timeout=.15)
                except TimeoutError:
                    pass
            finally:
                resume.set()
            first.result(timeout=5)
            later.result(timeout=5)
        actual = {key: value['kind'] for key, value in album_feedback_storage.original(store.conn).items()}
        assert actual == {'baseline-release': 'reject', 'first-release': 'more', 'second-release': 'known'}
        # Competing writers may defer bounded evidence replay. Their committed
        # judgments must each own either a published response or a durable intent.
        published = store.rec_ledger.evidence().records('response')
        pending = store.album_feedback.pending_responses(after=0, limit=128)
        assert set(actual) <= ({row['opportunity_id'] for row in published}
                               | {row['opportunity_id'] for row in pending})
        album_feedback.drain(store)
        assert not store.album_feedback.pending_responses(after=0, limit=128)
        responses = store.rec_ledger.evidence().records('response')
        assert sorted((row['opportunity_id'], row['kind']) for row in responses) == [
            ('baseline-release', 'feedback'), ('first-release', 'feedback'), ('second-release', 'feedback')], {
                'responses': responses, 'other_responses': other.rec_ledger.evidence().records('response'),
                'group_files': getattr(other, '_compact_group_files', None),
                'paused': other.activity_paused}
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        resume.set()
        other.conn.close()


@pytest.mark.parametrize('installed', [79, 93, 107], indirect=True)
def test_feedback_writer_prefix_has_private_sql_and_evidence(
        installed, feedback_writer_prefix, tmp_path):
    store, _ = installed
    template = feedback_writer_prefix
    other = Store(tmp_path / 'other.db')
    try:
        template.restore(other)
        other.init_schema(defer_metadata=True)
        before = album_feedback_storage.original(store.conn)
        assert set(before) == {'baseline-release'}
        original = template.source.rec_ledger.evidence().records('response')
        assert len(original) == 1
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
        assert set(album_feedback_storage.original(other.conn)) == {'baseline-release', 'first-release'}
        assert album_feedback_storage.original(store.conn) == before
        assert album_feedback_storage.original(template.source.conn) == before
        assert store.rec_ledger.evidence().records('response') == original
        assert template.source.rec_ledger.evidence().records('response') == original
        # Damage bytes in a copied preexisting file: hard links would corrupt
        # both the immutable source and the other case's original response.
        source_file = next((template.source.rec_ledger.evidence().directory / 'response').glob('*/*.json'))
        private = other.rec_ledger.evidence().directory / source_file.relative_to(
            template.source.rec_ledger.evidence().directory)
        private.write_bytes(b'controlled private damage')
        assert store.rec_ledger.evidence().records('response') == original
        assert template.source.rec_ledger.evidence().records('response') == original
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [79, 93, 107], indirect=True)
def test_failed_feedback_sql_does_not_emit_response_and_retry_keeps_withdrawals_unique(installed, monkeypatch):
    store, _ = installed
    store.accounts.upsert_account('Main', 'main.json', None, True)
    choice_evidence.issue(store, 'albums', [{'id': 'release'}], 0, context={}, opportunity_id='offer')
    album_feedback.set_feedback(store, 'release', 'more', 1, 'offer')
    before = album_feedback_storage.original(store.conn)
    responses = store.rec_ledger.evidence().records('response')
    with monkeypatch.context() as patch:
        original = AlbumFeedbackRepo._save_intents
        def fail(repo, intents):
            original(repo, intents)
            raise OSError('controlled feedback SQL failure')
        patch.setattr(AlbumFeedbackRepo, '_save_intents', fail)
        with pytest.raises(OSError, match='controlled feedback SQL failure'):
            album_feedback.set_feedback(store, 'release', 'known', 2, 'offer')
    assert album_feedback_storage.original(store.conn) == before
    assert store.rec_ledger.evidence().records('response') == responses
    album_feedback.set_feedback(store, 'release', 'known', 2, 'offer')
    album_feedback.set_feedback(store, 'release', 'clear', 3)
    album_feedback.set_feedback(store, 'release', 'clear', 4)
    assert not album_feedback_storage.original(store.conn)
    responses = store.rec_ledger.evidence().records('response')
    feedback = [row for row in responses if row['kind'] == 'feedback']
    withdrawn = [row for row in responses if row['kind'] == 'withdraw']
    assert len(feedback) == len(withdrawn) == 2
    assert {row['details']['action_id'] for row in withdrawn} == {row['id'] for row in feedback}


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
@pytest.mark.parametrize('state', ['source-only', 'selected'])
def test_clearing_alias_keeps_unattributed_minimal_legacy_judgments(installed, state):
    store, _ = installed
    for browse in ('legacy-alias', 'current-alias'):
        store.collection.observe_album_representation(browse, release())
    unrelated = {'kind': 'known', 'at': 9}
    store.settings.set_setting('album_feedback', json.dumps({
        'legacy-alias': {'kind': 'reject', 'at': 10}, 'unrelated': unrelated}))
    prepare_owner(store, 'album_feedback', state)
    album_feedback.set_feedback(store, 'current-alias', 'clear', 11)
    assert album_feedback_storage.original(store.conn) == {'unrelated': unrelated}
