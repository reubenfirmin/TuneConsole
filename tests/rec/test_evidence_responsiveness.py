"""Recovery owns publication, while foreground reads use the last committed state."""
from tests.file_content_support import read_bytes as original_bytes
from tests.file_content_support import patch_byte_reads
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import hashlib
import json
import logging
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_catalog_track
from tests.core.test_group_content import prepared
from tests.genre_catalog_support import bound_catalog
from tests.home_fixture_support import returning_listener, without_metadata_providers
from tests.onboarding_support import publish_personal_mix
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import fao
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.core.evidence_ledger import canonical, EvidenceBudgetExceeded, EvidenceLedger
from yt_playlist.core.storage_maintenance import EVIDENCE_MESSAGE, EVIDENCE_RETRY_MESSAGE, StorageMaintenance
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence, choice_retention, group_root_backfill, group_roots, home_cards, home_cache, stored_home
from yt_playlist.rec.choice_capture import CaptureQueue
from yt_playlist.rec.choice_context import checkpoint, CORRECTION_CONTRACT
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.stored_radio import PreparedRadio
from yt_playlist.rec.worker_queue import Job
from yt_playlist.repos.compact_taste_groups import CompactTasteGroupsRepo
from yt_playlist.repos import group_queries
from yt_playlist.web.app import create_app


EVIDENCE_GENRES = (('house', 'House'), ('rock', 'Rock'), ('jazz', 'Jazz'))


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(EVIDENCE_GENRES) as schema:
        yield schema


@pytest.fixture(autouse=True)
def evidence_genres(monkeypatch):
    # Recovery owns real schema admission and SQL/file custody. Taxonomy
    # curation is independent; retain the genres used by these serving inputs.
    bound_catalog(monkeypatch, entries=EVIDENCE_GENRES)


@pytest.fixture(scope='module')
def serving_bytecode(tmp_path_factory):
    from jinja2 import FileSystemBytecodeCache

    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('evidence-template-bytecode')))


@pytest.fixture(autouse=True)
def serving_routes(monkeypatch, serving_bytecode, without_metadata_providers):
    install_serving_routes(monkeypatch, serving_bytecode)


def install_serving_routes(monkeypatch, serving_bytecode):
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home, playlists

    # Exercise real pages, settings writes, middleware and maintenance handlers.
    # Registration of unrelated feature routers and desktop idle shutdown are
    # independent of the storage/evidence admission boundaries under test.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (app_update, audio_preview, home, playlists)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    # Diagnostic JSONL history is owned by test_home_audit. Opportunity capture,
    # visibility, retained roots and recovery publication still run normally.
    monkeypatch.setattr(home_cards.HomeCards, 'audit', lambda *args, **kwargs: None)
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = serving_bytecode
        return result

    # Reuse compiled source, with a private environment and request context for
    # every app. No rendered markup or database-dependent values are cached.
    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)


def assert_maintenance_banner(response, *, visible=False):
    banners = []
    banner_text = []

    class Banners(HTMLParser):
        depth = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get('id') == 'storage-maintenance':
                banners.append(attrs)
                self.depth = 1
            elif self.depth:
                self.depth += 1

        def handle_endtag(self, tag):
            if self.depth:
                self.depth -= 1

        def handle_data(self, data):
            if self.depth:
                banner_text.append(data)

    Banners().feed(response.text)
    assert len(banners) == 1 and ('hidden' not in banners[0]) is visible
    if visible:
        assert ''.join(banner_text).strip() == (
            'Optimizing app storage; some recommendations are temporarily unavailable.')


def historical_inventory(store, identity):
    """A predecessor file plus a prepared comparison not yet in root inventory.

    Choices no longer own musical-group history (#688). Prepared comparisons
    still do, so use that real owner to exercise historical context expansion.
    """
    captured = {'at': 10, 'group_generation': 'first', 'musical_groups': [
        {'group_id': 'stable-parent', 'version': 'first-parent'},
        {'group_id': 'stable-leaf', 'version': 'first-leaf', 'parent_version': 'first-parent'}]}
    ledger = store.rec_ledger.evidence()
    artifact = ledger.artifact(captured)
    ledger.put('opportunity', identity, {'id': identity, 'schema': 1, 'format': 'mixes',
        'at': 10, 'observed_at': 10, 'context': artifact,
        'options': [{'id': 'one', 'score': 1}], 'prediction': ['one'], 'prediction_status': 'issued'})
    surface = 'model_pair:' + identity + ':incumbent:mixes'
    # Deliberately bypass live publication to represent pre-inventory storage.
    with store.conn:
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,10)',
                           (surface, json.dumps({'context': {'artifact': artifact}})))
        store.conn.execute('UPDATE group_retention_state SET roots_complete=0 WHERE id=1')
    store._group_root_backfill = group_root_backfill.Backfill(store)
    return identity, surface


