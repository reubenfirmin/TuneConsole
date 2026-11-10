"""Captured choice custody survives real split analytics and foreground writes."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import time

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.rebuild_startup_support import startup_destination, startup_development_source
from tests.store_fixture_support import use_native_sql_calls
from yt_playlist.core import db_handles, evidence_ledger, fao, home_outputs, mix_choices
from yt_playlist.core.db_rebuild import build, destination, job, mix_history, mix_verification
from yt_playlist.core.migrations import runner
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('captured-choice-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        # These synchronous analytics owners retain actual SQL, transactions,
        # custody files, installer and restart. Query diagnostics and per-call
        # SQL admission are below their captured-evidence boundary.
        use_native_sql_calls(patch)
        yield


def test_choice_source_is_the_populated_v93_catalog(startup_development_source):
    runner.validate_version(startup_development_source, 93)
    assert startup_development_source.execute(
        'SELECT recording_id FROM catalog_entries WHERE id=108').fetchone()[0] == 8


@pytest.fixture(scope='module')
def choice_contract(startup_destination):
    # Catalog conversion has independent owners. Retain the actual menu SQL
    # dictionary, mix preparation, root backfill, collection, captured inventory
    # verification, construct/checkpoint protocol, installer and restart here.
    completed_runtime = startup_destination.stages[0]
    dictionary = tuple(name for name, sql in mix_choices.definitions()
                       if sql.startswith('CREATE TABLE') and name in mix_history.DICTIONARY)

    def runtime(reader, target, cursor, limit):
        result = completed_runtime.copy(reader, target, cursor, limit)
        for table in dictionary:
            columns = [row[1] for row in reader.execute('PRAGMA table_info(' + table + ')')]
            target.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in columns) + ')',
                               reader.execute('SELECT * FROM ' + table))
            assert target.execute('SELECT count(*) FROM ' + table).fetchone()[0] == reader.execute(
                'SELECT count(*) FROM ' + table).fetchone()[0]
        return result

    def verify(reader, target):
        mix_verification.verify_captured(reader, target, inventory=build._checkpoint(target).get('evidence'))

    return replace(startup_destination, version='captured-choice-owner-v1',
        stages=(build.Stage('completed-catalog-and-captured-choice-dictionary', runtime),
                *mix_history.stages(time.time())), runtime_stages=1,
        capture=mix_verification.capture, verify_analytics=verify)


def test_controlled_catalog_keeps_all_real_mix_analytics_owners(choice_contract):
    assert [stage.name for stage in choice_contract.stages[1:]] == [
        stage.name for stage in mix_history.stages(0)]
    assert choice_contract.capture is mix_verification.capture


SCENARIOS = [(new_format, legacy, damage) for legacy, damage in (
    (False, None), (True, None), (False, 'descriptor'), (False, 'file'))
    for new_format in (False, True)]


@pytest.fixture(scope='module', params=SCENARIOS,
                ids=[f"{'compact' if new else 'legacy'}-{'old-checkpoint' if legacy else damage or 'restart'}"
                     for new, legacy, damage in SCENARIOS])
def choice_branch(request, startup_development_source, choice_contract, tmp_path_factory):
    new_format, legacy, damage = request.param
    path = tmp_path_factory.mktemp('captured-choice-branch') / 'source.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA synchronous=OFF')
        startup_development_source.backup(conn)
        conn.row_factory = sqlite3.Row
        mix_choices.install(conn)
        conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (choice_evidence.STORAGE_PENDING, '1'))
        ledger = evidence_ledger.EvidenceLedger(evidence_ledger.directory(conn), 2**63 - 1)
        context = ledger.artifact({'at': 99, 'group_generation': 'retired', 'groups': []})
        record = dict(schema=1, id='captured-menu', format='mixes', at=100, observed_at=100,
            episode_id='old-episode', context=context, prediction=None,
            options=[dict(id='old-option', label='Original label', genres=['house'])])
        if new_format:
            compact = ledger.artifact(mix_choices.context({'at': 99}, mixes=True))
            record = mix_choices.publish_menu(conn, record, compact)
        ledger.put('opportunity', record['id'], record)
        conn.commit()
        expected = {str(file.relative_to(ledger.directory)): list(fao.read(file).digest)
                    for kind in ('artifact', 'opportunity')
                    for file in (ledger.directory / kind).glob('*/*.json')}
    store = Store(path)
    store.conn.execute('PRAGMA synchronous=OFF')
    branch = dict(store=store, path=path, record=record, expected=expected,
                  legacy=legacy, damage=damage, new_format=new_format, published=False)
    try:
        store.init_schema(defer_metadata=True)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(destination, 'for_source', lambda path, **kwargs: choice_contract)
            yield branch
    finally:
        branch['store'].conn.close()


def test_installed_source_keeps_the_actual_original_menu_and_file_inventory(choice_branch):
    branch, store = choice_branch, choice_branch['store']
    assert {'artifact', 'opportunity'} == {Path(name).parts[0] for name in branch['expected']}
    assert store.rec_ledger.evidence().get('opportunity', 'captured-menu') == branch['record']
    assert store.conn.execute('SELECT count(*) FROM mix_choice_menus').fetchone()[0] == int(branch['new_format'])


@pytest.fixture(scope='module')
def runtime_cutover(choice_branch):
    branch = choice_branch
    branch['first'] = job.run(branch['store'], rebuild=True)
    pending = build._checkpoint(branch['store'].conn)
    assert dict(pending['evidence']) == branch['expected']
    if branch['legacy']:
        with branch['store'].conn:
            pending.pop('evidence')
            build._checkpoint(branch['store'].conn, pending)
    branch['captured'] = pending.get('evidence')
    return branch


def test_real_runtime_install_captures_original_digests_before_resuming_writes(runtime_cutover):
    branch = runtime_cutover
    assert branch['first']['phase'] == 'complete' and branch['first']['analytics_pending']
    pending = build._checkpoint(branch['store'].conn)
    assert 0 < pending['stage'] < len(pending['stages'])
    assert not branch['store'].activity_paused


def publish_and_damage(branch):
    store = branch['store']
    display = home_outputs.prepare('home_choice', json.dumps(
        {'schema': 1, 'protos': [{'label': 'New accepted card'}]}), 200)
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        home_outputs.publish(store.conn, display, store=store, input_revision=200)
    assert choice_evidence.issue(store, 'mixes',
        [dict(id='new-option', label='New descriptor', genres=['jazz'])], 201,
        context={'at': 200}, opportunity_id='new-menu') == 'new-menu'
    assert choice_evidence.respond(store, 'new-menu', 'choice', 202,
        option_id='new-option', action_id='accepted-choice')
    assert choice_evidence.issue(store, 'albums',
        [dict(id='new-album', label='Accepted album')], 203,
        context={'at': 200}, opportunity_id='new-album-menu') == 'new-album-menu'
    branch['published'] = True
    if branch['damage'] == 'descriptor':
        with store.conn:
            for name, in store.conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' "
                                            "AND tbl_name='mix_choice_themes'").fetchall():
                store.conn.execute('DROP TRIGGER ' + build.quote(name))
            store.conn.execute("UPDATE mix_choice_themes SET label='Corrupted label' "
                "WHERE id=(SELECT d.theme_id FROM mix_choice_options o JOIN mix_choice_descriptors d "
                "ON d.id=o.descriptor_id WHERE o.opportunity_id='captured-menu')")
    elif branch['damage'] == 'file':
        path = store.rec_ledger.evidence()._path('opportunity', 'captured-menu')
        fao.write(path, evidence_ledger.canonical({**branch['record'], 'episode_id': 'corrupted'}))
    else:
        raise InterruptedError('restart with accepted choices and captured inventory')


@pytest.fixture(scope='module')
def analytics_interrupted(runtime_cutover):
    branch = runtime_cutover

    def progress(phase, detail):
        if not branch['published'] and detail['stage'] == 'mixes:roots':
            publish_and_damage(branch)

    expected = (pytest.raises(ValueError, match='original listening evidence|displayed mix meaning')
                if branch['damage'] else pytest.raises(InterruptedError, match='restart with accepted choices'))
    with expected:
        job.finish_analytics(branch['store'], progress=progress)
    return branch


def test_real_analytics_rejects_corruption_or_persists_restart_after_foreground_writes(analytics_interrupted):
    branch = analytics_interrupted
    assert branch['published']
    assert db_handles.state(branch['path'])['analytics_pending']
    assert build._checkpoint(branch['store'].conn).get('evidence') == branch['captured']


@pytest.fixture(scope='module')
def reopened_analytics(analytics_interrupted):
    branch = analytics_interrupted
    if not branch['damage']:
        branch['store'].conn.close()
        branch['store'] = Store(branch['path'])
        branch['store'].conn.execute('PRAGMA synchronous=OFF')
        branch['store'].init_schema()
    return branch


def test_real_reopen_retains_the_exact_inventory_and_accepted_publications(reopened_analytics):
    branch, store = reopened_analytics, reopened_analytics['store']
    assert build._checkpoint(store.conn).get('evidence') == branch['captured']
    assert store.rec_proposals.get_proposals('home_choice')['protos'][0]['label'] == 'New accepted card'
    assert store.rec_ledger.evidence().get('response', 'accepted-choice')['option_id'] == 'new-option'


@pytest.fixture(scope='module')
def analytics_finished(reopened_analytics):
    branch = reopened_analytics
    if not branch['damage']:
        job.finish_analytics(branch['store'])
    return branch


def test_real_resumed_analytics_keeps_new_mix_and_independent_album_choices(analytics_finished):
    branch, store = analytics_finished, analytics_finished['store']
    assert db_handles.state(branch['path'])['analytics_pending'] == bool(branch['damage'])
    offered = store.rec_ledger.evidence().get('opportunity', 'new-menu')
    assert mix_choices.expand(store.conn, offered['options'][0])['label'] == 'New descriptor'
    assert store.rec_ledger.evidence().get('opportunity', 'new-album-menu')
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_fresh_completed_store_reopen_preserves_accepted_display_and_choice_meaning(analytics_finished):
    branch = analytics_finished
    if branch['damage']:
        # Corruption must remain pending; fresh admission must not manufacture
        # completion or repair the invalid immutable source behind verification.
        assert db_handles.state(branch['path'])['analytics_pending']
        return
    branch['store'].conn.close()
    branch['store'] = Store(branch['path'])
    store = branch['store']
    store.conn.execute('PRAGMA synchronous=OFF')
    store.init_schema()
    assert not db_handles.state(branch['path'])['analytics_pending']
    assert store.rec_proposals.get_proposals('home_choice')['protos'][0]['label'] == 'New accepted card'
    offered = store.rec_ledger.evidence().get('opportunity', 'new-menu')
    assert mix_choices.expand(store.conn, offered['options'][0])['label'] == 'New descriptor'
    assert store.rec_ledger.evidence().get('response', 'accepted-choice')['option_id'] == 'new-option'
    assert store.rec_ledger.evidence().get('opportunity', 'new-album-menu')
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