def seed_evidence_library(store):
    """Initial retained evidence, independent of the recovery transition under test."""
    for key in ('one', 'two'):
        seed_catalog_track(store, key, key, 'Artist', video_id=key, genre='house')
    value = prepared('first', 1)
    value.update(unknown=[], edges=[], lineage=[])
    for group in value['groups']:
        group['payload'] = json.dumps({**json.loads(group['payload']), 'size': len(group['members'])})
    owner = store.taste_groups.stage(value)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        store.taste_groups.finish_staged('first', owner)
    choice_evidence.issue(store, 'mixes', [{'id': 'one'}, {'id': 'two'}], 10,
        context={'at': 10, 'group_generation': 'first', 'musical_groups': [
            {'group_id': 'stable-leaf', 'version': 'first-leaf', 'parent_version': 'first-parent'}]})


@pytest.fixture(scope='module')
def evidence_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, seed_evidence_library) as template:
        yield template


@pytest.fixture(scope='module')
def historical_evidence_template(_pristine_installed_schema, tmp_path_factory):
    # Admit one genuine installed v94 source, retaining its physical owners and
    # history. Consumers receive private SQL/files, not a relabelled new schema.
    path = tmp_path_factory.mktemp('historical-evidence') / 'library.db'
    with prepared_template(_pristine_installed_schema(94), seed_evidence_library,
                           database=path) as template:
        assert template.source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 94
        yield template


@pytest.fixture
def library(request, tmp_path):
    historical = getattr(request, 'param', None) == 94
    name = 'historical_evidence_template' if historical else 'evidence_template'
    with request.getfixturevalue(name).open(tmp_path / 'evidence.db') as store:
        yield store


def publish_serving_inputs(store, *, tracks_per_card=4):
    """Consume completed menus and candidates without running unrelated rankers."""
    publish_personal_mix(store, tracks_per_card=tracks_per_card)
    menu = home_cache.read(store, 'home_tuned')['menus'][0]
    lanes = {proto['lane']: proto['tracks'] for proto in menu['protos']}
    assert set(lanes) == {'comfort', 'wheelhouse', 'explore', 'temporal'}
    # The responsiveness checks exercise real snapshot/SQL publication leases,
    # including Radio's retained head. Numerical ranking has independent owners
    # in test_ranking_playback_publication and test_model_refresh.
    publish_bundles(store, {'all': lanes, '_meta': {'full_pools': True}}, 1000)


def recovery_worker(store, now):
    # These recovery consumers need the actual worker and maintenance admission,
    # independently of HTTP routing, templates and provider configuration.
    context = SimpleNamespace(store=store, now_fn=lambda: now,
        client_provider=lambda: {}, logger=logging.getLogger(__name__),
        storage_maintenance=StorageMaintenance(store))
    return RecWorker(context, debounce_s=0)


def test_evidence_input_keeps_published_groups_choices_and_read_only_sql(evidence_template):
    source = evidence_template.source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.taste_groups.head_generation() == 'first'
    assert source.taste_groups.definition('first-leaf', generation='first')
    opportunity, = source.rec_ledger.evidence().records('opportunity')
    assert [option['id'] for option in opportunity['options']] == ['one', 'two']
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_correction_identity_and_current_learning_descriptors_match_the_published_head(library, monkeypatch):
    head = library.taste_groups.head()
    expected_head = {'generation': head['generation'], 'groups': [
        {key: group[key] for key in ('group_id', 'version', 'parent_version', 'leaf')}
        | {'legacy': bool(group.get('legacy')), 'genres': group.get('genres', [])}
        for group in head['groups']]}
    for version in ('first-parent', 'first-leaf'):
        original = library.taste_groups.definition(version, generation='first')
        expected = {key: original[key] for key in (
            'group_id', 'version', 'generation', 'created_at', 'parent_version')}
        assert library.taste_groups.correction_definition(version, generation='first') == expected
    assert library.taste_groups.correction_definition('first-leaf', generation='other') is None
    monkeypatch.setattr(group_queries, '_definition', lambda *a: pytest.fail('Decoded a diagnostic graph'))
    monkeypatch.setattr(group_queries, '_generation', lambda *a: pytest.fail('Decoded model inputs'))
    assert library.taste_groups.learning_head() == expected_head
    acknowledged = choice_retention.acknowledge(library, 11, lambda *a: {'watermark': 'published'})
    assert len(acknowledged) == 1


def test_mix_learning_does_not_recover_retired_legacy_ancestry(library, monkeypatch):
    definitions = [library.taste_groups.correction_definition(version, generation='first')
                   for version in ('first-parent', 'first-leaf')]
    oid = choice_evidence.issue(library, 'mixes', [
        {'id': 'leaf', 'group_version': 'first-leaf'}], 12,
        context={'at': 12, 'group_generation': 'first', 'musical_groups': [
            {'group_id': row['group_id'], 'version': row['version']} for row in definitions]})
    assert choice_evidence.respond(library, oid, 'visible', 13, visible=['leaf'])
    assert choice_evidence.respond(library, oid, 'choice', 14, option_id='leaf')
    expected = choice_evidence.compile_report(library, 15)
    ledger = library.rec_ledger.evidence()
    identity = ledger.get('opportunity', oid)['context']
    context = {'at': 12, 'group_generation': 'first', 'musical_groups': [
        {'group_id': row['group_id'], 'version': row['version']} for row in definitions],
        'musical_definitions': [{key: value for key, value in row.items() if key != 'parent_version'}
                               for row in definitions]}
    # Controlled early-v1 checkpoint: unrelated history is unknown and must
    # not be loaded merely to learn from the one actually served alternative.
    context['musical_definitions'].extend({'group_id': f'other-{i}', 'version': f'other-{i}',
        'generation': 'first', 'created_at': 1} for i in range(500))
    ledger.put('checkpoint', identity, {'schema': 1, 'contract': CORRECTION_CONTRACT,
        'source_context': identity, 'context': context, 'digest': hashlib.sha256(canonical(context)).hexdigest(),
        'watermark': expected['watermark'], 'incorporated_at': 15})
    lookup, readers = CompactTasteGroupsRepo.correction_definition, set()

    def relevant(self, version, **kwargs):
        assert version in ('first-parent', 'first-leaf')
        assert self._store._read_only
        readers.add(id(self._store))
        return lookup(self, version, **kwargs)

    monkeypatch.setattr(CompactTasteGroupsRepo, 'correction_definition', relevant)
    monkeypatch.setattr(group_queries, '_definition', lambda *a: pytest.fail('Decoded a diagnostic graph'))
    monkeypatch.setattr(group_queries, '_generation', lambda *a: pytest.fail('Decoded model inputs'))
    actual = choice_evidence.compile_report(library, 15)
    assert actual == expected
    assert not readers
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind IN ('offer','checkpoint')").fetchone()


def test_small_inventory_batches_advance_before_repeating_the_whole_ledger_scan(library, monkeypatch):
    monkeypatch.setattr(group_root_backfill, 'BATCH_ROWS', 1)
    monkeypatch.setattr(group_root_backfill, 'BATCH_BYTES', 1)
    publish, calls = choice_evidence.compile_report, []

    def counted(*args, **kwargs):
        assert library.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0]
        calls.append(True)
        return publish(*args, **kwargs)

    monkeypatch.setattr(choice_evidence, 'compile_report', counted)
    result = choice_evidence.maintain_storage(library, 11)
    assert not result.get('deferred')
    assert calls == [True]
    assert not library.activity_paused
    offer, = library.rec_ledger.evidence().records('opportunity')
    assert checkpoint(library.rec_ledger.evidence(), offer['context'])


@pytest.mark.parametrize('failure', [WorkBudgetExceeded, WorkClosing, OSError])
def test_pending_cleanup_survives_interruption_without_pausing_activity(library, monkeypatch, failure):
    path = library._db_path
    original = choice_evidence._maintain_storage
    monkeypatch.setattr(choice_evidence, '_maintain_storage', lambda *a, **kw: (_ for _ in ()).throw(failure('stop')))
    if failure is WorkBudgetExceeded:
        assert choice_evidence.maintain_storage(library, 11) == {'deferred': True}
    else:
        with pytest.raises(failure):
            choice_evidence.maintain_storage(library, 11)
    assert not library.activity_paused and library._evidence_maintenance == 0
    assert library.settings.get_setting(choice_evidence.STORAGE_PENDING) == '1'
    status = StorageMaintenance(library).status()
    assert not status['active'] and status['recording']
    assert status['evidence_maintenance']['pending']
    assert status['message'] == (EVIDENCE_MESSAGE if failure is WorkBudgetExceeded else EVIDENCE_RETRY_MESSAGE)
    monkeypatch.setattr(choice_evidence, '_maintain_storage', original)
    # A new Store and worker reconstruct intent from committed state, without
    # inheriting an iterator, cache, old thread or old retry timestamp.
    reopened = Store(path)
    reopened.init_schema()
    worker = recovery_worker(reopened, 11)
    assert not reopened.activity_paused
    scheduled = []
    monkeypatch.setattr(worker, '_start_loop', lambda: scheduled.append(True))
    worker._poll_evidence_storage()
    assert scheduled and worker.jobs.has('evidence_storage')
    for _ in range(10):
        result = choice_evidence.maintain_storage(reopened, 11)
        if not result.get('deferred'):
            break
    else:
        pytest.fail('Committed evidence recovery did not finish after restart')
    assert reopened.settings.get_setting(choice_evidence.STORAGE_PENDING) == '0'
    assert not reopened.activity_paused
    assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
    assert reopened.taste_groups.definition('first-leaf', generation='first')
    worker.shutdown()
    worker.join(0)
    reopened.conn.close()


def test_playlists_keep_committed_account_metadata_while_schema_writer_is_held(library):
    main = library.accounts.upsert_account('Main', 'bridge-main', None, True)
    other = library.accounts.upsert_account('Other', 'bridge-other', None, False)
    audio = library.tracks.upsert_track('playlist-audio', 'Audio', 'Artist', None, 100,
        video_type='MUSIC_VIDEO_TYPE_ATV', thumbnail='/audio.png')
    video = library.tracks.upsert_track('playlist-video', 'Video', 'Artist', None, 100,
        video_type='MUSIC_VIDEO_TYPE_OMV', thumbnail='/video.png')
    first = library.playlists.upsert_playlist(main, 'LM', 'Main liked', 1, '', 1,
                                             thumbnail='/main-cover.png')
    second = library.playlists.upsert_playlist(other, 'LM', 'Other liked', 1, '', 1)
    generated = library.playlists.upsert_playlist(main, 'GENERATED', 'Generated mix', 1, '', 1)
    library.playlists.set_playlist_tracks(first, [audio])
    library.playlists.set_playlist_tracks(second, [video])
    library.playlists.set_playlist_tracks(generated, [video])
    library.playlists.set_playlist_group('LM', 'Main group', account_id=main)
    library.playlists.set_playlist_group('LM', 'Other group', account_id=other)
    library.playlists.mark_generated_playlist('GENERATED')
    library.playlists.set_playlist_group('GENERATED', 'Generated')
    library.playlist_recipes.set_recipe('GENERATED', {'media': 'video'}, now=900)
    library.playlists.upsert_playlist(main, 'HIDDEN', 'Hidden playlist', 0, '', 1)
    library.playlists.hide_playlist('HIDDEN')
    app = create_app(library, lambda: {}, now_fn=lambda: 1000, background_work=False)
    entered, release = threading.Event(), threading.Event()
    assert not library.activity_paused

    def hold_writer():
        with library._lock:
            library.conn.execute('BEGIN IMMEDIATE')
            try:
                library.conn.execute("UPDATE playlists SET title='Uncommitted',thumbnail='/uncommitted.png' "
                                     'WHERE id=?', (first,))
                library.conn.execute("UPDATE accounts SET label='Uncommitted account' WHERE id=?", (main,))
                library.conn.execute("UPDATE playlist_scopes SET group_name='Uncommitted group' "
                                     'WHERE id=?', (f'@account:{main}:LM',))
                entered.set()
                assert release.wait(10), 'Playlists GET waited for the schema writer'
            finally:
                library.conn.rollback()

    with TestClient(app, base_url='http://127.0.0.1') as client:
        # Warm templates through the unchanged normal route before holding SQL.
        assert client.get('/playlists').status_code == 200
        try:
            with ThreadPoolExecutor(2) as executor:
                writer = executor.submit(hold_writer)
                try:
                    assert entered.wait(2)
                    response = executor.submit(client.get, '/playlists').result(timeout=2)
                    assert response.status_code == 200 and 'playlistsTab(' in response.text
                    assert not writer.done() and not release.is_set()
                    rows = {row['id']: row for row in response.context['rows']}
                    assert set(rows) == {first, second, generated}
                    fields = ('title', 'ytm', 'identity', 'thumbnail', 'kind', 'group', 'is_generated', 'is_video')
                    assert tuple(rows[first][field] for field in fields) == (
                        'Main liked', 'LM', 'Main', '/main-cover.png', 'audio', 'Main group', False, False)
                    assert tuple(rows[second][field] for field in fields) == (
                        'Other liked', 'LM', 'Other', '/video.png', 'video', 'Other group', False, False)
                    assert tuple(rows[generated][field] for field in fields) == (
                        'Generated mix', 'GENERATED', 'Main', '/video.png', 'video', 'Generated', True, True)
                    assert rows[generated]['created'] is None
                    assert rows[generated]['generated_created'] == 900
                finally:
                    release.set()
                writer.result(timeout=2)
        finally:
            release.set()
    assert library.playlists.get_playlist(first).title == 'Main liked'


def test_admission_race_refuses_capture_and_cleans_its_lifetime_file(library, monkeypatch):
    from yt_playlist.core import policy_documents

    jobs = []
    library.choice_capture = CaptureQueue(lambda job: jobs.append(job) or True)
    entered, release = threading.Event(), threading.Event()
    pending = group_roots.pending
    admission = group_roots.admission
    directory = group_roots.group_documents.directory(library.conn) / '.pending'

    def late(store, captured, at):
        assert captured == {}, 'Listening choices must not restore group-history pins'
        entered.set()
        assert release.wait(5)
        return pending(store, captured, at)

    # #688 removed historical-group custody from queued listening choices. An
    # empty capture must stay usable while the independent group owner is busy.
    monkeypatch.setattr(group_roots, 'pending', late)
    try:
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(choice_evidence.issue, library, 'mixes', [{'id': 'one'}], 12,
                context={'group_generation': 'first'}, opportunity_id='no-group-menu')
            try:
                assert entered.wait(2)
                with admission(library):
                    release.set()
                    assert future.result(1) == 'no-group-menu'
                    capture, = jobs
                    assert capture.opportunity['id'] == 'no-group-menu'
                    assert 'group_generation' not in capture.group_context
                    assert library.choice_capture.get('no-group-menu')['id'] == 'no-group-menu'
                    assert not library.rec_ledger.evidence().get('opportunity', 'no-group-menu')
                    assert not list(directory.glob('*.lock'))
                    assert not library.conn.execute(
                        "SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
            finally:
                release.set()
        assert capture.run() == 'no-group-menu'
        assert library.rec_ledger.evidence().get('opportunity', 'no-group-menu')['id'] == 'no-group-menu'
    finally:
        release.set()
        for job in jobs:
            job.close()
            job.close()
    assert library.choice_capture.get('no-group-menu') is None
    assert not library.choice_capture.opportunities()
    jobs.clear()
    monkeypatch.setattr(group_roots, 'pending', pending)

    # A genuine group reader still needs admission. Pause after _pending has
    # reserved its OS lifetime file, before it tries the real admission lock.
    entered.clear()
    release.clear()

    def late_admission(*args, **kwargs):
        assert kwargs.get('blocking') is False
        entered.set()
        assert release.wait(5)
        return admission(*args, **kwargs)

    monkeypatch.setattr(group_roots, 'admission', late_admission)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(pending, library, {'group_generation': 'first'}, 12)
        try:
            assert entered.wait(2)
            path, = directory.glob('*.lock')
            with path.open('r+b') as stream:
                unlock = policy_documents._try_file_lock(stream, exclusive=True)
                try:
                    assert unlock is None, 'The paused capture must already own its lifetime file'
                finally:
                    if unlock is not None:
                        unlock()
            with admission(library):
                release.set()
                with pytest.raises(WorkBudgetExceeded, match='publication'):
                    future.result(1)
                assert not jobs and not library.choice_capture.opportunities()
                assert not list(directory.glob('*.lock'))
                assert not library.conn.execute(
                    "SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
        finally:
            release.set()
            # If an earlier assertion failed before acquiring admission, the
            # reader may have succeeded. Retire its real callback before exit.
            try:
                close = future.result(2)
            except WorkBudgetExceeded:
                pass
            else:
                if close is not None:
                    close()
    monkeypatch.setattr(group_roots, 'admission', admission)

    # The same nonempty reader succeeds once the contended owner releases,
    # proving the negative did not merely exercise an absent generation.
    close = pending(library, {'group_generation': 'first'}, 12)
    try:
        assert callable(close)
        path, = directory.glob('*.lock')
        assert library.conn.execute("SELECT 1 FROM group_evidence_owners "
            "WHERE kind='pending' AND external_key=?", (path.stem,)).fetchone()
    finally:
        if close is not None:
            close()
            close()

    # Synchronous publication still needs admission and must not persist an
    # offer while another owner holds it. There is no queued fallback here.
    with monkeypatch.context() as synchronous:
        synchronous.delattr(library, 'choice_capture')
        with ThreadPoolExecutor(1) as executor, admission(library):
            future = executor.submit(choice_evidence.issue, library, 'mixes', [{'id': 'one'}], 12,
                context={'group_generation': 'first'}, opportunity_id='refused-menu')
            assert future.result(1) is None
            assert not library.rec_ledger.evidence().get('opportunity', 'refused-menu')
    assert not jobs and not library.choice_capture.opportunities()
    assert not list(directory.glob('*.lock'))
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()


def test_reader_close_during_maintenance_leaves_a_conservative_root_for_the_reaper(library):
    close = group_roots.pending(library, {'group_generation': 'first'}, 12)
    with ThreadPoolExecutor(1) as executor, group_roots.admission(library):
        executor.submit(close).result(1)
        assert library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    group_roots.reap_pending(library)
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    directory = group_roots.group_documents.directory(library.conn) / '.pending'
    assert not list(directory.glob('*.lock'))


def test_repeated_cutoffs_commit_checkpoints_and_resume_beyond_the_prefix_after_reopen(library, monkeypatch):
    for index in range(10):
        choice_evidence.issue(library, 'mixes', [{'id': 'one'}], 20 + index,
            context={'at': 20 + index, 'group_generation': 'first', 'musical_groups': [
                {'group_id': 'stable-parent', 'version': 'first-parent'},
                {'group_id': 'stable-leaf', 'version': 'first-leaf'}]})
    ledger = library.rec_ledger.evidence()
    original = ledger.records('opportunity')
    definition = CompactTasteGroupsRepo.correction_definition
    reads = [0]

    def cutoff(self, *args, **kwargs):
        reads[0] += 1
        if reads[0] % 3 == 0:
            raise WorkBudgetExceeded('Controlled deadline during historical definition processing')
        return definition(self, *args, **kwargs)

    monkeypatch.setattr(CompactTasteGroupsRepo, 'correction_definition', cutoff)
    reopened = None
    store, previous = library, 0
    try:
        for turn in range(30):
            try:
                choice_retention.acknowledge(store, 100, lambda *a: {'watermark': 'published'})
            except WorkBudgetExceeded:
                pass
            count = len(ledger.records('checkpoint'))
            assert count > previous or count == len(original)
            previous = count
            if turn == 2:
                assert store.settings.get_setting(choice_retention.AFTER)
                reopened = Store(library._db_path)
                reopened.init_schema()
                store = reopened
            if count == len(original):
                break
        assert previous == len(original)
        for offer in original:
            assert checkpoint(ledger, offer['context'])
            assert ledger.get('artifact', offer['context'])
    finally:
        if reopened is not None:
            reopened.conn.close()


def test_accepted_fifo_tail_keeps_roots_and_retries_while_recovery_is_pending(library, monkeypatch):
    # Drive retry ordering without the unrelated two-second scheduling delay.
    worker = recovery_worker(library, 100)
    scheduled = []
    monkeypatch.setattr(worker, '_start_loop', lambda: scheduled.append(True))
    oid = choice_evidence.issue(library, 'mixes', [{'id': 'one'}], 20,
                                context={'group_generation': 'first'})
    assert oid and choice_evidence.respond(library, oid, 'choice', 21, option_id='one')
    captures = list(worker.jobs.measurements)
    put, maintain = EvidenceLedger.put, choice_evidence._maintain_storage

    def full(self, kind, *args, **kwargs):
        if kind == 'opportunity':
            raise EvidenceBudgetExceeded('Controlled capacity failure', required_bytes=100)
        return put(self, kind, *args, **kwargs)

    def deferred(*args, **kwargs):
        raise WorkBudgetExceeded('Controlled recovery deadline')

    monkeypatch.setattr(EvidenceLedger, 'put', full)
    monkeypatch.setattr(choice_evidence, '_maintain_storage', deferred)
    worker._flush_measurements()
    assert not library.activity_paused
    assert list(worker.jobs.measurements) == captures
    assert library.choice_capture.get(oid)
    # The accepted FIFO owns its immutable menu/response custody. Compact mix
    # evidence must not recreate historical group pins during capacity recovery.
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    monkeypatch.setattr(EvidenceLedger, 'put', put)
    monkeypatch.setattr(choice_evidence, '_maintain_storage', maintain)
    worker.jobs.retry[Job.MEASUREMENT].retry_at = 0
    worker._running = False
    scheduled.clear()
    worker._retry_measurements()
    assert scheduled == [True]
    worker._run_pending()
    assert not worker.jobs.measurements and not library.choice_capture.opportunities()
    assert library.rec_ledger.evidence().get('response', oid + ':choice:one')
    assert not library.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()
    for _ in range(10):
        if not choice_evidence.maintain_storage(library, 100).get('deferred'):
            break
    assert not library.activity_paused
    worker.shutdown()
    worker.join(0)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX application interruption signals')
@pytest.mark.parametrize('termination', [signal.SIGINT, signal.SIGKILL])
def test_process_interruption_releases_publication_and_restart_finishes_recovery(library, tmp_path, termination):
    gate = tmp_path / 'historical-definition-entered'
    code = textwrap.dedent('''
        import sys, threading
        from pathlib import Path
        from yt_playlist.core.store import Store
        from yt_playlist.rec import choice_evidence, choice_retention
        store = Store(sys.argv[1])
        store.init_schema()
        original = choice_retention.correction_context
        def held(*args, **kwargs):
            value = original(*args, **kwargs)
            Path(sys.argv[2]).write_text('Compact correction prepared under publication admission')
            threading.Event().wait(30)
            return value
        choice_retention.correction_context = held
        try:
            choice_evidence.maintain_storage(store, 100)
        except KeyboardInterrupt:
            sys.exit(130)
        finally:
            store.conn.close()
    ''')
    process = subprocess.Popen([sys.executable, '-c', code, str(library._db_path), str(gate)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not gate.exists():
            assert process.poll() is None, process.communicate()[1]
            assert time.monotonic() < deadline, 'Child did not enter historical definition processing'
            time.sleep(.02)
        process.send_signal(termination)
        _stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == (130 if termination == signal.SIGINT else -signal.SIGKILL), stderr
        reopened = Store(library._db_path)
        try:
            reopened.init_schema()
            worker = recovery_worker(reopened, 100)
            try:
                assert not reopened.activity_paused
                status = worker.ctx.storage_maintenance.status()
                assert not status['active'] and status['evidence_maintenance']['pending']
                for _ in range(10):
                    if not choice_evidence.maintain_storage(reopened, 100).get('deferred'):
                        break
                else:
                    pytest.fail('Committed evidence recovery did not finish after process interruption')
                assert not reopened.activity_paused
                assert not worker.ctx.storage_maintenance.status()['active']
                assert reopened.taste_groups.definition('first-leaf', generation='first')
                assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchone()
            finally:
                worker.shutdown()
                worker.join(0)
        finally:
            reopened.conn.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
